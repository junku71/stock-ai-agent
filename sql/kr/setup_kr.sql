-- ═══════════════════════════════════════════════════════════════════════════
-- 국내주식(KOSPI 100) 트랙 — 전체 스키마 설치 / 마이그레이션 (단일 파일)
--
-- 이 파일 하나면 충분하다. 신규 프로젝트에도, 이미 만들어진 DB 에도 그대로 실행한다.
-- 모든 구문이 멱등(idempotent)이라 몇 번을 다시 돌려도 안전하다.
--     CREATE TABLE IF NOT EXISTS  → 있으면 건너뜀
--     ADD COLUMN IF NOT EXISTS    → 있으면 건너뜀 (유니버스 확장 시 자동 반영)
--     DISABLE ROW LEVEL SECURITY  → 이미 꺼져 있어도 무해
--
-- 실행: Supabase 대시보드 → SQL Editor 에 전체 붙여넣고 Run
-- 실행 후: 백필을 돌려야 데이터가 채워진다
--     POST /kr/market-data/collect?full=true
--
-- ※ 유니버스(100종목)를 바꾸면 아래 세 곳을 함께 고쳐야 한다:
--     1. app/services/kr/universe.py 의 UNIVERSE
--     2. 이 파일의 종목 컬럼 (섹션 1-1 과 섹션 2)
--     3. kaggle_notebook_kr/predict_kr.py 의 TARGET_COLUMNS
--
-- 참조: documents/20_국내주식_KOSPI30_설계.md
-- ═══════════════════════════════════════════════════════════════════════════


-- ╔═════════════════════════════════════════════════════════════════════════╗
-- ║ 섹션 1. 테이블 생성                                                      ║
-- ╚═════════════════════════════════════════════════════════════════════════╝

