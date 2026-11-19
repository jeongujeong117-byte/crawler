"""
저장소 및 중간 저장(Checkpointing) 모듈 (`크롤링.md` 섹션 5, 6 기준)
- 원글("post"), 댓글("comment"), 대댓글("reply")을 평면형(Flat) JSON 배열로 저장
- encoding="utf-8", ensure_ascii=False, indent=2 엄격 적용
- 게시글 1개 세트 수집 완료 시마다 즉시 중간 저장 및 재실행 시 중복 id 건너뛰기 지원
"""
import json
import os
from pathlib import Path
from typing import List, Set, Dict, Any
from src.models import DiscussionItem


class CheckpointStorage:
    def __init__(self, output_path: str):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.items: List[Dict[str, Any]] = []
        self.existing_ids: Set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        """기존 출력 파일이 존재하면 로드하여 이미 수집된 id 목록을 복구합니다."""
        if not self.output_path.exists():
            return
        try:
            with open(self.output_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.items = data
                for row in data:
                    if isinstance(row, dict) and "id" in row:
                        self.existing_ids.add(str(row["id"]))
        except Exception:
            # 파일이 손상되었거나 비어있는 경우 새로 시작
            self.items = []
            self.existing_ids = set()

    def has_post(self, post_id: str) -> bool:
        """해당 게시글 ID(예: 'post_291847210' 또는 '291847210')가 이미 저장되어 있는지 확인합니다."""
        normalized_id = post_id if str(post_id).startswith("post_") else f"post_{post_id}"
        return normalized_id in self.existing_ids

    def has_id(self, item_id: str) -> bool:
        return str(item_id) in self.existing_ids

    def save_post_bundle(self, bundle: List[DiscussionItem]) -> int:
        """
        게시글 1개 세트(원글 + 댓글 + 대댓글 리스트)를 평면형 배열에 추가하고
        즉시 JSON 파일에 체크포인트(중간 저장)합니다.
        반환값: 새로 추가된 항목(원글/댓글/대댓글) 개수
        """
        added_count = 0
        for item in bundle:
            item_dict = item.to_dict()
            item_id = item_dict["id"]
            if item_id not in self.existing_ids:
                self.items.append(item_dict)
                self.existing_ids.add(item_id)
                added_count += 1
            else:
                # 이미 존재하는 경우 최신 정보로 업데이트
                for idx, existing in enumerate(self.items):
                    if existing.get("id") == item_id:
                        self.items[idx] = item_dict
                        break

        self.flush()
        return added_count

    def flush(self) -> None:
        """
        원자적(Atomic) 임시 파일 쓰기 방식으로 중단 시에도 JSON 깨짐 없이 안전하게 저장합니다.
        """
        temp_path = self.output_path.with_suffix(".json.tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.output_path)

    def count_posts(self) -> int:
        """현재까지 저장된 원글('post')의 총 개수를 반환합니다."""
        return sum(1 for item in self.items if item.get("item_type") == "post")

    def count_total_items(self) -> int:
        """현재까지 저장된 전체 항목(원글+댓글+대댓글) 개수를 반환합니다."""
        return len(self.items)
