"""
토스증권 종목 커뮤니티(`https://tossinvest.com/stocks/{toss_code}/community`)
메인 크롤러 모듈 (`크롤링.md` 명세 전체 반영)

주요 기능:
1. Playwright `headless=False` 시각적 브라우저 실행 + `TossNetworkInterceptor` 하이브리드 JSON 가로채기
   - 선택적으로 `user_data_dir`을 지정해 로그인 세션 유지 지원
2. 국내 6자리 종목코드 입력 시 자동으로 `A` 접두사 URL 변환 (`005930` -> `A005930`), JSON 저장 시 6자리 통일
3. 페이지 상단 및 API 응답에서 종목명(`stock_name`) 자동 추출
4. 커뮤니티 진입 직후 정렬 탭('최신순' / '인기순') 자동 전환 (클린봇 해제 불필요)
5. 비로그인 제한 / 로그인 유도 모달 감지 시 닫기 시도 및 필요 시 수동 로그인 대기
6. 인간과 유사한 마우스 커서 이동, 랜덤 오프셋 클릭, 무한 스크롤, 긴 글 '더보기' 및 댓글/대댓글 확장
7. 원글('post'), 댓글('comment'), 대댓글('reply')을 평면형(Flat) JSON 배열로 구성
   - `source="toss"`, `dislike_count=0`, 백그라운드 JSON ISO `createdAt` 최우선 적용
8. 게시글 1개 세트 수집 완료 시마다 즉시 원자적 중간 저장(Checkpoint) 및 재실행 시 기존 ID 건너뛰기
"""
import asyncio
import re
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from playwright.async_api import async_playwright, Page, BrowserContext

from src.models import DiscussionItem
from src.human_actor import HumanActor
from src.network_interceptor import TossNetworkInterceptor
from src.storage import CheckpointStorage
from src.utils import (
    normalize_stock_codes,
    get_now_scraped_at,
    normalize_created_at,
    is_older_than_since_date,
    sanitize_filename,
    parse_int_safe,
)


