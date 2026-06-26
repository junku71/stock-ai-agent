-- signal_snapshots: 시점별(point-in-time) 매수 의사결정 입력/출력 스냅샷
-- ─────────────────────────────────────────────────────────────────────────
-- 목적: 매일 매수 파이프라인이 평가한 모든 종목의 팩터값·점수·게이트 통과여부를
--       그날 그대로 보존 → 나중에 forward return 과 조인해 "ML/감성/점수가 실제로
--       알파를 더하는가"를 검증(backtest/snapshot_analysis.py).
-- 기존 테이블(stock_recommendations, ticker_sentiment_analysis)은 매일 덮어써서
-- 과거 시점 예측이 사라지는 문제 → 본 테이블은 append/upsert 로 누적 보존.
--
-- 조인 키: (snapshot_date, ticker)
--   - LLM 최종 판정 → llm_decision_logs (decision_date, ticker)
--   - 실제 매수      → trade_records (buy_date, ticker)
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signal_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_date   DATE        NOT NULL,          -- NY 기준 평가 일자
    ticker          TEXT        NOT NULL,
    stock_name      TEXT,
    account_type    TEXT        NOT NULL DEFAULT 'real',

    -- ML (Transformer) 예측
    accuracy            FLOAT8,                     -- 모델 정확도(%)
    rise_probability    FLOAT8,                     -- 예측 상승확률/상승률(%)
    last_price          FLOAT8,
    predicted_price     FLOAT8,

    -- 기술적 지표 (시점별 보존)
    golden_cross        BOOLEAN,
    rsi                 FLOAT8,
    macd                FLOAT8,
    signal              FLOAT8,
    sma20               FLOAT8,
    sma50               FLOAT8,
    macd_buy_signal     BOOLEAN,
    volume_ratio        FLOAT8,
    adx                 FLOAT8,
    technical_recommended BOOLEAN,

    -- 뉴스 감성
    sentiment_score     FLOAT8,                     -- average_sentiment_score
    article_count       INTEGER,

    -- 점수화 / 게이트 결과
    composite_score     FLOAT8,                     -- NULL = prefilter 탈락(점수 미산출)
    scoring_version     TEXT,                       -- 'v1' | 'v2'
    passed_prefilter    BOOLEAN NOT NULL DEFAULT FALSE,  -- RSI≤80 & 기술신호 2개↑
    passed_threshold    BOOLEAN NOT NULL DEFAULT FALSE,  -- composite ≥ 임계값 (최종 후보)

    -- 시장 상태
    vix_value           FLOAT8,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 같은 날 재실행 시 중복 방지 (idempotent upsert 키)
    CONSTRAINT uq_signal_snapshot UNIQUE (snapshot_date, ticker, account_type)
);

CREATE INDEX IF NOT EXISTS idx_signal_snapshots_date   ON signal_snapshots (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_signal_snapshots_ticker ON signal_snapshots (ticker);

-- RLS 비활성화: 서버(서비스 키)가 직접 읽고 쓰므로 다른 테이블과 동일하게 RLS off.
-- (RLS 켜진 상태면 클라이언트의 insert/select 가 정책에 막힐 수 있음)
ALTER TABLE signal_snapshots DISABLE ROW LEVEL SECURITY;
