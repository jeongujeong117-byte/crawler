"""
네이버 증권 종목토론방(`https://m.stock.naver.com/domestic/stock/{stock_code}/discussion`)
메인 크롤러 모듈 (`크롤링.md` 명세 전체 반영)

주요 기능:
1. Playwright `headless=False` 시각적 브라우저 실행 + `NetworkInterceptor` 하이브리드 JSON 가로채기
2. 접속 직후 클린봇(Cleanbot) OFF(해제) 자동 처리
3. 페이지 상단에서 종목명(`stock_name`) 자동 추출
4. 인간과 유사한 마우스 커서 이동, 랜덤 오프셋 클릭, 휠 스크롤, 1.0~2.8초 가변 대기
5. 상세 페이지 내 접힌 댓글/대댓글('댓글 더보기', '답글 N개 보기') 자동 클릭 확장
6. 원글('post'), 댓글('comment'), 대댓글('reply')을 평면형(Flat) JSON 배열로 구성
7. 게시글 1개 세트 수집 완료 시마다 즉시 중간 저장(Checkpoint) 및 재실행 시 기존 ID 건너뛰기
"""
import asyncio
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from playwright.async_api import async_playwright, Page, BrowserContext

from src.models import DiscussionItem
from src.human_actor import HumanActor
from src.network_interceptor import NetworkInterceptor
from src.storage import CheckpointStorage
from src.utils import (
    get_now_scraped_at,
    normalize_created_at,
    is_older_than_since_date,
    sanitize_filename,
    parse_int_safe,
)


