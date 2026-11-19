"""
유틸리티 모듈:
- 종목코드 변환 규칙 (국내 6자리 <-> 토스 A접두사 변환, 해외주식 심볼 유지)
- ISO 8601(UTC/KST), Epoch 밀리초, 상대 시간('방금 전', 'N분 전', 'N시간 전', '어제', 'N일 전' 등) -> 'YYYY.MM.DD HH:MM' 보정
- 기준 날짜(--since-date) 비교
- 로깅 설정 및 파일명 안전 처리
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple, Any

KST = timezone(timedelta(hours=9))


def normalize_stock_codes(raw_code: str) -> Tuple[str, str]:
    """
    사용자가 입력한 종목코드를 (url용 toss_code, JSON 저장용 stock_code) 튜플로 변환합니다.

    규칙 (`크롤링.md` 섹션 1):
    - 국내 6자리 숫자(예: '005930') -> toss_code='A005930', stock_code='005930'
    - 'A' + 6자리 숫자(예: 'A005930', 'a005930') -> toss_code='A005930', stock_code='005930'
    - 해외주식 심볼 또는 'US...' 등 이미 접두사가 있는 코드(예: 'TSLA', 'US20201029001')
      -> 그대로 대문자 통일 (toss_code='TSLA', stock_code='TSLA')
    """
    code = str(raw_code or "").strip().upper()
    if re.fullmatch(r"\d{6}", code):
        return f"A{code}", code
    if re.fullmatch(r"A\d{6}", code):
        return code, code[1:]
    return code, code


def setup_logger(log_dir: str = "logs") -> logging.Logger:
    """콘솔 및 파일 로거를 초기화합니다."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("toss_stock_crawler")
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


def normalize_created_at(raw_time: Any, scraped_at: Optional[str] = None) -> str:
    """
    작성시간을 명세서 기준인 'YYYY.MM.DD HH:MM' (KST 기준) 형식으로 변환합니다.
    - Epoch 타임스탬프(초 또는 밀리초) 지원
    - ISO 8601 형식(UTC 'Z' 또는 타임존 오프셋 포함)은 KST(+09:00)로 정확히 변환
    - '2026.11.19 21:10' 등의 절대 시간 표준화
    - '방금', 'N초 전', 'N분 전', 'N시간 전', '어제', 'N일 전', 'N주 전', 'N개월 전' 등 상대 시간은 scraped_at 기준으로 보정
    """
    base_dt = _parse_scraped_at(scraped_at)
    if raw_time is None or str(raw_time).strip() == "":
        return base_dt.strftime("%Y.%m.%d %H:%M")

    # 0. 숫자형(Epoch timestamp: 초 또는 밀리초)
    if isinstance(raw_time, (int, float)) or re.fullmatch(r"\d{10,13}", str(raw_time).strip()):
        try:
            ts = float(raw_time)
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=KST)
            return dt.strftime("%Y.%m.%d %H:%M")
        except Exception:
            pass

    text = str(raw_time).strip()

    # 1. 타임존이 명시된 ISO 8601 형식 (예: 2026-11-19T12:10:00Z 또는 2026-11-19T21:10:00+09:00)
    if "T" in text and (text.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", text)):
        try:
            iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
            dt = datetime.fromisoformat(iso_text)
            if dt.tzinfo is not None:
                dt = dt.astimezone(KST)
            return dt.strftime("%Y.%m.%d %H:%M")
        except Exception:
            pass

    # 2. 일반 절대 날짜+시간 (예: 2026-11-19T21:10:00, 2026.11.19 21:10)
    m_iso = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})[T\s]+(\d{1,2}):(\d{2})", text)
    if m_iso:
        y, mo, d, h, mi = (int(m_iso.group(i)) for i in range(1, 6))
        return f"{y:04d}.{mo:02d}.{d:02d} {h:02d}:{mi:02d}"

    # 3. 날짜만 있는 경우 (예: 2026.11.19. 또는 2026-11-19 또는 2026년 11월 19일)
    m_date = re.search(r"^(\d{4})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})[일.]?$", text)
    if m_date:
        y, mo, d = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        return f"{y:04d}.{mo:02d}.{d:02d} 00:00"

    # 4. 연도 없이 월.일 시:분 또는 N월 N일 형식 (예: 11.19 21:10, 11월 19일)
    m_mdhm = re.search(r"^(\d{1,2})[./](\d{1,2})\.?\s+(\d{1,2}):(\d{2})", text)
    if m_mdhm:
        mo, d, h, mi = (int(m_mdhm.group(i)) for i in range(1, 5))
        return f"{base_dt.year:04d}.{mo:02d}.{d:02d} {h:02d}:{mi:02d}"

    m_kor_md = re.search(r"^(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m_kor_md:
        mo, d = int(m_kor_md.group(1)), int(m_kor_md.group(2))
        m_hm_opt = re.search(r"(\d{1,2}):(\d{2})", text)
        h = int(m_hm_opt.group(1)) if m_hm_opt else 0
        mi = int(m_hm_opt.group(2)) if m_hm_opt else 0
        return f"{base_dt.year:04d}.{mo:02d}.{d:02d} {h:02d}:{mi:02d}"

    # 5. 오늘 시:분만 있는 경우 (예: 21:10)
    m_hm = re.search(r"^(\d{1,2}):(\d{2})$", text)
    if m_hm:
        h, mi = int(m_hm.group(1)), int(m_hm.group(2))
        return f"{base_dt.year:04d}.{base_dt.month:02d}.{base_dt.day:02d} {h:02d}:{mi:02d}"

    # 6. 상대 시간 처리 ('방금', 'N초 전', 'N분 전', 'N시간 전', '어제', 'N일 전', 'N주 전', 'N달/개월 전', 'N년 전')
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

    m_year = re.search(r"(\d+)\s*년\s*전", text)
    if m_year:
        dt = base_dt - timedelta(days=365 * int(m_year.group(1)))
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


def parse_int_safe(val: Any) -> int:
    """문자열이나 숫자에서 정수값을 안전하게 추출합니다 (예: '1,234' -> 1234, '1.2만' -> 12000)."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    text = str(val).strip()
    m_man = re.search(r"([\d.]+)\s*만", text)
    if m_man:
        try:
            return int(float(m_man.group(1)) * 10000)
        except ValueError:
            pass
    m_chun = re.search(r"([\d.]+)\s*천", text)
    if m_chun:
        try:
            return int(float(m_chun.group(1)) * 1000)
        except ValueError:
            pass
    digits = re.sub(r"[^\d-]", "", text)
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0