-- ───────────────────────────────────────────────────────────────
-- 1-1) kr_economic_and_stock_data
--      날짜별 시장 데이터 (지수·환율·글로벌·거시 + 변동성 + KOSPI100 종가)
--      Kaggle ML 노트북이 이 컬럼명을 TARGET_COLUMNS / ECONOMIC_FEATURES 로 참조한다.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_economic_and_stock_data (
    id           BIGSERIAL PRIMARY KEY,
    "날짜"        DATE NOT NULL UNIQUE,

    -- 한국 시장 지수 / 환율
    "코스피"              FLOAT8,
    "코스피200"           FLOAT8,   -- 소스: KODEX 200 ETF (^KS200 은 시계열이 깨져 있음)
    "코스닥"              FLOAT8,
    "원달러환율"          FLOAT8,
    "엔원환율"            FLOAT8,
    -- 코스피 20일 실현변동성(연율 %). 거래일 기준. 매수 게이트(한국판 공포지수).
    "코스피 변동성 20일"   FLOAT8,

    -- 글로벌 (한국 증시는 미국장·반도체 업황·달러에 강하게 연동)
    "S&P 500 지수"         FLOAT8,
    "나스닥 종합지수"       FLOAT8,
    "VIX 지수"             FLOAT8,   -- ※ 미국 VIX. 한국 공포지수는 위 변동성 컬럼
    "필라델피아 반도체 지수" FLOAT8,
    "달러 인덱스"          FLOAT8,
    "미국 10년 국채금리"    FLOAT8,
    "금 가격"              FLOAT8,
    "WTI 유가"             FLOAT8,
    "닛케이 225"           FLOAT8,
    "상해종합"             FLOAT8,
    "항셍"                 FLOAT8,

    -- 한국은행 ECOS 거시지표 (키 미설정/코드 변경 시 NULL 로 남는다)
    "한국 기준금리"         FLOAT8,
    "국고채 3년"           FLOAT8,
    "국고채 10년"          FLOAT8,
    "CD 91일"              FLOAT8,
    "한국 소비자물가지수"    FLOAT8,
    "한국 통화량 M2"        FLOAT8,

    -- KOSPI 100 종가 (시총 상위 보통주, 2026-08 기준)
    "삼성전자"             FLOAT8,
    "SK하이닉스"           FLOAT8,
    "SK스퀘어"            FLOAT8,
    "삼성전기"             FLOAT8,
    "현대차"              FLOAT8,
    "LG에너지솔루션"         FLOAT8,
    "삼성바이오로직스"         FLOAT8,
    "삼성생명"             FLOAT8,
    "삼성물산"             FLOAT8,
    "KB금융"             FLOAT8,
    "한화에어로스페이스"        FLOAT8,
    "기아"               FLOAT8,
    "신한지주"             FLOAT8,
    "HD현대중공업"          FLOAT8,
    "두산에너빌리티"          FLOAT8,
    "현대모비스"            FLOAT8,
    "셀트리온"             FLOAT8,
    "SK"               FLOAT8,
    "삼성SDI"            FLOAT8,
    "하나금융지주"           FLOAT8,
    "NAVER"            FLOAT8,
    "LG전자"             FLOAT8,
    "삼성화재"             FLOAT8,
    "LS ELECTRIC"      FLOAT8,
    "HD현대일렉트릭"         FLOAT8,
    "고려아연"             FLOAT8,
    "한화오션"             FLOAT8,
    "효성중공업"            FLOAT8,
    "HD한국조선해양"         FLOAT8,
    "POSCO홀딩스"         FLOAT8,
    "우리금융지주"           FLOAT8,
    "SK텔레콤"            FLOAT8,
    "SK이노베이션"          FLOAT8,
    "한국전력"             FLOAT8,
    "HMM"              FLOAT8,
    "한미반도체"            FLOAT8,
    "메리츠금융지주"          FLOAT8,
    "미래에셋증권"           FLOAT8,
    "KT&G"             FLOAT8,
    "LG화학"             FLOAT8,
    "삼성중공업"            FLOAT8,
    "삼성에스디에스"          FLOAT8,
    "두산"               FLOAT8,
    "HD현대"             FLOAT8,
    "LG"               FLOAT8,
    "기업은행"             FLOAT8,
    "S-Oil"            FLOAT8,
    "카카오"              FLOAT8,
    "LIG디펜스앤에어로스페이스"   FLOAT8,
    "현대글로비스"           FLOAT8,
    "에이피알"             FLOAT8,
    "현대로템"             FLOAT8,
    "포스코퓨처엠"           FLOAT8,
    "한화시스템"            FLOAT8,
    "KT"               FLOAT8,
    "LG이노텍"            FLOAT8,
    "한국항공우주"           FLOAT8,
    "현대오토에버"           FLOAT8,
    "현대건설"             FLOAT8,
    "DB손해보험"           FLOAT8,
    "GS"               FLOAT8,
    "삼양식품"             FLOAT8,
    "한국금융지주"           FLOAT8,
    "크래프톤"             FLOAT8,
    "카카오뱅크"            FLOAT8,
    "NH투자증권"           FLOAT8,
    "대한항공"             FLOAT8,
    "LS"               FLOAT8,
    "포스코인터내셔널"         FLOAT8,
    "HD현대마린솔루션"        FLOAT8,
    "삼성E&A"            FLOAT8,
    "삼성증권"             FLOAT8,
    "한국타이어앤테크놀로지"      FLOAT8,
    "아모레퍼시픽"           FLOAT8,
    "한진칼"              FLOAT8,
    "이수페타시스"           FLOAT8,
    "하이브"              FLOAT8,
    "한화솔루션"            FLOAT8,
    "키움증권"             FLOAT8,
    "LG씨엔에스"           FLOAT8,
    "코웨이"              FLOAT8,
    "유한양행"             FLOAT8,
    "SK바이오팜"           FLOAT8,
    "대우건설"             FLOAT8,
    "LG유플러스"           FLOAT8,
    "한화"               FLOAT8,
    "카카오페이"            FLOAT8,
    "두산밥캣"             FLOAT8,
    "HD건설기계"           FLOAT8,
    "삼성카드"             FLOAT8,
    "한미약품"             FLOAT8,
    "대한전선"             FLOAT8,
    "대덕전자"             FLOAT8,
    "산일전기"             FLOAT8,
    "JB금융지주"           FLOAT8,
    "오리온"              FLOAT8,
    "OCI홀딩스"           FLOAT8,
    "NC"               FLOAT8,
    "한화생명"             FLOAT8,
    "LG디스플레이"          FLOAT8,

    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kr_market_date
    ON kr_economic_and_stock_data ("날짜" DESC);


-- ───────────────────────────────────────────────────────────────
-- 1-2) kr_stock_recommendations — 기술적 지표 스냅샷 (매 분석 시 전량 교체)
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_stock_recommendations (
    id                BIGSERIAL PRIMARY KEY,
    "날짜"             DATE NOT NULL,
    code              TEXT NOT NULL,        -- 종목코드 6자리
    "종목"             TEXT NOT NULL,
    "SMA20"            FLOAT8,
    "SMA50"            FLOAT8,
    "골든_크로스"       BOOLEAN,
    "RSI"              FLOAT8,
    "MACD"             FLOAT8,
    "Signal"           FLOAT8,
    "MACD_매수_신호"    BOOLEAN,
    "추천_여부"         BOOLEAN,
    volume_ratio      FLOAT8,               -- 5일 평균 대비 거래량 배율
    adx               FLOAT8,               -- 추세 강도
    atr               FLOAT8,               -- 익절/손절선 산출용
    daily_change_pct  FLOAT8,               -- 당일 변동률 (패닉셀 판단)
    net_buy_5d        FLOAT8,               -- 외국인+기관 5일 누적 순매수 (백만원)
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kr_tech_code ON kr_stock_recommendations (code);
CREATE INDEX IF NOT EXISTS idx_kr_tech_date ON kr_stock_recommendations ("날짜" DESC);


-- ───────────────────────────────────────────────────────────────
-- 1-3) kr_stock_analysis_results — Kaggle ML(Transformer) 예측 결과
--      predict_kr.py 가 기록한다.
--      상장이 늦어 학습 구간(MIN_TRAIN_ROWS)을 확보 못한 종목은 여기서 빠진다.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_stock_analysis_results (
    id                     BIGSERIAL PRIMARY KEY,
    code                   TEXT,
    stock_name             TEXT NOT NULL,
    accuracy               FLOAT8,           -- 방향성 정확도 (100 - MAPE, %)
    rise_probability       FLOAT8,           -- 예측 상승률 (%)
    last_actual_price      FLOAT8,           -- 최근 실제 종가 (원)
    predicted_future_price FLOAT8,           -- 예측 미래가 (원)
    recommendation         TEXT,             -- STRONG BUY / BUY / SELL / No Data
    analysis               TEXT,
    created_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kr_ml_created ON kr_stock_analysis_results (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kr_ml_code    ON kr_stock_analysis_results (code);


-- ───────────────────────────────────────────────────────────────
-- 1-4) kr_ticker_sentiment_analysis — 네이버 뉴스 감성 (매 분석 시 전량 교체)
--      네이버는 감성 점수를 주지 않아 Claude 가 기사 텍스트를 직접 채점한다.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_ticker_sentiment_analysis (
    id                       BIGSERIAL PRIMARY KEY,
    code                     TEXT NOT NULL,
    stock_name               TEXT,
    sentiment_score          FLOAT8,   -- -1.0 ~ +1.0
    article_count            INT,      -- 관련 있다고 판단된 기사 수
    collected_article_count  INT,      -- 수집된 전체 기사 수
    summary                  TEXT,     -- 판단 근거 한 문장
    model                    TEXT,
    calculation_date         TEXT,
    created_at               TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kr_sentiment_code ON kr_ticker_sentiment_analysis (code);


-- ───────────────────────────────────────────────────────────────
-- 1-5) kr_news_articles — 감성 점수의 근거 기사 (사후 추적용, 날짜별 교체)
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_news_articles (
    id              BIGSERIAL PRIMARY KEY,
    collected_date  DATE NOT NULL,
    code            TEXT NOT NULL,
    stock_name      TEXT,
    title           TEXT,
    description     TEXT,
    link            TEXT,
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kr_news_date_code
    ON kr_news_articles (collected_date DESC, code);


-- ───────────────────────────────────────────────────────────────
-- 1-6) kr_buy_queue — 다음 개장일 매수 예약
--      한국 증시는 15:30 에 닫혀 장 마감 후 분석 시점엔 주문을 낼 수 없다.
--      분석 결과를 쌓아두고 다음 개장일 아침에 집행한다.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_buy_queue (
    id                BIGSERIAL PRIMARY KEY,
    queued_date       DATE NOT NULL,
    code              TEXT NOT NULL,
    stock_name        TEXT,
    composite_score   FLOAT8,
    rise_probability  FLOAT8,
    llm_reason        TEXT,
    atr               FLOAT8,
    -- pending(집행 대기) / executed(주문 접수) / skipped(조건 미달)
    -- failed(주문 실패) / expired(새 분석 또는 기한 초과로 폐기)
    status            TEXT NOT NULL DEFAULT 'pending',
    note              TEXT,
    account_type      TEXT NOT NULL DEFAULT 'mock',   -- mock / real
    executed_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kr_queue_pending
    ON kr_buy_queue (status, account_type, queued_date DESC);


-- ───────────────────────────────────────────────────────────────
-- 1-7) kr_trade_records — 국내 매매 기록 + ATR 익절/손절
--      금액 단위는 모두 원(KRW). 미국 트랙 trade_records(USD)와 분리.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_trade_records (
    id                 BIGSERIAL PRIMARY KEY,
    code               TEXT NOT NULL,
    stock_name         TEXT,
    buy_price          FLOAT8 NOT NULL,
    buy_date           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quantity           INT NOT NULL,
    holding_quantity   INT DEFAULT 0,      -- KIS 원장과 동기화되는 실제 보유수량
    atr                FLOAT8,
    take_profit_price  FLOAT8,             -- buy_price + ATR x 2.5
    stop_loss_price    FLOAT8,             -- buy_price - ATR x 1.5
    -- buy_ordered → holding → sell_ordered → sold
    -- buy_failed: 당일 미체결로 주문 실효
    status             TEXT NOT NULL DEFAULT 'buy_ordered',
    sell_price         FLOAT8,
    sell_date          TIMESTAMPTZ,
    sell_reason        TEXT,               -- take_profit / stop_loss / signal / panic_sell / flow_out
    profit_loss        FLOAT8,
    profit_loss_pct    FLOAT8,
    composite_score    FLOAT8,
    account_type       TEXT NOT NULL DEFAULT 'mock',
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kr_trades_status   ON kr_trade_records (status, account_type);
CREATE INDEX IF NOT EXISTS idx_kr_trades_code     ON kr_trade_records (code);
CREATE INDEX IF NOT EXISTS idx_kr_trades_buy_date ON kr_trade_records (buy_date DESC);


-- ───────────────────────────────────────────────────────────────
-- 1-7b) kr_trade_records 확장 — 부분익절 + 샹들리에 트레일링
--       status 에 'partial_sell_ordered' 값이 추가된다 (TEXT 자유값이라 스키마 변경 불필요).
--       진행: holding → (부분익절 도달 시) partial_sell_ordered → holding(partial_exit_done=TRUE)
--             → (샹들리에 이탈 또는 손절) sell_ordered → sold
-- ───────────────────────────────────────────────────────────────
ALTER TABLE kr_trade_records ADD COLUMN IF NOT EXISTS partial_exit_done     BOOLEAN DEFAULT FALSE;
ALTER TABLE kr_trade_records ADD COLUMN IF NOT EXISTS pending_partial_qty   INT;
ALTER TABLE kr_trade_records ADD COLUMN IF NOT EXISTS chandelier_peak_price FLOAT8;
ALTER TABLE kr_trade_records ADD COLUMN IF NOT EXISTS chandelier_stop_price FLOAT8;
ALTER TABLE kr_trade_records ADD COLUMN IF NOT EXISTS realized_partial_pnl  FLOAT8 DEFAULT 0;
ALTER TABLE kr_trade_records ADD COLUMN IF NOT EXISTS realized_partial_qty  INT DEFAULT 0;

-- kr_llm_sell_decision_logs 가 이미 존재하는 설치본을 위한 컬럼 추가 (신규 설치는 CREATE TABLE 에 이미 포함)
ALTER TABLE kr_llm_sell_decision_logs ADD COLUMN IF NOT EXISTS price_at_decision FLOAT8;
ALTER TABLE kr_llm_sell_decision_logs ADD COLUMN IF NOT EXISTS signal_count_at_decision INT;


-- ───────────────────────────────────────────────────────────────
-- 1-8) kr_llm_decision_logs — LLM 최종 검토 판단 기록
--      (decision_date, code) 유니크 → 같은 날 재실행 시 upsert
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_llm_decision_logs (
    id                BIGSERIAL PRIMARY KEY,
    decision_date     DATE NOT NULL,
    code              TEXT NOT NULL,
    stock_name        TEXT,
    decision          TEXT,        -- BUY / HOLD / FAIL / N/A
    reason            TEXT,
    market_analysis   TEXT,
    composite_score   FLOAT8,
    rise_probability  FLOAT8,
    rsi               FLOAT8,
    adx               FLOAT8,
    sentiment_score   FLOAT8,
    net_buy_5d        FLOAT8,
    kospi_vol_20d     FLOAT8,      -- 판단 시점 한국 공포지수
    usdkrw            FLOAT8,
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_kr_llm_decision UNIQUE (decision_date, code)
);

