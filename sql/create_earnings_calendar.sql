-- ============================================================
-- 실적 캘린더(Earnings Calendar) 연동 — DDL
-- 기획: documents/17_실적캘린더_연동_기획.md
-- ============================================================

-- 1) 실적 캘린더 저장 테이블
create table if not exists earnings_calendar (
    id bigint generated always as identity primary key,
    ticker text not null,
    company_name text,
    report_date date not null,
    fiscal_date_ending date,
    eps_estimate numeric,          -- estimate 빈 값이면 null
    currency text,
    time_of_day text,              -- timeOfTheDay (pre/post-market, 보통 빈 값)
    fetched_at timestamptz not null default now(),
    unique (ticker, report_date)
);
create index if not exists idx_earnings_ticker_date on earnings_calendar (ticker, report_date);

-- 2) LLM 결정 로그에 실적 스냅샷 컬럼 추가 (사후 추적용)
alter table llm_decision_logs add column if not exists earnings_date date;
alter table llm_decision_logs add column if not exists days_to_earnings integer;
alter table llm_decision_logs add column if not exists earnings_estimate numeric;
