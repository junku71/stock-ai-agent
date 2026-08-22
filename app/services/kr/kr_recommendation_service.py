"""
국내주식 기술적 지표 생성 + 매수/매도 후보 산출.

미국 트랙(app/services/stock_recommendation_service.py)과 같은 구조를 따르되,
지표 계산 수식은 그대로 재사용한다 (SMA/EMA/RSI/MACD/ATR/ADX 는 시장 무관).
KIS 국내 일봉의 필드명만 해외 일봉 형식으로 정규화해서 넘긴다.

매수 후보 = ML 예측 ∩ 기술적 지표 ∩ 감성 ∩ 수급 → kr_scoring 으로 채점
매도 후보 = ATR 익절/손절 ∪ 기술적 매도신호 ∪ 공포장 조건
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pytz

from app.core.config import settings
from app.db.supabase import supabase
from app.services.kr import kis_domestic_service as kis
from app.services.kr import kr_market_data_service, kr_override_service, kr_scoring, universe
# 지표 수식(SMA/EMA/RSI/MACD/ATR/ADX)은 시장 무관이라 미국 트랙 구현을 그대로 쓴다
from app.services.stock_recommendation_service import StockRecommendationService

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

TABLE_MARKET = "kr_economic_and_stock_data"
TABLE_TECH = "kr_stock_recommendations"
TABLE_ML = "kr_stock_analysis_results"
TABLE_SENTIMENT = "kr_ticker_sentiment_analysis"
TABLE_TRADES = "kr_trade_records"

LOOKBACK_DAYS = 180  # 기술적 지표 계산용 조회 기간

# ATR 배수 — 미국 트랙과 동일 (익절 2.5×ATR, 손절 1.5×ATR)
ATR_TAKE_PROFIT_MULT = 2.5
ATR_STOP_LOSS_MULT = 1.5

# ATR 정보가 없는 레코드용 고정 비율 폴백
FALLBACK_TAKE_PROFIT_PCT = 6.0
FALLBACK_STOP_LOSS_PCT = -7.0

_indicators = StockRecommendationService()


def _normalize_daily(rows: List[dict]) -> List[dict]:
    """
    KIS 국내 일봉 → 해외 일봉 필드명으로 정규화.
    calculate_atr / calculate_adx 가 high/low/clos/tvol/xymd 를 기대하기 때문.
    입력은 최신일 우선, 출력도 최신일 우선을 유지한다.
    """
    normalized = []
    for r in rows:
        try:
            close = float(r.get("stck_clpr", 0) or 0)
            if close <= 0:
                continue
            normalized.append(
                {
                    "xymd": r.get("stck_bsop_date", ""),
                    "clos": str(close),
                    "open": str(float(r.get("stck_oprc", 0) or 0)),
                    "high": str(float(r.get("stck_hgpr", 0) or 0)),
                    "low": str(float(r.get("stck_lwpr", 0) or 0)),
                    "tvol": str(int(float(r.get("acml_vol", 0) or 0))),
                }
            )
        except (ValueError, TypeError):
            continue
    return normalized


def compute_atr(code: str, days: int = 40) -> Optional[float]:
    """
    종목의 최근 ATR(14). 매수 직전 익절/손절선 산출에 쓴다.
    계산 불가 시 None — 호출부는 ATR 없이 매수하지 않는다.
    """
    try:
        daily = _normalize_daily(kis.get_recent_daily_chart(code, days=days))
        return _indicators.calculate_atr(daily) if daily else None
    except Exception as e:
        logger.warning(f"  {universe.display(code)} ATR 계산 실패: {e}")
        return None


def _net_buy_strength(code: str) -> Optional[float]:
    """
    최근 5거래일 외국인+기관 누적 순매수 대금(백만원).

    한국 시장에서 가장 검증된 단기 수급 신호. 조회 실패 시 None(중립).
    """
    rows = kis.get_stock_investor_trend(code)
    if not rows:
        return None

    total = 0.0
    counted = 0
    for r in rows[:5]:
        try:
            frgn = float(r.get("frgn_ntby_tr_pbmn", 0) or 0)
            orgn = float(r.get("orgn_ntby_tr_pbmn", 0) or 0)
        except (ValueError, TypeError):
            continue
        total += frgn + orgn
        counted += 1

    if counted == 0:
        return None
    return round(total, 1)


# ══════════════════════════════════════════════════════════════════
# 1) 기술적 지표 생성
# ══════════════════════════════════════════════════════════════════

def generate_technical_recommendations() -> dict:
    """
    KOSPI100 각 종목의 기술적 지표를 계산해 kr_stock_recommendations 에 저장한다.

    가격 시계열: kr_economic_and_stock_data (종가 기반 SMA/RSI/MACD)
    일봉 OHLCV : KIS API (ADX/ATR/거래량비율/당일변동률)
    수급        : KIS 투자자매매동향 (외국인+기관 5일 순매수)
    """
    start_date = (datetime.now(KST) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    quoted = [f'"{name}"' for name in universe.ALL_NAMES] + ['"날짜"']
    resp = (
        supabase.table(TABLE_MARKET)
        .select(*quoted)
        .gte("날짜", start_date)
        .order("날짜")
        .execute()
    )
    if not resp.data:
        return {"message": "가격 데이터가 없습니다. 먼저 시장 데이터를 수집하세요.", "data": []}

    df = pd.DataFrame(resp.data)
    df["날짜"] = pd.to_datetime(df["날짜"])
    df.set_index("날짜", inplace=True)
    df = df.apply(pd.to_numeric, errors="coerce")
    df.ffill(inplace=True)  # 휴장일 갭 (rolling 계산에 필수)
    df.bfill(inplace=True)

    latest_date = df.index[-1]
    recommendations = []

    for stock in universe.UNIVERSE:
        code, name = stock["code"], stock["name"]
        if name not in df.columns:
            logger.warning(f"  {universe.display(code)} 가격 컬럼 없음 — 스킵")
            continue

        prices = df[name]
        if prices.isna().all():
            logger.warning(f"  {universe.display(code)} 가격 전부 결측 — 스킵")
            continue

        sma20 = _indicators.calculate_sma(prices, 20)
        sma50 = _indicators.calculate_sma(prices, 50)
        golden_cross = sma20 > sma50
        rsi = _indicators.calculate_rsi(prices)
        macd, signal = _indicators.calculate_macd(prices)
        macd_buy = macd > signal
        rsi_val = rsi.iloc[-1] if not rsi.empty else 50
        recommended = bool(
            golden_cross[latest_date] and rsi_val <= 65 and macd_buy[latest_date]
        )

        # ── KIS 일봉 기반 지표 ──────────────────────────────
        volume_ratio = adx = atr = daily_change_pct = None
        try:
            raw = kis.get_recent_daily_chart(code, days=60)
            daily = _normalize_daily(raw)

            # 장 마감(15:30 KST) 전이면 당일 봉은 미완성이므로 제외한다.
            # 미완성 봉을 쓰면 거래량비율이 비정상적으로 낮게 나오고 ATR 도 왜곡된다.
            if daily and daily[0]["xymd"] == datetime.now(KST).strftime("%Y%m%d"):
                now = datetime.now(KST)
                market_closed = now.hour > 15 or (now.hour == 15 and now.minute >= 30)
                if not market_closed:
                    logger.info(f"  {universe.display(code)} 당일 봉 미완성 → 제외")
                    daily = daily[1:]

            if len(daily) >= 6:
                today_vol = int(daily[0]["tvol"])
                past = [int(d["tvol"]) for d in daily[1:6] if int(d["tvol"]) > 0]
                if past:
                    avg = sum(past) / len(past)
                    volume_ratio = round(today_vol / avg, 2) if avg > 0 else None

            if len(daily) >= 2:
                today_close = float(daily[0]["clos"])
                prev_close = float(daily[1]["clos"])
                if prev_close > 0:
                    daily_change_pct = round(
                        (today_close - prev_close) / prev_close * 100, 2
                    )

            if daily:
                adx = _indicators.calculate_adx(daily)
                atr = _indicators.calculate_atr(daily)
        except Exception as e:
            logger.warning(f"  {universe.display(code)} 일봉 지표 계산 실패: {e}")

        # ── 수급 ────────────────────────────────────────────
        try:
            net_buy = _net_buy_strength(code)
        except Exception as e:
            logger.warning(f"  {universe.display(code)} 수급 조회 실패: {e}")
            net_buy = None

        if pd.isna(sma20[latest_date]) or pd.isna(macd[latest_date]):
            logger.warning(f"  {universe.display(code)} 지표 계산 불가(데이터 부족) — 스킵")
            continue

        recommendations.append(
            {
                "날짜": latest_date.strftime("%Y-%m-%d"),
                "code": code,
                "종목": name,
                "SMA20": float(sma20[latest_date]),
                "SMA50": float(sma50[latest_date]),
                "골든_크로스": bool(golden_cross[latest_date]),
                "RSI": float(rsi[latest_date]),
                "MACD": float(macd[latest_date]),
                "Signal": float(signal[latest_date]),
                "MACD_매수_신호": bool(macd_buy[latest_date]),
                "추천_여부": recommended,
                "volume_ratio": volume_ratio,
                "adx": adx,
                "atr": atr,
                "daily_change_pct": daily_change_pct,
                "net_buy_5d": net_buy,
            }
        )
        logger.info(
            f"  {universe.display(code)} RSI={float(rsi[latest_date]):.1f} "
            f"GC={'O' if golden_cross[latest_date] else 'X'} "
            f"ADX={adx} vol={volume_ratio} 순매수5일={net_buy}"
        )

    if not recommendations:
        return {"message": "생성된 기술적 지표가 없습니다", "data": []}

    try:
        supabase.table(TABLE_TECH).delete().gte("날짜", "1900-01-01").execute()
        supabase.table(TABLE_TECH).insert(recommendations).execute()
    except Exception as e:
        logger.error(f"기술적 지표 저장 실패: {e}", exc_info=True)
        raise Exception(f"{TABLE_TECH} 저장 실패: {e}")

    return {
        "message": f"{len(recommendations)}개 종목의 기술적 지표를 생성했습니다",
        "data": recommendations,
    }


# ══════════════════════════════════════════════════════════════════
# 2) 매수 후보 통합
# ══════════════════════════════════════════════════════════════════

def _load_ml_predictions() -> Dict[str, dict]:
    """kr_stock_analysis_results (Kaggle ML 결과) → {code: row}. 최신 실행분만."""
    try:
        resp = (
            supabase.table(TABLE_ML)
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        logger.warning(f"ML 예측 결과 조회 실패: {e}")
        return {}

    latest: Dict[str, dict] = {}
    for row in resp.data or []:
        code = row.get("code") or universe.NAME_TO_CODE.get(row.get("stock_name", ""))
        if code and code not in latest:  # created_at 내림차순이라 첫 등장이 최신
            latest[code] = row
    return latest


def get_buy_candidates() -> dict:
    """
    ML + 기술 + 감성 + 수급 + 시장환경을 통합해 매수 후보를 반환한다.

    필터:
      - ML 정확도 >= KR_MIN_ML_ACCURACY (기본 80%) — 신뢰도 낮은 예측 배제
      - ML 예측 상승률 >= KR_MIN_RISE_PROBABILITY (기본 2%)
      - 코스피 20일 실현변동성 > FEAR_HARD_BLOCK → 매수 전면 중단 (공포장 하드블록)
        단, kr_override_service 의 수동 오버라이드가 활성이면 계속 진행한다
      - RSI > 80 하드블록, 기술 신호 2개 이상 (kr_scoring 사전 필터)
      - composite_score >= 변동성 적응형 임계값
    """
    tech_resp = supabase.table(TABLE_TECH).select("*").order("날짜", desc=True).execute()
    if not tech_resp.data:
        return {"message": "기술적 지표 데이터가 없습니다", "results": []}

    tech_map: Dict[str, dict] = {}
    for row in tech_resp.data:
        code = row.get("code")
        if code and code not in tech_map:
            tech_map[code] = row

    ml_map = _load_ml_predictions()

    try:
        sent_resp = supabase.table(TABLE_SENTIMENT).select("*").execute()
        sentiment_map = {r["code"]: r for r in (sent_resp.data or [])}
    except Exception as e:
        logger.warning(f"감성 데이터 조회 실패(중립 처리): {e}")
        sentiment_map = {}

    market = kr_market_data_service.get_market_context()
    fear_index = market.get("kospi_vol_20d")

    # 운영자가 명시적으로 발급한 한시적 게이트 해제가 있는지 확인
    override = kr_override_service.get_active_override()
    market["override"] = kr_override_service.describe()

    # 공포장 하드블록
    if fear_index is not None and fear_index > kr_scoring.FEAR_HARD_BLOCK:
        if override is None:
            msg = (
                f"코스피 20일 실현변동성 {fear_index:.1f}% > "
                f"{kr_scoring.FEAR_HARD_BLOCK:.0f}% — 공포장으로 매수를 중단합니다"
            )
            logger.warning(f"  {msg}")
            return {"message": msg, "results": [], "market": market}

        logger.warning(
            f"  ⚠️ 변동성 {fear_index:.1f}% 로 하드블록 대상이지만 "
            f"수동 오버라이드가 활성이라 매수 후보 산출을 계속합니다 "
            f"(사유: {override.get('reason')})"
        )

    candidates = []
    dropped = {"no_ml": 0, "low_accuracy": 0, "low_rise": 0}

    for code, tech in tech_map.items():
        ml = ml_map.get(code)
        if not ml:
            dropped["no_ml"] += 1
            continue  # ML 예측이 없으면 후보에서 제외 (미국 트랙과 동일 정책)

        try:
            rise_probability = float(ml.get("rise_probability") or 0)
        except (ValueError, TypeError):
            dropped["no_ml"] += 1
            continue

        # ML 신뢰도 하한 — 정확도(100 - MAPE)가 낮은 예측은 상승률이 커도 믿을 수 없다.
        # accuracy 가 비어 있으면 신뢰도를 알 수 없으므로 보수적으로 제외한다.
        if settings.KR_MIN_ML_ACCURACY > 0:
            try:
                accuracy = float(ml.get("accuracy"))
            except (ValueError, TypeError):
                accuracy = None
            if accuracy is None or accuracy < settings.KR_MIN_ML_ACCURACY:
                dropped["low_accuracy"] += 1
                continue

        if rise_probability < settings.KR_MIN_RISE_PROBABILITY:
            dropped["low_rise"] += 1
            continue

        sentiment = sentiment_map.get(code)

        def _f(v, default=None):
            try:
                return None if v is None else float(v)
            except (ValueError, TypeError):
                return default

        candidates.append(
            {
                "code": code,
                "ticker": code,  # 공통 모듈 호환용 별칭
                "stock_name": tech.get("종목") or universe.CODE_TO_NAME.get(code, code),
                "sector": universe.CODE_TO_SECTOR.get(code, ""),
                # ML
                "accuracy": _f(ml.get("accuracy")),
                "rise_probability": rise_probability,
                "last_price": _f(ml.get("last_actual_price")),
                "predicted_price": _f(ml.get("predicted_future_price")),
                "analysis": ml.get("analysis"),
                # 기술
                "technical_date": tech.get("날짜"),
                "sma20": _f(tech.get("SMA20"), 0.0),
                "sma50": _f(tech.get("SMA50"), 0.0),
                "golden_cross": bool(tech.get("골든_크로스")),
                "rsi": _f(tech.get("RSI"), 50.0),
                "macd": _f(tech.get("MACD"), 0.0),
                "signal": _f(tech.get("Signal"), 0.0),
                "macd_buy_signal": bool(tech.get("MACD_매수_신호")),
                "technical_recommended": bool(tech.get("추천_여부")),
                "volume_ratio": _f(tech.get("volume_ratio")),
                "adx": _f(tech.get("adx")),
                "atr": _f(tech.get("atr")),
                "daily_change_pct": _f(tech.get("daily_change_pct")),
                # 수급 (kr_scoring 이 z-score 화)
                "net_buy_score": _f(tech.get("net_buy_5d")),
                # 감성
                "sentiment_score": _f(sentiment.get("sentiment_score")) if sentiment else None,
                "article_count": (sentiment or {}).get("article_count"),
                "sentiment_summary": (sentiment or {}).get("summary"),
            }
        )

    logger.info(
        f"  ML 필터: {len(tech_map)}종목 중 {len(candidates)}개 통과 "
        f"(ML예측없음 {dropped['no_ml']} / "
        f"정확도<{settings.KR_MIN_ML_ACCURACY:.0f}% {dropped['low_accuracy']} / "
        f"상승률<{settings.KR_MIN_RISE_PROBABILITY:.0f}% {dropped['low_rise']})"
    )

    if not candidates:
        return {
            "message": (
                f"ML 예측을 통과한 종목이 없습니다 "
                f"(정확도 하한 {settings.KR_MIN_ML_ACCURACY:.0f}%, "
                f"상승률 하한 {settings.KR_MIN_RISE_PROBABILITY:.0f}%)"
            ),
            "results": [],
            "market": market,
            "ml_filter": dropped,
        }

    # relax_threshold 오버라이드면 변동성에 따른 선별도 상향까지 끄고 평온장 기준으로 본다.
    # (하드블록만 푸는 기본 오버라이드와 달리, 파이프라인 전 구간을 관통시켜 볼 때 쓴다)
    scoring_fear = fear_index
    if override is not None and override.get("relax_threshold"):
        scoring_fear = None
        logger.warning("  ⚠️ 오버라이드(relax_threshold) — 임계값을 평온장 기준으로 낮춥니다")

    final = kr_scoring.score_and_filter(candidates, scoring_fear)

    threshold = kr_scoring.get_threshold(scoring_fear)
    logger.info(
        f"  매수 후보 채점: {len(candidates)}개 중 {len(final)}개 통과 "
        f"(임계값 {threshold:.2f}, 변동성 {fear_index})"
    )
    for c in final:
        logger.info(f"    {universe.display(c['code'])} score={c['composite_score']:+.4f}")

    return {
        "message": f"{len(final)}개의 매수 후보를 찾았습니다",
        "results": final,
        "market": market,
        "ml_filter": dropped,
        "scored_count": len(candidates),
        "threshold": threshold,
    }


# ══════════════════════════════════════════════════════════════════
# 3) 매도 후보
# ══════════════════════════════════════════════════════════════════

def get_sell_candidates(balance: Optional[dict] = None) -> dict:
    """
    보유 종목 중 매도 대상 식별.

    조건 1: ATR 기반 익절/손절 (kr_trade_records 기준, 없으면 고정 비율 폴백)
    조건 2: 기술적 매도 신호 (데드크로스 / RSI>70 / MACD 매도 / 패닉셀)
            - ADX>25 면 필요 신호 수 1개 차감
            - 2a: 감성 < -0.15 이면 신호 2개(ADX 보정 시 1개)로 매도
            - 2b: 신호 3개(ADX 보정 시 2개)면 매도
    조건 3: 공포장 — 변동성>30 + 신호2개, 변동성>40 + 신호1개
    """
    if balance is None:
        balance = kis.get_balance()

    if balance.get("rt_cd") != "0":
        return {
            "message": f"잔고 조회 실패: {balance.get('msg1', '')}",
            "sell_candidates": [],
        }

    holdings = balance.get("output1", [])
    if not holdings:
        return {"message": "보유 종목이 없습니다", "sell_candidates": []}

    # 기술적 지표
    tech_map: Dict[str, dict] = {}
    try:
        tech_resp = supabase.table(TABLE_TECH).select("*").order("날짜", desc=True).execute()
        for row in tech_resp.data or []:
            code = row.get("code")
            if code and code not in tech_map:
                tech_map[code] = row
    except Exception as e:
        logger.warning(f"매도 판단용 기술 지표 조회 실패: {e}")

    # 감성
    sentiment_map: Dict[str, dict] = {}
    try:
        sent_resp = supabase.table(TABLE_SENTIMENT).select("*").execute()
        sentiment_map = {r["code"]: r for r in (sent_resp.data or [])}
    except Exception as e:
        logger.warning(f"매도 판단용 감성 조회 실패: {e}")

    # 시장 환경
    market = kr_market_data_service.get_market_context()
    fear_index = market.get("kospi_vol_20d")

    # 매수 시점의 ATR 익절/손절선
    trade_map: Dict[str, dict] = {}
    try:
        tr_resp = (
            supabase.table(TABLE_TRADES)
            .select("*")
            .eq("status", "holding")
            .eq("account_type", kis.current_account_type())
            .execute()
        )
        for tr in tr_resp.data or []:
            trade_map[tr["code"]] = tr
    except Exception as e:
        logger.warning(f"kr_trade_records 조회 실패 (고정비율 폴백): {e}")

    sell_candidates = []

    for item in holdings:
        code = item.get("pdno", "")
        name = item.get("prdt_name", code)
        try:
            quantity = int(item.get("ord_psbl_qty", 0) or 0)
            buy_price = float(item.get("pchs_avg_pric", 0) or 0)
            current_price = float(item.get("prpr", 0) or 0)
        except (ValueError, TypeError):
            continue

        if quantity <= 0 or current_price <= 0:
            continue

        change_pct = ((current_price - buy_price) / buy_price * 100) if buy_price > 0 else 0.0
        reasons: List[str] = []

        # ── 조건 1: ATR 익절/손절 ─────────────────────────
        trade = trade_map.get(code)
        if trade and trade.get("take_profit_price") and trade.get("stop_loss_price"):
            tp = float(trade["take_profit_price"])
            sl = float(trade["stop_loss_price"])
            if current_price >= tp:
                reasons.append(
                    f"ATR 익절: 현재가 {current_price:,.0f}원 >= 익절가 {tp:,.0f}원 "
                    f"(매입가 대비 {change_pct:+.2f}%)"
                )
            elif current_price <= sl:
                reasons.append(
                    f"ATR 손절: 현재가 {current_price:,.0f}원 <= 손절가 {sl:,.0f}원 "
                    f"(매입가 대비 {change_pct:+.2f}%)"
                )
        else:
            if change_pct >= FALLBACK_TAKE_PROFIT_PCT:
                reasons.append(f"익절(고정비율): 매입가 대비 {change_pct:+.2f}%")
            elif change_pct <= FALLBACK_STOP_LOSS_PCT:
                reasons.append(f"손절(고정비율): 매입가 대비 {change_pct:+.2f}%")

        # ── 조건 2: 기술적 매도 신호 ──────────────────────
        tech = tech_map.get(code)
        signal_count = 0
        signal_details: List[str] = []
        adx_value = None

        if tech:
            if not tech.get("골든_크로스"):
                signal_count += 1
                signal_details.append("데드 크로스")

            try:
                rsi_val = float(tech.get("RSI", 50) or 50)
                if rsi_val > 70:
                    signal_count += 1
                    signal_details.append(f"RSI 과매수({rsi_val:.1f})")
            except (ValueError, TypeError):
                pass

            if not tech.get("MACD_매수_신호"):
                signal_count += 1
                signal_details.append("MACD 매도 신호")

            # 패닉셀: 거래량 2배 이상 + 당일 -3% 이상
            try:
                vr = tech.get("volume_ratio")
                dc = tech.get("daily_change_pct")
                if vr is not None and dc is not None and float(vr) >= 2.0 and float(dc) <= -3:
                    signal_count += 1
                    signal_details.append(
                        f"패닉셀(거래량 {float(vr):.1f}배, 당일 {float(dc):.1f}%)"
                    )
            except (ValueError, TypeError):
                pass

            # 수급 이탈: 외국인+기관 5일 순매도 (한국 시장 전용 신호)
            try:
                nb = tech.get("net_buy_5d")
                if nb is not None and float(nb) < 0:
                    signal_count += 1
                    signal_details.append(f"외국인·기관 5일 순매도({float(nb):,.0f}백만원)")
            except (ValueError, TypeError):
                pass

            try:
                adx_value = float(tech["adx"]) if tech.get("adx") is not None else None
            except (ValueError, TypeError):
                adx_value = None

        adx_adj = 1 if adx_value is not None and adx_value > 25 else 0
        adx_note = f", ADX={adx_value:.1f} 보정" if adx_adj else ""

        sentiment = sentiment_map.get(code)
        sentiment_score = None
        if sentiment:
            try:
                sentiment_score = float(sentiment.get("sentiment_score"))
            except (ValueError, TypeError):
                sentiment_score = None

        required_3 = 3 - adx_adj
        if signal_count >= required_3:
            reasons.append(
                f"기술적 매도 신호 {signal_count}/{required_3}개: "
                f"{', '.join(signal_details)}{adx_note}"
            )
        elif sentiment_score is not None and sentiment_score < -0.15:
            required_2 = 2 - adx_adj
            if signal_count >= required_2:
                reasons.append(
                    f"부정적 감성({sentiment_score:.2f}) + 매도 신호 "
                    f"{signal_count}/{required_2}개: {', '.join(signal_details)}{adx_note}"
                )

        # ── 조건 3: 공포장 ────────────────────────────────
        if fear_index is not None and signal_count >= 1:
            if fear_index > 40:
                reasons.append(
                    f"극단적 공포(변동성 {fear_index:.1f}%) + 매도 신호 {signal_count}개: "
                    f"{', '.join(signal_details)}"
                )
            elif fear_index > 30 and signal_count >= 2:
                reasons.append(
                    f"공포 시장(변동성 {fear_index:.1f}%) + 매도 신호 {signal_count}개: "
                    f"{', '.join(signal_details)}"
                )

        if reasons:
            sell_candidates.append(
                {
                    "code": code,
                    "stock_name": name,
                    "quantity": quantity,
                    "buy_price": buy_price,
                    "current_price": current_price,
                    "price_change_percent": round(change_pct, 2),
                    "sell_reasons": reasons,
                    "technical_sell_signals": signal_count,
                    "technical_sell_details": signal_details or None,
                    "sentiment_score": sentiment_score,
                    "adx": adx_value,
                    "fear_index": fear_index,
                }
            )

    sell_candidates.sort(key=lambda x: abs(x["price_change_percent"]), reverse=True)
    return {
        "message": f"{len(sell_candidates)}개의 매도 대상을 식별했습니다",
        "sell_candidates": sell_candidates,
    }