CREATE INDEX IF NOT EXISTS idx_kr_llm_date ON kr_llm_decision_logs (decision_date DESC);


-- ───────────────────────────────────────────────────────────────
-- 1-8b) kr_llm_sell_decision_logs — 보유 종목 LLM 매도 검토 판단 기록
--       append-only 로그다 — (decision_date, code) 당 여러 행이 쌓일 수 있다(장중 추가
--       매도검토가 같은 날 반복 실행되기 때문). 항상 INSERT 만 하고, 유니크 제약을 두지
--       않는다 — 그래야 하루 안에서 판단이 바뀐 과정(예: 11:05 HOLD -> 13:05 SELL_PARTIAL)이
--       덮어써지지 않고 그대로 남는다. 읽는 쪽은 (decision_date, code) 별 가장 최신
--       (created_at 기준) 행만 "오늘의 유효 판단"으로 취급한다.
--       기계적 손절/부분익절/샹들리에는 이 표와 무관하게 항상 별도로 실행된다.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_llm_sell_decision_logs (
    id                       BIGSERIAL PRIMARY KEY,
    decision_date            DATE NOT NULL,
    code                     TEXT NOT NULL,
    stock_name               TEXT,
    decision                 TEXT,        -- HOLD / SELL_ALL / SELL_PARTIAL / FAIL / N/A
    reason                   TEXT,
    market_analysis          TEXT,
    composite_score          FLOAT8,
    score_rank               INT,
    score_universe_size      INT,
    factor_reversals         TEXT,
    sentiment_score          FLOAT8,
    rotation_flag            BOOLEAN DEFAULT FALSE,
    rotation_candidate_code  TEXT,
    rotation_candidate_score FLOAT8,
    price_at_decision        FLOAT8,   -- 판단 시점 현재가 (집행 시점 재검증용)
    signal_count_at_decision INT,      -- 판단 시점 기술적 매도신호 개수 (집행 시점 근거 재검증용)
    -- pending(집행 대기) / executed(주문 접수) / skipped(기계적 처리와 충돌해 건너뜀) / expired(다음날 재검토로 폐기)
    status                   TEXT NOT NULL DEFAULT 'pending',
    executed_at              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ DEFAULT NOW()
);

