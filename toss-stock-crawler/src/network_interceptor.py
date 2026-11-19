"""
토스증권 백그라운드 네트워크 JSON 응답 가로채기(Hybrid Network Interceptor) 모듈 (`크롤링.md` 섹션 3.1)
- Playwright 브라우저가 실제 화면을 탐색하는 동안 백그라운드에서 오가는
  토스증권 커뮤니티 API(`wts-info-api.tossinvest.com`, `wts-cert-api.tossinvest.com` 등)의
  JSON 응답을 실시간으로 캡처합니다.
- 토스증권 피드 DOM은 '3분 전' 등 상대시간이 많고 클래스명이 해시( 난독화 )되어 있으므로,
  백그라운드 JSON 응답의 정확한 `createdAt`(ISO 절대시간), 게시글/댓글 고유 ID, 주주 인증 여부(`isShareholder`/`badges`),
  본문 전체, 첨부 이미지 URL을 최우선으로 추출하여 DOM 추출과 상호 보완합니다.
"""
import json
import re
from typing import Dict, Any, List, Optional
from playwright.async_api import Response


class TossNetworkInterceptor:
    def __init__(self, logger=None):
        self.logger = logger
        # post_id(문자열) -> 정규화된 원본 게시글 dict (발견 순서 유지)
        self.posts_by_id: Dict[str, Dict[str, Any]] = {}
        # post_id(문자열) -> 캡처된 댓글/대댓글 dict 리스트
        self.comments_by_post_id: Dict[str, List[Dict[str, Any]]] = {}
        # 최근 가로챈 모든 댓글/대댓글 (상세 진입 시 매핑 보완용)
        self.latest_comments: List[Dict[str, Any]] = []
        # 종목명 캐시
        self.detected_stock_name: Optional[str] = None

    async def on_response(self, response: Response) -> None:
        """page.on('response', interceptor.on_response) 핸들러"""
        try:
            url = response.url
            # 토스증권 API 관심 URL 필터링
            if "tossinvest.com" not in url and "toss.im" not in url:
                return

            keywords = (
                "wts-info-api",
                "wts-cert-api",
                "community",
                "comment",
                "post",
                "reply",
                "replies",
                "feed",
                "stock-info",
                "stock-infos",
                "stocks",
                "overview",
                "discussion",
            )
            if not any(k in url.lower() for k in keywords):
                return

            if response.status != 200:
                return

            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type and "text/plain" not in content_type:
                return

            text = await response.text()
            if not text:
                return

            stripped = text.strip()
            if not (stripped.startswith("{") or stripped.startswith("[")):
                return

            data = json.loads(stripped)
            self._extract_from_payload(url, data)
        except Exception:
            # 백그라운드 응답 파싱 실패는 무시하고 크롤링 흐름을 방해하지 않음
            pass

    def _extract_from_payload(self, url: str, data: Any) -> None:
        url_lower = url.lower()

        # 1. 종목 기본 정보 API에서 종목명 추출 (작성자 닉네임과 혼동 방지)
        if any(k in url_lower for k in ("stock-info", "overview", "price", "summary", "/stocks/")):
            self._try_extract_stock_name(data)

        # 2. URL에서 현재 컨텍스트의 post_id 추론
        url_post_id = self._extract_post_id_from_url(url)
        is_comment_endpoint = any(k in url_lower for k in ("/replies", "/comments/", "reply", "child"))

        # 3. 재귀적으로 게시글/댓글/대댓글 탐색
        self._walk_payload(
            data,
            current_post_id=url_post_id,
            in_comment_context=is_comment_endpoint,
        )

    def _try_extract_stock_name(self, node: Any) -> None:
        if self.detected_stock_name:
            return
        if isinstance(node, dict):
            # author/user/writer 객체 내부는 건너뜀
            if any(k in node for k in ("nickname", "profileImageUrl", "userId", "memberId")):
                return
            for key in ("stockName", "koreanName", "krName", "companyName", "itemName"):
                val = node.get(key)
                if isinstance(val, str) and 1 < len(val.strip()) < 35:
                    self.detected_stock_name = val.strip()
                    return
            # stockCode가 함께 있는 dict의 name 필드
            if ("stockCode" in node or "productCode" in node or "symbol" in node) and isinstance(node.get("name"), str):
                val = node["name"].strip()
                if 1 < len(val) < 35:
                    self.detected_stock_name = val
                    return
            for v in node.values():
                if isinstance(v, (dict, list)):
                    self._try_extract_stock_name(v)
        elif isinstance(node, list):
            for item in node[:10]:
                if isinstance(item, (dict, list)):
                    self._try_extract_stock_name(item)

    def _extract_post_id_from_url(self, url: str) -> Optional[str]:
        patterns = [
            r"/comments/(\d{4,})/(?:replies|comments)",
            r"/posts/(\d{4,})(?:/comments|/replies|$|\?)",
            r"(?:postId|commentId|parentId|rootId)=(\d{4,})",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return None

    def _walk_payload(
        self,
        node: Any,
        current_post_id: Optional[str] = None,
        parent_comment_id: Optional[str] = None,
        in_comment_context: bool = False,
    ) -> None:
        if isinstance(node, list):
            for item in node:
                self._walk_payload(
                    item,
                    current_post_id=current_post_id,
                    parent_comment_id=parent_comment_id,
                    in_comment_context=in_comment_context,
                )
            return

        if not isinstance(node, dict):
            return

        # 커뮤니티 아이템(게시글/댓글/대댓글) 후보 판별
        raw_id = node.get("id") or node.get("commentId") or node.get("postId") or node.get("articleId")
        has_body = any(k in node for k in ("body", "content", "contents", "message", "text"))
        has_meta = any(k in node for k in ("author", "writer", "user", "createdAt", "likeCount", "likesCount"))

        if raw_id is not None and has_body and has_meta:
            item_id = str(raw_id)
            normalized = self._normalize_raw_item(node, item_id)

            # 상위 ID 판별
            explicit_parent = (
                node.get("parentId")
                or node.get("parentCommentId")
                or node.get("replyToId")
                or node.get("parentCommentNo")
            )
            explicit_root = (
                node.get("rootId")
                or node.get("rootPostId")
                or node.get("postId")
                or current_post_id
            )

            is_reply_or_comment = (
                in_comment_context
                or (explicit_parent is not None and str(explicit_parent) not in ("", "0", item_id))
                or (explicit_root is not None and str(explicit_root) != item_id)
                or (parent_comment_id is not None and str(parent_comment_id) != item_id)
            )

            if is_reply_or_comment:
                root_pid = str(explicit_root or current_post_id or "")
                p_id = str(explicit_parent or parent_comment_id or root_pid or "")
                is_reply = bool(
                    (parent_comment_id and str(parent_comment_id) != root_pid)
                    or (explicit_parent and root_pid and str(explicit_parent) != root_pid)
                    or (node.get("depth") and int(node.get("depth", 0)) >= 2)
                    or (node.get("replyLevel") and int(node.get("replyLevel", 1)) > 1)
                )

                normalized["_captured_id"] = item_id
                normalized["_post_id"] = root_pid
                normalized["_parent_id"] = p_id if is_reply else root_pid
                normalized["_is_reply"] = is_reply

                if root_pid:
                    bucket = self.comments_by_post_id.setdefault(root_pid, [])
                    self._upsert_comment(bucket, normalized)
                self._upsert_comment(self.latest_comments, normalized)

                # 중첩 대댓글(replies / childComments 등) 재귀 탐색
                for child_key in ("replies", "childComments", "children", "subComments"):
                    children = node.get(child_key)
                    if isinstance(children, list) and children:
                        self._walk_payload(
                            children,
                            current_post_id=root_pid or item_id,
                            parent_comment_id=item_id,
                            in_comment_context=True,
                        )
                return
            else:
                # 원글(Post)로 저장
                existing = self.posts_by_id.get(item_id, {})
                existing.update(normalized)
                self.posts_by_id[item_id] = existing

                # 원글 내부에 중첩된 댓글/대댓글 배열이 있으면 재귀 탐색
                for child_key in ("comments", "replies", "childComments", "children"):
                    children = node.get(child_key)
                    if isinstance(children, list) and children:
                        self._walk_payload(
                            children,
                            current_post_id=item_id,
                            parent_comment_id=None,
                            in_comment_context=True,
                        )
                return

        # 하위 노드 계속 탐색
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                next_comment_ctx = in_comment_context or (k.lower() in ("replies", "childcomments", "subcomments"))
                self._walk_payload(
                    v,
                    current_post_id=current_post_id,
                    parent_comment_id=parent_comment_id,
                    in_comment_context=next_comment_ctx,
                )

    def _upsert_comment(self, bucket: List[Dict[str, Any]], entry: Dict[str, Any]) -> None:
        cid = entry.get("_captured_id")
        for idx, existing in enumerate(bucket):
            if existing.get("_captured_id") == cid:
                existing.update(entry)
                bucket[idx] = existing
                return
        bucket.append(entry)

    def _normalize_raw_item(self, node: Dict[str, Any], item_id: str) -> Dict[str, Any]:
        """토스증권 API 응답의 다양한 필드명을 표준 내부 딕셔너리로 정규화합니다."""
        author = node.get("author") or node.get("writer") or node.get("user") or {}
        if not isinstance(author, dict):
            author = {}

        nickname = (
            author.get("nickname")
            or author.get("name")
            or author.get("profileName")
            or node.get("nickname")
            or node.get("authorNickname")
            or node.get("writerName")
            or ""
        )

        # 주주 인증 여부 판별 (boolean 플래그 + badges 배열 모두 검사)
        is_shareholder = bool(
            author.get("isShareholder")
            or author.get("isStockHolder")
            or author.get("shareholder")
            or author.get("hasStock")
            or author.get("isHolder")
            or node.get("isShareholder")
            or node.get("isStockHolder")
            or node.get("shareholder")
            or node.get("hasStock")
        )
        if not is_shareholder:
            for badge_src in (author.get("badges"), author.get("badge"), node.get("badges"), node.get("badge")):
                if isinstance(badge_src, list):
                    for b in badge_src:
                        b_str = json.dumps(b, ensure_ascii=False).upper() if isinstance(b, dict) else str(b).upper()
                        if any(tok in b_str for tok in ("SHAREHOLDER", "HOLDER", "STOCK_OWNER", "주주")):
                            is_shareholder = True
                            break
                elif isinstance(badge_src, str):
                    if any(tok in badge_src.upper() for tok in ("SHAREHOLDER", "HOLDER", "주주")):
                        is_shareholder = True

        # 작성시간 (ISO 절대시간 최우선)
        created_at = (
            node.get("createdAt")
            or node.get("createdDateTime")
            or node.get("registeredAt")
            or node.get("updatedAt")
            or node.get("date")
            or ""
        )

        # 제목 (토스 SNS 피드는 제목이 없는 경우가 대부분 -> 없으면 None)
        raw_title = node.get("title")
        title = str(raw_title).strip() if isinstance(raw_title, str) and raw_title.strip() else None

        # 본문
        content = (
            node.get("body")
            or node.get("content")
            or node.get("contents")
            or node.get("message")
            or node.get("text")
            or ""
        )
        if isinstance(content, dict):
            content = content.get("text") or content.get("body") or ""

        # 좋아요 수
        like_count = (
            node.get("likeCount")
            if node.get("likeCount") is not None
            else (
                node.get("likesCount")
                if node.get("likesCount") is not None
                else (
                    node.get("heartCount")
                    if node.get("heartCount") is not None
                    else (node.get("sympathyCount") or node.get("reactionCount") or 0)
                )
            )
        )

        # 댓글 수 (게시글에 댓글이 달렸는지 빠른 판단용)
        comment_count = (
            node.get("commentCount")
            if node.get("commentCount") is not None
            else (node.get("replyCount") if node.get("replyCount") is not None else node.get("repliesCount"))
        )

        # 첨부 이미지 URL 배열 추출 (순수 문자열만)
        image_urls: List[str] = []
        for img_key in ("images", "imageUrls", "attachments", "media", "photos"):
            raw_imgs = node.get(img_key)
            if isinstance(raw_imgs, list):
                for img in raw_imgs:
                    u_cand = None
                    if isinstance(img, str):
                        u_cand = img
                    elif isinstance(img, dict):
                        u_cand = (
                            img.get("originUrl")
                            or img.get("url")
                            or img.get("imageUrl")
                            or img.get("src")
                            or img.get("thumbnailUrl")
                        )
                    if u_cand and str(u_cand).startswith("http") and str(u_cand) not in image_urls:
                        image_urls.append(str(u_cand).strip())

        return {
            "id": item_id,
            "nickname": str(nickname).strip(),
            "is_shareholder": is_shareholder,
            "created_at": created_at,
            "title": title,
            "content": str(content).strip(),
            "like_count": like_count,
            "comment_count": comment_count,
            "image_urls": image_urls,
        }

    def clear_latest_comments(self) -> None:
        self.latest_comments.clear()
