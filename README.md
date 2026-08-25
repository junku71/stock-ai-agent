# 국내주식 AI 자동매매 시스템 — KOSPI 100

ML 예측 · 기술적 분석 · 뉴스 감성 분석 · 수급 · LLM 최종 검토를 결합한 자동매매 시스템입니다.
한국투자증권(KIS) API로 **KOSPI 시가총액 상위 100개 보통주**를 매매합니다.

| 항목 | 내용 |
|---|---|
| 분석 후보군 | KOSPI 시총 상위 200종목 (보통주, 매번 동적 산출) |
| ML 학습 대상 | KOSPI 100종목 (고정 — 학습 컬럼과 1:1로 묶임) |
| 시세·주문 | KIS 국내주식 API |
| 거시지표 | 한국은행 ECOS + Yahoo Finance (글로벌 지표) |
| 뉴스 감성 | NAVER API Hub (기사 텍스트) → Claude가 채점 |
| 공포지수 | 코스피 20일 실현변동성 (연율 %) |
| ML 예측 | Kaggle GPU 커널 (Transformer) |
| 파이프라인 | 16:30 분석 → 다음 개장일 09:15 매수 집행 |
| 매도 감시 | 09:00~15:20 1분 주기 |

> **과거 이력** — 이 저장소는 원래 미국 주식 트랙과 병행 운영됐습니다. 미국 트랙은 전부
> 삭제됐고 관련 문서만 `documents/_archive_us/` 에 보존돼 있습니다. 거기 나오는 파일 경로·
> 테이블·라우트는 **현재 코드에 존재하지 않으니** 참고 자료로만 보세요.

---

## 목차