-- 기존 설치본에 유니크 제약이 남아있으면 제거한다 (append-only 로 전환 — 위 설명 참조)
ALTER TABLE kr_llm_sell_decision_logs DROP CONSTRAINT IF EXISTS uq_kr_llm_sell_decision;
-- append-only 라 행이 다시 업데이트되지 않아 created_at 과 항상 같아지는 죽은 컬럼 — 제거
ALTER TABLE kr_llm_sell_decision_logs DROP COLUMN IF EXISTS updated_at;

CREATE INDEX IF NOT EXISTS idx_kr_llm_sell_date ON kr_llm_sell_decision_logs (decision_date DESC);
CREATE INDEX IF NOT EXISTS idx_kr_llm_sell_pending
    ON kr_llm_sell_decision_logs (status, decision_date DESC);
-- (decision_date, code) 별 "가장 최신 행" 조회 패턴 전용 인덱스
CREATE INDEX IF NOT EXISTS idx_kr_llm_sell_code_date_created
    ON kr_llm_sell_decision_logs (code, decision_date DESC, created_at DESC);


-- ───────────────────────────────────────────────────────────────
-- 1-9) kr_fear_gate_overrides — 변동성 게이트 수동 해제 이력
--      공포장 매수 차단을 운영자가 한시적으로 푼 기록 (감사 추적용).
--      expires_at 이 지나면 코드가 자동으로 expired 처리한다.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kr_fear_gate_overrides (
    id                     BIGSERIAL PRIMARY KEY,
    reason                 TEXT NOT NULL,        -- 개입 사유 (5자 이상 강제)
    relax_threshold        BOOLEAN DEFAULT FALSE,-- 적응형 임계값까지 완화했는지
    minutes                INT,
    fear_index_at_creation FLOAT8,               -- 발급 시점 코스피 20일 변동성
    status                 TEXT NOT NULL DEFAULT 'active',   -- active / expired
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at             TIMESTAMPTZ NOT NULL,
    closed_at              TIMESTAMPTZ,
    close_note             TEXT
);

