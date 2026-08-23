-- ═══════════════════════════════════════════════════════════════════════════
-- market_switches — 시장별(미국/국내) 신규 매수 원격 on/off
--
-- 켜기 전까지 계속 꺼진 채 유지되는 영속 스위치다(자정 자동복귀 없음).
-- 신규 매수만 막는다 — 매도 감시/KIS 원장 정합성 확인/데이터 수집은 이 스위치와 무관하게
-- 항상 그대로 돈다. 상태 하나만 필요하므로(이력 테이블 아님) 행을 upsert 로 그대로 갱신한다.
--
-- 멱등이라 몇 번을 다시 돌려도 안전하다.
-- 참조: app/services/market_switch_service.py
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS market_switches (
    market      TEXT PRIMARY KEY CHECK (market IN ('US', 'KR')),
    buy_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    reason      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 초기 상태: 두 시장 모두 매수 허용
INSERT INTO market_switches (market, buy_enabled)
VALUES ('US', TRUE), ('KR', TRUE)
ON CONFLICT (market) DO NOTHING;

ALTER TABLE market_switches DISABLE ROW LEVEL SECURITY;
GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated, service_role;

-- 설치 확인
SELECT * FROM market_switches ORDER BY market;
