# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Algorithmic stock trading system that combines economic data analysis, technical analysis, sentiment analysis, and automated trade execution through the Korean Investment Securities (한국투자증권/KIS) brokerage API. Comments and variable names are primarily in Korean.

## Running the Application

```bash
# Start the FastAPI server (main entry point)
python run.py
# Runs uvicorn on 0.0.0.0:8000 with reload enabled

# Standalone data collection
python stock.py

# ML prediction
python predict.py
```

No test suite exists yet (tests/ directory is empty).

## Architecture

### Layered Structure (app/)

- **api/routes/** - FastAPI route handlers (4 route groups: balance, economic, stock_recommendations, stocks)
- **api/api.py** - Central router that aggregates all routes
- **services/** - Business logic layer (largest files in the codebase)
  - `balance_service.py` - KIS brokerage API integration (orders, balances, token management)
  - `stock_recommendation_service.py` - Technical indicators (SMA, EMA, RSI, MACD, Golden Cross) and recommendation filtering
  - `economic_service.py` - FRED + Yahoo Finance + KIS data collection pipeline
  - `auth_service.py` - Token management
- **utils/scheduler.py** - APScheduler-based automated trading (auto-buy daily at midnight KST, auto-sell every 1 minute)
- **core/config.py** - Pydantic BaseSettings configuration
- **db/supabase.py** - Supabase client initialization
- **main.py** - FastAPI app with lifespan (starts schedulers + initial data collection on startup)

### Standalone Modules (root)

- `stock.py` - FRED API + Yahoo Finance + KIS data collection, merges into Supabase `economic_and_stock_data` table
- `predict.py` / `predict_real.py` - TensorFlow Transformer model for stock price prediction

### Data Flow

1. **Collection**: FRED economic indicators + Yahoo Finance prices + KIS domestic stocks → merged by date → stored in Supabase
2. **Analysis**: Historical data → technical indicators (SMA/EMA/RSI/MACD) → filtered by accuracy >= 80% and rise probability >= 3%
3. **Sentiment**: AlphaVantage news API → sentiment scores → stored in `ticker_sentiment_analysis`
4. **Execution**: Recommendations → auto-buy at midnight KST → auto-sell monitors every minute during US market hours

## Database (Supabase/PostgreSQL)

Key tables: `economic_and_stock_data`, `stock_analysis_results`, `stock_recommendations`, `ticker_sentiment_analysis`, `access_tokens`, `stocks`

## Environment Variables

Required in `.env`:
- `KIS_BASE_URL`, `KIS_REAL_URL` - KIS API endpoints (mock vs real)
- `KIS_APPKEY`, `KIS_APPSECRET` - KIS API credentials
- `KIS_CANO`, `KIS_ACNT_PRDT_CD` - KIS account info
- `KIS_USE_MOCK` - Toggle mock/real trading mode
- `SUPABASE_URL`, `SUPABASE_KEY` - Database credentials
- `ALPHA_VANTAGE_API_KEY` - Stock data API
- `FRED_API_KEY` - Federal Reserve economic data API
- `DEBUG` - Debug mode flag

## Key Implementation Details

- **Token caching**: KIS access tokens cached in memory + Supabase with thread-lock-protected 1-minute refresh throttling
- **Market hours**: Auto-sell detects US market hours (9:30 AM - 4:00 PM ET) with automatic daylight savings handling
- **Missing data**: Forward/backward fill strategy for gaps in economic/stock data
- **Async**: Background tasks and async scheduling via asyncio
- **Timezone**: All scheduling uses Korea Standard Time (Asia/Seoul via pytz)

## Dependencies

Core: fastapi, uvicorn, pydantic, pydantic-settings, python-dotenv
Database: supabase-py
Data: pandas, numpy, yfinance, requests
ML: tensorflow, keras, scikit-learn
Scheduling: schedule, APScheduler


## 국내주식 (KOSPI 30) 트랙

미국 트랙과 **병행 운영**되는 별도 파이프라인. `KR_ENABLED=true` 일 때만 기동한다.
전체 설계는 `documents/20_국내주식_KOSPI30_설계.md` 참조.

### 구조 (app/services/kr/)
- `universe.py` - KOSPI 시총 상위 30 보통주 마스터 (코드/이름/섹터/뉴스 검색어)
- `kis_domestic_service.py` - KIS 국내주식 API (주문 TTTC0012U/0011U, 잔고 TTTC8434R,
  일봉 FHKST03010100, 수급 FHKST01010900) + 호가단위 정규화 + 휴장일/장시간 판정
- `naver_service.py` - NAVER API Hub (뉴스 검색, 데이터랩 트렌드)
- `kr_sentiment_service.py` - 기사 텍스트 → Claude 배치 스코어링 → 감성 점수 (-1~+1)
- `kr_market_data_service.py` - Yahoo + 한국은행 ECOS → `kr_economic_and_stock_data`
- `kr_scoring.py` - cross-sectional z-score (+ 외국인·기관 수급 팩터)
- `kr_recommendation_service.py` - 기술 지표 생성 / 매수 후보 / 매도 후보
- `kr_llm_review_service.py` - LLM 최종 검토 (한국 시장 프롬프트, Fail-Close)
- `kr_notification_service.py` - Slack 알림 (원화 포맷)
- `app/utils/kr_scheduler.py` - 2단계 파이프라인 + 매도 감시 + 주문 정합성
- `app/api/routes/kr.py` - `/kr/*` 라우트

### 시간 구조 (KST)
한국 증시는 15:30 에 닫혀 장 마감 후 주문이 불가하므로 **분석과 집행을 분리**한다.
- **16:30 Phase A (분석)**: 시장데이터 → Kaggle ML → 기술지표+뉴스감성 → LLM 검토 → `kr_buy_queue` 저장
- **09:05 Phase B (집행)**: 큐를 읽어 현재가 재조회 후 지정가 매수
- **09:00~15:20 매도 감시**: 1분 주기 (동시호가 구간 제외)

### 주의
- KR 전용 `schedule.Scheduler()` 인스턴스를 쓴다. 전역 큐를 미국 트랙과 공유하면
  두 스레드가 `run_pending()` 을 동시에 돌려 이중 주문이 날 수 있다.
- 주문 body 에 `EXCG_ID_DVSN_CD="KRX"` 필수.
- 지정가는 반드시 `round_to_tick()` 통과 (호가단위 불일치 시 주문 거부).
- 유니버스 교체 시 `universe.py` / `sql/kr/setup_kr.sql` 컬럼 /
  `kaggle_notebook_kr/predict_kr.py` 의 `TARGET_COLUMNS` 세 곳을 함께 고쳐야 한다.

### 테이블
`kr_economic_and_stock_data`, `kr_stock_recommendations`, `kr_stock_analysis_results`,
`kr_ticker_sentiment_analysis`, `kr_news_articles`, `kr_buy_queue`, `kr_trade_records`,
`kr_llm_decision_logs`, `kr_fear_gate_overrides` (DDL: `sql/kr/setup_kr.sql` — 멱등, 재실행 안전)

### 추가 환경 변수
`KR_ENABLED`, `KR_DRY_RUN`, `KR_SLOT_RATIO`, `KR_MAX_POSITIONS`,
`KR_ANALYSIS_TIME`, `KR_EXECUTION_TIME`, `NAVER_API_KEY_ID`, `NAVER_API_KEY`,
`NAVER_API_HUB_BASE`, `KR_SENTIMENT_MODEL`, `ECOS_API_KEY`, `KRX_AUTH_KEY`,
`DART_API_KEY`, `KAGGLE_KERNEL_SLUG_KR`, `KAGGLE_NOTEBOOK_DIR_KR`
