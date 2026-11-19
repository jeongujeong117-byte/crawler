# 네이버 증권 종목토론방 크롤러 (`naver-stock-crawler`)

`크롤링.md` 명세서를 바탕으로 네이버 증권 종목토론방(`https://m.stock.naver.com/domestic/stock/{stock_code}/discussion`)의 **원글(`post`)**, **댓글(`comment`)**, **대댓글(`reply`)**을 평면형(Flat) JSON 배열로 수집하는 Python 크롤러입니다.

---

## 📁 디렉토리 구조 (기능별 분리)

```text
naver-stock-crawler/
├── config/                     # [설정] 크롤러 동작, 딜레이, 브라우저, 저장 경로 설정
│   └── settings.yaml
├── src/                        # [프로그램 코어 모듈]
│   ├── __init__.py
│   ├── config_loader.py        # YAML 설정 로더 및 기본값 병합
│   ├── models.py               # 평면형(Flat) JSON 데이터 스키마 (DiscussionItem)
│   ├── crawler.py              # Playwright 메인 크롤링 엔진 (클린봇 해제, 댓글 확장 등)
│   ├── network_interceptor.py  # 백그라운드 JSON 응답(page.on("response")) 하이브리드 가로채기
│   ├── human_actor.py          # 인간 행동 모사 (다단계 마우스 커서 이동, 랜덤 오프셋 클릭, 휠 스크롤)
│   ├── storage.py              # 게시글 단위 즉시 중간 저장(Checkpointing) 및 중복 ID 건너뛰기
│   └── utils.py                # 상대시간('N분 전', '어제') -> 절대시간 보정, 로깅 유틸리티
├── output/                     # [결과 데이터] 수집된 JSON 파일이 저장되는 폴더
│   └── output_{종목명}_{종목코드}.json
├── logs/                       # [실행 로그] 일자별 크롤링 진행 상태 로그 폴더
├── main.py                     # [실행 진입점] CLI 인자(argparse) 및 콘솔 대화형(input) 지원
├── requirements.txt            # 필수 Python 패키지 목록
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
인자 없이 실행하면 종목코드, 수집 범위(`--max-posts` 또는 `--since-date`), 파일 경로를 순서대로 입력할 수 있습니다.
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
  python main.py --code 005930 --since-date 2026-11-18
  ```
* **저장 파일 경로 직접 지정 (`--output`)**
  ```bash
  python main.py --code 005930 --max-posts 10 --output output/output_삼성전자_005930.json
  ```

---

## ✨ 주요 구현 특징 (`크롤링.md` 명세 완벽 반영)

1. **시각적 모니터링 (`headless=False`) & 하이브리드 수집**
   * 브라우저 창을 항상 화면에 띄워(`headless=False`) 크롤링 과정을 실시간으로 확인할 수 있습니다.
   * DOM 파싱과 함께 `page.on("response")`로 백그라운드 JSON/CBOX 응답을 실시간 캡처(`src/network_interceptor.py`)하여 누락 없는 데이터를 보장합니다.
2. **클린봇(Cleanbot) 자동 OFF**
   * 종목토론방 진입 직후 클린봇 토글/모달을 감지하여 **OFF(해제)** 상태로 전환합니다.
3. **인간 행동과 유사한 인터랙션 (`src/human_actor.py`)**
   * **마우스 이동:** 베지어 곡선 및 다단계(`steps`) 분할 이동 + 요소 영역 내 랜덤 오프셋 클릭
   * **자연스러운 타이핑:** 글자당 `50~150ms` 랜덤 딜레이
   * **스크롤 및 읽기 대기:** `page.mouse.wheel` 다단계 스크롤 및 동작 간 `1.0~2.8초` 가변 대기
   * **접힌 댓글/대댓글 확장:** `'댓글 더보기'`, `'답글 N개 보기'` 버튼을 자동 감지해 클릭 확장 후 수집
4. **평면형(Flat) JSON & 즉시 중간 저장(Checkpointing)**
   * 원글(`post`), 댓글(`comment`), 대댓글(`reply`)을 중첩 없이 동일한 스키마의 1차원 배열로 저장합니다.
   * 게시글 1개 세트 수집이 끝날 때마다 원자적(Atomic) 파일 쓰기로 즉시 저장하며, 재실행 시 이미 저장된 `id`는 자동으로 건너뜁니다.