class NaverStockDiscussionCrawler:
    def __init__(
        self,
        stock_code: str,
        max_posts: Optional[int],
        since_date: Optional[str],
        output_path: Optional[str],
        settings: Dict[str, Any],
        logger,
    ):
        self.stock_code = str(stock_code).strip()
        self.max_posts = max_posts
        self.since_date = since_date
        self.custom_output_path = output_path
        self.settings = settings
        self.logger = logger

        self.stock_name: str = ""
        self.interceptor = NetworkInterceptor(logger=self.logger)
        self.storage: Optional[CheckpointStorage] = None

    @property
    def list_url(self) -> str:
        base = self.settings.get("target", {}).get(
            "base_url",
            "https://m.stock.naver.com/domestic/stock/{stock_code}/discussion",
        )
        return base.format(stock_code=self.stock_code)

    async def run(self) -> str:
        """크롤러 실행 메인 엔트리 포인트. 최종 저장된 JSON 파일 경로를 반환합니다."""
        browser_cfg = self.settings.get("browser", {})
        # 규칙 1: 크롤링 과정을 눈으로 직접 확인할 수 있도록 반드시 headless=False
        headless = False
        viewport = browser_cfg.get("viewport", {"width": 1280, "height": 900})

        self.logger.info(f"Playwright 브라우저를 실행합니다 (headless={headless})...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            context: BrowserContext = await browser.new_context(
                viewport=viewport,
                locale=browser_cfg.get("locale", "ko-KR"),
                timezone_id=browser_cfg.get("timezone_id", "Asia/Seoul"),
            )
            # 자동화 탐지 완화 스크립트 주입
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            page: Page = await context.new_page()
            # 백그라운드 네트워크 JSON 응답 가로채기 연결
            page.on("response", self.interceptor.on_response)

            actor = HumanActor(page, self.settings.get("human_interaction", {}))

            try:
                self.logger.info(f"종목토론방 접속 중: {self.list_url}")
                await page.goto(self.list_url, wait_until="domcontentloaded", timeout=30000)
                await actor.random_wait(1.5, 2.8)

                # 1. 페이지 상단에서 종목명(stock_name) 자동 추출
                self.stock_name = await self._extract_stock_name(page)
                self.logger.info(f"종목 확인 완료: {self.stock_name} ({self.stock_code})")

                # 2. 저장소(CheckpointStorage) 초기화
                final_output_path = self._resolve_output_path()
                self.storage = CheckpointStorage(final_output_path)
                self.logger.info(
                    f"저장 파일 경로: {final_output_path} "
                    f"(기존 수집 완료 원글: {self.storage.count_posts()}개, 전체 항목: {self.storage.count_total_items()}개)"
                )

                # 3. 클린봇(Cleanbot) OFF(해제) 처리
                if self.settings.get("collection", {}).get("cleanbot_off", True):
                    await self._turn_off_cleanbot(page, actor)

                # 4. 목록 스크롤 및 게시글 수집 루프
                await self._crawl_posts_loop(page, actor)

                self.logger.info(
                    f"크롤링 완료! 총 원글 {self.storage.count_posts()}개 "
                    f"(전체 항목 {self.storage.count_total_items()}개) 저장됨 -> {final_output_path}"
                )
                return str(final_output_path)

            finally:
                await actor.short_pause(0.8, 1.5)
                await context.close()
                await browser.close()

    def _resolve_output_path(self) -> str:
        """사용자가 지정한 경로 또는 output/output_{stock_name}_{stock_code}.json 경로를 결정합니다."""
        safe_name = sanitize_filename(self.stock_name or self.stock_code)
        if self.custom_output_path:
            out = Path(self.custom_output_path)
            if not out.parent or str(out.parent) == ".":
                out_dir = Path(self.settings.get("storage", {}).get("output_dir", "output"))
                return str(out_dir / out.name)
            return str(out)

        storage_cfg = self.settings.get("storage", {})
        out_dir = Path(storage_cfg.get("output_dir", "output"))
        template = storage_cfg.get("filename_template", "output_{stock_name}_{stock_code}.json")
        filename = template.format(stock_name=safe_name, stock_code=self.stock_code)
        return str(out_dir / filename)

    async def _extract_stock_name(self, page: Page) -> str:
        """페이지 상단 DOM, 메타 태그, 또는 가로챈 API 응답에서 종목명을 추출합니다."""
        # 1) 가로챈 API JSON에서 먼저 확인
        if self.interceptor.detected_stock_name:
            return self.interceptor.detected_stock_name

        # 2) DOM 및 document.title에서 추출
        try:
            name = await page.evaluate(
                """() => {
                    // 상단 헤더 타이틀 후보 셀렉터
                    const selectors = [
                        'h1', 'h2',
                        '[class*="StockInfo_name"]',
                        '[class*="GraphHeader_name"]',
                        '[class*="Header_name"]',
                        '[class*="stock_name"]',
                        '[class*="item_name"]'
                    ];
                    for (const sel of selectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const txt = (el.innerText || '').trim().split('\\n')[0].trim();
                            if (txt && txt.length >= 2 && txt.length <= 25 && !txt.includes('토론') && !txt.includes('네이버')) {
                                return txt;
                            }
                        }
                    }
                    // document.title 파싱 (예: "삼성전자 : 네이버페이 증권" 또는 "삼성전자 종목토론실")
                    const title = document.title || '';
                    if (title.includes(':')) {
                        return title.split(':')[0].replace('종목토론', '').trim();
                    }
                    if (title.includes('-')) {
                        return title.split('-')[0].replace('종목토론', '').trim();
                    }
                    return '';
                }"""
            )
            if name and str(name).strip():
                return str(name).strip()
        except Exception:
            pass

        return self.interceptor.detected_stock_name or self.stock_code

    async def _turn_off_cleanbot(self, page: Page, actor: HumanActor) -> None:
        """
        종목토론방 접속 직후 클린봇(Cleanbot) 설정을 OFF(해제)하여
        가려지는 게시글이나 댓글 없이 원문 전체가 보이도록 처리합니다.
        """
        self.logger.info("클린봇(Cleanbot) 상태 확인 및 OFF(해제) 시도 중...")
        try:
            # 1단계: 클린봇 버튼/토글 탐색
            cleanbot_selectors = [
                "button:has-text('클린봇')",
                "a:has-text('클린봇')",
                "label:has-text('클린봇')",
                "span:has-text('클린봇')",
                "[class*='cleanbot' i]",
                "[class*='CleanBot' i]",
                "[id*='cleanbot' i]",
            ]

            target_locator = None
            for sel in cleanbot_selectors:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    target_locator = loc
                    break

            if not target_locator:
                self.logger.info("클린봇 설정 버튼을 찾지 못했거나 이미 비활성화 상태입니다.")
                return

            # 현재 클린봇이 이미 OFF인지 텍스트/속성으로 확인
            status_info = await page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('button, a, label, span, div, input'));
                    for (const el of els) {
                        const txt = (el.innerText || '').trim();
                        if (txt.includes('클린봇')) {
                            if (txt.includes('꺼짐') || txt.includes('OFF')) return 'ALREADY_OFF';
                            const input = el.querySelector('input[type="checkbox"]') || (el.tagName === 'INPUT' ? el : null);
                            if (input && input.checked === false) return 'ALREADY_OFF';
                        }
                    }
                    return 'NEED_ACTION';
                }"""
            )
            if status_info == "ALREADY_OFF":
                self.logger.info("클린봇이 이미 OFF(해제) 상태입니다.")
                return

            # 사람처럼 클린봇 버튼 클릭
            await actor.human_click(target_locator, wait_after=True)

            # 2단계: 모달/팝업 창이 열렸다면 그 안의 토글 스위치나 체크박스/확인 버튼을 클릭하여 OFF로 전환
            modal_toggled = await page.evaluate(
                """() => {
                    // 모달 또는 레이어 내부의 클린봇 스위치 탐색
                    const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                    for (const cb of checkboxes) {
                        const parentText = (cb.closest('div, label, li')?.innerText || '');
                        if (parentText.includes('클린봇') || parentText.includes('악성') || cb.id.toLowerCase().includes('clean')) {
                            if (cb.checked) {
                                cb.click();
                                return 'CHECKBOX_UNCHECKED';
                            }
                        }
                    }
                    // aria-checked="true" 또는 active 클래스 토글 버튼 탐색
                    const switches = Array.from(document.querySelectorAll('[role="switch"], [aria-checked="true"], button[class*="toggle" i], button[class*="switch" i]'));
                    for (const sw of switches) {
                        const pTxt = (sw.closest('div, li')?.innerText || '');
                        if (pTxt.includes('클린봇') || sw.getAttribute('aria-checked') === 'true') {
                            sw.click();
                            return 'SWITCH_CLICKED';
                        }
                    }
                    return 'DIRECT_TOGGLED';
                }"""
            )
            self.logger.info(f"클린봇 OFF 조작 결과: {modal_toggled}")
            await actor.short_pause(0.6, 1.2)

            # 모달 닫기/확인 버튼이 있으면 사람처럼 닫기
            close_selectors = [
                "button:has-text('확인')",
                "button:has-text('적용')",
                "button:has-text('닫기')",
                "[class*='Layer_btn_close' i]",
                "[class*='Modal_close' i]",
                "button[aria-label*='닫기']",
            ]
            for c_sel in close_selectors:
                c_loc = page.locator(c_sel).first
                if await c_loc.count() > 0 and await c_loc.is_visible():
                    await actor.human_click(c_loc, wait_after=True)
                    break

            self.logger.info("클린봇(Cleanbot) OFF 설정을 완료했습니다.")
        except Exception as e:
            self.logger.warning(f"클린봇 OFF 전환 중 예외 발생 (계속 진행합니다): {e}")

    async def _crawl_posts_loop(self, page: Page, actor: HumanActor) -> None:
        """
        종목토론방 목록 페이지에서 게시글 링크들을 수집하고,
        사용자가 설정한 범위(--max-posts 또는 --since-date)에 도달할 때까지
        각 게시글 상세 페이지에 진입하여 원글+댓글+대댓글을 수집합니다.
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

            # 현재 페이지가 목록 페이지가 아니라면 목록으로 복귀
            if not self._is_list_page_url(page.url):
                await page.goto(self.list_url, wait_until="domcontentloaded", timeout=30000)
                await actor.random_wait()

            # 2) 현재 화면 및 네트워크 인터셉터에서 게시글 목록 추출
            post_summaries = await self._extract_post_list_from_page(page)

            # 아직 처리하지 않은 게시글 필터링
            pending_posts = []
            for p_info in post_summaries:
                nid = str(p_info["nid"])
                if nid in visited_in_session:
                    continue
                pending_posts.append(p_info)

            if not pending_posts:
                # 화면에 새로운 게시글이 없으면 사람처럼 아래로 스크롤하여 더 불러오기
                consecutive_no_new_scrolls += 1
                if consecutive_no_new_scrolls > 8:
                    self.logger.info("더 이상 새로운 게시글이 로드되지 않아 수집을 종료합니다.")
                    break

                self.logger.info(f"다음 게시글 목록 로드를 위해 스크롤합니다... ({consecutive_no_new_scrolls}/8)")
                await actor.human_scroll_down(chunks=3, wait_after=True)
                # '더보기' 버튼이 있다면 클릭
                more_btn = page.locator("button:has-text('더보기'), a:has-text('더보기')").first
                if await more_btn.count() > 0 and await more_btn.is_visible():
                    await actor.human_click(more_btn, wait_after=True)
                continue

            consecutive_no_new_scrolls = 0

            # 3) 발견된 게시글을 순차적으로 처리
            for p_info in pending_posts:
                nid = str(p_info["nid"])
                visited_in_session.add(nid)
                post_id_str = f"post_{nid}"

                # 목표 개수 재확인
                if self.max_posts is not None and self.storage.count_posts() >= self.max_posts:
                    break

                # 체크포인트 확인: 이미 파일에 존재하는 게시글 ID는 건너뛰기 (규칙 5)
                if self.storage.has_post(post_id_str):
                    self.logger.info(f"[건너뜀] 이미 수집된 게시글입니다: {post_id_str}")
                    # 단, since_date 기준일 이전인지 확인하기 위해 목록 날짜가 있으면 검사
                    if self.since_date and p_info.get("date"):
                        norm_dt = normalize_created_at(p_info["date"], get_now_scraped_at())
                        if is_older_than_since_date(norm_dt, self.since_date):
                            stop_by_since_date = True
                            break
                    continue

                # 게시글 상세 수집 실행
                bundle, post_created_at = await self._collect_single_post_detail(page, actor, p_info)
                if not bundle:
                    continue

                # 기준 날짜(--since-date) 이전 글인지 검사
                if self.since_date and is_older_than_since_date(post_created_at, self.since_date):
                    self.logger.info(
                        f"게시글({post_id_str}) 작성시간({post_created_at})이 기준일({self.since_date}) 이전입니다."
                    )
                    stop_by_since_date = True
                    break

                # 게시글 1개 세트(원글+댓글+대댓글) 즉시 중간 저장 (Checkpointing)
                added = self.storage.save_post_bundle(bundle)
                self.logger.info(
                    f"[체크포인트 저장 완료] {post_id_str} "
                    f"(원글 1개 + 댓글/대댓글 {len(bundle) - 1}개, 신규 저장 {added}건) "
                    f"| 누적 원글: {self.storage.count_posts()}개"
                )

                # 목록 페이지로 자연스럽게 돌아가기
                await self._return_to_list_page(page, actor)

            # 한 배치 처리 후 자연스럽게 아래로 스크롤
            if not stop_by_since_date and (self.max_posts is None or self.storage.count_posts() < self.max_posts):
                await actor.human_scroll_down(chunks=2, wait_after=True)

    def _is_list_page_url(self, url: str) -> bool:
        """현재 URL이 게시글 상세가 아닌 토론방 목록 URL인지 확인합니다."""
        clean = url.split("?")[0].rstrip("/")
        return clean.endswith("/discussion")

    async def _return_to_list_page(self, page: Page, actor: HumanActor) -> None:
        """상세 페이지에서 뒤로 가기 또는 목록 이동을 수행합니다."""
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
            await actor.random_wait(1.0, 2.2)
            if not self._is_list_page_url(page.url):
                await page.goto(self.list_url, wait_until="domcontentloaded", timeout=25000)
                await actor.random_wait(1.0, 2.2)
        except Exception:
            await page.goto(self.list_url, wait_until="domcontentloaded", timeout=25000)
            await actor.random_wait(1.0, 2.2)

    async def _extract_post_list_from_page(self, page: Page) -> List[Dict[str, Any]]:
        """
        현재 목록 페이지의 DOM과 NetworkInterceptor가 가로챈 JSON에서
        게시글 요약 목록(nid, title, post_url, date 등)을 추출합니다.
        """
        results: List[Dict[str, Any]] = []
        seen_nids = set()

        # 1) DOM에서 토론방 게시글 링크 추출 (/discussion/{nid} 또는 discussionId 파라미터)
        try:
            dom_posts = await page.evaluate(
                """(stockCode) => {
                    const items = [];
                    const anchors = Array.from(document.querySelectorAll('a[href*="discussion/"], a[href*="discussionId="], a[href*="nid="]'));
                    for (const a of anchors) {
                        const href = a.href || a.getAttribute('href') || '';
                        let nid = null;
                        const m1 = href.match(/discussion\\/(\\d{5,})/);
                        const m2 = href.match(/(?:discussionId|nid)=(\\d{5,})/);
                        if (m1) nid = m1[1];
                        else if (m2) nid = m2[1];
                        if (!nid) continue;

                        const container = a.closest('li, article, div[class*="item" i], div[class*="post" i]') || a;
                        const fullText = (container.innerText || '').trim();
                        const titleEl = container.querySelector('strong, h3, h4, [class*="title" i], [class*="subject" i]');
                        const title = titleEl ? titleEl.innerText.trim() : (a.innerText || '').trim().split('\\n')[0];

                        items.push({
                            nid: String(nid),
                            href: href,
                            title: title,
                            rawText: fullText
                        });
                    }
                    return items;
                }""",
                self.stock_code,
            )
            for item in dom_posts:
                nid = str(item.get("nid") or "")
                if not nid or nid in seen_nids:
                    continue
                seen_nids.add(nid)
                results.append(
                    {
                        "nid": nid,
                        "title": item.get("title") or "",
                        "post_url": f"https://m.stock.naver.com/domestic/stock/{self.stock_code}/discussion/{nid}",
                        "raw_text": item.get("rawText") or "",
                    }
                )
        except Exception as e:
            self.logger.debug(f"DOM 게시글 목록 추출 중 경미한 오류: {e}")

        # 2) 하이브리드 보강: NetworkInterceptor가 캡처한 게시글 JSON 목록 병합
        for nid, net_data in self.interceptor.posts_by_id.items():
            if nid not in seen_nids:
                seen_nids.add(nid)
                results.append(
                    {
                        "nid": nid,
                        "title": str(net_data.get("title") or ""),
                        "date": str(net_data.get("date") or net_data.get("regDate") or ""),
                        "post_url": f"https://m.stock.naver.com/domestic/stock/{self.stock_code}/discussion/{nid}",
                    }
                )

        return results

    async def _collect_single_post_detail(
        self, page: Page, actor: HumanActor, p_info: Dict[str, Any]
    ) -> Tuple[List[DiscussionItem], str]:
        """
        개별 게시글을 클릭(또는 이동)하여 상세 페이지에 진입하고:
        1. 사람처럼 스크롤하며 본문 읽기
        2. 접힌 댓글/대댓글('댓글 더보기', '답글 N개 보기') 모두 클릭 확장
        3. DOM + 백그라운드 JSON 데이터를 결합하여 원글('post'), 댓글('comment'), 대댓글('reply') 추출
        """
        nid = str(p_info["nid"])
        root_post_id = f"post_{nid}"
        post_url = f"https://m.stock.naver.com/domestic/stock/{self.stock_code}/discussion/{nid}"

        self.logger.info(f"게시글 상세 수집 시작: {root_post_id} ({p_info.get('title', '')[:30]})")
        self.interceptor.clear_latest_comments()

        # 목록 화면에서 해당 게시글 링크 요소가 보이면 사람처럼 마우스를 이동해 클릭
        link_locator = page.locator(f"a[href*='discussion/{nid}'], a[href*='discussionId={nid}'], a[href*='nid={nid}']").first
        clicked = False
        if await link_locator.count() > 0 and await link_locator.is_visible():
            clicked = await actor.human_click(link_locator, wait_after=True)

        if not clicked or nid not in page.url:
            await page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            await actor.random_wait(1.2, 2.5)

        # 사람이 본문을 읽는 것처럼 자연스럽게 스크롤
        await actor.human_scroll_to_read()

        # 접힌 댓글/대댓글('댓글 더보기', '답글 N개 보기' 등) 모두 클릭하여 펼치기 (규칙 4)
        if self.settings.get("collection", {}).get("expand_all_comments", True):
            await self._expand_all_comments_and_replies(page, actor)

        scraped_at = get_now_scraped_at()

        # 상세 페이지 DOM 데이터 추출
        dom_detail = await self._extract_detail_from_dom(page)
        # 인터셉터가 가로챈 네트워크 JSON 데이터
        net_post = self.interceptor.posts_by_id.get(nid, {})

        # --- 1. 원글(post) 데이터 조립 (DOM + Network JSON 상호 보완) ---
        title = (
            dom_detail.get("title")
            or net_post.get("title")
            or p_info.get("title")
            or ""
        ).strip()

        content = (
            dom_detail.get("content")
            or net_post.get("contents")
            or net_post.get("content")
            or net_post.get("body")
            or ""
        ).strip()
        # HTML 태그가 섞여 있을 경우 정제
        if "<br" in content or "<p" in content or "<div" in content:
            content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
            content = re.sub(r"<[^>]+>", "", content).strip()

        nickname = (
            dom_detail.get("nickname")
            or net_post.get("writerId")
            or net_post.get("nickname")
            or net_post.get("userName")
            or net_post.get("maskedUserId")
            or ""
        ).strip()

        is_shareholder = bool(
            dom_detail.get("is_shareholder")
            or net_post.get("isShareholder")
            or net_post.get("shareholder")
            or net_post.get("isHolder")
            or net_post.get("certifyHolder")
        )

        raw_created_at = (
            dom_detail.get("created_at")
            or net_post.get("date")
            or net_post.get("regDate")
            or net_post.get("createdAt")
            or p_info.get("date")
            or ""
        )
        created_at = normalize_created_at(str(raw_created_at), scraped_at)

        like_count = parse_int_safe(
            dom_detail.get("like_count")
            if dom_detail.get("like_count") is not None
            else (net_post.get("goodCount") or net_post.get("sympathyCount") or net_post.get("likeCount") or 0)
        )
        dislike_count = parse_int_safe(
            dom_detail.get("dislike_count")
            if dom_detail.get("dislike_count") is not None
            else (net_post.get("badCount") or net_post.get("antipathyCount") or net_post.get("dislikeCount") or 0)
        )

        # 첨부 이미지 URL 배열 (순수 문자열 리스트로만 처리, 다운로드 없음)
        image_urls: List[str] = []
        for u in (dom_detail.get("image_urls") or []):
            u_str = str(u).strip()
            if u_str and u_str.startswith("http") and u_str not in image_urls:
                image_urls.append(u_str)

        # 네트워크 JSON 내 이미지 배열이 있으면 추가 보완
        for img_key in ("images", "imageUrls", "attachImageList"):
            net_imgs = net_post.get(img_key)
            if isinstance(net_imgs, list):
                for img_item in net_imgs:
                    if isinstance(img_item, str) and img_item.startswith("http") and img_item not in image_urls:
                        image_urls.append(img_item)
                    elif isinstance(img_item, dict):
                        u_candidate = img_item.get("url") or img_item.get("imageUrl") or img_item.get("src")
                        if u_candidate and str(u_candidate).startswith("http") and str(u_candidate) not in image_urls:
                            image_urls.append(str(u_candidate))

        post_item = DiscussionItem(
            id=root_post_id,
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
            dislike_count=dislike_count,
            image_urls=image_urls,
            post_url=str(post_url),
        )

        # --- 2. 댓글(comment) 및 대댓글(reply) 평면형 리스트 조립 ---
        comment_and_reply_items = self._build_comments_and_replies(
            nid=nid,
            root_post_id=root_post_id,
            post_url=str(post_url),
            dom_comments=dom_detail.get("comments") or [],
            scraped_at=scraped_at,
        )

        bundle: List[DiscussionItem] = [post_item] + comment_and_reply_items
        return bundle, created_at

    async def _expand_all_comments_and_replies(self, page: Page, actor: HumanActor) -> None:
        """
        상세 페이지 내 '댓글 더보기', '답글 N개 보기', '답글 더보기', '이전 댓글 보기' 등의 버튼을
        사람처럼 클릭하여 접힌 댓글과 대댓글을 모두 펼칩니다.
        """
        expand_patterns = [
            "button:has-text('답글')",
            "a:has-text('답글')",
            "button:has-text('댓글 더보기')",
            "a:has-text('댓글 더보기')",
            "button:has-text('더보기')",
            "[class*='u_cbox_btn_more' i]",
            "[class*='u_cbox_btn_reply' i]",
            "[class*='Comment_more' i]",
            "[class*='Reply_more' i]",
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
                        # '답글 쓰기', '답글 접기', '답글 닫기' 등은 클릭 대상에서 제외
                        if any(skip in txt for skip in ("쓰기", "작성", "접기", "닫기", "등록", "취소", "신고")):
                            continue
                        # '답글', '더보기', '보기' 키워드나 숫자가 포함된 펼치기 버튼만 클릭
                        if any(k in txt for k in ("답글", "더보기", "보기")):
                            # 이미 열린 상태(aria-expanded="true")인지 확인
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
        상세 페이지의 DOM에서 원글 정보(제목, 본문, 작성자, 주주 배지, 작성시간, 공감/비공감, 첨부 이미지)와
        댓글/대댓글 목록을 정밀 추출합니다.
        """
        try:
            data = await page.evaluate(
                """() => {
                    const result = {
                        title: '',
                        nickname: '',
                        is_shareholder: false,
                        created_at: '',
                        content: '',
                        like_count: null,
                        dislike_count: null,
                        image_urls: [],
                        comments: []
                    };

                    // 1. 제목 추출
                    const titleSelectors = [
                        '[class*="DiscussionDetail_title" i]',
                        '[class*="ArticleHeader_title" i]',
                        '[class*="PostHeader_title" i]',
                        '[class*="view_title" i]',
                        'article h2', 'article h3', 'main h2', 'main h3'
                    ];
                    for (const sel of titleSelectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim()) {
                            result.title = el.innerText.trim();
                            break;
                        }
                    }

                    // 2. 작성자 닉네임 및 주주 배지 확인
                    const headerArea = document.querySelector(
                        '[class*="DiscussionDetail_header" i], [class*="ArticleHeader" i], [class*="PostHeader" i], [class*="writer_info" i], article header'
                    ) || document.body;

                    const nickEl = headerArea.querySelector(
                        '[class*="nickname" i], [class*="writer" i], [class*="author" i], [class*="user_name" i], [class*="name" i]'
                    );
                    if (nickEl) {
                        result.nickname = (nickEl.innerText || '').replace(/주주/g, '').trim().split('\\n')[0].trim();
                    }

                    // 주주 배지 판별 (닉네임 주변 또는 헤더 영역 내 '주주' 배지 텍스트/클래스)
                    const headerText = (headerArea.innerText || '');
                    const badgeEl = headerArea.querySelector('[class*="holder" i], [class*="shareholder" i], [class*="badge" i]');
                    if ((badgeEl && (badgeEl.innerText || '').includes('주주')) || /\\b주주\\b/.test(headerText)) {
                        result.is_shareholder = true;
                    }

                    // 3. 작성시간 (YYYY.MM.DD HH:MM 등)
                    const timeRegex = /\\d{4}[.-]\\d{1,2}[.-]\\d{1,2}\\.?\\s*\\d{1,2}:\\d{2}|\\d+\\s*(?:분|시간|일)\\s*전|어제/;
                    const timeEl = headerArea.querySelector('time, [class*="date" i], [class*="time" i]');
                    if (timeEl && timeEl.innerText.trim()) {
                        result.created_at = timeEl.innerText.trim();
                    } else {
                        const m = headerText.match(timeRegex);
                        if (m) result.created_at = m[0];
                    }

                    // 4. 본문 내용 및 첨부 이미지 URL 추출
                    const contentSelectors = [
                        '[class*="DiscussionDetail_content" i]',
                        '[class*="ArticleContent" i]',
                        '[class*="PostContent" i]',
                        '[class*="view_content" i]',
                        '[class*="contents" i]',
                        'article [class*="body" i]'
                    ];
                    let contentEl = null;
                    for (const sel of contentSelectors) {
                        const el = document.querySelector(sel);
                        if (el && (el.innerText || '').trim().length > 0) {
                            contentEl = el;
                            break;
                        }
                    }
                    if (contentEl) {
                        result.content = (contentEl.innerText || '').trim();
                        const imgs = Array.from(contentEl.querySelectorAll('img'));
                        for (const img of imgs) {
                            const src = img.getAttribute('data-src') || img.src || '';
                            if (src && src.startsWith('http') && !src.includes('profile') && !src.includes('icon') && !src.includes('badge')) {
                                result.image_urls.push(String(src));
                            }
                        }
                    }

                    // 5. 좋아요(공감) / 싫어요(비공감) 추출
                    const reactionBtns = Array.from(document.querySelectorAll('button, a, span'));
                    for (const btn of reactionBtns) {
                        const txt = (btn.innerText || '').trim();
                        const cls = (btn.className || '').toString().toLowerCase();
                        const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                        // 댓글 영역 내부 버튼은 건너뜀
                        if (btn.closest('[class*="cbox" i], [class*="comment" i], [class*="reply" i]')) continue;

                        if (cls.includes('recomm') || cls.includes('good') || cls.includes('like') || cls.includes('sympathy') || aria.includes('공감') || aria.includes('좋아요') || txt.startsWith('공감') || txt.startsWith('좋아요')) {
                            const numMatch = txt.match(/(\\d[\\d,]*)/);
                            if (numMatch && result.like_count === null) {
                                result.like_count = parseInt(numMatch[1].replace(/,/g, ''), 10);
                            }
                        }
                        if (cls.includes('unrecomm') || cls.includes('bad') || cls.includes('dislike') || cls.includes('antipathy') || aria.includes('비공감') || aria.includes('싫어요') || txt.startsWith('비공감') || txt.startsWith('싫어요')) {
                            const numMatch = txt.match(/(\\d[\\d,]*)/);
                            if (numMatch && result.dislike_count === null) {
                                result.dislike_count = parseInt(numMatch[1].replace(/,/g, ''), 10);
                            }
                        }
                    }

                    // 6. 댓글 및 대댓글 DOM 추출 (네이버 CBOX 및 자체 댓글 UI 모두 지원)
                    const commentNodes = Array.from(document.querySelectorAll(
                        'li.u_cbox_comment, [class*="CommentItem" i], [class*="comment_item" i], [class*="ReplyItem" i]'
                    ));

                    let lastTopCommentId = null;
                    commentNodes.forEach((node, idx) => {
                        const rawInfo = node.getAttribute('data-info') || node.getAttribute('data-comment-no') || node.getAttribute('data-id') || '';
                        let cid = '';
                        const mNo = rawInfo.match(/commentNo\\s*:\\s*['"]?(\\d+)/) || rawInfo.match(/^(\\d+)$/);
                        if (mNo) cid = mNo[1];
                        else cid = String(idx + 1);

                        const cls = (node.className || '').toString().toLowerCase();
                        const isReply = (
                            cls.includes('reply') ||
                            cls.includes('sub') ||
                            cls.includes('child') ||
                            node.getAttribute('data-reply') === 'true' ||
                            Boolean(node.closest('.u_cbox_reply_area, [class*="reply_list" i], [class*="ReplyList" i]')) ||
                            rawInfo.includes('replyLevel:2')
                        );

                        const cNickEl = node.querySelector('.u_cbox_nick, [class*="nick" i], [class*="writer" i], [class*="author" i]');
                        const cNick = cNickEl ? cNickEl.innerText.replace(/주주/g, '').trim() : '';

                        const cBadgeEl = node.querySelector('[class*="holder" i], [class*="badge" i]');
                        const cIsHolder = Boolean(
                            (cBadgeEl && (cBadgeEl.innerText || '').includes('주주')) ||
                            (node.querySelector('.u_cbox_info, header') && (node.querySelector('.u_cbox_info, header').innerText || '').includes('주주'))
                        );

                        const cContentEl = node.querySelector('.u_cbox_contents, [class*="content" i], [class*="text" i]');
                        const cContent = cContentEl ? cContentEl.innerText.trim() : '';

                        const cDateEl = node.querySelector('.u_cbox_date, time, [class*="date" i], [class*="time" i]');
                        const cDate = cDateEl ? cDateEl.innerText.trim() : '';

                        const cLikeEl = node.querySelector('.u_cbox_cnt_recomm, [class*="recomm" i] em, [class*="like" i] em, [class*="sympathy" i]');
                        const cDislikeEl = node.querySelector('.u_cbox_cnt_unrecomm, [class*="unrecomm" i] em, [class*="dislike" i] em, [class*="antipathy" i]');

                        const cImgs = [];
                        node.querySelectorAll('.u_cbox_image_section img, [class*="content" i] img').forEach(img => {
                            const s = img.getAttribute('data-src') || img.src || '';
                            if (s && s.startsWith('http')) cImgs.push(String(s));
                        });

                        let parentCommentId = null;
                        if (isReply) {
                            const pMatch = rawInfo.match(/parentCommentNo\\s*:\\s*['"]?(\\d+)/);
                            parentCommentId = pMatch ? pMatch[1] : lastTopCommentId;
                        } else {
                            lastTopCommentId = cid;
                        }

                        if (cContent || cImgs.length > 0) {
                            result.comments.push({
                                raw_id: String(cid),
                                is_reply: isReply,
                                parent_raw_id: parentCommentId ? String(parentCommentId) : null,
                                nickname: cNick,
                                is_shareholder: cIsHolder,
                                created_at: cDate,
                                content: cContent,
                                like_count: cLikeEl ? cLikeEl.innerText.trim() : '0',
                                dislike_count: cDislikeEl ? cDislikeEl.innerText.trim() : '0',
                                image_urls: cImgs
                            });
                        }
                    });

                    return result;
                }"""
            )
            return data or {}
        except Exception as e:
            self.logger.debug(f"상세 페이지 DOM 파싱 중 예외: {e}")
            return {}

    def _build_comments_and_replies(
        self,
        nid: str,
        root_post_id: str,
        post_url: str,
        dom_comments: List[Dict[str, Any]],
        scraped_at: str,
    ) -> List[DiscussionItem]:
        """
        DOM에서 추출된 댓글/대댓글과 NetworkInterceptor가 가로챈 JSON 댓글 데이터를 병합하여
        평면형(Flat) DiscussionItem 리스트로 반환합니다.
        - 댓글("comment"): id="comment_{번호}", parent_id=root_post_id, root_post_id=root_post_id
        - 대댓글("reply"): id="reply_{번호}", parent_id="comment_{상위댓글번호}", root_post_id=root_post_id
        """
        items: List[DiscussionItem] = []
        seen_ids = set()

        # 1) 네트워크 인터셉터에서 해당 게시글의 댓글이 캡처된 경우 우선 매핑 테이블 구성
        net_comments = list(self.interceptor.comments_by_post_id.get(nid, []))
        for c in self.interceptor.latest_comments:
            if c not in net_comments:
                net_comments.append(c)

        net_by_id: Dict[str, Dict[str, Any]] = {}
        for nc in net_comments:
            cid = str(nc.get("commentNo") or nc.get("commentId") or nc.get("_captured_id") or "")
            if cid:
                net_by_id[cid] = nc

        # 2) DOM 댓글이 있으면 DOM 순서대로 생성하되 네트워크 JSON 정보로 보강
        if dom_comments:
            last_comment_full_id = root_post_id
            for dc in dom_comments:
                raw_id = str(dc.get("raw_id") or "")
                nc = net_by_id.pop(raw_id, {})

                is_reply = bool(
                    dc.get("is_reply")
                    or (nc.get("replyLevel", 1) and int(nc.get("replyLevel", 1)) > 1)
                    or (
                        nc.get("parentCommentNo")
                        and str(nc.get("parentCommentNo")) != raw_id
                        and str(nc.get("parentCommentNo")) != "0"
                    )
                )

                if is_reply:
                    item_type = "reply"
                    full_id = f"reply_{raw_id}"
                    p_raw = dc.get("parent_raw_id") or nc.get("parentCommentNo")
                    parent_id = f"comment_{p_raw}" if p_raw else last_comment_full_id
                else:
                    item_type = "comment"
                    full_id = f"comment_{raw_id}"
                    parent_id = root_post_id
                    last_comment_full_id = full_id

                if full_id in seen_ids:
                    continue
                seen_ids.add(full_id)

                nickname = (
                    dc.get("nickname")
                    or nc.get("userName")
                    or nc.get("maskedUserId")
                    or nc.get("writerId")
                    or ""
                ).strip()

                is_shareholder = bool(
                    dc.get("is_shareholder")
                    or nc.get("isShareholder")
                    or nc.get("shareholder")
                    or nc.get("isHolder")
                )

                raw_date = dc.get("created_at") or nc.get("regTime") or nc.get("modTime") or nc.get("date") or ""
                created_at = normalize_created_at(str(raw_date), scraped_at)

                content = (dc.get("content") or nc.get("contents") or nc.get("content") or "").strip()
                like_cnt = parse_int_safe(dc.get("like_count") or nc.get("sympathyCount") or nc.get("goodCount") or 0)
                dislike_cnt = parse_int_safe(
                    dc.get("dislike_count") or nc.get("antipathyCount") or nc.get("badCount") or 0
                )

                imgs = [str(u) for u in (dc.get("image_urls") or []) if u and str(u).startswith("http")]

                items.append(
                    DiscussionItem(
                        id=full_id,
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
                        dislike_count=dislike_cnt,
                        image_urls=imgs,
                        post_url=post_url,
                    )
                )

        # 3) DOM에서 미처 잡히지 않았으나 백그라운드 JSON 응답으로 캡처된 댓글/대댓글 추가
        for raw_id, nc in net_by_id.items():
            parent_no = str(nc.get("parentCommentNo") or "")
            reply_level = int(nc.get("replyLevel") or 1)
            is_reply = (reply_level > 1) or (parent_no and parent_no != "0" and parent_no != raw_id)

            if is_reply:
                item_type = "reply"
                full_id = f"reply_{raw_id}"
                parent_id = f"comment_{parent_no}" if parent_no else root_post_id
            else:
                item_type = "comment"
                full_id = f"comment_{raw_id}"
                parent_id = root_post_id

            if full_id in seen_ids:
                continue
            seen_ids.add(full_id)

            content = str(nc.get("contents") or nc.get("content") or "").strip()
            if not content:
                continue

            nickname = str(nc.get("userName") or nc.get("maskedUserId") or nc.get("writerId") or "").strip()
            is_shareholder = bool(nc.get("isShareholder") or nc.get("shareholder") or nc.get("isHolder"))
            raw_date = str(nc.get("regTime") or nc.get("modTime") or nc.get("date") or "")
            created_at = normalize_created_at(raw_date, scraped_at)
            like_cnt = parse_int_safe(nc.get("sympathyCount") or nc.get("goodCount") or 0)
            dislike_cnt = parse_int_safe(nc.get("antipathyCount") or nc.get("badCount") or 0)

            items.append(
                DiscussionItem(
                    id=full_id,
                    item_type=item_type,
                    parent_id=parent_id,
                    root_post_id=root_post_id,
                    stock_name=self.stock_name,
                    stock_code=self.stock_code,
                    nickname=nickname,
                    is_shareholder=is_shareholder,
                    created_at=created_at,
                    scraped_at=created_at and scraped_at,
                    title=None,
                    content=content,
                    like_count=like_cnt,
                    dislike_count=dislike_cnt,
                    image_urls=[],
                    post_url=post_url,
                )
            )

        return items
