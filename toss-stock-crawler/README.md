# 토스증권 종목 커뮤니티 크롤러 (`toss-stock-crawler`)

`크롤링.md` 명세서를 바탕으로 토스증권 종목 커뮤니티(`https://tossinvest.com/stocks/{toss_code}/community`)의 **원글(`post`)**, **댓글(`comment`)**, **대댓글(`reply`)**을 네이버 증권 버전(`naver-stock-crawler`)과 호환되는 평면형(Flat) JSON 배열로 수집하는 Python 크롤러입니다.

---

## 📁 디렉토리 구조 (기능별 분리)

```text
toss-stock-crawler/
├── config/                     # [설정] 크롤러 동작, 딜레이, 브라우저, 저장 경로 설정
│   └── settings.yaml
├── src/                        # [프로그램 코어 모듈]
│   ├── __init__.py
│   ├── config_loader.py        # YAML 설정 로더 및 기본값 병합
│   ├── models.py               # 평면형(Flat) JSON 데이터 스키마 (DiscussionItem, source="toss")
│   ├── crawler.py              # Playwright 메인 크롤링 엔진 (정렬 전환, 로그인 모달 대응, 댓글 확장 등)
│   ├── network_interceptor.py  # 백그라운드 JSON 응답(page.on("response")) 하이브리드 가로채기
│   ├── human_actor.py          # 인간 행동 모사 (다단계 마우스 커서 이동, 랜덤 오프셋 클릭, 무한 스크롤)
│   ├── storage.py              # 게시글 단위 즉시 원자적 중간 저장(Checkpointing) 및 중복 ID 건너뛰기
│   └── utils.py                # 종목코드 변환(005930 <-> A005930), ISO/상대시간 KST 보정, 로깅 유틸리티
├── output/                     # [결과 데이터] 수집된 JSON 파일이 저장되는 폴더
│   └── output_toss_{종목명}_{종목코드}.json
├── logs/                       # [실행 로그] 일자별 크롤링 진행 상태 로그 폴더
├── main.py                     # [실행 진입점] CLI 인자(argparse) 및 콘솔 대화형(input) 지원
├── requirements.txt            # 필수 Python 패키지 목록
├── README.md                   # 프로젝트 안내 문서
└── 크롤링.md                   # 크롤링 요구사항 명세서
```

---

## 🚀 설치 및 실행 방법

### 1. 패키지 및 Playwright 브라우저 설치
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 실행 방법

#### 방법 A: 대화형 콘솔 입력 모드 (`input`)
인자 없이 실행하면 종목코드, 정렬 기준(`latest`/`popular`), 수집 범위(`--max-posts` 또는 `--since-date`), 파일 경로를 순서대로 입력할 수 있습니다.
```bash
python main.py
```

#### 방법 B: CLI 인자 실행 (`argparse`)
* **최신 게시글 N개 수집 (`--max-posts`)**
  ```bash
  python main.py --code 005930 --max-posts 30
  ```
* **특정 기준 날짜까지의 게시글 수집 (`--since-date`)**
  ```bash
  python main.py --code 005930 --sort latest --since-date 2026-11-18
  ```
* **저장 파일 경로 직접 지정 (`--output`)**
  ```bash
  python main.py --code 005930 --max-posts 10 --output output/output_toss_삼성전자_005930.json
  ```
* **해외주식 심볼 및 브라우저 세션 유지 (`--user-data-dir`)**
  ```bash
  python main.py --code TSLA --max-posts 20 --user-data-dir .browser_profile
  ```

---

## ✨ 주요 구현 특징 (`크롤링.md` 명세 반영)

1. **종목코드 자동 변환 및 네이버 스키마 호환**
   * 국내 6자리 종목코드(`005930`) 입력 시 접속 URL은 자동으로 `A005930`으로 변환하고, 저장 JSON의 `stock_code`는 `"005930"`으로 통일합니다.
   * `id`는 `toss_post_`, `toss_comment_`, `toss_reply_` 접두사를 사용하고 `source: "toss"`, `dislike_count: 0`을 보장해 네이버 데이터와 충돌 없이 통합 분석할 수 있습니다.
2. **시각적 모니터링 (`headless=False`) & 네트워크 JSON 하이브리드 수집**
   * 브라우저 창을 항상 화면에 띄워(`headless=False`) 진행 상황을 실시간으로 확인할 수 있습니다.
   * `page.on("response")`로 토스증권 커뮤니티 API JSON 응답을 가로채어 정확한 `createdAt`(ISO 절대시간), 주주 인증 여부(`is_shareholder`), 첨부 이미지 URL 등을 최우선 반영합니다.
3. **토스증권 전용 진입 처리**
   * 진입 직후 `'최신순'`(기본값) 또는 `'인기순'` 탭으로 자동 전환합니다.
   * 비로그인 제한 / 로그인 유도 모달 발생 시 닫기를 우선 시도하며, 필요 시 사용자가 브라우저에서 직접 로그인할 수 있도록 대기하고 `--user-data-dir`로 세션 유지를 지원합니다.
4. **인간 행동과 유사한 인터랙션 (`src/human_actor.py`)**
   * 베지어 곡선 다단계 마우스 이동, 랜덤 오프셋 클릭, `page.mouse.wheel` 다단계 무한 스크롤, 긴 글 `'더보기'` 및 `'댓글 더보기'` / `'답글 N개 보기'` 자동 확장을 수행합니다.
5. **평면형(Flat) JSON & 즉시 중간 저장(Checkpointing)**
   * 게시글 1개 세트(원글+댓글+대댓글) 수집 완료 시마다 원자적(Atomic) 임시 파일 쓰기로 즉시 저장하며, 재실행 시 이미 저장된 `id`는 자동으로 건너뜁니다.
