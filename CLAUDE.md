# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

한국투자증권(KIS) OpenAPI 로 **국내주식(KOSPI 시총 상위 100 보통주)** 을 자동매매하는 시스템.
시장 데이터 수집 → ML 예측 → 기술지표·뉴스감성·수급 채점 → LLM 최종 검토 → 주문 집행까지
하나의 파이프라인으로 돈다. 주석과 변수명은 대부분 한국어다.

**대상 시장은 국내 하나뿐이다.** 과거에는 미국 트랙과 병행 운영됐으나 전부 삭제됐다.
미국 트랙 문서는 `documents/_archive_us/` 에 보존돼 있으며 **현재 코드와 대조하면 안 된다**
(거기 나오는 파일 경로·테이블·라우트는 존재하지 않는다).

전체 설계: `documents/20_국내주식_KOSPI30_설계.md`

## Running the Application

```bash
python run.py              # API 서버(백그라운드 스레드) + 스케줄러 + 번호 메뉴(포그라운드)
python run.py --no-menu    # 서버만 (systemd/nohup 등 입력이 없는 환경)
python run.py --menu-only  # 메뉴만 (스케줄러 미기동 — 조회·스크리닝 전용)
```

메뉴가 뜨는 모드에서는 로그가 콘솔 대신 `logs/kr_trading.log` 로 간다. 로그와 메뉴가
같은 화면을 다투면 입력이 계속 깨지기 때문이다. 오래 걸리는 메뉴 항목을 고른 동안에만
`app/core/logging_config.console_logging()` 이 콘솔 핸들러를 붙였다 뗀다.

`KR_ENABLED=false` 로 두면 API 만 뜨고 스케줄러는 멈춘다 (점검·수동 백필용 킬 스위치).

테스트 스위트는 아직 없다 (`tests/` 비어 있음).

## Architecture

### 공용 모듈 (`app/services/` 최상위) — 시장에 종속되지 않는 것만 둔다

- `scoring_service.py` - 매수 후보 사전 필터 + cross-sectional z-score
- `indicators.py` - `TechnicalIndicators`: SMA/EMA/RSI/MACD/ATR/ADX 수식
- `position_sizing.py` - 확신도 가중 포지션 배분 (rank/score, tilt, 노출 상한)
- `kis_auth_service.py` - KIS 토큰 발급·캐싱 (메모리 + Supabase, 스레드 락 + 1분 스로틀)
- `slack_service.py` - Slack Webhook 저수준 전송 (`_send`)
- `ml_trigger_service.py` - Kaggle 커널 push + 완료 폴링
- `buy_switch_service.py` - 신규 매수 원격 on/off (영속 스위치, fail-open)

### 국내 전용 (`app/services/kr/`)

- `universe.py` - KOSPI 시총 상위 100 보통주 마스터 (코드/이름/섹터/뉴스 검색어)
- `kis_domestic_service.py` - KIS 국내주식 API (주문 TTTC0012U/0011U, 잔고 TTTC8434R,
  일봉 FHKST03010100, 수급 FHKST01010900) + 호가단위 정규화 + 휴장일/장시간 판정
