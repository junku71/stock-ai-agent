-- trade_records 테이블에 트레일링 스톱(Chandelier Exit)용 고점 추적 컬럼 추가
-- trail_high: 진입 후 도달한 최고가 (매도 폴링 1분 주기마다 max 로 갱신)
--   손절선(stop_loss_price) = trail_high - ATR × ATR_TRAIL_MULT(=3.0) 으로 동반 상향
--   고정 익절(take_profit_price)은 더 이상 사용하지 않음(NULL) → 트레일링 스톱이 청산 담당
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS trail_high FLOAT8;

-- 기존 보유중(holding) 종목 백필: 고점 앵커를 매수가로 초기화하고 손절선 재계산
-- (애플리케이션의 get_stocks_to_sell 백필 로직이 ATR까지 다시 채우므로 아래는 안전망)
UPDATE trade_records
SET trail_high = buy_price
WHERE status IN ('holding', 'buy_ordered', 'sell_ordered')
  AND trail_high IS NULL
  AND buy_price IS NOT NULL;