class TossStockCommunityCrawler:
    def __init__(
        self,
        stock_code: str,
        sort: str,
        max_posts: Optional[int],
        since_date: Optional[str],
        output_path: Optional[str],
        settings: Dict[str, Any],
        logger,
        user_data_dir: Optional[str] = None,
    ):
        self.toss_code, self.stock_code = normalize_stock_codes(stock_code)
        self.sort = (sort or "latest").strip().lower()
        if self.sort not in ("latest", "popular"):
            self.sort = "latest"
        self.max_posts = max_posts
        self.since_date = since_date
        self.custom_output_path = output_path
        self.settings = settings
        self.logger = logger
        self.user_data_dir = user_data_dir or settings.get("browser", {}).get("user_data_dir")

        self.stock_name: str = ""
        self.interceptor = TossNetworkInterceptor(logger=self.logger)
        self.storage: Optional[CheckpointStorage] = None

    @property
    def community_url(self) -> str:
        base = self.settings.get("target", {}).get(
            "base_url",
            "https://tossinvest.com/stocks/{toss_code}/community",
        )
        return base.format(toss_code=self.toss_code)

    async def run(self) -> str:
        """크롤러 실행 메인 엔트리 포인트. 최종 저장된 JSON 파일 경로를 반환합니다."""
        browser_cfg = self.settings.get("browser", {})
        # 규칙: 크롤링 과정을 눈으로 직접 확인할 수 있도록 반드시 headless=False
        headless = False
        viewport = browser_cfg.get("viewport", {"width": 1280, "height": 900})
        locale = browser_cfg.get("locale", "ko-KR")
        timezone_id = browser_cfg.get("timezone_id", "Asia/Seoul")
        user_agent = browser_cfg.get("user_agent")

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
        ]

        self.logger.info(
            f"Playwright 브라우저를 실행합니다 (headless={headless}, "
            f"user_data_dir={self.user_data_dir or '임시 세션'})..."
        )

        async with async_playwright() as p:
            browser = None
            if self.user_data_dir:
                profile_path = str(Path(self.user_data_dir).resolve())
                Path(profile_path).mkdir(parents=True, exist_ok=True)
                context_kwargs: Dict[str, Any] = {
                    "headless": headless,
                    "args": launch_args,
                    "viewport": viewport,
                    "locale": locale,
                    "timezone_id": timezone_id,
                }
                if user_agent:
                    context_kwargs["user_agent"] = user_agent
                context: BrowserContext = await p.chromium.launch_persistent_context(
                    profile_path, **context_kwargs
                )
                page: Page = context.pages[0] if context.pages else await context.new_page()
            else:
                browser = await p.chromium.launch(headless=headless, args=launch_args)
                context_kwargs = {
                    "viewport": viewport,
                    "locale": locale,
                    "timezone_id": timezone_id,
                }
                if user_agent:
                    context_kwargs["user_agent"] = user_agent
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

            # 자동화 탐지 완화 스크립트 주입
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            # 백그라운드 네트워크 JSON 응답 가로채기 연결
            page.on("response", self.interceptor.on_response)

            actor = HumanActor(page, self.settings.get("human_interaction", {}))

            try:
                self.logger.info(f"토스증권 커뮤니티 접속 중: {self.community_url}")
                await page.goto(self.community_url, wait_until="domcontentloaded", timeout=35000)
                await actor.random_wait(1.8, 3.0)

                # 0. 초기 로그인 유도 모달 대응
                if self.settings.get("collection", {}).get("handle_login_modal", True):
                    await self._handle_login_modal_if_needed(page, actor)

                # 1. 페이지 상단 또는 API 응답에서 종목명(stock_name) 자동 추출
                self.stock_name = await self._extract_stock_name(page)
                self.logger.info(
                    f"종목 확인 완료: {self.stock_name} (저장코드: {self.stock_code}, URL코드: {self.toss_code})"
                )

                # 2. 저장소(CheckpointStorage) 초기화
                final_output_path = self._resolve_output_path()
                self.storage = CheckpointStorage(final_output_path)
                self.logger.info(
                    f"저장 파일 경로: {final_output_path} "
                    f"(기존 수집 완료 원글: {self.storage.count_posts()}개, 전체 항목: {self.storage.count_total_items()}개)"
                )

                # 3. 정렬 탭 전환 ('최신순' / '인기순') — 토스증권은 클린봇 해제 불필요
                await self._select_sort_tab(page, actor)

                # 4. 무한 스크롤 피드 탐색 및 게시글 수집 루프
                await self._crawl_posts_loop(page, actor)

                self.logger.info(
                    f"크롤링 완료! 총 원글 {self.storage.count_posts()}개 "
                    f"(전체 항목 {self.storage.count_total_items()}개) 저장됨 -> {final_output_path}"
                )
                return str(final_output_path)

            finally:
                await actor.short_pause(0.8, 1.5)
                await context.close()
                if browser is not None:
                    await browser.close()

    def _resolve_output_path(self) -> str:
        """사용자가 지정한 경로 또는 output/output_toss_{stock_name}_{stock_code}.json 경로를 결정합니다."""
        safe_name = sanitize_filename(self.stock_name or self.stock_code)
        if self.custom_output_path:
            out = Path(self.custom_output_path)
            if not out.parent or str(out.parent) == ".":
                out_dir = Path(self.settings.get("storage", {}).get("output_dir", "output"))
                return str(out_dir / out.name)
            return str(out)

        storage_cfg = self.settings.get("storage", {})
        out_dir = Path(storage_cfg.get("output_dir", "output"))
        template = storage_cfg.get("filename_template", "output_toss_{stock_name}_{stock_code}.json")
        filename = template.format(stock_name=safe_name, stock_code=self.stock_code)
        return str(out_dir / filename)

    async def _extract_stock_name(self, page: Page) -> str:
        """가로챈 API 응답, 페이지 상단 DOM, 또는 document.title에서 종목명을 추출합니다."""
        if self.interceptor.detected_stock_name:
            return self.interceptor.detected_stock_name

        try:
            name = await page.evaluate(
                """() => {
                    const selectors = [
                        'h1',
                        '[data-testid*="stock-name" i]',
                        '[class*="StockName" i]',
                        '[class*="stock-name" i]',
                        'header h1', 'header h2', 'main h1'
                    ];
                    for (const sel of selectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const txt = (el.innerText || '').trim().split('\\n')[0].trim();
                            if (
                                txt && txt.length >= 2 && txt.length <= 30 &&
                                !txt.includes('토스증권') && !txt.includes('커뮤니티') && !txt.includes('로그인')
                            ) {
                                return txt;
                            }
                        }
                    }
                    // document.title 파싱 (예: "삼성전자 - 토스증권" 또는 "삼성전자 주가, 커뮤니티 | 토스증권")
                    const title = document.title || '';
                    const cleaned = title
                        .replace(/\\|.*$/, '')
                        .replace(/-\\s*토스증권.*$/, '')
                        .replace(/주가.*$/, '')
                        .replace(/커뮤니티.*$/, '')
                        .trim();
                    if (cleaned && cleaned.length >= 2 && cleaned.length <= 30) {
                        return cleaned;
                    }
                    return '';
                }"""
            )
            if name and str(name).strip():
                return str(name).strip()
        except Exception:
            pass

        return self.interceptor.detected_stock_name or self.stock_code

    async def _select_sort_tab(self, page: Page, actor: HumanActor) -> None:
        """
        커뮤니티 진입 직후 사용자가 설정한 정렬 기준('최신순' 또는 '인기순')으로 탭을 전환합니다.
        """
        target_label = "최신순" if self.sort == "latest" else "인기순"
        self.logger.info(f"커뮤니티 정렬 기준 전환 시도: '{target_label}' (--sort {self.sort})")

        try:
            sort_selectors = [
                f"button:has-text('{target_label}')",
                f"[role='tab']:has-text('{target_label}')",
                f"a:has-text('{target_label}')",
                f"span:has-text('{target_label}')",
            ]
            for sel in sort_selectors:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    # 이미 선택된 상태인지 확인
                    aria_selected = await loc.get_attribute("aria-selected")
                    aria_pressed = await loc.get_attribute("aria-pressed")
                    if aria_selected == "true" or aria_pressed == "true":
                        self.logger.info(f"이미 '{target_label}' 정렬이 선택되어 있습니다.")
                        return

                    # 기존 정렬(예: 인기순)에서 캡처된 피드 캐시를 비우고 최신순 피드를 새로 받기 위해 초기화
                    self.interceptor.posts_by_id.clear()
                    clicked = await actor.human_click(loc, wait_after=True)
                    if clicked:
                        self.logger.info(f"'{target_label}' 탭 클릭 완료.")
                        await actor.random_wait(1.0, 2.0)
                        return

            self.logger.info(f"'{target_label}' 버튼을 별도로 찾지 못해 현재 정렬 상태로 진행합니다.")
        except Exception as e:
            self.logger.warning(f"정렬 탭('{target_label}') 전환 중 예외 발생 (계속 진행): {e}")

    async def _handle_login_modal_if_needed(self, page: Page, actor: HumanActor) -> bool:
        """
        비로그인 제한 / 로그인 유도 모달이 나타났을 때:
        1) 먼저 닫기 버튼('닫기', '나중에', 'X' 등) 클릭을 시도합니다.
        2) 닫기 불가하거나 로그인 페이지로 리다이렉트된 경우, headless=False 화면에서
           사용자가 직접 QR/앱 로그인을 완료할 수 있도록 대기합니다.
        반환값: 로그인 모달/화면이 감지되어 처리되었으면 True
        """
        try:
            is_login_page = "/login" in page.url.lower() or "signin" in page.url.lower()
            modal_loc = page.locator(
                "[role='dialog']:has-text('로그인'), [class*='modal' i]:has-text('로그인'), "
                "[class*='Modal' i]:has-text('토스 앱'), [class*='Dialog' i]:has-text('로그인')"
            ).first
            has_modal = await modal_loc.count() > 0 and await modal_loc.is_visible()

            if not is_login_page and not has_modal:
                return False

            # 1단계: 팝업 닫기 시도
            if has_modal and not is_login_page:
                close_selectors = [
                    "[role='dialog'] button[aria-label*='닫기']",
                    "[role='dialog'] button:has-text('닫기')",
                    "[role='dialog'] button:has-text('나중에')",
                    "[role='dialog'] button:has-text('다음에')",
                    "[role='dialog'] button:has-text('취소')",
                    "[class*='modal' i] button[aria-label*='close' i]",
                    "[class*='modal' i] button[aria-label*='닫기']",
                ]
                for c_sel in close_selectors:
                    c_btn = page.locator(c_sel).first
                    if await c_btn.count() > 0 and await c_btn.is_visible():
                        self.logger.info("로그인 유도 모달 감지 -> 닫기 버튼 클릭 시도")
                        await actor.human_click(c_btn, wait_after=True)
                        if not (await modal_loc.count() > 0 and await modal_loc.is_visible()):
                            return True
                        break

                # Escape 키로 닫기 시도
                await page.keyboard.press("Escape")
                await actor.short_pause(0.5, 1.0)
                if not (await modal_loc.count() > 0 and await modal_loc.is_visible()):
                    return True

            # 2단계: 닫을 수 없는 필수 로그인 요구 시 사용자 수동 로그인 대기
            self.logger.warning(
                "로그인이 필요한 화면 또는 닫을 수 없는 로그인 모달이 감지되었습니다. "
                "열려 있는 브라우저 창에서 QR/앱 로그인을 완료해주세요."
            )
            if sys.stdin.isatty():
                await asyncio.to_thread(
                    input,
                    "\n[안내] 브라우저에서 토스증권 로그인을 완료한 뒤 Enter 키를 누르세요 (건너뛰려면 그냥 Enter): ",
                )
            else:
                # 비대화형 실행 시 최대 60초간 로그인 완료(URL 복귀 또는 모달 해제) 자동 대기
                for _ in range(30):
                    await asyncio.sleep(2.0)
                    still_login = "/login" in page.url.lower()
                    still_modal = await modal_loc.count() > 0 and await modal_loc.is_visible()
                    if not still_login and not still_modal:
                        break

            # 로그인 후 원래 커뮤니티 페이지로 복귀 확인
            if self.toss_code.lower() not in page.url.lower():
                await page.goto(self.community_url, wait_until="domcontentloaded", timeout=35000)
                await actor.random_wait(1.5, 2.5)
            return True
        except Exception as e:
            self.logger.debug(f"로그인 모달 처리 중 경미한 예외: {e}")
            return False

    async def _crawl_posts_loop(self, page: Page, actor: HumanActor) -> None:
        """
        토스증권 커뮤니티 SNS 무한 스크롤 피드를 탐색하며
        --max-posts 또는 --since-date 조건에 도달할 때까지 원글+댓글+대댓글을 수집합니다.
        """
        assert self.storage is not None
        visited_in_session = set()
        consecutive_no_new_scrolls = 0
        stop_by_since_date = False

        while True:
            # 1) 종료 조건 확인: max_posts 달성 여부
            current_saved_posts = self.storage.count_posts()
            if self.max_posts is not None and current_saved_posts >= self.max_posts:
                self.logger.info(f"목표 게시글 수({self.max_posts}개)에 도달하여 수집을 종료합니다.")
                break

            if stop_by_since_date:
                self.logger.info(f"기준 날짜({self.since_date}) 이전 게시글에 도달하여 수집을 종료합니다.")
                break

            # 로그인 유도 모달 체크
            if self.settings.get("collection", {}).get("handle_login_modal", True):
                await self._handle_login_modal_if_needed(page, actor)

            # 현재 페이지가 커뮤니티 피드가 아니라면 복귀
            if not self._is_community_feed_url(page.url):
                await page.goto(self.community_url, wait_until="domcontentloaded", timeout=35000)
                await actor.random_wait(1.5, 2.5)

            # 2) 현재 피드 화면 + NetworkInterceptor에서 게시글 후보 목록 추출
            post_summaries = await self._extract_post_list_from_feed(page)

            pending_posts = []
            for p_info in post_summaries:
                raw_id = str(p_info["raw_id"])
                if raw_id in visited_in_session:
                    continue
                pending_posts.append(p_info)

            if not pending_posts:
                consecutive_no_new_scrolls += 1
                if consecutive_no_new_scrolls > 10:
                    self.logger.info("더 이상 새로운 게시글이 로드되지 않아 수집을 종료합니다.")
                    break

                self.logger.info(f"무한 스크롤로 다음 피드 게시글을 불러옵니다... ({consecutive_no_new_scrolls}/10)")
                await actor.human_scroll_down(chunks=3, wait_after=True)
                continue

            consecutive_no_new_scrolls = 0

            # 3) 발견된 게시글을 순차적으로 처리
            for p_info in pending_posts:
                raw_id = str(p_info["raw_id"])
                visited_in_session.add(raw_id)
                root_post_id = f"toss_post_{raw_id}"

                if self.max_posts is not None and self.storage.count_posts() >= self.max_posts:
                    break

                # 체크포인트 확인: 이미 저장된 게시글 ID는 건너뛰기
                if self.storage.has_post(root_post_id):
                    self.logger.info(f"[건너뜀] 이미 수집된 게시글입니다: {root_post_id}")
                    if self.since_date and p_info.get("created_at"):
                        norm_dt = normalize_created_at(p_info["created_at"], get_now_scraped_at())
                        if is_older_than_since_date(norm_dt, self.since_date):
                            stop_by_since_date = True
                            break
                    continue

                # 게시글 1개 세트(원글 + 댓글 + 대댓글) 수집
                bundle, post_created_at = await self._collect_single_post_detail(page, actor, p_info)
                if not bundle:
                    continue

                # 기준 날짜(--since-date) 이전 글인지 검사 (최신순일 때 즉시 종료)
                if self.since_date and is_older_than_since_date(post_created_at, self.since_date):
                    self.logger.info(
                        f"게시글({root_post_id}) 작성시간({post_created_at})이 기준일({self.since_date}) 이전입니다."
                    )
                    if self.sort == "latest":
                        stop_by_since_date = True
                        break
                    continue

                # 게시글 1개 세트 즉시 원자적 중간 저장 (Checkpointing)
                added = self.storage.save_post_bundle(bundle)
                self.logger.info(
                    f"[체크포인트 저장 완료] {root_post_id} "
                    f"(원글 1개 + 댓글/대댓글 {len(bundle) - 1}개, 신규 저장 {added}건) "
                    f"| 누적 원글: {self.storage.count_posts()}개"
                )

                # 상세 화면으로 이동했다면 피드로 복귀
                await self._return_to_feed_if_needed(page, actor)

            # 한 배치 처리 후 자연스럽게 아래로 스크롤
            if not stop_by_since_date and (self.max_posts is None or self.storage.count_posts() < self.max_posts):
                await actor.human_scroll_down(chunks=2, wait_after=True)

    def _is_community_feed_url(self, url: str) -> bool:
        clean = url.split("?")[0].rstrip("/").lower()
        return clean.endswith("/community")

    async def _return_to_feed_if_needed(self, page: Page, actor: HumanActor) -> None:
        """상세 URL 또는 댓글 모달이 열려 있다면 커뮤니티 피드로 자연스럽게 복귀합니다."""
        try:
            # 모달/드로어가 열려 있으면 닫기
            dialog_close = page.locator(
                "[role='dialog'] button[aria-label*='닫기'], [role='dialog'] button[aria-label*='뒤로']"
            ).first
            if await dialog_close.count() > 0 and await dialog_close.is_visible():
                await actor.human_click(dialog_close, wait_after=True)
                return

            if not self._is_community_feed_url(page.url) or "postId=" in page.url:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
                await actor.random_wait(1.0, 2.0)
                if not self._is_community_feed_url(page.url):
                    await page.goto(self.community_url, wait_until="domcontentloaded", timeout=30000)
                    await actor.random_wait(1.2, 2.2)
        except Exception:
            await page.goto(self.community_url, wait_until="domcontentloaded", timeout=30000)
            await actor.random_wait(1.2, 2.2)

    async def _extract_post_list_from_feed(self, page: Page) -> List[Dict[str, Any]]:
        """
        백그라운드 JSON(TossNetworkInterceptor)과 현재 피드 DOM을 결합하여
        게시글 요약 목록을 추출합니다.
        """
        results: List[Dict[str, Any]] = []
        seen_ids = set()

        # 1) 하이브리드 최우선: NetworkInterceptor가 캡처한 정확한 게시글 목록
        for raw_id, net_data in list(self.interceptor.posts_by_id.items()):
            if raw_id in seen_ids:
                continue
            seen_ids.add(raw_id)
            results.append(
                {
                    "raw_id": raw_id,
                    "title": net_data.get("title"),
                    "content": net_data.get("content") or "",
                    "nickname": net_data.get("nickname") or "",
                    "is_shareholder": bool(net_data.get("is_shareholder")),
                    "created_at": net_data.get("created_at") or "",
                    "like_count": net_data.get("like_count", 0),
                    "comment_count": net_data.get("comment_count"),
                    "image_urls": list(net_data.get("image_urls") or []),
                    "post_url": f"{self.community_url}?postId={raw_id}",
                }
            )

        # 2) DOM 보강: 피드 카드에서 링크/데이터 속성 기반 게시글 추출
        try:
            dom_posts = await page.evaluate(
                """() => {
                    const items = [];
                    const links = Array.from(document.querySelectorAll(
                        'a[href*="postId="], a[href*="/comments/"], a[href*="/community/"], article[data-post-id], article[data-id]'
                    ));
                    for (const el of links) {
                        const href = el.href || el.getAttribute('href') || '';
                        let rawId = el.getAttribute('data-post-id') || el.getAttribute('data-id') || '';
                        if (!rawId) {
                            const m = href.match(/(?:postId=|comments\\/|community\\/)(\\d{4,})/);
                            if (m) rawId = m[1];
                        }
                        if (!rawId) continue;

                        const card = el.closest('article, li, div[class*="post" i], div[class*="feed" i]') || el;
                        const text = (card.innerText || '').trim();
                        items.push({
                            raw_id: String(rawId),
                            href: href,
                            raw_text: text
                        });
                    }
                    return items;
                }"""
            )
            for item in dom_posts:
                raw_id = str(item.get("raw_id") or "")
                if not raw_id or raw_id in seen_ids:
                    continue
                seen_ids.add(raw_id)
                href = str(item.get("href") or "")
                post_url = href if href.startswith("http") else f"{self.community_url}?postId={raw_id}"
                results.append(
                    {
                        "raw_id": raw_id,
                        "title": None,
                        "content": "",
                        "post_url": post_url,
                        "raw_text": item.get("raw_text") or "",
                    }
                )
        except Exception as e:
            self.logger.debug(f"피드 DOM 게시글 목록 추출 중 경미한 오류: {e}")

        return results

    async def _collect_single_post_detail(
        self, page: Page, actor: HumanActor, p_info: Dict[str, Any]
    ) -> Tuple[List[DiscussionItem], str]:
        """
        개별 게시글의 본문 전체(긴 글 '더보기' 포함) 및 댓글/대댓글을 수집합니다.
        """
        raw_id = str(p_info["raw_id"])
        root_post_id = f"toss_post_{raw_id}"
        post_url = str(p_info.get("post_url") or f"{self.community_url}?postId={raw_id}")

        preview = (p_info.get("content") or p_info.get("title") or "")[:30].replace("\n", " ")
        self.logger.info(f"게시글 수집 시작: {root_post_id} ({preview})")
        self.interceptor.clear_latest_comments()

        net_post = self.interceptor.posts_by_id.get(raw_id, {})
        comment_count = net_post.get("comment_count")
        need_detail_open = (
            comment_count is None
            or parse_int_safe(comment_count) > 0
            or not net_post.get("content")
        )

        dom_detail: Dict[str, Any] = {}

        if need_detail_open:
            # 화면에 해당 게시글 링크/카드가 보이면 사람처럼 클릭, 아니면 상세 URL로 이동
            opened = await self._open_post_detail_view(page, actor, raw_id, post_url)
            if opened:
                if self.settings.get("collection", {}).get("handle_login_modal", True):
                    await self._handle_login_modal_if_needed(page, actor)

                # 긴 글 '더보기' 확장
                if self.settings.get("collection", {}).get("expand_long_posts", True):
                    await self._expand_long_post_body(page, actor)

                # 본문을 읽듯 자연스럽게 스크롤
                await actor.human_scroll_to_read()

                # 접힌 댓글/대댓글('댓글 더보기', '답글 N개 보기') 확장
                if self.settings.get("collection", {}).get("expand_all_comments", True):
                    await self._expand_all_comments_and_replies(page, actor)

                dom_detail = await self._extract_detail_from_dom(page)

        scraped_at = get_now_scraped_at()
        # 상세 진입 후 인터셉터에 업데이트된 최신 게시글 데이터 재조회
        net_post = self.interceptor.posts_by_id.get(raw_id, net_post)

        # --- 1. 원글(post) 데이터 조립 (백그라운드 JSON 최우선 + DOM 보완) ---
        raw_title = net_post.get("title") or dom_detail.get("title") or p_info.get("title")
        title = str(raw_title).strip() if raw_title and str(raw_title).strip() else None

        # 본문: 더 길고 완전한 텍스트를 우선 채택
        net_content = str(net_post.get("content") or "").strip()
        dom_content = str(dom_detail.get("content") or "").strip()
        content = dom_content if len(dom_content) > len(net_content) else (net_content or dom_content)
        if "<br" in content or "<p" in content or "<div" in content:
            content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
            content = re.sub(r"<[^>]+>", "", content).strip()

        nickname = (
            net_post.get("nickname")
            or dom_detail.get("nickname")
            or p_info.get("nickname")
            or ""
        ).strip()

        is_shareholder = bool(
            net_post.get("is_shareholder")
            or dom_detail.get("is_shareholder")
            or p_info.get("is_shareholder")
        )

        # 작성시간: 백그라운드 JSON의 ISO 절대시간(`createdAt`)을 최우선 사용, 없을 때만 DOM 상대시간 보정
        raw_created_at = (
            net_post.get("created_at")
            or p_info.get("created_at")
            or dom_detail.get("created_at")
            or ""
        )
        created_at = normalize_created_at(raw_created_at, scraped_at)

        like_count = parse_int_safe(
            net_post.get("like_count")
            if net_post.get("like_count") is not None
            else (dom_detail.get("like_count") if dom_detail.get("like_count") is not None else 0)
        )

        # 첨부 이미지 URL 배열 (순수 문자열 리스트, 파일 다운로드 없음)
        image_urls: List[str] = []
        for u in list(net_post.get("image_urls") or []) + list(dom_detail.get("image_urls") or []):
            u_str = str(u).strip()
            if u_str and u_str.startswith("http") and u_str not in image_urls:
                image_urls.append(u_str)

        post_item = DiscussionItem(
            id=root_post_id,
            source="toss",
            item_type="post",
            parent_id=None,
            root_post_id=root_post_id,
            stock_name=self.stock_name,
            stock_code=self.stock_code,
            nickname=nickname,
            is_shareholder=is_shareholder,
            created_at=created_at,
            scraped_at=scraped_at,
            title=title,
            content=content,
            like_count=like_count,
            dislike_count=0,
            image_urls=image_urls,
            post_url=post_url,
        )

        # --- 2. 댓글(comment) 및 대댓글(reply) 평면형 리스트 조립 ---
        comment_and_reply_items = self._build_comments_and_replies(
            raw_post_id=raw_id,
            root_post_id=root_post_id,
            post_url=post_url,
            dom_comments=dom_detail.get("comments") or [],
            scraped_at=scraped_at,
        )

        bundle: List[DiscussionItem] = [post_item] + comment_and_reply_items
        return bundle, created_at

    async def _open_post_detail_view(
        self, page: Page, actor: HumanActor, raw_id: str, post_url: str
    ) -> bool:
        """피드에서 해당 게시글을 사람처럼 클릭하거나 상세 URL로 진입합니다."""
        try:
            link_locator = page.locator(
                f"a[href*='postId={raw_id}'], a[href*='/comments/{raw_id}'], "
                f"a[href*='/community/{raw_id}'], [data-post-id='{raw_id}']"
            ).first
            if await link_locator.count() > 0 and await link_locator.is_visible():
                clicked = await actor.human_click(link_locator, wait_after=True)
                if clicked:
                    return True

            await page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            await actor.random_wait(1.2, 2.4)
            return True
        except Exception as e:
            self.logger.debug(f"게시글 상세 진입 실패 ({raw_id}): {e}")
            return False

    async def _expand_long_post_body(self, page: Page, actor: HumanActor) -> None:
        """긴 게시글 본문에 '더보기' 버튼이 있으면 사람처럼 클릭해 전체 본문을 펼칩니다."""
        try:
            more_btns = page.locator("article button:has-text('더보기'), main button:has-text('더보기')")
            count = min(await more_btns.count(), 3)
            for idx in range(count):
                btn = more_btns.nth(idx)
                if not await btn.is_visible():
                    continue
                txt = (await btn.inner_text()).strip()
                if txt in ("더보기", "더 보기", "...더보기", "본문 더보기"):
                    await actor.human_click(btn, wait_after=False)
                    await actor.short_pause(0.4, 0.9)
        except Exception:
            pass

    async def _expand_all_comments_and_replies(self, page: Page, actor: HumanActor) -> None:
        """
        게시글 상세/댓글 영역 내 '댓글 더보기', '답글 N개 보기', '답글 더보기', '이전 댓글 보기' 등의
        버튼을 사람처럼 클릭하여 접힌 댓글과 대댓글을 모두 펼칩니다.
        """
        expand_patterns = [
            "button:has-text('답글')",
            "button:has-text('댓글 더보기')",
            "button:has-text('이전 댓글')",
            "button:has-text('더보기')",
            "a:has-text('답글')",
            "a:has-text('댓글 더보기')",
        ]

        max_rounds = 10
        for _ in range(max_rounds):
            clicked_any = False
            for sel in expand_patterns:
                locators = page.locator(sel)
                count = await locators.count()
                for idx in range(count):
                    btn = locators.nth(idx)
                    try:
                        if not await btn.is_visible():
                            continue
                        txt = (await btn.inner_text()).strip()
                        if any(skip in txt for skip in ("쓰기", "작성", "남기기", "접기", "닫기", "등록", "취소", "신고", "숨기기")):
                            continue
                        if any(k in txt for k in ("답글", "더보기", "더 보기", "이전 댓글", "보기")):
                            expanded_attr = await btn.get_attribute("aria-expanded")
                            if expanded_attr == "true":
                                continue
                            await actor.human_click(btn, wait_after=True)
                            clicked_any = True
                    except Exception:
                        continue

            if not clicked_any:
                break
            await actor.human_scroll_down(chunks=1, wait_after=True)

    async def _extract_detail_from_dom(self, page: Page) -> Dict[str, Any]:
        """
        상세 화면 또는 댓글 모달의 DOM에서 원글 정보와 댓글/대댓글 목록을 추출합니다.
        (백그라운드 JSON이 누락되었거나 본문이 확장된 경우 상호 보완용)
        """
        try:
            data = await page.evaluate(
                """() => {
                    const result = {
                        title: null,
                        nickname: '',
                        is_shareholder: false,
                        created_at: '',
                        content: '',
                        like_count: null,
                        image_urls: [],
                        comments: []
                    };

                    const root = document.querySelector('[role="dialog"]') || document.querySelector('main') || document.body;
                    const article = root.querySelector('article') || root;

                    // 1. 작성자 닉네임 및 주주 배지
                    const header = article.querySelector('header') || article;
                    const nickEl = header.querySelector('[class*="nickname" i], [class*="author" i], [class*="writer" i], strong, span');
                    if (nickEl) {
                        result.nickname = (nickEl.innerText || '').replace(/주주/g, '').trim().split('\\n')[0].trim();
                    }
                    const headerText = (header.innerText || '');
                    const badgeEl = header.querySelector('[class*="holder" i], [class*="shareholder" i], [class*="badge" i]');
                    if ((badgeEl && (badgeEl.innerText || '').includes('주주')) || /(^|\\s)주주(\\s|$)/.test(headerText)) {
                        result.is_shareholder = true;
                    }

                    // 2. 작성시간 (DOM 상대시간/절대시간 폴백)
                    const timeEl = header.querySelector('time, [class*="date" i], [class*="time" i]');
                    if (timeEl && timeEl.innerText.trim()) {
                        result.created_at = timeEl.getAttribute('datetime') || timeEl.innerText.trim();
                    }

                    // 3. 본문 및 첨부 이미지 URL
                    const bodyEl = article.querySelector('[class*="content" i], [class*="body" i], p');
                    if (bodyEl && (bodyEl.innerText || '').trim()) {
                        result.content = bodyEl.innerText.trim();
                    }
                    article.querySelectorAll('img').forEach(img => {
                        const src = img.getAttribute('data-src') || img.src || '';
                        if (src && src.startsWith('http') && !src.includes('profile') && !src.includes('avatar') && !src.includes('icon') && !src.includes('badge')) {
                            if (!result.image_urls.includes(src)) {
                                result.image_urls.push(String(src));
                            }
                        }
                    });

                    // 4. 좋아요(하트) 수
                    const likeBtns = Array.from(article.querySelectorAll('button, span'));
                    for (const btn of likeBtns) {
                        const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                        const cls = (btn.className || '').toString().toLowerCase();
                        if (aria.includes('좋아요') || aria.includes('like') || cls.includes('like') || cls.includes('heart')) {
                            const m = (btn.innerText || '').match(/(\\d[\\d,]*)/);
                            if (m && result.like_count === null) {
                                result.like_count = parseInt(m[1].replace(/,/g, ''), 10);
                                break;
                            }
                        }
                    }

                    // 5. 댓글 및 대댓글 DOM 추출
                    const commentNodes = Array.from(root.querySelectorAll(
                        '[class*="CommentItem" i], [class*="comment_item" i], [class*="ReplyItem" i], [data-comment-id], li[class*="comment" i]'
                    ));

                    let lastTopCommentId = null;
                    commentNodes.forEach((node, idx) => {
                        const cid = node.getAttribute('data-comment-id') || node.getAttribute('data-id') || String(idx + 1);
                        const cls = (node.className || '').toString().toLowerCase();
                        const isReply = (
                            cls.includes('reply') ||
                            cls.includes('child') ||
                            cls.includes('sub') ||
                            node.getAttribute('data-reply') === 'true' ||
                            Boolean(node.closest('[class*="reply" i], [class*="child" i]'))
                        );

                        const cNickEl = node.querySelector('[class*="nick" i], [class*="author" i], [class*="writer" i], strong');
                        const cNick = cNickEl ? cNickEl.innerText.replace(/주주/g, '').trim().split('\\n')[0].trim() : '';

                        const cBadgeEl = node.querySelector('[class*="holder" i], [class*="badge" i]');
                        const cIsHolder = Boolean(
                            (cBadgeEl && (cBadgeEl.innerText || '').includes('주주')) ||
                            /(^|\\s)주주(\\s|$)/.test(node.innerText || '')
                        );

                        const cContentEl = node.querySelector('[class*="content" i], [class*="body" i], [class*="text" i], p');
                        const cContent = cContentEl ? cContentEl.innerText.trim() : '';

                        const cTimeEl = node.querySelector('time, [class*="date" i], [class*="time" i]');
                        const cDate = cTimeEl ? (cTimeEl.getAttribute('datetime') || cTimeEl.innerText.trim()) : '';

                        const cLikeEl = node.querySelector('[aria-label*="좋아요"], [class*="like" i], [class*="heart" i]');
                        const cImgs = [];
                        node.querySelectorAll('img').forEach(img => {
                            const s = img.getAttribute('data-src') || img.src || '';
                            if (s && s.startsWith('http') && !s.includes('profile') && !s.includes('avatar') && !s.includes('icon')) {
                                cImgs.push(String(s));
                            }
                        });

                        let parentRawId = null;
                        if (isReply) {
                            parentRawId = node.getAttribute('data-parent-id') || lastTopCommentId;
                        } else {
                            lastTopCommentId = cid;
                        }

                        if (cContent || cImgs.length > 0) {
                            result.comments.push({
                                raw_id: String(cid),
                                is_reply: isReply,
                                parent_raw_id: parentRawId ? String(parentRawId) : null,
                                nickname: cNick,
                                is_shareholder: cIsHolder,
                                created_at: cDate,
                                content: cContent,
                                like_count: cLikeEl ? cLikeEl.innerText.trim() : '0',
                                image_urls: cImgs
                            });
                        }
                    });

                    return result;
                }"""
            )
            return data or {}
        except Exception as e:
            self.logger.debug(f"상세 화면 DOM 파싱 중 예외: {e}")
            return {}

    def _build_comments_and_replies(
        self,
        raw_post_id: str,
        root_post_id: str,
        post_url: str,
        dom_comments: List[Dict[str, Any]],
        scraped_at: str,
    ) -> List[DiscussionItem]:
        """
        백그라운드 JSON(TossNetworkInterceptor)과 DOM 댓글/대댓글을 병합하여
        평면형(Flat) DiscussionItem 리스트로 반환합니다.
        - 댓글("comment"): id="toss_comment_{번호}", parent_id=root_post_id, root_post_id=root_post_id
        - 대댓글("reply"): id="toss_reply_{번호}", parent_id="toss_comment_{상위댓글번호}", root_post_id=root_post_id
        """
        items: List[DiscussionItem] = []
        seen_ids = set()

        # 1) 백그라운드 JSON 댓글 최우선 수집 (정확한 ID, ISO 절대시간, 주주 여부 보유)
        net_comments = list(self.interceptor.comments_by_post_id.get(raw_post_id, []))
        for c in self.interceptor.latest_comments:
            c_pid = str(c.get("_post_id") or "")
            if (not c_pid or c_pid == raw_post_id) and c not in net_comments:
                net_comments.append(c)

        net_by_id: Dict[str, Dict[str, Any]] = {}
        for nc in net_comments:
            cid = str(nc.get("_captured_id") or nc.get("id") or "")
            if cid:
                net_by_id[cid] = nc

        # 2) 백그라운드 JSON 기반 댓글/대댓글 생성
        for raw_id, nc in net_by_id.items():
            is_reply = bool(nc.get("_is_reply"))
            p_raw = str(nc.get("_parent_id") or "")

            if is_reply:
                item_type = "reply"
                full_id = f"toss_reply_{raw_id}"
                parent_id = f"toss_comment_{p_raw}" if (p_raw and p_raw != raw_post_id) else root_post_id
            else:
                item_type = "comment"
                full_id = f"toss_comment_{raw_id}"
                parent_id = root_post_id

            if full_id in seen_ids:
                continue
            seen_ids.add(full_id)

            content = str(nc.get("content") or "").strip()
            imgs = [str(u) for u in (nc.get("image_urls") or []) if u and str(u).startswith("http")]
            if not content and not imgs:
                continue

            nickname = str(nc.get("nickname") or "").strip()
            is_shareholder = bool(nc.get("is_shareholder"))
            created_at = normalize_created_at(nc.get("created_at"), scraped_at)
            like_cnt = parse_int_safe(nc.get("like_count") or 0)

            items.append(
                DiscussionItem(
                    id=full_id,
                    source="toss",
                    item_type=item_type,
                    parent_id=parent_id,
                    root_post_id=root_post_id,
                    stock_name=self.stock_name,
                    stock_code=self.stock_code,
                    nickname=nickname,
                    is_shareholder=is_shareholder,
                    created_at=created_at,
                    scraped_at=scraped_at,
                    title=None,
                    content=content,
                    like_count=like_cnt,
                    dislike_count=0,
                    image_urls=imgs,
                    post_url=post_url,
                )
            )

        # 3) 네트워크 JSON에서 잡히지 않은 DOM 댓글/대댓글 보완 추가
        if dom_comments and not items:
            last_comment_full_id = root_post_id
            for dc in dom_comments:
                raw_id = str(dc.get("raw_id") or "")
                is_reply = bool(dc.get("is_reply"))

                if is_reply:
                    item_type = "reply"
                    full_id = f"toss_reply_{raw_id}"
                    p_raw = dc.get("parent_raw_id")
                    parent_id = f"toss_comment_{p_raw}" if p_raw else last_comment_full_id
                else:
                    item_type = "comment"
                    full_id = f"toss_comment_{raw_id}"
                    parent_id = root_post_id
                    last_comment_full_id = full_id

                if full_id in seen_ids:
                    continue
                seen_ids.add(full_id)

                content = str(dc.get("content") or "").strip()
                imgs = [str(u) for u in (dc.get("image_urls") or []) if u and str(u).startswith("http")]
                if not content and not imgs:
                    continue

                items.append(
                    DiscussionItem(
                        id=full_id,
                        source="toss",
                        item_type=item_type,
                        parent_id=parent_id,
                        root_post_id=root_post_id,
                        stock_name=self.stock_name,
                        stock_code=self.stock_code,
                        nickname=str(dc.get("nickname") or "").strip(),
                        is_shareholder=bool(dc.get("is_shareholder")),
                        created_at=normalize_created_at(dc.get("created_at"), scraped_at),
                        scraped_at=scraped_at,
                        title=None,
                        content=content,
                        like_count=parse_int_safe(dc.get("like_count") or 0),
                        dislike_count=0,
                        image_urls=imgs,
                        post_url=post_url,
                    )
                )

        return items
