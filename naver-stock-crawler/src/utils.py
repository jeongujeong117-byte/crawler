"""
유틸리티 모듈:
- 상대 시간('방금 전', 'N분 전', 'N시간 전', '어제', 'N일 전' 등) 및 ISO 시간을 'YYYY.MM.DD HH:MM'으로 보정
- 기준 날짜(--since-date) 비교
- 로깅 설정 및 파일명 안전 처리
"""
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def setup_logger(log_dir: str = "logs") -> logging.Logger:
    """콘솔 및 파일 로거를 초기화합니다."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("naver_stock_crawler")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    log_file = Path(log_dir) / f"crawler_{datetime.now().strftime('%Y%m%d')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def get_now_scraped_at() -> str:
    """현재 수집 시각을 'YYYY-MM-DD HH:MM:SS' 형식으로 반환합니다."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_created_at(raw_time: str, scraped_at: Optional[str] = None) -> str:
    """
    작성시간 문자열을 명세서 기준인 'YYYY.MM.DD HH:MM' 형식으로 변환합니다.
    - 이미 '2026.11.19 11:45' 등의 절대 시간이면 표준화하여 반환
    - '2026-11-19T11:45:12...' 등의 ISO 형식이면 '2026.11.19 11:45'로 변환
    - '방금', 'N초 전', 'N분 전', 'N시간 전', '어제', 'N일 전' 등 상대 시간이면 scraped_at 기준으로 계산
    """
    if not raw_time:
        base_dt = _parse_scraped_at(scraped_at)
        return base_dt.strftime("%Y.%m.%d %H:%M")

    text = str(raw_time).strip()
    base_dt = _parse_scraped_at(scraped_at)

    # 1. ISO 8601 형식 (예: 2026-11-19T11:45:00+09:00 또는 2026-11-19 11:45:22)
    m_iso = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})[T\s]+(\d{1,2}):(\d{2})", text)
    if m_iso:
        y, mo, d, h, mi = (int(m_iso.group(i)) for i in range(1, 6))
        return f"{y:04d}.{mo:02d}.{d:02d} {h:02d}:{mi:02d}"

    # 2. 날짜만 있는 경우 (예: 2026.11.19. 또는 2026-11-19)
    m_date = re.search(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})\.?$", text)
    if m_date:
        y, mo, d = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        return f"{y:04d}.{mo:02d}.{d:02d} 00:00"

    # 3. 연도 없이 월.일 시:분 형식 (예: 11.19 11:45)
    m_mdhm = re.search(r"^(\d{1,2})[./](\d{1,2})\.?\s+(\d{1,2}):(\d{2})", text)
    if m_mdhm:
        mo, d, h, mi = (int(m_mdhm.group(i)) for i in range(1, 5))
        return f"{base_dt.year:04d}.{mo:02d}.{d:02d} {h:02d}:{mi:02d}"

    # 4. 오늘 시:분만 있는 경우 (예: 11:45)
    m_hm = re.search(r"^(\d{1,2}):(\d{2})$", text)
    if m_hm:
        h, mi = int(m_hm.group(1)), int(m_hm.group(2))
        return f"{base_dt.year:04d}.{base_dt.month:02d}.{base_dt.day:02d} {h:02d}:{mi:02d}"

    # 5. 상대 시간 처리 ('방금', 'N초 전', 'N분 전', 'N시간 전', '어제', 'N일 전', 'N주 전', 'N달 전')
    if "방금" in text or "초 전" in text or "초전" in text:
        return base_dt.strftime("%Y.%m.%d %H:%M")

    m_min = re.search(r"(\d+)\s*분\s*전", text)
    if m_min:
        dt = base_dt - timedelta(minutes=int(m_min.group(1)))
        return dt.strftime("%Y.%m.%d %H:%M")

    m_hour = re.search(r"(\d+)\s*시간\s*전", text)
    if m_hour:
        dt = base_dt - timedelta(hours=int(m_hour.group(1)))
        return dt.strftime("%Y.%m.%d %H:%M")

    if "어제" in text:
        dt = base_dt - timedelta(days=1)
        # '어제 14:20' 형태 지원
        m_yhm = re.search(r"(\d{1,2}):(\d{2})", text)
        if m_yhm:
            dt = dt.replace(hour=int(m_yhm.group(1)), minute=int(m_yhm.group(2)))
        return dt.strftime("%Y.%m.%d %H:%M")

    m_day = re.search(r"(\d+)\s*일\s*전", text)
    if m_day:
        dt = base_dt - timedelta(days=int(m_day.group(1)))
        return dt.strftime("%Y.%m.%d %H:%M")

    m_week = re.search(r"(\d+)\s*주\s*전", text)
    if m_week:
        dt = base_dt - timedelta(weeks=int(m_week.group(1)))
        return dt.strftime("%Y.%m.%d %H:%M")

    m_month = re.search(r"(\d+)\s*(?:달|개월)\s*전", text)
    if m_month:
        dt = base_dt - timedelta(days=30 * int(m_month.group(1)))
        return dt.strftime("%Y.%m.%d %H:%M")

    return text


def _parse_scraped_at(scraped_at: Optional[str]) -> datetime:
    if scraped_at:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(scraped_at, fmt)
            except ValueError:
                continue
    return datetime.now()


def parse_created_at_date(created_at_str: str) -> Optional[datetime]:
    """'YYYY.MM.DD HH:MM'에서 날짜/시간 객체를 추출합니다."""
    if not created_at_str:
        return None
    m = re.search(r"(\d{4})[.-/](\d{1,2})[.-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", created_at_str)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h = int(m.group(4)) if m.group(4) is not None else 0
    mi = int(m.group(5)) if m.group(5) is not None else 0
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError:
        return None


def is_older_than_since_date(created_at_str: str, since_date_str: Optional[str]) -> bool:
    """
    created_at_str이 사용자가 지정한 since_date(예: '2026-11-18')보다 이전 날짜인지 판별합니다.
    since_date 당일에 작성된 글까지는 수집 대상(False)이고, 그 이전 날짜이면 True를 반환합니다.
    """
    if not since_date_str:
        return False
    post_dt = parse_created_at_date(created_at_str)
    if not post_dt:
        return False
    m = re.search(r"(\d{4})[.-/](\d{1,2})[.-/](\d{1,2})", str(since_date_str))
    if not m:
        return False
    since_dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0)
    return post_dt < since_dt


def sanitize_filename(name: str) -> str:
    """Windows/Linux 파일명에 사용할 수 없는 특수문자를 제거합니다."""
    return re.sub(r'[\\/:*?"<>|]+', "_", str(name).strip())


def parse_int_safe(val) -> int:
    """문자열이나 숫자에서 정수값을 안전하게 추출합니다 (예: '1,234' -> 1234)."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    digits = re.sub(r"[^\d-]", "", str(val))
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0