- `naver_service.py` - NAVER API Hub (뉴스 검색, 데이터랩 트렌드)
- `kr_sentiment_service.py` - 기사 텍스트 → Claude 배치 스코어링 → 감성 점수 (-1~+1)
- `kr_market_data_service.py` - Yahoo + 한국은행 ECOS → `kr_economic_and_stock_data`
- `kr_scoring.py` - cross-sectional z-score (+ 외국인·기관 수급 팩터) + 변동성 적응형 임계값
- `kr_recommendation_service.py` - 기술 지표 생성 / 매수 후보 / 매도 후보
- `kr_llm_review_service.py` - LLM 매수 최종 검토 (Fail-Close = 매수 차단)
- `kr_llm_sell_review_service.py` - LLM 보유종목 매도검토 (HOLD/SELL_ALL, Fail-Close = HOLD)
- `kr_override_service.py` - 변동성 게이트 수동 오버라이드 (한시적, 감사 기록)
- `kr_notification_service.py` - Slack 텍스트 알림 (원화 포맷)
- `kr_report_service.py` - 분석 리포트: 매수 견적서 산출 + LLM 서술 + 전송 오케스트레이션
- `kr_pdf_service.py` - 리포트 PDF 렌더러 (ReportLab, 한글 폰트 자동 탐색/CID 폴백)
- `slack_file_service.py` - Slack 파일 업로드 (Bot Token 3단계 API — Webhook 은 첨부 불가)
- `kr_universe_service.py` - **동적 후보군**: KIS 종목마스터 + 시가총액 → 상위 N (캐시)
- `dart_service.py` - OpenDART 재무제표 → 영업이익률/ROE/ROIC/PER/안정성/FCF
- `kr_fundamental_review_service.py` - 1단계 기본적 분석 LLM 판정 (Fail-Close = 전원 탈락)
- `kr_screening_service.py` - **신규 종목 추천 파이프라인**(1·2단계) + 포트폴리오 규칙
- `kr_rebalance_service.py` - 3단계 LLM 매수 추천 + 리밸런싱 제안 (주문 안 냄, 승인 후 적재)

### 그 외

- `app/cli/menu.py` - 번호 입력식 운영 메뉴 (서비스 함수 직접 호출, HTTP 경유 X)
- `app/core/logging_config.py` - 파일 기본 로깅 + `console_logging()` 컨텍스트
- `app/utils/kr_scheduler.py` - 2단계 파이프라인 + 매도 감시 + 주문 정합성
- `app/api/routes/kr.py` - `/kr/*` 라우트
- `app/api/routes/buy_switch.py` - `/buy-switch/*` 라우트
- `app/core/config.py` - Pydantic BaseSettings 설정
- `app/db/supabase.py` - Supabase 클라이언트
- `app/main.py` - FastAPI 앱 (lifespan 에서 스케줄러 기동/종료)
- `kaggle_notebook_kr/predict_kr.py` - Transformer 주가 예측 커널 (Kaggle 에서 실행)

## 신규 종목 추천 (kr_screening_service)

후보군은 **KOSPI 시가총액 상위 KR_UNIVERSE_SIZE(기본 200)** 이며 매번 새로 계산한다.

```
후보군 200
  │  1단계  DART 재무 → 하드게이트 → LLM 기본적분석 판정 → 뉴스·블로그 감성 + 섹터 업황
  │  2단계  기술적 신호 2개 이상 → 외국인·기관 3일 연속 순매수
  │         → ML 예측 상승률이 **양수인 것 중** 상위 5 (KR_STAGE2_TOP_N)
  │  3단계  LLM 이 그 5개 중 최대 3개(KR_LLM_MAX_PICKS) 매수 추천 + 보유종목 리밸런싱 제안
  └─ 사용자 승인 (메뉴) → 매수는 kr_buy_queue, 매도는 kr_llm_sell_decision_logs 로 적재
```

**주문은 사람이 승인해야만 나간다.** 1~3단계 어디서도 주문 API 를 부르지 않는다.
승인된 항목도 직접 주문하지 않고 기존 집행 경로(매수 큐 / 매도 판정)에 넣는다 —
그래야 현재가 재조회·호가단위 정규화·수량 재계산·중복 방지·정합성 확인이 그대로 걸린다.

### 보유 종목수 상한이 둘인 이유

| 설정 | 값 | 성격 |
|---|---|---|
| `KR_MAX_POSITIONS` | 10 | **하드 상한**. 스크리닝이 후보를 만들 때의 여유 슬롯 계산 기준 |
| `KR_REBALANCE_MAX_POSITIONS` | 8 | **리밸런싱 제안이 지켜야 할 목표**. 여유를 남겨 다음 회차에 더 좋은 후보가 나와도 기존 종목을 억지로 팔지 않게 한다 |

`kr_rebalance_service._validate()` 가 LLM 응답을 받은 뒤 **다시 한 번 제약을 강제**한다
(매수 상한 / 후보 목록 밖 종목 / 미보유 종목 매도 / 집행 후 보유수 / 섹터 한도).
프롬프트로만 걸면 지켜지지 않는 경우가 있어서다. 잘라낸 항목은 경고로 남겨 화면에 띄운다.