CREATE INDEX IF NOT EXISTS idx_kr_override_active
    ON kr_fear_gate_overrides (status, created_at DESC);


-- ╔═════════════════════════════════════════════════════════════════════════╗
-- ║ 섹션 2. 컬럼 마이그레이션 (기존 테이블 대응)                              ║
-- ║                                                                          ║
-- ║ 위 CREATE TABLE 은 테이블이 이미 있으면 아무것도 하지 않는다. 그래서      ║
-- ║ 유니버스를 늘렸거나 예전 스키마로 만든 DB 를 위해 컬럼을 개별 추가한다.   ║
-- ║ 종목 수가 많아 ALTER 를 나열하는 대신 루프로 처리한다.                    ║
-- ╚═════════════════════════════════════════════════════════════════════════╝

DO $$
DECLARE
    col TEXT;
    stock_names TEXT[] := ARRAY[
        '삼성전자', 'SK하이닉스', 'SK스퀘어', '삼성전기', '현대차', 'LG에너지솔루션',
        '삼성바이오로직스', '삼성생명', '삼성물산', 'KB금융', '한화에어로스페이스', '기아',
        '신한지주', 'HD현대중공업', '두산에너빌리티', '현대모비스', '셀트리온', 'SK',
        '삼성SDI', '하나금융지주', 'NAVER', 'LG전자', '삼성화재', 'LS ELECTRIC',
        'HD현대일렉트릭', '고려아연', '한화오션', '효성중공업', 'HD한국조선해양', 'POSCO홀딩스',
        '우리금융지주', 'SK텔레콤', 'SK이노베이션', '한국전력', 'HMM', '한미반도체',
        '메리츠금융지주', '미래에셋증권', 'KT&G', 'LG화학', '삼성중공업', '삼성에스디에스',
        '두산', 'HD현대', 'LG', '기업은행', 'S-Oil', '카카오',
        'LIG디펜스앤에어로스페이스', '현대글로비스', '에이피알', '현대로템', '포스코퓨처엠', '한화시스템',
        'KT', 'LG이노텍', '한국항공우주', '현대오토에버', '현대건설', 'DB손해보험',
        'GS', '삼양식품', '한국금융지주', '크래프톤', '카카오뱅크', 'NH투자증권',
        '대한항공', 'LS', '포스코인터내셔널', 'HD현대마린솔루션', '삼성E&A', '삼성증권',
        '한국타이어앤테크놀로지', '아모레퍼시픽', '한진칼', '이수페타시스', '하이브', '한화솔루션',
        '키움증권', 'LG씨엔에스', '코웨이', '유한양행', 'SK바이오팜', '대우건설',
        'LG유플러스', '한화', '카카오페이', '두산밥캣', 'HD건설기계', '삼성카드',
        '한미약품', '대한전선', '대덕전자', '산일전기', 'JB금융지주', '오리온',
        'OCI홀딩스', 'NC', '한화생명', 'LG디스플레이'
    ];
