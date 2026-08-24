# AI 주식 자동매매 시스템 — 미국 + 한국 동시 운영

ML 예측 · 기술적 분석 · 뉴스 감성 분석 · LLM 최종 검토를 결합한 자동매매 시스템입니다.
한국투자증권(KIS) API로 **미국 주식과 한국 주식을 하나의 서버에서 병행 매매**합니다.

두 시장은 장 운영 시간과 데이터 소스가 달라 **독립된 트랙**으로 돌아가고,
시장과 무관한 로직(점수 산출, Slack 전송, KIS 토큰 관리)만 공유합니다.

| | 🇺🇸 미국 트랙 | 🇰🇷 한국 트랙 |
|---|---|---|
| 대상 | 나스닥 25종목 + ETF 2 | **KOSPI 시총 상위 100종목** |
| 시세·주문 | KIS 해외주식 API | KIS 국내주식 API |
| 거시지표 | FRED | **한국은행 ECOS** + FRED 일부 |
| 뉴스 감성 | AlphaVantage (점수 제공) | **NAVER API Hub** (기사만 → Claude가 채점) |
| 공포지수 | VIX | **코스피 20일 실현변동성** |
| 매매 시각 | 매수 NY 10:30 ET / 매도 1분 주기 | 매수 09:15 KST / 매도 1분 주기 |
| 파이프라인 | KST 21:00 일괄 (분석+매수) | **16:30 분석 → 다음날 09:15 집행** |
| 활성 스위치 | 항상 | `KR_ENABLED=true` |

---

## 목차

