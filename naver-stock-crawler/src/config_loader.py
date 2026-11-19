"""
설정 파일(config/settings.yaml) 로더 및 기본 설정 관리 모듈
"""
import os
import json
from pathlib import Path
from typing import Dict, Any

DEFAULT_SETTINGS: Dict[str, Any] = {
    "target": {
        "default_stock_code": "005930",
        "base_url": "https://m.stock.naver.com/domestic/stock/{stock_code}/discussion",
    },
    "collection": {
        "max_posts": 20,
        "since_date": None,
        "cleanbot_off": True,
        "expand_all_comments": True,
    },
    "browser": {
        "headless": False,  # 규칙 1: 반드시 headless=False
        "viewport": {"width": 1280, "height": 900},
        "locale": "ko-KR",
        "timezone_id": "Asia/Seoul",
        "user_agent": None,
        "engine": "playwright_hybrid",
    },
    "human_interaction": {
        "mouse_move_steps_min": 12,
        "mouse_move_steps_max": 28,
        "click_offset_px": 5,
        "typing_delay_min_ms": 50,
        "typing_delay_max_ms": 150,
        "action_wait_min_sec": 1.0,
        "action_wait_max_sec": 2.8,
        "scroll_step_min_px": 250,
        "scroll_step_max_px": 550,
    },
    "storage": {
        "output_dir": "output",
        "filename_template": "output_{stock_name}_{stock_code}.json",
        "log_dir": "logs",
        "checkpoint_per_post": True,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_settings(config_path: str = "config/settings.yaml") -> Dict[str, Any]:
    """
    config/settings.yaml 파일을 읽어 기본 설정과 병합하여 반환합니다.
    PyYAML이 설치되어 있지 않은 환경에서도 기본 파서로 동작하도록 안전하게 처리합니다.
    """
    path = Path(config_path)
    if not path.exists():
        return dict(DEFAULT_SETTINGS)

    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        return _deep_merge(DEFAULT_SETTINGS, loaded)
    except ImportError:
        # PyYAML 미설치 시 간단한 키-값 파싱 또는 기본값 사용
        return dict(DEFAULT_SETTINGS)
    except Exception:
        return dict(DEFAULT_SETTINGS)