BEGIN
    -- 종목 종가 컬럼
    FOREACH col IN ARRAY stock_names LOOP
        EXECUTE format(
            'ALTER TABLE kr_economic_and_stock_data ADD COLUMN IF NOT EXISTS %I FLOAT8', col
        );
    END LOOP;

    -- 지표 컬럼 (구버전 스키마에 없을 수 있는 것들)
    FOREACH col IN ARRAY ARRAY[
        '코스피 변동성 20일', '코스피', '코스피200', '코스닥', '원달러환율', '엔원환율',
        '필라델피아 반도체 지수', '미국 10년 국채금리', 'WTI 유가',
        '한국 기준금리', '국고채 3년', '국고채 10년', 'CD 91일',
        '한국 소비자물가지수', '한국 통화량 M2'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE kr_economic_and_stock_data ADD COLUMN IF NOT EXISTS %I FLOAT8', col
        );
    END LOOP;
END $$;

-- 기술 지표 테이블에 뒤늦게 추가된 컬럼
ALTER TABLE kr_stock_recommendations ADD COLUMN IF NOT EXISTS atr        FLOAT8;
ALTER TABLE kr_stock_recommendations ADD COLUMN IF NOT EXISTS net_buy_5d FLOAT8;


-- ╔═════════════════════════════════════════════════════════════════════════╗
-- ║ 섹션 3. 권한 (42501 / RLS 차단 방지)                                     ║
-- ║                                                                          ║
-- ║ GRANT 는 테이블 권한, RLS 는 그 위에 따로 걸리는 관문이다.                ║
-- ║ 서버가 anon 키로 접속하므로 둘 다 열어야 쓰기가 통과한다.                 ║
-- ║ 더 안전하게 가려면 이 섹션 대신 .env 에 SUPABASE_SERVICE_ROLE_KEY 를      ║
-- ║ 넣어라 — RLS 를 켠 채 서버만 우회한다 (app/db/supabase.py 가 자동 사용).  ║
-- ╚═════════════════════════════════════════════════════════════════════════╝