1. [동작 흐름](#동작-흐름)
2. [프로젝트 구조](#프로젝트-구조)
3. [설치](#설치)
4. [미국 트랙 설정](#미국-트랙-설정)
5. [한국 트랙 설정](#한국-트랙-설정)
6. [서버 실행과 확인](#서버-실행과-확인)
7. [API 사용법](#api-사용법)
8. [운영 가이드](#운영-가이드)
9. [외부 API 키 발급](#외부-api-키-발급)
10. [문제 해결](#문제-해결)

---

## 동작 흐름

두 트랙 모두 같은 6단계를 거치지만, 한국은 **장 마감 후 분석 시점에 주문을 낼 수 없어**
분석과 집행이 분리돼 있습니다.

```
[공통 6단계]
1. 데이터 수집   지수·환율·거시지표·종목 종가  →  Supabase
2. ML 예측      Kaggle GPU 커널 (Transformer)  →  상승률·정확도
3. 기술적 분석   SMA / RSI / MACD / ADX / ATR / 거래량
4. 뉴스 감성     기사 수집  →  감성 점수 (-1 ~ +1)
5. 종합 점수     cross-sectional z-score 가중합  →  후보 선정
6. LLM 검토      Claude 거부권 (BUY → HOLD 만 가능, 종목 추가 불가)
```

**🇺🇸 미국** — KST 21:00에 1~6단계를 한 번에 돌리고 바로 매수 주문을 냅니다.
그 시각이 NY 장 시작 전이라 주문이 개장 때 체결됩니다.

**🇰🇷 한국** — 15:30에 장이 닫히므로 2단계로 나눕니다.

```
평일 09:00 ─────────────── 15:20 ── 15:30    16:30 ────────── (다음 개장일) 09:15
   │                          │       │         │                       │
   │  ◀── 매도 감시 1분 주기 ──▶│    장마감    Phase A                Phase B
   │                          │              (1~6단계 분석)          (매수 집행)
   └ Phase B 매수 주문                        → kr_buy_queue 저장    → 현재가 재조회
                                              → Slack 보고            → 수량 재계산
                                                                      → 지정가 주문
```

매도는 두 트랙 모두 **1분마다** 감시하며 ATR 기반 익절/손절 + 기술적 매도 신호로 판단합니다.

---

## 프로젝트 구조

```
stock-ai-agent/
├── app/
│   ├── main.py                      # FastAPI 진입점 (두 트랙 스케줄러 기동)
│   ├── core/config.py               # 환경변수 (US + KR 통합)
│   ├── db/supabase.py               # Supabase 클라이언트
│   ├── api/
│   │   ├── api.py                   # 라우터 통합
│   │   └── routes/
│   │       ├── balance.py           # 🇺🇸 잔고/주문
│   │       ├── economic.py          # 🇺🇸 경제 데이터
│   │       ├── stock_recommendations.py
│   │       ├── stocks.py
│   │       ├── volume.py
│   │       ├── llm_review.py
│   │       ├── pipeline.py          # 🇺🇸 통합 파이프라인
│   │       └── kr.py                # 🇰🇷 국내 트랙 전체 (21개 엔드포인트)
│   ├── services/                    # 🇺🇸 미국 트랙 + 공유 모듈
│   │   ├── scoring_service.py           # ★ 공유: z-score 채점, 사전필터
│   │   ├── position_sizing.py           # ★ 공유: 확신도 가중 포지션 사이징
│   │   ├── notification_service.py      # ★ 공유: Slack 전송(_send)
│   │   ├── balance_service.py           # ★ 공유: KIS 토큰 + 해외주식 주문
│   │   ├── ml_trigger_service.py        # ★ 공유: Kaggle 커널 트리거
│   │   ├── stock_recommendation_service.py
│   │   ├── economic_service.py
│   │   ├── llm_review_service.py
│   │   ├── volume_service.py
│   │   ├── snapshot_service.py
│   │   └── auth_service.py
│   ├── services/kr/                 # 🇰🇷 한국 트랙
│   │   ├── universe.py                  # KOSPI 100 종목 마스터
│   │   ├── kis_domestic_service.py      # KIS 국내 API + 호가단위 + 휴장일
│   │   ├── naver_service.py             # NAVER API Hub (뉴스/블로그/데이터랩)
│   │   ├── kr_sentiment_service.py      # 기사 → Claude 배치 채점
│   │   ├── kr_market_data_service.py    # Yahoo + ECOS 수집
│   │   ├── kr_scoring.py                # z-score (+수급 팩터)
│   │   ├── kr_recommendation_service.py # 기술지표 / 매수·매도 후보
│   │   ├── kr_llm_review_service.py     # LLM 검토 (한국 시장 프롬프트)
│   │   ├── kr_notification_service.py   # Slack (원화)
│   │   └── kr_override_service.py       # 변동성 게이트 수동 해제
│   └── utils/
│       ├── scheduler.py             # 🇺🇸 스케줄러
│       └── kr_scheduler.py          # 🇰🇷 스케줄러 (전용 인스턴스)
├── kaggle_notebook/predict.py       # 🇺🇸 ML 커널
├── kaggle_notebook_kr/predict_kr.py # 🇰🇷 ML 커널
├── stock.py                         # 🇺🇸 FRED + Yahoo 수집
├── sql/                             # 🇺🇸 테이블 DDL
├── sql/kr/setup_kr.sql              # 🇰🇷 전체 스키마 (단일 파일, 멱등)
├── db_backup/                       # 🇺🇸 스키마 + 초기 데이터 CSV
├── documents/                       # 설계 문서
└── run.py                           # 서버 실행
```

---

## 설치

### 1. Python 3.12 권장

```bash
python --version   # Python 3.12.x
```

Windows는 설치 시 **"Add Python to PATH"**를 반드시 체크하세요.

### 2. 가상환경 + 패키지

```bash
# Mac / Linux
python3 -m venv .venv && source .venv/bin/activate

# Windows
python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
```

> **TensorFlow** — `requirements.txt`에서 주석 처리돼 있습니다. ML 학습은 Kaggle GPU에서
> 돌기 때문에 로컬에는 필요 없습니다. 로컬에서 직접 학습하려면 환경에 맞게 설치하세요.
> Mac(Apple Silicon) `pip install tensorflow-macos tensorflow-metal` / 그 외 `pip install tensorflow`

### 3. Supabase 프로젝트

1. https://supabase.com 에서 프로젝트 생성
2. **Project Settings → API**에서 URL과 key 복사

> **권장: `service_role` 키 사용**
> `.env`에 `SUPABASE_SERVICE_ROLE_KEY`를 넣으면 RLS를 켜둔 채로 서버만 우회합니다.
> 없으면 anon 키로 동작하는데, 이때는 각 테이블의 RLS를 꺼야 쓰기가 됩니다.

---

## 미국 트랙 설정

### 1. 테이블 생성

Supabase **SQL Editor**에 `db_backup/schema/create_all_tables.sql` 실행

### 2. 초기 데이터 임포트

**Table Editor → Insert → Import data from CSV**로 `db_backup/data/`의 파일을 올립니다.

| 파일 | 필수 | 설명 |
|---|---|---|
| `economic_and_stock_data.csv` | ✅ | 경제지표 + 주가 히스토리 (없으면 구동 불가) |
| `stock_analysis_results.csv` | ✅ | ML 예측 결과 |
| `predicted_stocks.csv` | | ML 예측 히스토리 |

나머지 테이블은 시스템이 자동으로 채웁니다.

### 3. `.env` 설정

```env
# ── 한국투자증권 (필수) ──
KIS_USE_MOCK=true                  # true: 모의투자 / false: 실전투자
KIS_MOCK_APPKEY=발급받은_앱키
KIS_MOCK_APPSECRET=발급받은_앱시크릿
KIS_MOCK_CANO=모의투자_계좌번호
KIS_ACNT_PRDT_CD=01

# ── Supabase (필수) ──
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJhbGci...
SUPABASE_SERVICE_ROLE_KEY=          # 권장 (RLS 우회)

# ── 미국 트랙 ──
ALPHA_VANTAGE_API_KEY=              # 뉴스 감성
ALPHA_VANTAGE_API_KEY_EARNINGS=     # 실적 캘린더 (한도 분리용)
FINNHUB_API_KEY=                    # 실적일 보강
ANTHROPIC_API_KEY=sk-ant-...        # LLM 검토
USE_SCORING_V2=true                 # z-score 채점 사용

# ── Kaggle (ML 예측) ──
KAGGLE_USERNAME=
KAGGLE_API_TOKEN=
KAGGLE_KERNEL_SLUG=stock-prediction

# ── Slack (선택) ──
SLACK_WEBHOOK_URL=
```

> FRED API 키는 `stock.py` 상단 `api_key` 변수에 직접 넣습니다.

---

## 한국 트랙 설정

### 1. 스키마 설치

Supabase **SQL Editor**에 **`sql/kr/setup_kr.sql`** 전체를 붙여넣고 Run.

이 파일 하나면 충분합니다 — 테이블 9개 생성 + 컬럼 마이그레이션 + RLS/권한이 모두 들어
있고, **멱등(idempotent)이라 몇 번을 다시 돌려도 안전**합니다. 나중에 유니버스를 늘렸을
때도 이 파일만 재실행하면 컬럼이 자동으로 추가됩니다.

| 테이블 | 역할 |
|---|---|
| `kr_economic_and_stock_data` | 날짜별 지수·환율·거시 + 변동성 + 100종목 종가 (ML 입력) |
| `kr_stock_recommendations` | 기술적 지표 스냅샷 |
| `kr_stock_analysis_results` | ML 예측 결과 |
| `kr_ticker_sentiment_analysis` | 뉴스 감성 점수 |
| `kr_news_articles` | 감성 판단 근거 기사 (사후 추적) |
| `kr_buy_queue` | 다음 개장일 매수 예약 |
| `kr_trade_records` | 매매 기록 + ATR 익절/손절 (원화) |
| `kr_llm_decision_logs` | LLM 판단 로그 |
| `kr_fear_gate_overrides` | 변동성 게이트 수동 해제 이력 |

### 2. `.env` 추가 설정

```env
# ── 한국 트랙 ──
KR_ENABLED=true
KR_DRY_RUN=true                     # true면 주문 API 호출 없이 로그만 (검증용)

KR_HISTORY_YEARS=5                  # 백필 기간 (년)
KR_SLOT_RATIO=0.10                  # 종목당 기준 투자 비중 (총자산 대비)
KR_MAX_POSITIONS=8                  # 동시 보유 최대 종목 수

# 확신도 가중 배분 — 점수 높은 종목에 더 많이
KR_SLOT_TILT=0.5                    # 0=균등, 0.5=1위 1.5배·꼴찌 0.5배, 1.0=최대
KR_SLOT_METHOD=rank                 # rank(총액 고정, 권장) / score(점수 차 반영)
KR_MIN_SLOT_RATIO=0.05              # 종목별 하한
KR_MAX_SLOT_RATIO=0.20              # 종목별 상한
KR_MAX_TOTAL_EXPOSURE=0.80          # 총 투입 비율 상한
KR_MIN_ML_ACCURACY=80               # ML 신뢰도 하한 (%)
KR_MIN_RISE_PROBABILITY=2           # 예측 상승률 하한 (%)

KR_ANALYSIS_TIME=16:30              # 분석 파이프라인 (KST)
KR_EXECUTION_TIME=09:15             # 매수 집행 (KST)

KR_FEAR_SELL_LOSS_PCT=3.0           # 공포장 강제청산 발동 최소 손실률 (%)

# NAVER API Hub — 뉴스 감성 (필수)
NAVER_API_KEY_ID=
NAVER_API_KEY=
NAVER_API_HUB_BASE=https://naverapihub.apigw.ntruss.com
NAVER_DATALAB_BASE=https://naveropenapi.apigw.ntruss.com

KR_SENTIMENT_MODEL=claude-opus-5    # 기사 채점 모델
KR_SENTIMENT_LOOKBACK_DAYS=3

ECOS_API_KEY=                       # 한국은행 거시지표 (권장)
KRX_AUTH_KEY=                       # KRX Open API (선택)
DART_API_KEY=                       # 공시/재무제표 (선택)

KAGGLE_KERNEL_SLUG_KR=stock-prediction-kr
KAGGLE_NOTEBOOK_DIR_KR=kaggle_notebook_kr
```

> **NAVER API Hub 주의** — 기존 `openapi.naver.com` 검색 API는 API Hub로 **이관**됐습니다.
> 호스트와 인증 헤더가 모두 바뀌어 도메인만 갈아끼우면 동작하지 않습니다.
> **API는 Application마다 하나씩 활성화**해야 하며(뉴스만 켜고 블로그를 안 켜면 블로그는 401),
> 데이터랩(검색어 트렌드)은 아예 다른 상품이라 별도 신청이 필요합니다.
> 뉴스만 있어도 파이프라인은 완전히 동작합니다.

### 3. 히스토리 백필 (약 2분)

```bash
python -c "
import logging; logging.basicConfig(level=logging.INFO)
from app.services.kr import kr_market_data_service as md
print(md.collect_market_data(force_full=True))"
```

또는 서버 실행 후 `POST /kr/market-data/collect?full=true`

### 4. Kaggle KR 커널 생성

`kaggle_notebook_kr/kernel-metadata.json`의 `id`를 본인 계정으로 맞춘 뒤 (push 직전 자동 교정도 됩니다):

```bash
python -c "
import logging; logging.basicConfig(level=logging.INFO)
from app.services import ml_trigger_service as mt
print(mt.trigger_and_wait(kernel_slug='stock-prediction-kr',
    notebook_dir='kaggle_notebook_kr', script_name='predict_kr.py'))"
```

첫 실행 시 커널이 자동 생성됩니다. GPU로 약 3분 걸립니다.

---

## 서버 실행과 확인

```bash
python run.py
```

정상 기동 로그:

```
서비스 시작 시 경제 데이터 수집을 즉시 실행합니다...
초기 경제 데이터 수집이 완료되었습니다.
주식 자동매매 스케줄러가 시작되었습니다. 뉴욕 시간 10:30 ET에 매수 작업이 실행됩니다.
매도 스케줄러가 시작되었습니다. 1분마다 매도 대상을 확인합니다.
일일 파이프라인 스케줄러 시작 (매일 KST 21:00)
국내주식(KOSPI 100) 스케줄러를 시작합니다...
국내 스케줄러 시작 [드라이런] — 분석 매일 16:30 KST, 매수 집행 매일 09:15 KST, 매도 감시 1분 주기
INFO:     Uvicorn running on http://0.0.0.0:8000
```

- **API 문서** — http://localhost:8000/docs
- **한국 트랙 상태** — http://localhost:8000/kr/status

`/kr/status`의 `scheduler.thread_alive`와 각 job의 `last_run`/`next_run`으로 스케줄러가
실제로 살아 있는지 확인할 수 있습니다. 휴장일에는 매도 감시가 로그를 남기지 않고 조용히
스킵하므로, 로그 대신 이 값을 보세요.

---

## API 사용법

### 🇺🇸 미국 트랙

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/pipeline/run-full-daily` | 4단계 일일 파이프라인 (경제데이터 → ML → 기술·감성 → LLM·매수) |
| POST | `/pipeline/run-buy-pipeline` | 기술지표 + 감성 + LLM 검토만 실행 |
| POST | `/pipeline/kaggle/trigger-ml` | Kaggle ML 노트북 트리거 (최대 15분 대기) |
| GET | `/pipeline/kaggle/auth-check` · `/status` | Kaggle 인증·실행 상태 |
| POST | `/pipeline/earnings/fetch` | 실적 캘린더 수집 |
| GET | `/stocks/recommendations/recommended-stocks/with-technical-and-sentiment` | 매수 후보 (ML+기술+감성 통합) |
| GET | `/stocks/recommendations/sell-candidates` | 매도 후보 |
| POST | `/stocks/recommendations/purchase/trigger` · `/sell/trigger` | 매수·매도 즉시 실행 |
| GET | `/stocks/recommendations/scheduler/status` | 스케줄러 상태 |
| GET | `/balance/overseas` | 해외 잔고 |
| POST | `/economic/update` | 경제 데이터 수집 |

### 🇰🇷 한국 트랙

**상태 / 유니버스**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/kr/status` | 스케줄러·설정·연동·장 상태 |
| GET | `/kr/universe` | KOSPI 100 종목 목록 |
| POST | `/kr/scheduler/start` · `/stop` | 스케줄러 제어 |

**데이터 / 분석**

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/kr/market-data/collect?full=true` | 시장 데이터 수집 |
| GET | `/kr/market-data/context` | 코스피·변동성·환율·VIX |
| GET | `/kr/economic/ecos-check` | ECOS 통계코드 유효성 진단 |
| POST | `/kr/technical/generate` | 기술적 지표 생성 (~3.5분) |
| POST | `/kr/sentiment/collect` | 뉴스 감성 수집 (~3분) |
| GET | `/kr/sentiment` | 감성 점수 조회 |
| GET | `/kr/news/{code}` | 종목 기사 원문 (근거 확인) |
| GET | `/kr/candidates/buy` · `/sell` | 매수·매도 후보 |

**실행**

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/kr/pipeline/analysis` | 분석 파이프라인 (1~6단계) |
| POST | `/kr/pipeline/execute-buy?force=true` | 매수 큐 집행 |
| POST | `/kr/pipeline/execute-sell` | 매도 판단 |

**변동성 게이트**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/kr/fear-gate` | 현재 변동성·임계값·차단 여부 |
| POST | `/kr/fear-gate/override` | 수동 해제 (운영자 개입) |
| POST | `/kr/fear-gate/revoke` | 즉시 복구 |

**계좌**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/kr/balance` | 국내 잔고 + 계좌 요약 |
| GET | `/kr/price/{code}` | 종목 현재가 |

---

## 운영 가이드

### 점수 체계

두 트랙 모두 후보군 내 **cross-sectional z-score**를 가중합해 채점합니다.
절대값이 아니라 상대 순위를 쓰므로, ML 예측이 전반적으로 낙관적이어도 자동으로 중화됩니다.

| 팩터 | 🇺🇸 미국 | 🇰🇷 한국 |
|---|---|---|
| 기술 모멘텀 (MACD·SMA·RSI) | 0.30 | 0.27 |
| ML 예측 상승률 | 0.20 | 0.18 |
| **수급 (외국인+기관 순매수)** | — | **0.15** ← 국내 전용 |
| 거래량 비율 | 0.20 | 0.15 |
| ADX 추세 강도 | 0.20 | 0.15 |
| 뉴스 감성 | 0.10 | 0.10 |

**사전 필터(공유)** — RSI > 80 하드블록 + 기술 신호 2개 이상.

### 포지션 사이징 — 확신도 가중 배분 (한국)

통과 종목에 **균등 배분하지 않고 종합점수가 높은 종목에 더 많이** 넣습니다.
`app/services/position_sizing.py` (시장 무관 공용 모듈)

```
mult = 1 + tilt × (2×rel − 1)      slot = KR_SLOT_RATIO × mult
```

`rel`(후보군 내 상대 강도 0~1)을 구하는 방식이 둘입니다.

| `KR_SLOT_METHOD` | 방식 | 특징 |
|---|---|---|
| **`rank`** (기본) | 점수 **순위**를 등간격 배치 | 총 투입액이 `종목수 × base`로 **고정**, 이상치에 강함 |
| `score` | 점수 **값**을 min-max 정규화 | 점수 차 크기를 반영하나, 이상치 하나가 나머지를 바닥으로 누름 |

`score` 방식의 위험은 실측으로 확인했습니다 — 점수가 `[1.301, 0.552, …, 0.464]`처럼
1위만 튀는 날 `tilt=0.5`를 주면 나머지 6종목이 전부 하한에 붙어 **총 투입이 70%→48.8%로
급감**합니다. "얼마나 살지"는 임계값이 정할 몫인데 사이징이 이를 흔드는 셈이라 기본값을
`rank`로 뒀습니다.

**tilt별 배분** (7종목, base 10%)

| `KR_SLOT_TILT` | 1위 → 7위 | 총 투입 |
|---|---|---|
| 0.0 | 10.0 · 10.0 · 10.0 · 10.0 · 10.0 · 10.0 · 10.0 | 70% (기존 균등) |
| 0.3 | 13.0 · 12.0 · 11.0 · 10.0 · 9.0 · 8.0 · 7.0 | 70% |
| **0.5** (기본) | **15.0 · 13.3 · 11.7 · 10.0 · 8.3 · 6.7 · 5.0** | **70%** |
| 1.0 | 20.0 · 16.7 · 13.3 · 10.0 · 6.7 · 5.0 · 5.0 | 76.7% |

`KR_SLOT_TILT=0`이면 기존 균등 배분과 **완전히 동일**하므로 언제든 되돌릴 수 있습니다.

안전장치 — `KR_MIN_SLOT_RATIO`(5%) / `KR_MAX_SLOT_RATIO`(20%)로 개별 종목을 클램프하고,
`KR_MAX_TOTAL_EXPOSURE`(80%)로 총 투입을 제한합니다. 총 노출 상한이 개별 하한보다 우선합니다.

### 변동성 게이트 (한국)

한국에는 VIX에 대응하는 무료 실시간 지수가 없어 **코스피 20일 실현변동성(연율 %)**을 씁니다.
임계값은 2006~2026 실측 분포로 잡았습니다 (중위 15.0% / 90%ile 29.7% / 95%ile 41.7%).

| 변동성 | 백분위 | 임계값 |
|---|---|---|
| < 20% | ~하위 74% | 0.40 |
| 20~25% | 74~82% | 0.45 |
| 25~30% | 82~90% | 0.55 |
| 30~40% | 90~95% | 0.70 |
| 40~55% | 95~98% | 0.90 |
| 55~90% | 98~99.9% | 1.10 |
| **> 90%** | 상위 0.1% | **매수 전면 중단** |

폭락장에서 운영자 판단으로 매수해야 할 때는 **한시적 오버라이드**를 씁니다.

```bash
curl -X POST "http://localhost:8000/kr/fear-gate/override?confirm=OVERRIDE&reason=사유입력&minutes=120"
```

안전장치가 겹쳐 있습니다 — `confirm=OVERRIDE` 정확 일치, 사유 5자 이상 필수,
**기본 120분 후 자동 복구**(최대 24시간), 발급/해제 시 Slack 알림, 감사 테이블 기록,
조회 실패 시 게이트 정상 작동 쪽으로 판단(fail-safe).

기본은 **하드블록만 해제**하고 변동성 기반 선별도는 유지합니다.
`relax_threshold=true`를 주면 임계값까지 평온장 기준으로 낮추는데, 이건 파이프라인 관통
확인용이며 실매매에는 권장하지 않습니다.

### 매도 조건

1. **ATR 익절/손절** — 매수가 ± ATR×(2.5 / 1.5). 기록이 없으면 고정비율 폴백
2. **기술적 매도 신호** — 데드크로스 / RSI>70 / MACD 매도 / 패닉셀 / **외국인·기관 순매도**(국내)
   - ADX > 25면 필요 신호 수 1개 차감
   - 감성 < −0.15이면 2개, 아니면 3개
3. **공포장** — 변동성 > 40% + 신호 1개, > 30% + 신호 2개

### 안전 장치

| 장치 | 동작 |
|---|---|
| `KR_DRY_RUN=true` | 주문 API 호출 없이 로그만 — 로직 검증용 |
| `KIS_USE_MOCK=true` | 모의투자 계좌 |
| ATR 없으면 매수 스킵 | 자동 익절/손절선 없이는 진입하지 않음 |
| LLM Fail-Close | LLM 호출 전체 실패 시 **매수 차단** + Slack 알림 |
| 호가단위 정규화 | 지정가를 KRX 호가단위에 맞춤 (안 맞으면 주문 거부됨) |
| 큐 2일 만료 | 연휴로 밀린 낡은 판단으로 매수하지 않음 |
| 주문 정합성 확인 | 1분마다 KIS 원장과 대조 (체결 확인 / 미체결 정리 / 고아 레코드 복구) |

### 실전 전환 순서

```
1) KR_DRY_RUN=true  + KIS_USE_MOCK=true   ← 로직 검증 (며칠 관찰)
2) KR_DRY_RUN=false + KIS_USE_MOCK=true   ← 모의투자 실주문
3) KR_DRY_RUN=false + KIS_USE_MOCK=false  ← 실전 (충분히 검증한 뒤에만)
```

### 유니버스 교체

시총 순위는 분기 단위로 바뀝니다. 종목을 바꾸면 **세 곳을 함께** 고쳐야 합니다.

1. `app/services/kr/universe.py`의 `UNIVERSE`
2. `sql/kr/setup_kr.sql` 재실행 (컬럼 자동 추가)
3. `kaggle_notebook_kr/predict_kr.py`의 `TARGET_COLUMNS`

상장이 늦어 학습 구간(`MIN_TRAIN_ROWS`)을 확보하지 못하는 종목은 ML이 **자동 제외**하며,
예측이 없는 종목은 매수 후보에서도 빠집니다.

---

## 외부 API 키 발급

| API | 용도 | 링크 | 비용 |
|---|---|---|---|
| 한국투자증권 | 🇺🇸🇰🇷 매매 주문 | https://apiportal.koreainvestment.com | 무료 |
| Supabase | 데이터베이스 | https://supabase.com | 무료 플랜 |
| Anthropic Claude | LLM 검토 + 감성 채점 | https://console.anthropic.com | 유료 |
| Kaggle | ML 학습 (GPU) | https://kaggle.com/settings | 무료 |
| **NAVER API Hub** | 🇰🇷 뉴스 감성 | https://www.ncloud.com/product/applicationService/naverApiHub | 무료 (일 25,000) |
| **한국은행 ECOS** | 🇰🇷 거시지표 | https://ecos.bok.or.kr/api | 무료 |
| KRX Open API | 🇰🇷 시장 시세 (선택) | https://openapi.krx.co.kr | 무료 |
| OpenDART | 🇰🇷 공시 (선택) | https://opendart.fss.or.kr | 무료 |
| FRED | 🇺🇸 거시지표 | https://fred.stlouisfed.org/docs/api/api_key.html | 무료 |
| AlphaVantage | 🇺🇸 뉴스 감성 | https://www.alphavantage.co/support/#api-key | 무료 (일 25건) |
| Finnhub | 🇺🇸 실적 캘린더 | https://finnhub.io | 무료 |
| Slack Webhook | 알림 | https://api.slack.com/messaging/webhooks | 무료 |

### KIS 모의투자 설정

1. https://apiportal.koreainvestment.com 회원가입
2. **모의투자** 앱키 발급 (앱키 + 앱시크릿)
3. 모의투자 계좌 개설 → 계좌번호 발급
4. `.env`의 `KIS_MOCK_*`에 입력

> 국내주식과 해외주식은 **같은 앱키**로 둘 다 호출됩니다. TR_ID만 다릅니다.

---

## 문제 해결

### `42501 new row violates row-level security policy`

RLS가 켜져 있는데 anon 키로 쓰려는 경우입니다. 둘 중 하나로 해결합니다.

- **권장** — `.env`에 `SUPABASE_SERVICE_ROLE_KEY` 추가 (RLS 유지, 서버만 우회)
- 또는 `sql/kr/setup_kr.sql` 재실행 (RLS 해제 구문 포함)

### `EGW02006 모의투자 TR 이 아닙니다`

실전 전용 TR을 모의투자 계좌로 호출한 경우입니다. 국내 트랙에서
`chk-holiday`(휴장일)·`inquire-index-price`(업종지수)·`financial-ratio`(재무비율)가 여기
해당하며, 이들 없이도 매매 로직은 완결되도록 설계돼 있습니다.

### `EGW00201 초당 거래건수를 초과하였습니다`

KIS 호출 제한(모의 2건/초)입니다. 자동 재시도로 복구되므로 로그에 보이더라도 정상입니다.

### 매수 후보가 계속 0건

순서대로 확인하세요.

1. `GET /kr/fear-gate` — 변동성이 90%를 넘어 차단 중인지
2. ML 예측 존재 여부 — `kr_stock_analysis_results`가 비어 있으면 Kaggle 커널 실행
3. `KR_MIN_ML_ACCURACY`(기본 80) 필터에 다 걸리는지 — 로그의 `ML 필터` 줄 확인
4. 이미 보유/주문 중인 종목은 후보에서 제외됩니다

### 네이버 API 401

- `"이 Application에서 활성화되어 있지 않습니다"` → API Hub 콘솔에서 해당 API를
  Application에 추가 (뉴스·블로그는 각각 따로 켜야 합니다)
- `"A subscription to..."` → 데이터랩은 별도 상품이라 신청이 필요합니다

### ECOS 지표가 비어 있음

한국은행이 통계코드를 개편하면 특정 지표만 조용히 빕니다.
`GET /kr/economic/ecos-check`로 진단하고, 실패한 코드를
`app/services/kr/kr_market_data_service.py`의 `ECOS_SERIES`에서 고치세요.
실패해도 파이프라인은 계속 진행됩니다.

---

## 설계 문서

| 문서 | 내용 |
|---|---|
| `documents/00_시스템_총정리.md` | 전체 개요 |
| `documents/03_지표_및_종합점수_계산.md` | 점수 산출 |
| `documents/06_LLM_검토_로직_상세.md` | LLM 거부권 |
| `documents/09_Slack_연동.md` | 알림 |
| `documents/10_멀티팩터_변별력_개선_기획.md` | z-score v2 설계 |
| **`documents/20_국내주식_KOSPI30_설계.md`** | **한국 트랙 전체 설계** |

---

## 주의사항

- 이 시스템은 **교육 목적**으로 제작되었습니다
- 실전 투자에서 발생하는 손실에 대해 책임지지 않습니다
- 반드시 **드라이런 → 모의투자 → 실전** 순서로 검증하세요
- API 키와 시크릿은 절대 외부에 공유하지 마세요 (`.env`는 `.gitignore`에 포함)
- `SUPABASE_SERVICE_ROLE_KEY`는 RLS를 우회하므로 **서버 백엔드 전용**입니다
