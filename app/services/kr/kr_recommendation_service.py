"""
국내주식 기술적 지표 생성 + 매수/매도 후보 산출.

미국 트랙(app/services/stock_recommendation_service.py)과 같은 구조를 따르되,
지표 계산 수식은 그대로 재사용한다 (SMA/EMA/RSI/MACD/ATR/ADX 는 시장 무관).
KIS 국내 일봉의 필드명만 해외 일봉 형식으로 정규화해서 넘긴다.

매수 후보 = ML 예측 ∩ 기술적 지표 ∩ 감성 ∩ 수급 → kr_scoring 으로 채점

매도는 두 층으로 나뉜다:
  기계적(get_mechanical_sell_candidates) = ATR 손절/부분익절+샹들리에 트레일링 ∪ 기술적 매도신호 ∪ 공포장 조건
  LLM 정성적(get_llm_sell_context + kr_llm_sell_review_service) = 점수감쇠 ∪ 교체매매 후보
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pytz

from app.core.config import settings
from app.db.supabase import supabase
from app.services import market_switch_service
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


def _kis_technical_snapshot(code: str) -> dict:
    """
    KIS 일봉 기반 지표(거래량비율/ADX/ATR/당일변동률) 한 종목분.

    generate_technical_recommendations()(유니버스 100종목)와 get_scored_universe()
    (유니버스 밖 보유종목 포함)가 공유한다.
    """
    result = {"volume_ratio": None, "adx": None, "atr": None, "daily_change_pct": None}
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
                result["volume_ratio"] = round(today_vol / avg, 2) if avg > 0 else None

        if len(daily) >= 2:
            today_close = float(daily[0]["clos"])
            prev_close = float(daily[1]["clos"])
            if prev_close > 0:
                result["daily_change_pct"] = round(
                    (today_close - prev_close) / prev_close * 100, 2
                )

        if daily:
            result["adx"] = _indicators.calculate_adx(daily)
            result["atr"] = _indicators.calculate_atr(daily)
    except Exception as e:
        logger.warning(f"  {universe.display(code)} 일봉 지표 계산 실패: {e}")
    return result


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
        snap = _kis_technical_snapshot(code)
        volume_ratio = snap["volume_ratio"]
        adx = snap["adx"]
        atr = snap["atr"]
        daily_change_pct = snap["daily_change_pct"]

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


def _load_candidate_maps() -> "tuple[Dict[str, dict], Dict[str, dict], Dict[str, dict]]":
    """(tech_map, ml_map, sentiment_map) — get_buy_candidates()/get_scored_universe() 공용."""
    tech_resp = supabase.table(TABLE_TECH).select("*").order("날짜", desc=True).execute()
    tech_map: Dict[str, dict] = {}
    for row in tech_resp.data or []:
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

    return tech_map, ml_map, sentiment_map


def _f(v, default=None):
    try:
        return None if v is None else float(v)
    except (ValueError, TypeError):
        return default


def _build_candidate_dict(
    code: str, tech: dict, ml: Optional[dict], sentiment: Optional[dict]
) -> dict:
    """tech/ml/sentiment 원본 행 → 채점용 후보 dict. ml 이 None 이어도 동작한다(점수용 유니버스)."""
    ml = ml or {}
    return {
        "code": code,
        "ticker": code,  # 공통 모듈 호환용 별칭
        "stock_name": tech.get("종목") or universe.CODE_TO_NAME.get(code, code),
        "sector": universe.CODE_TO_SECTOR.get(code, ""),
        # ML
        "accuracy": _f(ml.get("accuracy")),
        "rise_probability": _f(ml.get("rise_probability"), 0.0) or 0.0,
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
    tech_map, ml_map, sentiment_map = _load_candidate_maps()
    if not tech_map:
        return {"message": "기술적 지표 데이터가 없습니다", "results": []}

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

        candidates.append(_build_candidate_dict(code, tech, ml, sentiment_map.get(code)))

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
# 3) 점수 유니버스 (보유종목 점수감쇠 판단용 — 임계값 컷 없음)
# ══════════════════════════════════════════════════════════════════

def get_scored_universe(extra_codes: Optional[List[str]] = None) -> dict:
    """
    유니버스 전체(정확도/상승률 프리필터·임계값 컷 없이) cross-sectional 채점.

    get_buy_candidates() 는 오늘의 매수 자격이 있는 종목만 반환하지만, 보유종목의 점수감쇠를
    보려면 필터를 통과했는지와 무관하게 전체 피어 그룹 내 상대 위치가 필요하다.
    extra_codes 는 유니버스 밖 보유종목 — SMA/RSI/MACD 는 계산할 수 없어 중립값으로 두고
    ADX/거래량/ATR/감성만 반영한 부분 점수를 매긴다.
    """
    tech_map, ml_map, sentiment_map = _load_candidate_maps()
    market = kr_market_data_service.get_market_context()
    fear_index = market.get("kospi_vol_20d")

    candidates = [
        _build_candidate_dict(code, tech, ml_map.get(code), sentiment_map.get(code))
        for code, tech in tech_map.items()
    ]

    for code in extra_codes or []:
        if code in tech_map:
            continue
        snap = _kis_technical_snapshot(code)
        pseudo_tech = {
            "종목": universe.CODE_TO_NAME.get(code, code),
            "SMA20": 0.0,
            "SMA50": 0.0,
            "골든_크로스": False,
            "RSI": 50.0,
            "MACD": 0.0,
            "Signal": 0.0,
            "MACD_매수_신호": False,
            "volume_ratio": snap["volume_ratio"],
            "adx": snap["adx"],
            "atr": snap["atr"],
            "daily_change_pct": snap["daily_change_pct"],
            "net_buy_5d": None,
        }
        cand = _build_candidate_dict(code, pseudo_tech, ml_map.get(code), sentiment_map.get(code))
        cand["score_note"] = "유니버스 밖 — SMA/RSI/MACD 미포함(중립값), ADX/거래량/ATR/감성만 반영"
        candidates.append(cand)

    if not candidates:
        return {"scored": {}, "market": market, "asof": datetime.now(KST).strftime("%Y-%m-%d")}

    kr_scoring.compute_scores(candidates, fear_index)
    candidates.sort(key=lambda c: c["composite_score"], reverse=True)
    for i, c in enumerate(candidates, 1):
        c["rank"] = i

    return {
        "scored": {c["code"]: c for c in candidates},
        "universe_size": len(candidates),
        "market": market,
        "asof": datetime.now(KST).strftime("%Y-%m-%d"),
    }


# ══════════════════════════════════════════════════════════════════
# 4) 매도 후보
# ══════════════════════════════════════════════════════════════════

def _technical_sell_signals(
    tech: Optional[dict],
    sentiment: Optional[dict],
    fear_index: Optional[float],
    change_pct: Optional[float] = None,
) -> dict:
    """
    기술적 매도 신호(데드크로스/RSI/MACD/패닉셀/수급이탈) + 공포장 조건 계산.

    get_mechanical_sell_candidates()(기계적 자동매도, 조건 2/3)와 get_llm_sell_context()
    (LLM 정성적 컨텍스트)가 동일한 계산을 공유한다 — 두 곳의 신호값이 어긋나지 않게 하기 위함.

    change_pct 는 매입가 대비 등락률(%)로, 공포장 조건(조건 3)의 손실 게이트에만 쓴다.
    주지 않으면(None) 손실 게이트 없이 종전대로 판정한다 — 모르면 보호 쪽으로 실패한다.
    """
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

    sentiment_score = None
    if sentiment:
        try:
            sentiment_score = float(sentiment.get("sentiment_score"))
        except (ValueError, TypeError):
            sentiment_score = None

    reasons: List[str] = []
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

    # ── 공포장 ────────────────────────────────────────────
    # 극단 40→60, 완화 30→40 으로 문턱을 올렸다 — 종전 문턱은 "불안한 정도"에도 신호 1개로
    # 전량매도가 나가버려, 같은 원본 신호를 보고 더 정교하게 판단하는 LLM 의 SELL_PARTIAL 같은
    # 선택지가 실행 기회조차 못 얻는 경우가 많았다. 진짜 극단적 국면에서만 기계적으로 개입한다.
    #
    # 손실 게이트 — 조건 3 은 '손실 중인 포지션'에만 적용한다. 규칙의 취지는 패닉 국면에서
    # 위험을 줄이는 것이지, 본전이거나 수익 중인 포지션을 국면만 보고 털어내는 게 아니다.
    # (2026-08-24 HMM: 22,650원 매수 → 22,650원 매도, 손익 0원으로 청산됐다. 손절가
    # 21,300원 근처에도 가지 않았고 조건 3 하나만으로 나간 주문이었다.)
    # 문턱은 KR_FEAR_SELL_LOSS_PCT(기본 3.0 → -3.00% 이하)로 조정한다.
    loss_gate_open = (
        change_pct is None or change_pct <= -settings.KR_FEAR_SELL_LOSS_PCT
    )
    if fear_index is not None and signal_count >= 1 and loss_gate_open:
        loss_note = f", 매입가 대비 {change_pct:+.2f}%" if change_pct is not None else ""
        if fear_index > 60:
            reasons.append(
                f"극단적 공포(변동성 {fear_index:.1f}%) + 매도 신호 {signal_count}개: "
                f"{', '.join(signal_details)}{loss_note}"
            )
        elif fear_index > 40 and signal_count >= 2:
            reasons.append(
                f"공포 시장(변동성 {fear_index:.1f}%) + 매도 신호 {signal_count}개: "
                f"{', '.join(signal_details)}{loss_note}"
            )

    return {
        "reasons": reasons,
        "signal_count": signal_count,
        "signal_details": signal_details,
        "adx_value": adx_value,
        "sentiment_score": sentiment_score,
    }


def _reason_code_from_signal_details(signal_details: List[str]) -> str:
    joined = " ".join(signal_details)
    if "패닉셀" in joined:
        return "panic_sell"
    if "순매도" in joined:
        return "flow_out"
    return "signal"


def get_mechanical_sell_candidates(balance: Optional[dict] = None) -> dict:
    """
    보유 종목 중 매도 대상 식별 (기계적 규칙 — LLM 과 무관하게 항상 실행).

    종목당 아래 순서로 한 가지만 발동한다 (조건 1 이 발동하면 조건 2/3 은 건너뜀):
      조건 1: ATR 손절 / 샹들리에 트레일링 이탈(부분익절 후) → 전량매도
              부분익절 미실행 + 익절가 도달 → 부분매도(KR_PARTIAL_SELL_RATIO), 잔량은 샹들리에로 전환
              (ATR/거래기록 없는 레거시 보유분은 고정비율 전량 익절/손절만)
      조건 2: 기술적 매도 신호 개수 (데드크로스/RSI>70/MACD매도/패닉셀/수급이탈, ADX 보정) → 전량매도
      조건 3: 공포장(변동성>60+신호1개, >40+신호2개) + 매입가 대비 손실 KR_FEAR_SELL_LOSS_PCT
              이상 → 전량매도. 단 변동성 게이트가 수동 해제된 동안에는 잠재운다 (아래 주석 참조)

    반환:
      sell_candidates — 각 항목에 exit_type("full"/"partial"), reason_code 포함
      trailing_updates — 부분익절 완료 후 보유 중인 종목의 최신 peak/stop (매도 여부와 무관하게
                          매 사이클 갱신 필요. 이 함수는 읽기 전용이라 DB 반영은 호출부가 한다)
    """
    if balance is None:
        balance = kis.get_balance()

    if balance.get("rt_cd") != "0":
        return {
            "message": f"잔고 조회 실패: {balance.get('msg1', '')}",
            "sell_candidates": [],
            "trailing_updates": [],
        }

    holdings = balance.get("output1", [])
    if not holdings:
        return {"message": "보유 종목이 없습니다", "sell_candidates": [], "trailing_updates": []}

    tech_map: Dict[str, dict] = {}
    try:
        tech_resp = supabase.table(TABLE_TECH).select("*").order("날짜", desc=True).execute()
        for row in tech_resp.data or []:
            code = row.get("code")
            if code and code not in tech_map:
                tech_map[code] = row
    except Exception as e:
        logger.warning(f"매도 판단용 기술 지표 조회 실패: {e}")

    sentiment_map: Dict[str, dict] = {}
    try:
        sent_resp = supabase.table(TABLE_SENTIMENT).select("*").execute()
        sentiment_map = {r["code"]: r for r in (sent_resp.data or [])}
    except Exception as e:
        logger.warning(f"매도 판단용 감성 조회 실패: {e}")

    market = kr_market_data_service.get_market_context()
    fear_index = market.get("kospi_vol_20d")

    # 공포장 강제청산(조건 3)은 매수 하드블록과 같은 축의 규칙이다 — 둘 다 "공포 국면이니
    # 기계적으로 개입한다"는 판단이다. 운영자가 게이트를 수동 해제해 매수를 열어둔 동안
    # 매도 쪽만 공포지수를 그대로 보면, 방금 매수한 포지션이 신호 1개에 즉시 전량청산돼
    # 왕복매매가 난다 (실제로 2026-08-24 HMM 이 매수 1분 만에 청산됐다).
    # 그래서 오버라이드가 살아 있는 동안에는 조건 3 만 잠재운다.
    # ATR 손절/샹들리에 트레일링/부분익절(조건 1)과 기술신호 개수(조건 2)는 공포지수를
    # 쓰지 않으므로 그대로 살아 있다 — 하방 방어가 사라지는 것이 아니다.
    sell_override = kr_override_service.get_active_override()
    mechanical_fear = fear_index
    if sell_override is not None:
        mechanical_fear = None
        if fear_index is not None:
            logger.info(
                f"  변동성 게이트 수동 해제 중 — 공포장 강제청산 규칙 보류 "
                f"(변동성 {fear_index:.1f}%, 사유: {sell_override.get('reason')}). "
                f"손절/트레일링/기술신호 규칙은 그대로 적용됩니다"
            )

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
    trailing_updates = []

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
        trade = trade_map.get(code)
        tech = tech_map.get(code)
        sentiment = sentiment_map.get(code)

        atr = None
        if trade:
            try:
                atr = float(trade.get("atr")) if trade.get("atr") is not None else None
            except (ValueError, TypeError):
                atr = None

        action = None  # (exit_type, reason_code, reasons, sell_qty)

        if trade and trade.get("take_profit_price") and trade.get("stop_loss_price") and atr:
            tp = float(trade["take_profit_price"])
            sl = float(trade["stop_loss_price"])
            partial_done = bool(trade.get("partial_exit_done"))

            if partial_done:
                # 샹들리에 트레일링 — 진입시점 ATR 고정, 고점 대비 배수. 래칫(상향만).
                stored_peak = trade.get("chandelier_peak_price")
                peak = max(
                    float(stored_peak) if stored_peak is not None else current_price,
                    current_price,
                )
                candidate_stop = peak - settings.KR_CHANDELIER_ATR_MULT * atr
                stored_stop = trade.get("chandelier_stop_price")
                stop = max(
                    float(stored_stop) if stored_stop is not None else float("-inf"),
                    candidate_stop,
                    sl,  # 손절선 아래로는 내려가지 않는다
                )
                trailing_updates.append(
                    {
                        "trade_id": trade.get("id"),
                        "code": code,
                        "peak_price": peak,
                        "stop_price": stop,
                    }
                )
                if current_price <= stop:
                    action = (
                        "full",
                        "chandelier_stop",
                        [
                            f"샹들리에 트레일링 이탈: 현재가 {current_price:,.0f}원 <= "
                            f"트레일링 스탑 {stop:,.0f}원 (고점 {peak:,.0f}원 대비 "
                            f"{settings.KR_CHANDELIER_ATR_MULT:.1f}×ATR, 매입가 대비 {change_pct:+.2f}%)"
                        ],
                        quantity,
                    )
            elif current_price <= sl:
                action = (
                    "full",
                    "stop_loss",
                    [
                        f"ATR 손절: 현재가 {current_price:,.0f}원 <= 손절가 {sl:,.0f}원 "
                        f"(매입가 대비 {change_pct:+.2f}%)"
                    ],
                    quantity,
                )
            elif current_price >= tp:
                partial_qty = int(quantity * settings.KR_PARTIAL_SELL_RATIO)
                if partial_qty < settings.KR_MIN_PARTIAL_SHARES or quantity - partial_qty < 1:
                    action = (
                        "full",
                        "take_profit",
                        [
                            f"ATR 익절(전량 — 부분매도 수량 미달): 현재가 {current_price:,.0f}원 >= "
                            f"익절가 {tp:,.0f}원 (매입가 대비 {change_pct:+.2f}%)"
                        ],
                        quantity,
                    )
                else:
                    action = (
                        "partial",
                        "partial_take_profit",
                        [
                            f"ATR 부분익절({settings.KR_PARTIAL_SELL_RATIO:.0%}): 현재가 "
                            f"{current_price:,.0f}원 >= 익절가 {tp:,.0f}원 (매입가 대비 "
                            f"{change_pct:+.2f}%) — 잔량은 샹들리에 트레일링으로 전환"
                        ],
                        partial_qty,
                    )
        else:
            # 레거시(ATR/거래기록 없음) — 고정비율 전량 익절/손절만
            if change_pct >= FALLBACK_TAKE_PROFIT_PCT:
                action = (
                    "full",
                    "take_profit",
                    [f"익절(고정비율): 매입가 대비 {change_pct:+.2f}%"],
                    quantity,
                )
            elif change_pct <= FALLBACK_STOP_LOSS_PCT:
                action = (
                    "full",
                    "stop_loss",
                    [f"손절(고정비율): 매입가 대비 {change_pct:+.2f}%"],
                    quantity,
                )

        # ── 조건 2/3 (조건 1 이 이미 발동했으면 건너뜀) ──────
        # mechanical_fear 는 오버라이드 활성 시 None — 조건 3 만 비활성화된다
        # change_pct 는 조건 3 의 손실 게이트용 (KR_FEAR_SELL_LOSS_PCT)
        sig = _technical_sell_signals(tech, sentiment, mechanical_fear, change_pct)
        if action is None and sig["reasons"]:
            action = ("full", _reason_code_from_signal_details(sig["signal_details"]), sig["reasons"], quantity)

        if action is None:
            continue

        exit_type, reason_code, reasons, sell_qty = action
        sell_candidates.append(
            {
                "code": code,
                "stock_name": name,
                "quantity": sell_qty,
                "full_quantity": quantity,
                "exit_type": exit_type,
                "reason_code": reason_code,
                "trade_id": trade.get("id") if trade else None,
                "realized_partial_pnl": float(trade.get("realized_partial_pnl") or 0) if trade else 0.0,
                "realized_partial_qty": int(trade.get("realized_partial_qty") or 0) if trade else 0,
                "buy_price": buy_price,
                "current_price": current_price,
                "price_change_percent": round(change_pct, 2),
                "sell_reasons": reasons,
                "technical_sell_signals": sig["signal_count"],
                "technical_sell_details": sig["signal_details"] or None,
                "sentiment_score": sig["sentiment_score"],
                "adx": sig["adx_value"],
                "fear_index": fear_index,
            }
        )

    sell_candidates.sort(key=lambda x: abs(x["price_change_percent"]), reverse=True)
    return {
        "message": f"{len(sell_candidates)}개의 매도 대상을 식별했습니다",
        "sell_candidates": sell_candidates,
        "trailing_updates": trailing_updates,
    }


# ══════════════════════════════════════════════════════════════════
# 5) LLM 매도검토용 정성적 컨텍스트
# ══════════════════════════════════════════════════════════════════

def get_score_trend(code: str, days: int = 5, since: Optional[str] = None) -> List[dict]:
    """
    최근 N일 LLM 매도검토 로그(kr_llm_sell_decision_logs)에서 이 종목의 점수/순위 추이를 가져온다.

    "하루짜리 노이즈인지 추세적 악화인지 구분하라"는 지시를 LLM 이 실제로 수행할 수 있게
    하려면 스냅샷 하나가 아니라 이력이 필요하다 — 매일 쌓이는 이 로그를 그대로 재활용한다.
    새 데이터 수집 없이 과거 판단 기록만 조회하므로 비용이 없다.

    since 를 주면(보통 이번 매수의 buy_date) 그 날짜 이후 기록만 본다 — 같은 종목을 예전에
    샀다 판 이력까지 섞여서 "지금 보유분의 추세"인 것처럼 보이는 걸 막는다.

    kr_llm_sell_decision_logs 는 append-only 라 하루에 여러 행이 쌓일 수 있다(장중 추가
    매도검토 반복 실행). "N 일치 추이"는 날짜 단위여야 의미가 있으므로, 하루에 여러 행이 있으면
    그 날의 가장 최신(created_at) 행 하나만 남기고 나머지는 버린다.
    """
    try:
        query = (
            supabase.table("kr_llm_sell_decision_logs")
            .select("decision_date,composite_score,score_rank,score_universe_size,created_at")
            .eq("code", code)
        )
        if since:
            query = query.gte("decision_date", since[:10])
        # 하루에 여러 행이 쌓일 수 있어 넉넉히 가져온 뒤 날짜별 최신 행만 남긴다
        resp = (
            query.order("decision_date", desc=True)
            .order("created_at", desc=True)
            .limit(days * 20)
            .execute()
        )
        raw_rows = resp.data or []
    except Exception as e:
        logger.warning(f"  {universe.display(code)} 점수 추세 조회 실패: {e}")
        return []

    latest_by_date: Dict[str, dict] = {}
    for r in raw_rows:  # 날짜 desc, 같은 날짜 안에서는 created_at desc 라 첫 등장이 그날의 최신
        d = r["decision_date"]
        if d not in latest_by_date:
            latest_by_date[d] = r

    rows = sorted(latest_by_date.values(), key=lambda r: r["decision_date"])[-days:]
    return rows


def get_llm_sell_context(balance: Optional[dict] = None) -> dict:
    """
    보유 종목별 점수 추이(감쇠)·팩터반전 상세·교체매매 후보를 모아 LLM 매도검토 입력으로 만든다.

    기계적 손절/부분익절/샹들리에/기술신호개수/공포장 규칙은 이 함수와 무관하게 항상 별도로
    실행되므로, 여기서는 그 위에 얹을 정성적 판단 재료만 준비한다.
    """
    if balance is None:
        balance = kis.get_balance()
    if balance.get("rt_cd") != "0":
        return {"message": f"잔고 조회 실패: {balance.get('msg1', '')}", "holdings": [], "market": {}}

    holdings = balance.get("output1", [])
    if not holdings:
        return {"message": "보유 종목이 없습니다", "holdings": [], "market": {}}

    held_codes = [h.get("pdno") for h in holdings if h.get("pdno")]
    extra_codes = [c for c in held_codes if c not in universe.CODE_TO_NAME]

    scored = get_scored_universe(extra_codes=extra_codes)
    scored_map = scored["scored"]
    market = scored["market"]
    fear_index = market.get("kospi_vol_20d")

    tech_map, _, sentiment_map = _load_candidate_maps()

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
        logger.warning(f"kr_trade_records 조회 실패: {e}")

    # 교체매매 후보 — 오늘 매수 후보 중 미보유 최상위 종목.
    # 국내 매수가 스위치로 꺼져 있으면 대기 후보를 애초에 살 수 없으므로, 로테이션 이유로
    # 보유종목을 파는 것 자체가 앞뒤가 안 맞는다 — 후보 산출(비용 드는 채점)까지 건너뛴다.
    best_waiting = None
    at_capacity = False
    if market_switch_service.is_buy_enabled("KR"):
        buy_result = get_buy_candidates()
        unheld_candidates = [c for c in buy_result.get("results", []) if c["code"] not in held_codes]
        best_waiting = unheld_candidates[0] if unheld_candidates else None
        at_capacity = len(held_codes) >= settings.KR_MAX_POSITIONS

    context_list = []
    for item in holdings:
        code = item.get("pdno", "")
        name = item.get("prdt_name", code)
        try:
            quantity = int(item.get("hldg_qty", 0) or 0)
            buy_price = float(item.get("pchs_avg_pric", 0) or 0)
            current_price = float(item.get("prpr", 0) or 0)
        except (ValueError, TypeError):
            continue
        if quantity <= 0:
            continue

        change_pct = ((current_price - buy_price) / buy_price * 100) if buy_price > 0 else 0.0
        trade = trade_map.get(code)
        scored_entry = scored_map.get(code)
        sig = _technical_sell_signals(
            tech_map.get(code), sentiment_map.get(code), fear_index, change_pct
        )

        # 활성 손절/트레일링선까지 남은 여유, 1차 익절선까지 남은 폭 — LLM 이 "지금 내 판단이
        # 실제로 얼마나 중요한지"(기계적 안전망이 곧 작동할지 여부)를 가늠할 수 있게 한다.
        stop_distance_pct = None
        take_profit_distance_pct = None
        if trade and current_price > 0:
            active_stop = None
            if trade.get("partial_exit_done") and trade.get("chandelier_stop_price"):
                active_stop = trade.get("chandelier_stop_price")
            elif trade.get("stop_loss_price"):
                active_stop = trade.get("stop_loss_price")
            if active_stop:
                stop_distance_pct = round((current_price - float(active_stop)) / current_price * 100, 2)
            if not trade.get("partial_exit_done") and trade.get("take_profit_price"):
                take_profit_distance_pct = round(
                    (float(trade["take_profit_price"]) - current_price) / current_price * 100, 2
                )

        rotation_flag = False
        rotation_candidate = None
        if (
            at_capacity
            and best_waiting is not None
            and scored_entry is not None
            and best_waiting["composite_score"] - scored_entry["composite_score"]
            >= settings.KR_ROTATION_MIN_SCORE_GAP
        ):
            rotation_flag = True
            rotation_candidate = {
                "code": best_waiting["code"],
                "stock_name": best_waiting.get("stock_name"),
                "composite_score": best_waiting["composite_score"],
            }

        context_list.append(
            {
                "code": code,
                "stock_name": name,
                "quantity": quantity,
                "buy_price": buy_price,
                "current_price": current_price,
                "price_change_percent": round(change_pct, 2),
                "composite_score": scored_entry.get("composite_score") if scored_entry else None,
                "score_rank": scored_entry.get("rank") if scored_entry else None,
                "score_universe_size": scored.get("universe_size"),
                "score_note": (scored_entry or {}).get("score_note"),
                "score_trend": get_score_trend(
                    code, settings.KR_SCORE_TREND_DAYS, since=(trade or {}).get("buy_date")
                ),
                "technical_sell_signals": sig["signal_count"],
                "technical_sell_details": sig["signal_details"],
                "sentiment_score": sig["sentiment_score"],
                "adx": sig["adx_value"],
                "fear_index": fear_index,
                "partial_exit_done": bool(trade.get("partial_exit_done")) if trade else False,
                "realized_partial_qty": int((trade or {}).get("realized_partial_qty") or 0),
                "chandelier_stop_price": (trade or {}).get("chandelier_stop_price"),
                "atr": (trade or {}).get("atr"),
                "take_profit_price": (trade or {}).get("take_profit_price"),
                "stop_loss_price": (trade or {}).get("stop_loss_price"),
                "stop_distance_pct": stop_distance_pct,
                "take_profit_distance_pct": take_profit_distance_pct,
                "rotation_flag": rotation_flag,
                "rotation_candidate": rotation_candidate,
            }
        )

    return {
        "message": f"{len(context_list)}개 보유종목 컨텍스트를 생성했습니다",
        "holdings": context_list,
        "market": market,
    }


def get_current_signal_count(code: str) -> Optional[int]:
    """
    한 종목의 현재 기술적 매도신호 개수만 가볍게 계산한다 (LLM 매도판정 집행 시 근거 재검증용).

    get_llm_sell_context() 처럼 전체 유니버스를 로드하지 않고 이 종목 하나만 조회한다 —
    실행 시점에 "판단 때보다 신호가 줄었는지(근거 개선)"만 싸게 확인하면 되기 때문이다.
    """
    try:
        tech_resp = (
            supabase.table(TABLE_TECH)
            .select("*")
            .eq("code", code)
            .order("날짜", desc=True)
            .limit(1)
            .execute()
        )
        tech = (tech_resp.data or [None])[0]
    except Exception as e:
        logger.warning(f"  {universe.display(code)} 재검증용 기술지표 조회 실패: {e}")
        tech = None

    sentiment = None
    try:
        sent_resp = supabase.table(TABLE_SENTIMENT).select("*").eq("code", code).limit(1).execute()
        sentiment = (sent_resp.data or [None])[0]
    except Exception as e:
        logger.warning(f"  {universe.display(code)} 재검증용 감성 조회 실패: {e}")

    try:
        fear_index = kr_market_data_service.get_market_context().get("kospi_vol_20d")
    except Exception as e:
        logger.warning(f"  재검증용 시장환경 조회 실패: {e}")
        fear_index = None

    return _technical_sell_signals(tech, sentiment, fear_index)["signal_count"]