ALTER TABLE kr_economic_and_stock_data       DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_stock_recommendations         DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_stock_analysis_results        DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_ticker_sentiment_analysis     DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_news_articles                 DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_buy_queue                     DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_trade_records                 DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_llm_decision_logs             DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_llm_sell_decision_logs        DISABLE ROW LEVEL SECURITY;
ALTER TABLE kr_fear_gate_overrides           DISABLE ROW LEVEL SECURITY;

GRANT ALL ON ALL TABLES    IN SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;


-- ╔═════════════════════════════════════════════════════════════════════════╗
-- ║ 섹션 4. 설치 확인                                                        ║
-- ╚═════════════════════════════════════════════════════════════════════════╝

-- 4-1) 테이블 9개와 RLS 상태 (rowsecurity 가 모두 false 여야 한다)
SELECT tablename, rowsecurity AS "RLS켜짐"
FROM pg_tables
WHERE schemaname = 'public' AND tablename LIKE 'kr\_%'
ORDER BY tablename;

-- 4-2) 시장 데이터 컬럼 수 (지표 23 + 종목 100 + id/날짜/created_at = 126 예상)
SELECT COUNT(*) AS "컬럼수"
FROM information_schema.columns
WHERE table_name = 'kr_economic_and_stock_data';

-- 4-3) 적재 현황 (백필 전에는 모두 0)
SELECT 'kr_economic_and_stock_data'   AS "테이블", COUNT(*) AS "행수" FROM kr_economic_and_stock_data
UNION ALL SELECT 'kr_stock_recommendations',     COUNT(*) FROM kr_stock_recommendations
UNION ALL SELECT 'kr_stock_analysis_results',    COUNT(*) FROM kr_stock_analysis_results
UNION ALL SELECT 'kr_ticker_sentiment_analysis', COUNT(*) FROM kr_ticker_sentiment_analysis
UNION ALL SELECT 'kr_news_articles',             COUNT(*) FROM kr_news_articles
UNION ALL SELECT 'kr_buy_queue',                 COUNT(*) FROM kr_buy_queue
UNION ALL SELECT 'kr_trade_records',             COUNT(*) FROM kr_trade_records
UNION ALL SELECT 'kr_llm_decision_logs',         COUNT(*) FROM kr_llm_decision_logs
UNION ALL SELECT 'kr_llm_sell_decision_logs',    COUNT(*) FROM kr_llm_sell_decision_logs
UNION ALL SELECT 'kr_fear_gate_overrides',       COUNT(*) FROM kr_fear_gate_overrides;