비싼 단계를 뒤로 미루는 순서다 — 기술적 분석은 종목당 KIS 일봉 조회(0.6초), 수급은 한 번
더 호출하므로, 1단계에서 후보가 줄어든 뒤에 돌린다.

**2단계 매수 신호** (2개 이상 필요): 골든크로스(20EMA>50EMA) / RSI 매수구간 상승 /
MACD 시그널 상향돌파 / ADX≥25 / 거래량 급증. 데드크로스가 최근에 났으면 신호 수와
무관하게 탈락. ATR 은 신호가 아니라 익절·손절선 산출값이라 없으면 탈락.

### 유니버스가 둘인 이유

| | 무엇 | 왜 |
|---|---|---|
| `universe.py` (고정 100) | **ML 학습 대상** | `kr_economic_and_stock_data` 종가 컬럼과 `predict_kr.py` 의 `TARGET_COLUMNS` 가 1:1로 묶여 있어 바꾸면 학습 데이터가 깨진다 |
| `kr_universe_service` (동적 200) | **1·2단계 후보군** | 매매 판단만 하므로 최신 시총 순위를 그대로 쓰는 편이 낫다 |

둘을 일치시키려면 메뉴 9번(또는 `POST /kr/screening/universe/sync`)이 만드는 산출물로
세 파일을 함께 고치고 `setup_kr.sql` 재실행 → Kaggle 재학습을 해야 한다. **자동으로
덮어쓰지 않는다** — ML 학습 대상 변경은 과거 데이터와의 정합성 문제라 사람이 판단할 몫이다.

동적 후보군에만 있고 ML 컬럼에 없는 종목은 2단계 ML 필터에서 "ML 예측 없음"으로 탈락한다.

### 데이터 소스 주의

- **KRX Open API 는 쓰지 않는다.** API 별 이용신청이 따로 필요해 승인 전에는 전 엔드포인트가
  401 이다. 시총 순위는 KIS 종목마스터(`kospi_code.mst`) + 현재가 `hts_avls` 로 직접 만든다.
- KIS 종목마스터는 고정폭 레코드이고 **뒤 227 바이트**가 고정 필드부다. 한글이 cp949
  2바이트라 문자열로 자르면 어긋난다 — 반드시 bytes 로 잘라야 한다.
- KIS `ranking/market-cap` 은 모의계좌에서 연속조회가 막혀 30건이 상한이라 200 을 못 채운다.
- DART 계정과목은 회사마다 표기가 갈린다(삼성전자 "영업활동현금흐름" vs SK하이닉스
  "영업활동 현금흐름"). **IFRS 표준계정코드(`account_id`)를 먼저 보고** 이름은 폴백으로만 쓴다.
- 네이버 금융 산업분석은 공식 API 가 없어 **섹터명 뉴스 검색**으로 대체한다.
- 섹터 라벨이 둘이다. `sector` 는 고정 유니버스의 세분류(100종목분뿐, 표시·LLM 프롬프트용),
  `sector_krx` 는 KIS 현재가 응답의 `bstp_kor_isnm`(전 종목에 있음). **분산 규칙
  (`KR_MAX_PER_SECTOR`)은 `sector_krx` 를 기준으로 센다** — 세분류만 쓰면 유니버스 밖
  100종목이 '미분류' 한 덩어리가 돼서 은행과 화학을 같은 섹터로 세는 사고가 난다.

## 시간 구조 (KST)

한국 증시는 15:30 에 닫혀 장 마감 후 주문이 불가하므로 **분석과 집행을 분리**한다.
- **16:30 Phase A (분석)**: 시장데이터 → Kaggle ML → 기술지표+뉴스감성 → LLM 검토 → `kr_buy_queue` 저장
  → 분석 리포트 PDF 생성 후 Slack 채널 업로드 (6단계, 실패해도 파이프라인은 성공 처리)
- **09:15 Phase B (집행)**: 큐를 읽어 현재가 재조회 후 지정가 매수
- **09:00~15:20 매도 감시**: 1분 주기 (동시호가 구간 제외)

