"""시점별(point-in-time) 신호 스냅샷 적재.

매수 파이프라인이 평가한 모든 종목의 팩터값·점수·게이트 결과를 그날 그대로
signal_snapshots 테이블에 누적 저장한다. 나중에 forward return 과 조인해
ML/감성/점수의 실제 예측력(알파)을 검증하기 위함.

설계 원칙:
  - 비치명적(non-fatal): 스냅샷 실패가 매매를 절대 막지 않음 (try/except 로 호출)
  - 멱등(idempotent): (snapshot_date, ticker, account_type) upsert → 당일 재실행 안전
  - 평가된 전체 후보 저장: 통과/탈락 모두 기록해야 "게이트가 알파를 더하는가" 비교 가능
"""
from datetime import datetime
import pytz

from app.db.supabase import supabase
from app.services.balance_service import current_account_type


def _f(v):
    """안전 float 변환 (None/빈값 → None)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _b(v):
    return bool(v) if v is not None else None


def snapshot_signals(evaluated: list, final_tickers: set, vix_value=None) -> int:
    """평가된 후보(evaluated)를 signal_snapshots 에 적재.

    Args:
        evaluated:      score_and_filter 에 넘긴 후보 리스트(점수 in-place 반영된 상태).
                        prefilter 통과 항목은 composite_score 보유, 탈락 항목은 없음.
        final_tickers:  임계값까지 통과한 최종 후보 티커 집합 (passed_threshold 판정용).
        vix_value:      당일 VIX.

    Returns:
        적재 행 수 (실패 시 0).
    """
    if not evaluated:
        return 0

    snap_date = datetime.now(pytz.timezone("America/New_York")).date().isoformat()
    acct = current_account_type()

    rows = []
    for c in evaluated:
        comp = c.get("composite_score")
        rows.append({
            "snapshot_date": snap_date,
            "ticker": c.get("ticker"),
            "stock_name": c.get("stock_name"),
            "account_type": acct,
            # ML
            "accuracy": _f(c.get("accuracy")),
            "rise_probability": _f(c.get("rise_probability")),
            "last_price": _f(c.get("last_price")),
            "predicted_price": _f(c.get("predicted_price")),
            # 기술적
            "golden_cross": _b(c.get("golden_cross")),
            "rsi": _f(c.get("rsi")),
            "macd": _f(c.get("macd")),
            "signal": _f(c.get("signal")),
            "sma20": _f(c.get("sma20")),
            "sma50": _f(c.get("sma50")),
            "macd_buy_signal": _b(c.get("macd_buy_signal")),
            "volume_ratio": _f(c.get("volume_ratio")),
            "adx": _f(c.get("adx")),
            "technical_recommended": _b(c.get("technical_recommended")),
            # 감성
            "sentiment_score": _f(c.get("sentiment_score")),
            "article_count": int(c["article_count"]) if c.get("article_count") not in (None, "") else None,
            # 점수/게이트
            "composite_score": _f(comp),
            "scoring_version": c.get("scoring_version"),
            "passed_prefilter": comp is not None,
            "passed_threshold": c.get("ticker") in final_tickers,
            # 시장
            "vix_value": _f(vix_value),
        })

    try:
        supabase.table("signal_snapshots").upsert(
            rows, on_conflict="snapshot_date,ticker,account_type"
        ).execute()
        print(f"  [snapshot] signal_snapshots 적재 {len(rows)}건 ({snap_date}, {acct})")
        return len(rows)
    except Exception as e:
        print(f"  [snapshot] 적재 실패(매매에는 영향 없음): {e}")
        return 0