1. [동작 흐름](#동작-흐름)
2. [프로젝트 구조](#프로젝트-구조)
3. [설치](#설치)
4. [설정](#설정)
5. [서버 실행과 확인](#서버-실행과-확인)
6. [API 사용법](#api-사용법)
7. [운영 가이드](#운영-가이드)
8. [외부 API 키 발급](#외부-api-키-발급)
9. [문제 해결](#문제-해결)

---

## 동작 흐름

분석은 6단계를 거칩니다. 한국 증시는 **15:30에 닫혀 장 마감 후 분석 시점에는 주문을 낼 수
없으므로**, 분석과 집행을 시간적으로 분리했습니다.

```
[신규 종목 추천 — 2단계 스크리닝]

후보군: KOSPI 시가총액 상위 200종목
  │
  ├ 1단계 ─ 기본적 분석  DART 재무제표 -> 하드게이트 -> Claude 판정 (PASS/FAIL)
  │           수익성(영업이익률·ROE·ROIC) / 성장성(PER) / 안정성(부채·유동비율) / FCF
  │         감성 분석    네이버 뉴스·블로그 -> Claude 채점 (-1~+1) + 섹터 업황
  │
  ├ 2단계 ─ 기술적 분석  매수 신호 2개 이상 필요
  │           골든크로스(20EMA>50EMA) / RSI 매수구간 상승 / MACD 상향돌파
  │           / ADX>=25 / 거래량 급증        (데드크로스면 즉시 탈락)
  │         수급        외국인·기관이 각각 3일 연속 순매수 (최근 5거래일 내)
  │         ML 예측     Kaggle Transformer 상승률이 **양수인 것 중** 상위 5개
  │                     (포트폴리오 규칙: 섹터당 2종목)
  │
  ├ 3단계 ─ LLM 판단   위 5개 중 **최대 3개** 매수 추천
  │                     + 보유 종목과의 **리밸런싱(교체) 제안**
  │                     제약: 집행 후 보유 8종목 이하, 섹터당 2종목
  │
  └ 사용자 승인  매수/매도를 각각 골라 승인 → 매수 큐 / 매도 판정으로 적재
```

**승인 전에는 어떤 주문도 나가지 않습니다.** 1~3단계는 분석만 합니다. 승인된 항목도
직접 주문하지 않고 기존 집행 경로에 넣습니다 — 그래야 현재가 재조회·호가단위 정규화·
수량 재계산·중복 방지·정합성 확인이 그대로 걸립니다.

| 집행 시점 | |
|---|---|
| 매수 | 다음 `KR_EXECUTION_TIME`(09:15) 또는 메뉴 13번 |
| 매도 | 다음 매도 감시 사이클(1분 주기) 또는 메뉴 14번 |

보유 종목수 상한이 둘입니다 — `KR_MAX_POSITIONS`(10)는 하드 상한이고,
`KR_REBALANCE_MAX_POSITIONS`(8)은 리밸런싱 제안이 지켜야 할 목표입니다. 여유를 남겨두면
다음 회차에 더 좋은 후보가 나와도 기존 종목을 억지로 팔지 않아도 됩니다.

LLM 응답은 코드가 **다시 한 번 검증**합니다(`_validate`) — 매수 상한 초과, 후보 목록에
없는 종목 창작, 미보유 종목 매도, 집행 후 보유수 초과, 섹터 한도 위반을 모두 잘라내고
잘라낸 이유를 화면에 띄웁니다.

섹터 분산은 **KRX 업종명**(KIS 현재가 응답의 `bstp_kor_isnm`) 기준입니다. 고정 유니버스의
세분류 라벨은 100종목분뿐이라, 그것만 쓰면 나머지 100종목이 '미분류' 한 덩어리가 돼서
은행과 화학이 같은 섹터로 세어집니다. KRX 업종명은 전 종목에 있어 그런 구멍이 없습니다.

비싼 단계를 뒤로 미룹니다 — 기술적 분석은 종목당 KIS 일봉 조회(0.6초), 수급은 한 번 더
호출하므로, 1단계에서 후보가 200 -> 수십 개로 줄어든 뒤에 돌립니다.

```
평일 09:00 --------------- 15:20 -- 15:30    16:30 ---------- (다음 개장일) 09:15
   |                          |       |         |                       |
   |  <-- 매도 감시 1분 주기 -->|    장마감    Phase A                Phase B
   |                          |              (1~6단계 분석)          (매수 집행)
   +- Phase B 매수 주문                       -> kr_buy_queue 저장    -> 현재가 재조회
                                              -> 리포트 PDF + Slack   -> 수량 재계산
                                                                      -> 지정가 주문
```

매도는 **1분마다** 감시하며 ATR 기반 익절/손절 + 기술적 매도 신호 + LLM 종합판단으로
결정합니다. **팔 때는 언제나 전량입니다** — 부분매도는 하지 않습니다.

---

## 프로젝트 구조

시장에 종속되지 않는 것만 `app/services/` 최상위에 두고, 국내 시장에 종속된 것은 전부
`app/services/kr/` 아래로 모았습니다.

```
stock-auto-tradingbot/
├── app/
│   ├── main.py                          # FastAPI 진입점 (스케줄러 기동/종료)
│   ├── core/config.py                   # 환경변수
│   ├── db/supabase.py                   # Supabase 클라이언트
│   ├── api/
│   │   ├── api.py                       # 라우터 통합
│   │   └── routes/
│   │       ├── kr.py                    # /kr/* (국내 트랙 전체)
│   │       └── buy_switch.py            # /buy-switch/* (신규 매수 on/off)
│   ├── services/                        # ── 시장 무관 공용 모듈 ──
│   │   ├── scoring_service.py           # 사전 필터 + cross-sectional z-score
│   │   ├── indicators.py                # SMA/EMA/RSI/MACD/ATR/ADX 수식
│   │   ├── position_sizing.py           # 확신도 가중 포지션 배분
│   │   ├── kis_auth_service.py          # KIS 토큰 발급/캐싱
│   │   ├── slack_service.py             # Slack Webhook 저수준 전송
│   │   ├── ml_trigger_service.py        # Kaggle 커널 트리거
│   │   └── buy_switch_service.py        # 신규 매수 원격 스위치
│   ├── services/kr/                     # ── 국내 전용 ──
│   │   ├── universe.py                  # KOSPI 100 종목 마스터
│   │   ├── kis_domestic_service.py      # KIS 국내 API + 호가단위 + 휴장일
│   │   ├── naver_service.py             # NAVER API Hub (뉴스/블로그/데이터랩)
│   │   ├── kr_sentiment_service.py      # 기사 -> Claude 배치 채점
│   │   ├── kr_market_data_service.py    # Yahoo + ECOS 수집
│   │   ├── kr_scoring.py                # z-score (+수급 팩터) + 변동성 임계값
│   │   ├── kr_recommendation_service.py # 기술지표 / 매수·매도 후보
│   │   ├── kr_llm_review_service.py     # LLM 매수 검토 (Fail-Close)
│   │   ├── kr_llm_sell_review_service.py# LLM 매도 검토 (Fail-Close = HOLD)
│   │   ├── kr_override_service.py       # 변동성 게이트 수동 해제
│   │   ├── kr_notification_service.py   # Slack 텍스트 알림 (원화)
│   │   ├── kr_report_service.py         # 분석 리포트 오케스트레이션
│   │   ├── kr_pdf_service.py            # 리포트 PDF 렌더러 (ReportLab)
│   │   └── slack_file_service.py        # Slack 파일 업로드 (Bot Token)
│   └── utils/kr_scheduler.py            # 2단계 파이프라인 + 매도 감시 + 정합성
├── kaggle_notebook_kr/predict_kr.py     # ML 커널 (Transformer)
├── sql/kr/setup_kr.sql                  # 전체 스키마 (단일 파일, 멱등)
├── sql/setup_market_switches.sql        # 매수 스위치 테이블
├── scripts/buy_switch.{sh,ps1}          # 매수 스위치 CLI 래퍼
├── documents/                           # 설계 문서 (_archive_us/ = 구 미국 트랙)
└── run.py                               # 서버 실행
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

> **TensorFlow는 필요 없습니다.** ML 학습·예측은 Kaggle GPU 커널에서만 돌기 때문에
> 서버에는 설치하지 않습니다. `requirements.txt`에는 서버 실행에 필요한 것만 있습니다.

### 3. Supabase 프로젝트

1. https://supabase.com 에서 프로젝트 생성
2. **Project Settings → API**에서 URL과 key 복사

> **권장: `service_role` 키 사용**
> `.env`에 `SUPABASE_SERVICE_ROLE_KEY`를 넣으면 RLS를 켜둔 채로 서버만 우회합니다.
> 없으면 anon 키로 동작하는데, 이때는 각 테이블의 RLS를 꺼야 쓰기가 됩니다.

---

## 설정

### 1. 스키마 설치

Supabase **SQL Editor**에 **`sql/kr/setup_kr.sql`** 전체를 붙여넣고 Run.
이어서 **`sql/setup_market_switches.sql`** 도 실행합니다 (매수 on/off 스위치용).

`setup_kr.sql` 하나면 국내 테이블은 충분합니다 — 테이블 생성 + 컬럼 마이그레이션 +
RLS/권한이 모두 들어 있고, **멱등(idempotent)이라 몇 번을 다시 돌려도 안전**합니다.
나중에 유니버스를 늘렸을 때도 이 파일만 재실행하면 컬럼이 자동으로 추가됩니다.

| 테이블 | 역할 |
|---|---|
| `kr_economic_and_stock_data` | 날짜별 지수·환율·거시 + 변동성 + 100종목 종가 (ML 입력) |
| `kr_stock_recommendations` | 기술적 지표 스냅샷 |
| `kr_stock_analysis_results` | ML 예측 결과 |
| `kr_ticker_sentiment_analysis` | 뉴스 감성 점수 |
| `kr_news_articles` | 감성 판단 근거 기사 (사후 추적) |
| `kr_buy_queue` | 다음 개장일 매수 예약 |
| `kr_trade_records` | 매매 기록 + ATR 익절/손절 (원화) |
| `kr_llm_decision_logs` | LLM 매수 판단 로그 |
| `kr_llm_sell_decision_logs` | LLM 매도 판단 로그 |
| `kr_fear_gate_overrides` | 변동성 게이트 수동 해제 이력 |
| `access_tokens` | KIS 토큰 캐시 (공용) |
| `market_switches` | 신규 매수 on/off 스위치 (공용, `market='KR'` 행만 사용) |

### 2. `.env` 설정

```env
# ── 한국투자증권 (필수) ──
KIS_USE_MOCK=true                   # true: 모의투자 / false: 실전투자
KIS_MOCK_APPKEY=발급받은_앱키
KIS_MOCK_APPSECRET=발급받은_앱시크릿
KIS_MOCK_CANO=모의투자_계좌번호
KIS_REAL_APPKEY=
KIS_REAL_APPSECRET=
KIS_REAL_CANO=
KIS_ACNT_PRDT_CD=01

# ── Supabase (필수) ──
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJhbGci...
SUPABASE_SERVICE_ROLE_KEY=          # 권장 (RLS 우회)

# ── Claude (필수) ── 뉴스 감성 채점 + LLM 매수/매도 검토 + 리포트
ANTHROPIC_API_KEY=sk-ant-...

# ── 매매 동작 ──
KR_ENABLED=true                     # false면 API만 뜨고 스케줄러는 멈춤 (점검용)
KR_DRY_RUN=true                     # true면 주문 API 호출 없이 로그만 (검증용)

KR_HISTORY_YEARS=5                  # 백필 기간 (년)
KR_SLOT_RATIO=0.10                  # 종목당 기준 투자 비중 (총자산 대비)
KR_MAX_POSITIONS=10                 # 동시 보유 최대 종목 수

# 확신도 가중 배분 — 점수 높은 종목에 더 많이
KR_SLOT_TILT=0.5                    # 0=균등, 0.5=1위 1.5배·꼴찌 0.5배, 1.0=최대
KR_SLOT_METHOD=rank                 # rank(총액 고정, 권장) / score(점수 차 반영)
KR_MIN_SLOT_RATIO=0.05              # 종목별 하한
KR_MAX_SLOT_RATIO=0.20              # 종목별 상한
KR_MAX_TOTAL_EXPOSURE=0.80          # 총 투입 비율 상한
KR_MIN_ML_ACCURACY=80               # ML 신뢰도 하한 (%)
KR_MIN_RISE_PROBABILITY=2           # 예측 상승률 하한 (%)

# ── 신규 종목 추천 스크리닝 ──
KR_UNIVERSE_SIZE=200                # 분석 후보군 크기 (KOSPI 시총 상위 N)
KR_UNIVERSE_REFRESH_DAYS=7          # 시총 순위 캐시 유효기간(일). 갱신 1회에 8분 소요
KR_DART_CACHE_DAYS=14               # DART 재무제표 캐시 유효기간(일)
KR_STAGE2_TOP_N=5                   # 2단계 통과 후보 수 (ML 상승률 양수 중 상위 N)
KR_LLM_MAX_PICKS=3                  # 3단계 LLM 이 매수 추천할 최대 종목 수
KR_REBALANCE_MAX_POSITIONS=8        # 리밸런싱 집행 후 보유 종목수 상한
KR_REBALANCE_MODEL=claude-opus-5    # 3단계 리밸런싱 판단 모델
KR_MAX_PER_SECTOR=2                 # 같은 섹터 최대 보유 종목 수 (섹터 분산)
KR_MIN_RISE_PROBABILITY=0           # 예측 상승률 추가 하한(%). 기본 규칙은 '양수(>0)'
KR_FUNDAMENTAL_MODEL=claude-opus-5  # 1단계 기본적 분석 판정 모델
KR_SENTIMENT_MIN_SCORE=-0.1         # 1단계 감성 하한 (이하면 탈락)
KR_MIN_BUY_SIGNALS=2                # 2단계 최소 매수 신호 개수
KR_CROSS_LOOKBACK_DAYS=10           # 골든/데드크로스를 '최근'으로 볼 기간(일)
KR_VOLUME_SURGE_RATIO=1.5           # 거래량 급증 판정 배수 (5일 평균 대비)
KR_FLOW_WINDOW_DAYS=5               # 수급 관측 구간(거래일)
KR_FLOW_STREAK_DAYS=3               # 외국인·기관 각각 필요한 연속 순매수 일수

KR_ANALYSIS_TIME=16:30              # 분석 파이프라인 (KST)
KR_EXECUTION_TIME=09:15             # 매수 집행 (KST)

# ── 매도 전략 ──
KR_ROTATION_MIN_SCORE_GAP=0.30      # 교체매매 최소 점수차
KR_SCORE_TREND_DAYS=5               # LLM 프롬프트에 보여줄 점수 추세 일수
KR_SELL_REVALIDATE_PCT=3.0          # LLM 매도판정 집행 재검증 임계값 (%)
KR_FEAR_SELL_LOSS_PCT=3.0           # 공포장 강제청산 발동 최소 손실률 (%)
KR_INTRADAY_FEAR_REVIEW_THRESHOLD=40.0   # 장중 추가 매도검토 발동 공포지수
KR_INTRADAY_REVIEW_INTERVAL_HOURS=2      # 장중 재검토 주기 (시간)

# ── NAVER API Hub — 뉴스 감성 (필수) ──
NAVER_API_KEY_ID=
NAVER_API_KEY=
NAVER_API_HUB_BASE=https://naverapihub.apigw.ntruss.com
NAVER_DATALAB_BASE=https://naveropenapi.apigw.ntruss.com

KR_SENTIMENT_MODEL=claude-opus-5    # 기사 채점 모델
KR_SENTIMENT_LOOKBACK_DAYS=3

# ── 데이터 소스 (선택) ──
ECOS_API_KEY=                       # 한국은행 거시지표 (권장)
KRX_AUTH_KEY=                       # KRX Open API
DART_API_KEY=                       # 공시/재무제표

# ── Kaggle (ML 예측) ──
KAGGLE_USERNAME=
KAGGLE_API_TOKEN=
KAGGLE_KERNEL_SLUG_KR=stock-prediction-kr
KAGGLE_NOTEBOOK_DIR_KR=kaggle_notebook_kr

# ── Slack (선택) ──
SLACK_WEBHOOK_URL=                  # 텍스트 알림
SLACK_BOT_TOKEN=                    # xoxb-… (scope: files:write) — PDF 리포트 첨부용
SLACK_REPORT_CHANNEL=               # 채널 ID 권장
KR_REPORT_ENABLED=true
KR_REPORT_MODEL=claude-opus-5
KR_REPORT_DIR=reports/kr
KR_REPORT_KEEP_DAYS=60
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

### 4. Kaggle 커널 생성

`kaggle_notebook_kr/kernel-metadata.json`의 `id`를 본인 계정으로 맞춘 뒤
(push 직전 자동 교정도 됩니다):

```bash
python -c "
import logging; logging.basicConfig(level=logging.INFO)
from app.services import ml_trigger_service as mt
print(mt.trigger_and_wait())"
```

첫 실행 시 커널이 자동 생성됩니다. GPU로 약 3분 걸립니다.

---

## 서버 실행과 확인

```bash
python run.py              # API 서버 + 스케줄러 + 번호 메뉴 (권장)
python run.py --no-menu    # 서버만 (systemd/nohup 등 입력이 없는 환경)
python run.py --menu-only  # 메뉴만 (스케줄러 미기동 — 조회·스크리닝 전용)
```

`python run.py` 로 띄우면 서버·스케줄러가 백그라운드 스레드로 돌고 같은 터미널에
**번호 메뉴**가 뜹니다.

```
╔══════════════════════════════════════════════════════════════════════╗
║ 국내주식 자동매매 운영 메뉴  [모의투자]                                  ║
╚══════════════════════════════════════════════════════════════════════╝

  ── 상태 ──
    1. 시스템 상태 (스케줄러 · 장 운영 · 설정)
    2. 잔고 · 보유 종목
    3. 포트폴리오 현황 (슬롯 · 섹터 분포)

  ── 신규 종목 추천 ──
    4. 전체 스크리닝 + LLM 리밸런싱 제안 → 승인
    5. 1단계만 (기본적 분석 + 감성 분석)
    6. 2단계 + 3단계만 (직전 1단계 결과로)
    7. 마지막 추천 결과 다시 보기 / 재승인

  ── 유니버스 · 데이터 ──
    8. 유니버스 갱신 (KOSPI 시총 상위 재조회)
    9. 유니버스 동기화 산출물 생성 (ML 컬럼 3곳)
   10. 시장 데이터 수집
   11. ML 예측 트리거 (Kaggle)

  ── 매매 ──
   12. 매도 후보 조회
   13. 매수 집행 (큐)
   14. 매도 감시 1회 실행
   15. 신규 매수 스위치 on/off

  ── 기타 ──
   16. 분석 리포트 전송
    0. 종료 (매매 시스템도 함께 종료됩니다)
```

- 메뉴가 뜨는 동안 **매매 스케줄러는 그대로 동작합니다.** 메뉴는 조회·수동 실행 창구일 뿐입니다.
- 실제 주문이 나갈 수 있는 항목(13·14번)은 `y` 를 정확히 입력해야 진행됩니다.
- 로그는 화면 대신 `logs/kr_trading.log` 에 쌓입니다. 로그와 메뉴가 같은 화면을 다투면
  입력이 계속 깨지기 때문입니다. 오래 걸리는 항목을 고른 동안에만 진행 상황이 화면에 나옵니다.

- **API 문서** — http://localhost:8000/docs
- **트랙 상태** — http://localhost:8000/kr/status

`/kr/status`의 `scheduler.thread_alive`와 각 job의 `last_run`/`next_run`으로 스케줄러가
실제로 살아 있는지 확인할 수 있습니다. 휴장일에는 매도 감시가 로그를 남기지 않고 조용히
스킵하므로, 로그 대신 이 값을 보세요.

---

## API 사용법

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
| GET | `/kr/candidates/scored-universe` | 점수 유니버스 (임계값 컷 없음, 디버깅용) |
| GET | `/kr/holdings/sell-review` | LLM 매도검토 입력 확인 (LLM 호출 없음) |

**신규 종목 추천 (스크리닝)**

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/kr/screening/run` | 전체 스크리닝 (1 -> 2 -> 3단계 제안). **주문은 내지 않음** |
| POST | `/kr/screening/stage1` | 1단계만 (기본적 분석 + 감성) |
| GET | `/kr/screening/portfolio` | 포트폴리오 현황 (슬롯 · 섹터 분포) |
| GET | `/kr/screening/universe?refresh=true` | 후보군 조회 / 시총 재조회(8분) |
| POST | `/kr/screening/universe/sync` | ML 고정 유니버스 동기화 산출물 생성 |

**실행**

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/kr/pipeline/analysis` | 분석 파이프라인 (1~6단계) |
| POST | `/kr/pipeline/execute-buy?force=true` | 매수 큐 집행 |
| POST | `/kr/pipeline/execute-sell` | 매도 판단 (기계적 + LLM 판정 집행) |
| POST | `/kr/pipeline/execute-sell-review` | 보유종목 LLM 매도검토만 즉시 실행 |

**리포트**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/kr/report/config` | 리포트 설정 진단 (LLM 키 / Slack 업로드 준비 여부) |
| POST | `/kr/report/send` | 직전 파이프라인 결과로 리포트 재생성 + 전송 |

**변동성 게이트**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/kr/fear-gate` | 현재 변동성·임계값·차단 여부 |
| POST | `/kr/fear-gate/override` | 수동 해제 (운영자 개입) |
| POST | `/kr/fear-gate/revoke` | 즉시 복구 |

**계좌 / 매수 스위치**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/kr/balance` | 국내 잔고 + 계좌 요약 |
| GET | `/kr/price/{code}` | 종목 현재가 |
| GET | `/buy-switch` | 신규 매수 허용 여부 |
| POST | `/buy-switch/enable` | 신규 매수 재개 |
| POST | `/buy-switch/disable?reason=사유` | 신규 매수 중단 |

CLI 래퍼도 있습니다.

```bash
./scripts/buy_switch.sh status
./scripts/buy_switch.sh off "출장 - 8/25 복귀"
./scripts/buy_switch.sh on
```

Windows(PowerShell)에서는 `scripts/buy_switch.ps1` 을 쓰세요 (한글 사유 인코딩 고정).

---

## 운영 가이드

### 점수 체계

후보군 내 **cross-sectional z-score**를 가중합해 채점합니다. 절대값이 아니라 상대 순위를
쓰므로, ML 예측이 전반적으로 낙관적이어도 자동으로 중화됩니다.

| 팩터 | 가중치 | 근거 |
|---|---|---|
| 기술 모멘텀 (MACD diff + SMA 괴리 + RSI 평균) | 0.27 | 가장 검증된 신호 |
| ML 예측 상승률 | 0.18 | 보수적 |
| **수급 (외국인+기관 5일 순매수)** | **0.15** | **한국 시장에서 가장 검증된 단기 신호** |
| 거래량 비율 | 0.15 | 가격-거래량 컨펌 |
| ADX 추세 강도 | 0.15 | trend persistence |
| 뉴스 감성 | 0.10 | 학술 IC 약함 |

**사전 필터** (`scoring_service.apply_prefilters`) — RSI > 80 하드블록 + 기술 신호 2개 이상.

네이버 데이터랩 **검색 관심도는 점수에 넣지 않습니다.** 한국 시장에서 개인 관심 급증은
고점 신호로 작동하는 경우가 많아 부호가 불안정해서, LLM 참고 정보로만 넘깁니다.

### 포지션 사이징 — 확신도 가중 배분

통과 종목에 **균등 배분하지 않고 종합점수가 높은 종목에 더 많이** 넣습니다.
`app/services/position_sizing.py`

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
| 0.0 | 10.0 · 10.0 · 10.0 · 10.0 · 10.0 · 10.0 · 10.0 | 70% (균등) |
| 0.3 | 13.0 · 12.0 · 11.0 · 10.0 · 9.0 · 8.0 · 7.0 | 70% |
| **0.5** (기본) | **15.0 · 13.3 · 11.7 · 10.0 · 8.3 · 6.7 · 5.0** | **70%** |
| 1.0 | 20.0 · 16.7 · 13.3 · 10.0 · 6.7 · 5.0 · 5.0 | 76.7% |

`KR_SLOT_TILT=0`이면 균등 배분과 **완전히 동일**하므로 언제든 되돌릴 수 있습니다.

안전장치 — `KR_MIN_SLOT_RATIO`(5%) / `KR_MAX_SLOT_RATIO`(20%)로 개별 종목을 클램프하고,
`KR_MAX_TOTAL_EXPOSURE`(80%)로 총 투입을 제한합니다. 총 노출 상한이 개별 하한보다 우선합니다.

### 변동성 게이트

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

기계적 규칙과 LLM 종합판단 두 층으로 나뉩니다.

**기계적 (항상 실행)**

1. **ATR 전량 익절/손절** — 매수가 + 2.5×ATR 도달 시 **전량 익절**,
   매수가 − 1.5×ATR 도달 시 **전량 손절**. 손절선을 먼저 검사합니다.
2. **기술적 매도 신호** — 데드크로스 / RSI>70 / MACD 매도 / 패닉셀 / 외국인·기관 순매도
   - ADX > 25면 필요 신호 수 1개 차감
   - 감성 < −0.15이면 2개, 아니면 3개
3. **공포장 강제청산** — 변동성 > 40% + 신호 1개, > 30% + 신호 2개.
   단 **`KR_FEAR_SELL_LOSS_PCT`(3.0%) 이상 손실일 때만** 발동합니다 — 이 조건의 취지는
   패닉 국면에서 위험을 줄이는 것이지 본전/수익 포지션을 국면만 보고 털어내는 게 아닙니다.

**LLM 종합판단** — 점수 감쇠, 팩터 반전, 교체매매 후보를 함께 보고 **HOLD 또는 SELL_ALL**
둘 중 하나를 결정합니다(중간값 없음). 판단 시점 대비 현재가가 `KR_SELL_REVALIDATE_PCT`(3%)
이상 유리하게 움직였으면 판단이 낡았다고 보고 이번 사이클 집행을 보류합니다.

공포지수가 `KR_INTRADAY_FEAR_REVIEW_THRESHOLD`(40%)를 넘는 날에는
`KR_INTRADAY_REVIEW_INTERVAL_HOURS`(2시간)마다 장중 매도검토를 한 번 더 돌립니다.

### 안전 장치

| 장치 | 동작 |
|---|---|
| `KR_DRY_RUN=true` | 주문 API 호출 없이 로그만 — 로직 검증용 |
| `KIS_USE_MOCK=true` | 모의투자 계좌 |
| `KR_ENABLED=false` | 스케줄러 자체를 기동하지 않음 (API만) |
| `/buy-switch/disable` | 신규 매수만 원격 차단 (매도 감시·정합성 확인은 계속) |
| ATR 없으면 매수 스킵 | 자동 익절/손절선 없이는 진입하지 않음 |
| LLM Fail-Close | LLM 호출 전체 실패 시 **매수 차단** / 매도는 **HOLD** + Slack 알림 |
| 호가단위 정규화 | 지정가를 KRX 호가단위에 맞춤 (안 맞으면 주문 거부됨) |
| 큐 2일 만료 | 연휴로 밀린 낡은 판단으로 매수하지 않음 |
| 주문 정합성 확인 | 1분마다 KIS 원장과 대조 (체결 확인 / 미체결 정리 / 고아 레코드 복구) |

fail 방향은 모듈마다 다릅니다. **평상시 기본값이 무엇이냐**가 기준입니다 —
매수 스위치는 기본이 '켜짐'이라 조회 실패 시 **허용**(fail-open),
변동성 게이트는 기본이 '차단'이라 조회 실패 시 **차단 유지**(fail-close).

### 실전 전환 순서

```
1) KR_DRY_RUN=true  + KIS_USE_MOCK=true   <- 로직 검증 (며칠 관찰)
2) KR_DRY_RUN=false + KIS_USE_MOCK=true   <- 모의투자 실주문
3) KR_DRY_RUN=false + KIS_USE_MOCK=false  <- 실전 (충분히 검증한 뒤에만)
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
| 한국투자증권 | 매매 주문 · 시세 | https://apiportal.koreainvestment.com | 무료 |
| Supabase | 데이터베이스 | https://supabase.com | 무료 플랜 |
| Anthropic Claude | LLM 검토 + 감성 채점 + 리포트 | https://console.anthropic.com | 유료 |
| Kaggle | ML 학습 (GPU) | https://kaggle.com/settings | 무료 |
| **NAVER API Hub** | 뉴스 감성 | https://www.ncloud.com/product/applicationService/naverApiHub | 무료 (일 25,000) |
| **한국은행 ECOS** | 거시지표 | https://ecos.bok.or.kr/api | 무료 |
| KRX Open API | 시장 시세 (선택) | https://openapi.krx.co.kr | 무료 |
| OpenDART | 공시 (선택) | https://opendart.fss.or.kr | 무료 |
| Slack Webhook | 텍스트 알림 | https://api.slack.com/messaging/webhooks | 무료 |
| Slack Bot Token | PDF 리포트 첨부 | https://api.slack.com/apps (scope `files:write`) | 무료 |

### KIS 모의투자 설정

1. https://apiportal.koreainvestment.com 회원가입
2. **모의투자** 앱키 발급 (앱키 + 앱시크릿)
3. 모의투자 계좌 개설 → 계좌번호 발급
4. `.env`의 `KIS_MOCK_*`에 입력

---

## 문제 해결

### `42501 new row violates row-level security policy`

RLS가 켜져 있는데 anon 키로 쓰려는 경우입니다. 둘 중 하나로 해결합니다.

- **권장** — `.env`에 `SUPABASE_SERVICE_ROLE_KEY` 추가 (RLS 유지, 서버만 우회)
- 또는 `sql/kr/setup_kr.sql` 재실행 (RLS 해제 구문 포함)

### `EGW02006 모의투자 TR 이 아닙니다`

실전 전용 TR을 모의투자 계좌로 호출한 경우입니다.
`chk-holiday`(휴장일)·`inquire-index-price`(업종지수)·`financial-ratio`(재무비율)가 여기
해당하며, 이들 없이도 매매 로직은 완결되도록 설계돼 있습니다.

### `EGW00201 초당 거래건수를 초과하였습니다`

KIS 호출 제한(모의 2건/초)입니다. 자동 재시도로 복구되므로 로그에 보이더라도 정상입니다.

### 매수 후보가 계속 0건

순서대로 확인하세요.

1. `GET /buy-switch` — 매수 스위치가 꺼져 있는지
2. `GET /kr/fear-gate` — 변동성이 90%를 넘어 차단 중인지
3. ML 예측 존재 여부 — `kr_stock_analysis_results`가 비어 있으면 Kaggle 커널 실행
4. `KR_MIN_ML_ACCURACY`(기본 80) 필터에 다 걸리는지 — 로그의 `ML 필터` 줄 확인
5. 이미 보유/주문 중인 종목은 후보에서 제외됩니다

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
| **`documents/20_국내주식_KOSPI30_설계.md`** | **전체 설계 (이 시스템의 기준 문서)** |
| `documents/08_Kaggle_API_연동.md` | Kaggle push/폴링 메커니즘 |
| `documents/09_Slack_연동.md` | Slack Webhook 연동 |
| `documents/10_멀티팩터_변별력_개선_기획.md` | cross-sectional z-score 설계 근거 |
| `documents/16_클라우드_배포_및_보안_가이드.md` | AWS Lightsail 배포·보안 |
| `documents/_archive_us/` | 구 미국 트랙 (현재 코드에 없음 — 참고용) |

---

## 주의사항

- 이 시스템은 **교육 목적**으로 제작되었습니다
- 실전 투자에서 발생하는 손실에 대해 책임지지 않습니다
- 반드시 **드라이런 → 모의투자 → 실전** 순서로 검증하세요
- API 키와 시크릿은 절대 외부에 공유하지 마세요 (`.env`는 `.gitignore`에 포함)
- `SUPABASE_SERVICE_ROLE_KEY`는 RLS를 우회하므로 **서버 백엔드 전용**입니다
