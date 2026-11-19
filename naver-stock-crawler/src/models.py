"""
데이터 모델 및 평면형(Flat) JSON 스키마 정의 (`크롤링.md` 명세 기준)
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class DiscussionItem:
    """
    원글("post"), 댓글("comment"), 대댓글("reply")을 모두 동일한 구조로 표현하는 평면형(Flat) 데이터 모델
    """
    id: str                         # 예: "post_291847210", "comment_8812031", "reply_8812099"
    item_type: str                  # "post" | "comment" | "reply"
    parent_id: Optional[str]        # 원글은 None(null), 댓글은 원글 id, 대댓글은 상위 댓글 id
    root_post_id: str               # 최상위 원글의 id (예: "post_291847210")
    stock_name: str                 # 종목명 (예: "삼성전자")
    stock_code: str                 # 6자리 종목코드 (예: "005930")
    nickname: str                   # 작성자 닉네임
    is_shareholder: bool            # '주주' 인증 배지 유무 (True / False)
    created_at: str                 # 작성시간 (절대시간 YYYY.MM.DD HH:MM 형식 우선)
    scraped_at: str                 # 수집시각 (YYYY-MM-DD HH:MM:SS)
    title: Optional[str]            # 게시글 제목 (댓글/대댓글은 None)
    content: str                    # 본문 또는 댓글/대댓글 내용
    like_count: int = 0             # 좋아요(공감) 수
    dislike_count: int = 0          # 싫어요(비공감) 수
    image_urls: List[str] = field(default_factory=list)  # 순수 문자열(str) 이미지 URL 리스트
    post_url: str = ""              # 게시글 원본 주소 (순수 문자열 str)

    def to_dict(self) -> Dict[str, Any]:
        """
        명세서의 필드 순서와 타입 규칙(특히 모든 URL은 순수 str 처리)을 엄격히 보장하여 dict로 변환합니다.
        """
        clean_image_urls = [str(url).strip() for url in (self.image_urls or []) if url and str(url).strip()]
        return {
            "id": str(self.id),
            "item_type": str(self.item_type),
            "parent_id": str(self.parent_id) if self.parent_id is not None else None,
            "root_post_id": str(self.root_post_id),
            "stock_name": str(self.stock_name),
            "stock_code": str(self.stock_code),
            "nickname": str(self.nickname or ""),
            "is_shareholder": bool(self.is_shareholder),
            "created_at": str(self.created_at or ""),
            "scraped_at": str(self.scraped_at or ""),
            "title": str(self.title) if self.title is not None else None,
            "content": str(self.content or ""),
            "like_count": int(self.like_count or 0),
            "dislike_count": int(self.dislike_count or 0),
            "image_urls": clean_image_urls,
            "post_url": str(self.post_url or ""),
        }