## 주의

- 전용 `schedule.Scheduler()` 인스턴스를 쓴다. 전역 큐를 다른 워커와 공유하면
  두 스레드가 `run_pending()` 을 동시에 돌려 이중 주문이 날 수 있다.
- 주문 body 에 `EXCG_ID_DVSN_CD="KRX"` 필수.
- 지정가는 반드시 `round_to_tick()` 통과 (호가단위 불일치 시 주문 거부).
- 유니버스 교체 시 `universe.py` / `sql/kr/setup_kr.sql` 컬럼 /
  `kaggle_notebook_kr/predict_kr.py` 의 `TARGET_COLUMNS` 세 곳을 함께 고쳐야 한다.
- **매도는 언제나 전량이다.** 익절이면 전량 익절, 손절이면 전량 손절 — 부분매도·트레일링
  잔량 보유는 없고, LLM 판정도 HOLD/SELL_ALL 둘뿐이다. `kr_trade_records` 의
  `partial_exit_done`/`chandelier_*`/`realized_partial_*` 컬럼은 과거 기록 보존용으로 남아
  있을 뿐 새로 쓰이지 않는다 (전량매도 시 총손익 합산에만 읽는다).
- 리포트(6단계)는 Fail-Open 이다. LLM/PDF/Slack 중 무엇이 실패해도 예외를 밖으로 던지지 않고
  매매 결과에 영향을 주지 않는다 — 반대로 리포트를 매매 게이트로 쓰면 안 된다.
- Phase A 각 단계는 `KrScheduler._artifacts` 에 원자료를 남긴다. 리포트가 이걸 그대로 쓰므로
  단계 로직을 고칠 때 적재 코드를 같이 유지해야 한다 (LLM 판정 사유는 DB 로 복원 불가).
- fail 방향이 모듈마다 다르다. 평상시 기본값이 무엇이냐가 기준이다:
  매수 스위치(`buy_switch_service`)는 기본이 '켜짐'이라 **fail-open**,
  변동성 게이트(`kr_override_service`)는 기본이 '차단'이라 **fail-close**,
  LLM 검토는 근거 없이 매매하지 않도록 **Fail-Close**(매수 차단 / 매도는 HOLD).

## Database (Supabase/PostgreSQL)

`kr_economic_and_stock_data`, `kr_stock_recommendations`, `kr_stock_analysis_results`,
`kr_ticker_sentiment_analysis`, `kr_news_articles`, `kr_buy_queue`, `kr_trade_records`,
`kr_llm_decision_logs`, `kr_llm_sell_decision_logs`, `kr_fear_gate_overrides`
(DDL: `sql/kr/setup_kr.sql` — 멱등, 재실행 안전)

공용: `access_tokens`(KIS 토큰), `market_switches`(매수 스위치, `market='KR'` 행만 사용).

> 미국 트랙 테이블(`economic_and_stock_data`, `stock_recommendations` 등)과 그 DDL 파일
> (`sql/create_*.sql`)은 Supabase·저장소에 남아 있으나 **코드가 더 이상 읽고 쓰지 않는다.**

## Environment Variables

`.env` 필수:
- `KIS_BASE_URL`, `KIS_REAL_URL` — KIS API 엔드포인트 (모의/실전)
- `KIS_APPKEY`, `KIS_APPSECRET`, `KIS_CANO`, `KIS_ACNT_PRDT_CD` — KIS 인증/계좌
  (`KIS_MOCK_*` / `KIS_REAL_*` 를 채우면 `KIS_USE_MOCK` 에 따라 자동 전환된다)
- `KIS_USE_MOCK` — 모의/실전 전환
- `SUPABASE_URL`, `SUPABASE_KEY` (+ 권장 `SUPABASE_SERVICE_ROLE_KEY`)
- `ANTHROPIC_API_KEY` — 뉴스 감성 / LLM 검토 / 리포트
- `NAVER_API_KEY_ID`, `NAVER_API_KEY` — 뉴스 감성 수집
- `KAGGLE_USERNAME` + (`KAGGLE_API_TOKEN` 또는 `KAGGLE_KEY`) — ML 커널 트리거

