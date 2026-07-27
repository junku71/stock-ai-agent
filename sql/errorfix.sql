-- 0) 테이블 권한(GRANT) 부여
-- 최근 생성된 Supabase 프로젝트는 public 스키마 테이블에 anon/authenticated 기본 GRANT가
-- 없는 경우가 있어 "permission denied for table ..." (42501, HTTP 403) 에러가 발생한다.
-- 이 에러는 RLS 문제가 아니므로 아래 GRANT를 반드시 함께 실행해야 한다.
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;
-- 앞으로 새로 만들 테이블/시퀀스에도 자동 적용
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;

-- 1) RLS 비활성화
ALTER TABLE IF EXISTS economic_and_stock_data DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS stock_analysis_results DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS stock_recommendations DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS ticker_sentiment_analysis DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS access_tokens DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS stocks DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS predicted_stocks DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS trade_records DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS llm_decision_logs DISABLE ROW LEVEL SECURITY;

-- 2) public 스키마의 모든 테이블에 대해 id 시퀀스를 max(id) + 1 로 재동기화
-- 예를 들어 economic_and_stock_data 테이블의 마지막 행이 id=2865라면 시퀀스를 2866으로 reset 
-- → 다음 INSERT 시 충돌 안 남.
DO $$
DECLARE
    r RECORD;
    seq_name TEXT;
BEGIN
    FOR r IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        seq_name := pg_get_serial_sequence('public.' || quote_ident(r.tablename), 'id');
        IF seq_name IS NOT NULL THEN
            EXECUTE format(
                'SELECT setval(%L, COALESCE((SELECT MAX(id) FROM public.%I), 0) + 1, false)',
                seq_name, r.tablename
            );
        END IF;
    END LOOP;
END $$;