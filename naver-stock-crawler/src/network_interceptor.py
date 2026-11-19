"""
백그라운드 네트워크 JSON 응답 가로채기(Hybrid Network Interceptor) 모듈 (`크롤링.md` 섹션 3.1)
- Playwright 브라우저가 실제 화면을 탐색하는 동안 백그라운드에서 오가는
  네이버 증권 토론방 API(`api/discuss`, `commentBox/cbox`, `discussion` 등) JSON/JSONP 응답을 실시간 캡처합니다.
- DOM 추출과 상호 보완(Cross-Fill & Fallback)하여 DOM 클래스명이 바뀌거나 일부 요소가 가려져도
  데이터(본문, 작성시간, 주주 여부, 댓글/대댓글, 공감/비공감 수, 이미지 URL)를 완벽하게 복원합니다.
"""
import json
import re
from typing import Dict, Any, List, Optional
from playwright.async_api import Response


class NetworkInterceptor:
    def __init__(self, logger=None):
        self.logger = logger
        # post_nid(문자열) -> 캡처된 원본 게시글 dict
        self.posts_by_id: Dict[str, Dict[str, Any]] = {}
        # post_nid(문자열) -> 캡처된 댓글/대댓글 원본 dict 리스트
        self.comments_by_post_id: Dict[str, List[Dict[str, Any]]] = {}
        # 최근 가로챈 모든 댓글/대댓글 (cbox 등 objectId 매핑용)
        self.latest_comments: List[Dict[str, Any]] = []
        # 종목명 캐시
        self.detected_stock_name: Optional[str] = None

    async def on_response(self, response: Response) -> None:
        """page.on('response', interceptor.on_response) 핸들러"""
        try:
            url = response.url
            # 관심 대상 URL 필터링
            keywords = ("discuss", "discussion", "cbox", "comment", "localStock", "basic", "stock")
            if not any(k in url for k in keywords):
                return

            if response.status != 200:
                return

            content_type = (response.headers.get("content-type") or "").lower()
            if not any(t in content_type for t in ("json", "javascript", "text/plain", "html")):
                return

            text = await response.text()
            if not text:
                return

            data = self._parse_json_or_jsonp(text)
            if data is None:
                return

            self._extract_from_payload(url, data)
        except Exception:
            # 백그라운드 응답 파싱 실패는 무시하고 크롤링 흐름을 방해하지 않음
            pass

    def _parse_json_or_jsonp(self, raw_text: str) -> Any:
        stripped = raw_text.strip()
        if not stripped:
            return None

        # 일반 JSON
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except Exception:
                return None

        # 네이버 CBOX 등의 JSONP 형태: _callback({...}); 또는 jQuery123({...})
        m = re.search(r"^[a-zA-Z0-9_$.]+\s*\(\s*(\{.*\}|\[.*\])\s*\)\s*;?\s*$", stripped, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                return None
        return None

    def _extract_from_payload(self, url: str, data: Any) -> None:
        # 1. 종목 기본 정보에서 종목명(stockName) 추출
        if isinstance(data, dict):
            for key in ("stockName", "itemName", "name", "hname"):
                val = data.get(key)
                if isinstance(val, str) and val.strip() and len(val.strip()) < 30:
                    if not self.detected_stock_name:
                        self.detected_stock_name = val.strip()

        # 2. 재귀적으로 게시글/댓글 구조 탐색
        self._walk_payload(data, current_post_id=self._extract_post_id_from_url(url))

    def _extract_post_id_from_url(self, url: str) -> Optional[str]:
        # URL 내에 discussionId, nid, objectId 등이 있는 경우 추출
        m = re.search(r"(?:discussion|localStock)/[^/?]+/(\d{5,})", url)
        if m:
            return m.group(1)
        m2 = re.search(r"(?:discussionId|nid|articleId|contentId)=(\d{5,})", url)
        if m2:
            return m2.group(1)
        # CBOX objectId 패턴 (예: objectId=005930_291847210 또는 291847210)
        m3 = re.search(r"objectId=[^&]*?_(\d{5,})", url)
        if m3:
            return m3.group(1)
        return None

    def _walk_payload(self, node: Any, current_post_id: Optional[str] = None) -> None:
        if isinstance(node, list):
            for item in node:
                self._walk_payload(item, current_post_id)
            return

        if not isinstance(node, dict):
            return

        # (A) 게시글(Post) 객체인지 판별
        disc_id = node.get("discussionId") or node.get("nid") or node.get("articleId") or node.get("postId")
        if disc_id is not None and ("title" in node or "contents" in node or "content" in node or "body" in node):
            pid = str(disc_id)
            existing = self.posts_by_id.get(pid, {})
            existing.update(node)
            self.posts_by_id[pid] = existing
            current_post_id = pid

            if not self.detected_stock_name:
                for k in ("stockName", "itemName"):
                    if isinstance(node.get(k), str) and node.get(k).strip():
                        self.detected_stock_name = node.get(k).strip()

        # (B) 댓글/대댓글(Comment/Reply - CBOX 또는 자체 댓글 포맷) 객체인지 판별
        comment_no = node.get("commentNo") or node.get("commentId") or node.get("replyNo")
        if comment_no is not None and ("contents" in node or "content" in node or "text" in node):
            cid = str(comment_no)
            # 상위 게시글 ID 파악
            obj_id = str(node.get("objectId") or "")
            post_id = current_post_id
            if not post_id and "_" in obj_id:
                post_id = obj_id.split("_")[-1]
            elif not post_id and obj_id.isdigit():
                post_id = obj_id

            comment_entry = dict(node)
            comment_entry["_captured_id"] = cid
            comment_entry["_post_id"] = post_id

            if post_id:
                bucket = self.comments_by_post_id.setdefault(str(post_id), [])
                if not any(str(c.get("_captured_id")) == cid for c in bucket):
                    bucket.append(comment_entry)

            if not any(str(c.get("_captured_id")) == cid for c in self.latest_comments):
                self.latest_comments.append(comment_entry)

        # 하위 노드 계속 탐색
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                self._walk_payload(v, current_post_id)

    def clear_latest_comments(self) -> None:
        self.latest_comments.clear()
