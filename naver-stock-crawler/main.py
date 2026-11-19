"""
네이버 증권 종목토론방 크롤러 메인 실행 스크립트 (`main.py`)
- CLI 인자(`argparse`) 및 대화형 콘솔 입력(`input`) 모두 지원
- 실행 예시:
  1) 대화형 실행:
     python main.py
  2) CLI 인자 실행 (최신 게시글 N개):
     python main.py --code 005930 --max-posts 30
  3) CLI 인자 실행 (특정 기준 날짜까지):
     python main.py --code 005930 --since-date 2026-11-18 --output output/samsung.json
"""
import argparse
import asyncio
import sys
from typing import Optional, Tuple

from src.config_loader import load_settings
from src.crawler import NaverStockDiscussionCrawler
from src.utils import setup_logger


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="네이버 증권 종목토론방(m.stock.naver.com) 게시글 및 댓글/대댓글 평면형(Flat) JSON 크롤러"
    )
    parser.add_argument(
        "--code",
        "--stock",
        "--stock-code",
        dest="stock_code",
        type=str,
        default=None,
        help="수집 대상 주식 6자리 종목코드 (예: 005930)",
    )
    parser.add_argument(
        "--max-posts",
        dest="max_posts",
        type=int,
        default=None,
        help="최신 게시글부터 수집할 최대 원글 개수 (예: 50)",
    )
    parser.add_argument(
        "--since-date",
        dest="since_date",
        type=str,
        default=None,
        help="수집 종료 기준 날짜 YYYY-MM-DD (예: 2026-11-18)",
    )
    parser.add_argument(
        "--output",
        dest="output",
        type=str,
        default=None,
        help="저장할 JSON 파일 경로 (기본값: output/output_{stock_name}_{stock_code}.json)",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        type=str,
        default="config/settings.yaml",
        help="설정 파일 경로 (기본값: config/settings.yaml)",
    )
    return parser


def prompt_interactive_if_needed(
    args: argparse.Namespace, settings: dict
) -> Tuple[str, Optional[int], Optional[str], Optional[str]]:
    """
    CLI 인자가 주어지지 않은 경우 사용자가 콘솔(input)에서 직접 설정할 수 있도록 안내합니다.
    """
    stock_code = args.stock_code
    max_posts = args.max_posts
    since_date = args.since_date
    output_path = args.output

    default_code = settings.get("target", {}).get("default_stock_code", "005930")
    default_max = settings.get("collection", {}).get("max_posts", 20)

    # CLI 인자 없이 단독 실행된 경우 대화형 입력 진행
    if not stock_code and sys.stdin.isatty():
        print("=" * 65)
        print(" 📈 네이버 증권 종목토론방 크롤러 (Playwright Visual Mode)")
        print("=" * 65)

        raw_code = input(f"1. 대상 주식 종목코드를 입력하세요 [기본값: {default_code}]: ").strip()
        stock_code = raw_code if raw_code else default_code

        if max_posts is None and not since_date:
            print("\n2. 수집 범위를 선택하세요:")
            print("   [1] 최신 게시글 N개 수집 (--max-posts)")
            print("   [2] 특정 기준 날짜까지의 게시글 수집 (--since-date)")
            mode = input("   선택 [기본값: 1]: ").strip() or "1"

            if mode == "2":
                raw_date = input("   기준 날짜를 입력하세요 (예: 2026-11-18): ").strip()
                since_date = raw_date if raw_date else None
                raw_limit = input("   최대 안전 수집 개수 제한 (엔터 시 제한 없음): ").strip()
                max_posts = int(raw_limit) if raw_limit.isdigit() else None
            else:
                raw_max = input(f"   수집할 최신 게시글 수를 입력하세요 [기본값: {default_max}]: ").strip()
                max_posts = int(raw_max) if raw_max.isdigit() else int(default_max)

        if not output_path:
            raw_out = input(
                "\n3. 저장할 JSON 파일 경로 [엔터 시 output/output_{종목명}_{종목코드}.json]: "
            ).strip()
            output_path = raw_out if raw_out else None

        print("=" * 65)
    else:
        stock_code = stock_code or default_code
        if max_posts is None and not since_date:
            max_posts = int(default_max)

    return stock_code, max_posts, since_date, output_path


async def async_main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    settings = load_settings(args.config_path)

    log_dir = settings.get("storage", {}).get("log_dir", "logs")
    logger = setup_logger(log_dir)

    stock_code, max_posts, since_date, output_path = prompt_interactive_if_needed(args, settings)

    logger.info(
        f"수집 설정 -> 종목코드: {stock_code} | max_posts: {max_posts} | "
        f"since_date: {since_date} | output: {output_path or '자동(output_{stock_name}_{stock_code}.json)'}"
    )

    crawler = NaverStockDiscussionCrawler(
        stock_code=stock_code,
        max_posts=max_posts,
        since_date=since_date,
        output_path=output_path,
        settings=settings,
        logger=logger,
    )

    try:
        saved_file = await crawler.run()
        logger.info(f"정상 종료되었습니다. 결과 파일: {saved_file}")
        return 0
    except KeyboardInterrupt:
        logger.warning("사용자 요청(Ctrl+C)으로 크롤링이 중단되었습니다. (직전 게시글까지 체크포인트 저장 완료)")
        return 130
    except Exception as e:
        logger.error(f"크롤링 실행 중 오류 발생: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