주요 선택 설정:
- `KR_ENABLED`(기본 true), `KR_DRY_RUN`, `KR_ANALYSIS_TIME`(16:30), `KR_EXECUTION_TIME`(09:05)
- 포지션: `KR_SLOT_RATIO`, `KR_MAX_POSITIONS`(10), `KR_SLOT_TILT`, `KR_SLOT_METHOD`,
  `KR_MIN_SLOT_RATIO`, `KR_MAX_SLOT_RATIO`, `KR_MAX_TOTAL_EXPOSURE`
- 스크리닝: `KR_UNIVERSE_SIZE`(200), `KR_UNIVERSE_REFRESH_DAYS`(7), `KR_UNIVERSE_CACHE_DIR`,
  `KR_DART_CACHE_DAYS`(14), `KR_MAX_PER_SECTOR`(2),
  `KR_FUNDAMENTAL_MODEL`, `KR_SENTIMENT_MIN_SCORE`(-0.1), `KR_MIN_BUY_SIGNALS`(2),
  `KR_CROSS_LOOKBACK_DAYS`(10), `KR_VOLUME_SURGE_RATIO`(1.5),
  `KR_FLOW_WINDOW_DAYS`(5), `KR_FLOW_STREAK_DAYS`(3),
  `KR_MIN_RISE_PROBABILITY`(0 — 기본 규칙은 '양수', 올리면 더 조인다)
- 추천/리밸런싱: `KR_STAGE2_TOP_N`(5), `KR_LLM_MAX_PICKS`(3),
  `KR_REBALANCE_MAX_POSITIONS`(8), `KR_REBALANCE_MODEL`
- 필터: `KR_MIN_ML_ACCURACY`, `KR_MIN_RISE_PROBABILITY`, `KR_HISTORY_YEARS`
- 매도전략: `KR_ROTATION_MIN_SCORE_GAP`(0.30), `KR_SCORE_TREND_DAYS`(5),
  `KR_SELL_REVALIDATE_PCT`(3.0), `KR_FEAR_SELL_LOSS_PCT`(3.0 — 공포장 강제청산 발동 최소
  손실률, 양수로 적는다), `KR_INTRADAY_FEAR_REVIEW_THRESHOLD`(40.0),
  `KR_INTRADAY_REVIEW_INTERVAL_HOURS`(2) — 자세한 설명은 설계문서 6장
- 리포트: `KR_REPORT_ENABLED`(true), `KR_REPORT_MODEL`(claude-opus-5),
  `KR_REPORT_DIR`(`reports/kr`), `KR_REPORT_KEEP_DAYS`(60),
  `SLACK_BOT_TOKEN`(xoxb-…, scope `files:write`), `SLACK_REPORT_CHANNEL`(채널 ID 권장)
  — 설계문서 6-5장
- 데이터: `ECOS_API_KEY`, `KRX_AUTH_KEY`, `DART_API_KEY`, `NAVER_API_HUB_BASE`,
  `NAVER_DATALAB_BASE`, `KR_SENTIMENT_MODEL`, `KR_SENTIMENT_LOOKBACK_DAYS`
- ML: `KAGGLE_KERNEL_SLUG_KR`(stock-prediction-kr), `KAGGLE_NOTEBOOK_DIR_KR`(kaggle_notebook_kr)
- 알림: `SLACK_WEBHOOK_URL`, `SLACK_NOTIFY_LEVEL`

## Dependencies

fastapi, uvicorn, pydantic, pydantic-settings, python-dotenv, supabase-py,
pandas, numpy, requests, pytz, schedule, anthropic, reportlab, kaggle

설치된 anthropic SDK(1.x)의 `messages.create()` 에는 **`temperature` 파라미터가 없다.**
LLM 호출부에 temperature 를 넘기면 `TypeError` 로 죽고 폴백 모델까지 연쇄 실패한다.

TensorFlow/scikit-learn 은 **서버에 필요 없다** — 학습·예측은 Kaggle 커널에서만 돈다.
