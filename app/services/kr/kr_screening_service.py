"""
신규 종목 추천 — Feature 통합(1단계) → ML Filter(2단계) → LLM 종합판단(3단계).

    후보군: KOSPI 고정 100종목 (universe.py) — 시총·업종은 매주 갱신되는
            퀀트 스크리닝 엑셀(kr_quant_data_service)에서 함께 채운다
      │
      ├─ 0단계(최소필터) ─ 유동성(동전주/거래정지/관리종목) + 재무건전성(자본잠식·
      │                     영업적자·순손실·영업CF음수 하드게이트) — run_stage1() 전반부
      │
      ├─ 1단계 ─ 병렬 분석(재무/기술/수급, 서로 통과 여부에 의존하지 않는다) → 감성
      │           분석 → **Feature 통합**(kr_scoring.compute_scores, ML 제외,
      │           cross-sectional z-score 가중합: 재무+기술+수급+거래량+ADX+감성) →
      │           composite_score 상위 KR_FEATURE_TOP_N(기본 30)
      │           — run_stage1()(병렬분석) + run_stage2() 전반부(감성+Feature통합)
      │           예외: ATR 미산출만 탈락시킨다(익절/손절선을 못 만들면 주문 자체가
      │           불가능하므로 — 신호 강도 판단이 아니라 운영 제약).
      │
      ├─ 2단계 ─ ML Filter — 상승확률(양수)·정확도(KR_MIN_ML_ACCURACY 이상) 기준으로
      │           걸러 상승확률 내림차순 상위 KR_ML_FILTER_TOP_N(기본 10)
      │           — run_stage2() 후반부
      │
      └─ 3단계 ─ LLM 종합판단(kr_rebalance_service.propose) — 위에서 추린 후보 + 보유
                 종목 + 시장국면(코스피 20일 변동성, Risk/Market Regime)을 함께 보고
                 최대 KR_LLM_MAX_PICKS(기본 3) 종목의 매수/매도를 결정한다. 최종 집행
                 여부는 사람이 메뉴에서 승인한다 — 이 모듈은 주문을 내지 않는다.

## 왜 병렬 + 3단계 분리인가 (2026-08-25 재설계)

이전엔 재무 LLM 이 먼저 후보를 골라내고, 그 생존자만 기술·수급 분석을 받는 깔때기
구조였다. 포트폴리오 운영원칙 개정으로 "기본적/기술적/수급/감성 분석을 Feature
통합해 상위 30종목을 추리고, ML Filter 로 상위 10종목을 다시 추린 뒤, LLM 이
Risk/Market 국면까지 감안해 최종 3종목 이하로 종합판단한다"로 바뀌었다 — 재무가
약해도 기술·수급이 강하면(또는 그 반대) 낮은 순위로나마 후보에 남아 뒷 단계에서
직접 판단받게 하려는 의도다. 그래서 재무 LLM 의 PASS/FAIL 은 이제 게이트가 아니라
`fundamental_score` 로만 쓰고, z-score 가중합에서 자연스럽게 순위에 반영된다.

ML(상승확률·정확도)을 Feature 통합에 안 섞고 별도 2단계로 뺀 이유도 같은 맥락이다 —
재무·기술·수급·감성으로 이미 걸러진 30종목 안에서, ML 신뢰도가 낮은 예측(정확도 미달)
이나 하락 예측 종목을 명확히 배제하기 위해서다. Feature 통합 단계에서 ML 을 같이
섞으면(z-score 가중합) 다른 팩터가 강할 때 ML 이 음수여도 살아남는 경우가 생긴다
(2026-08-25 검증: 임계값 0.7 테스트에서 GS(ML -4.6%)가 재무·감성 점수로 통과한 사례).

## Feature 통합

`kr_scoring.py`는 원래 일간 정기 매수 파이프라인(`kr_recommendation_service.py`)
전용이었던 cross-sectional z-score 로직이다. 이번에 `W_FUND`(재무) 팩터를 추가하고
`compute_scores(..., include_rise=False)` 옵션을 넣어 이 모듈에도 연결했다 — 새
서브시스템을 만들지 않고 이미 검증된 컴포넌트를 재사용한 것. `fundamental_score` 가
없는 호출(기존 일간 파이프라인)은 z-score 가 중립(0)으로 빠지므로 그쪽 동작에는
영향이 없다.

## 기술 신호 (1단계, z-score 팩터로만 쓰인다 — 탈락 기준 아님)

| 신호 | 판정 |
|------|------|
| 골든크로스 | 20EMA 가 50EMA 를 상향 돌파 (최근 KR_CROSS_LOOKBACK_DAYS 일 이내) |
| RSI | 과매도 탈출 (35~65 구간이면서 전일 대비 상승) |
| MACD | MACD 가 시그널선을 상향 돌파 (최근 5일 이내) |
| ADX | 25 이상 (추세 강도 확인) |
| 거래량 | 5일 평균 대비 KR_VOLUME_SURGE_RATIO(기본 1.5)배 이상 |

데드크로스·신호 개수는 이제 하드 탈락 기준이 아니다(참고 필드 `buy_signals`/
`dead_cross` 로만 남는다) — 약한 기술적 신호는 z-score 가중합에서 낮은 순위로 밀린다.

ATR 만 예외로 하드 탈락시킨다 — 신호가 아니라 **주문 시 익절/손절선 산출값**이라
안 나오면 애초에 주문을 낼 수 없다.

## 수급 (1단계, z-score 팩터로만 쓰인다)

과거엔 "외국인·기관 각각 N일 연속 순매수"를 하드 게이트로 썼지만, 병렬화 원칙에
맞춰 순매수 금액 강도(`foreign_total + institution_total`)를 z-score 팩터로 바꿨다.
연속일수(streak)는 참고 필드(`flow`)로 여전히 남아있다.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import pytz

from app.core.config import settings
from app.db.supabase import supabase
from app.services import buy_switch_service
from app.services.indicators import TechnicalIndicators
from app.services.kr import (
    kis_domestic_service as kis,
    kr_fundamental_review_service,
    kr_market_data_service,
    kr_quant_data_service,
    kr_rebalance_service,
    kr_scoring,
    kr_sentiment_service,
    kr_universe_service,
    universe,
)

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")
_ind = TechnicalIndicators()

TABLE_TRADES = "kr_trade_records"
TABLE_ML = "kr_stock_analysis_results"


# ══════════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════════

def _normalize_daily(rows: List[dict]) -> List[dict]:
    """KIS 국내 일봉 → 공용 지표 모듈이 기대하는 형식 (최신일이 index 0)."""
    out = []
    for r in rows or []:
        try:
            close = float(r.get("stck_clpr", 0) or 0)
            if close <= 0:
                continue
            out.append(
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
    return out


def _drop_incomplete_today(daily: List[dict]) -> List[dict]:
    """장 마감(15:30 KST) 전이면 당일 봉은 미완성이라 제외한다."""
    if not daily:
        return daily
    now = datetime.now(KST)
    if daily[0]["xymd"] != now.strftime("%Y%m%d"):
        return daily
    closed = now.hour > 15 or (now.hour == 15 and now.minute >= 30)
    return daily if closed else daily[1:]


# ══════════════════════════════════════════════════════════════════
# 2단계 — 기술적 분석
# ══════════════════════════════════════════════════════════════════

def analyze_technicals(code: str) -> Optional[dict]:
    """
    종목 하나의 기술적 지표 + 매수 신호. 데이터 부족/조회 실패 시 None.

    Returns: {"signals": [...], "signal_count", "dead_cross", "atr", "rsi", ...}
    """
    try:
        daily = _drop_incomplete_today(
            _normalize_daily(kis.get_recent_daily_chart(code, days=140))
        )
    except Exception as e:
        logger.debug(f"  {code} 일봉 조회 실패: {e}")
        return None

    if len(daily) < 60:
        return None

    # 지표 계산은 과거→현재 순서가 필요하다 (일봉은 최신일이 index 0)
    closes = pd.Series([float(d["clos"]) for d in reversed(daily)], dtype=float)
    volumes = [int(d["tvol"]) for d in daily]

    ema20 = _ind.calculate_ema(closes, 20)
    ema50 = _ind.calculate_ema(closes, 50)
    rsi = _ind.calculate_rsi(closes)
    macd, macd_signal = _ind.calculate_macd(closes)
    adx = _ind.calculate_adx(daily)
    atr = _ind.calculate_atr(daily)

    lookback = settings.KR_CROSS_LOOKBACK_DAYS
    above = ema20 > ema50

    def crossed(up: bool, window: int) -> bool:
        """최근 window 일 안에 교차가 있었는지."""
        if len(above) < window + 2:
            return False
        recent = above.iloc[-(window + 1) :]
        for i in range(1, len(recent)):
            prev, cur = bool(recent.iloc[i - 1]), bool(recent.iloc[i])
            if up and not prev and cur:
                return True
            if not up and prev and not cur:
                return True
        return False

    golden_cross = crossed(True, lookback)
    dead_cross = crossed(False, lookback)

    macd_above = macd > macd_signal
    macd_cross_up = False
    if len(macd_above) >= 6:
        recent = macd_above.iloc[-6:]
        for i in range(1, len(recent)):
            if not bool(recent.iloc[i - 1]) and bool(recent.iloc[i]):
                macd_cross_up = True
                break

    rsi_now = float(rsi.iloc[-1]) if len(rsi) else 50.0
    rsi_prev = float(rsi.iloc[-2]) if len(rsi) >= 2 else rsi_now
    rsi_buy = 35.0 <= rsi_now <= 65.0 and rsi_now > rsi_prev

    volume_ratio = None
    if len(volumes) >= 6:
        past = [v for v in volumes[1:6] if v > 0]
        if past:
            avg = sum(past) / len(past)
            volume_ratio = round(volumes[0] / avg, 2) if avg > 0 else None
    volume_surge = bool(volume_ratio and volume_ratio >= settings.KR_VOLUME_SURGE_RATIO)

    adx_strong = bool(adx is not None and adx >= 25)

    signals = []
    if golden_cross:
        signals.append(f"골든크로스(20EMA>50EMA, 최근 {lookback}일 이내)")
    if rsi_buy:
        signals.append(f"RSI 매수구간 상승({rsi_prev:.1f}→{rsi_now:.1f})")
    if macd_cross_up:
        signals.append("MACD 시그널 상향돌파")
    if adx_strong:
        signals.append(f"ADX 추세강도 {adx:.1f}")
    if volume_surge:
        signals.append(f"거래량 급증({volume_ratio:.2f}배)")

    return {
        "signals": signals,
        "signal_count": len(signals),
        "dead_cross": dead_cross,
        "golden_cross": golden_cross,
        "rsi": round(rsi_now, 2),
        "macd_cross_up": macd_cross_up,
        # kr_scoring.compute_scores() 의 z_macd/z_sma 팩터용 raw 값 — calculate_macd() 가
        # 이미 계산한 두 시리즈에서 마지막 값만 추가로 뽑는다(새 계산 없음).
        "macd": round(float(macd.iloc[-1]), 4),
        "macd_signal": round(float(macd_signal.iloc[-1]), 4),
        "adx": adx,
        "atr": atr,
        "ema20": round(float(ema20.iloc[-1]), 2),
        "ema50": round(float(ema50.iloc[-1]), 2),
        "volume_ratio": volume_ratio,
        "current_price": float(daily[0]["clos"]),
    }


# ══════════════════════════════════════════════════════════════════
# 2단계 — 수급
# ══════════════════════════════════════════════════════════════════

def analyze_flow(code: str) -> Optional[dict]:
    """
    외국인·기관 수급. 최근 5거래일 안에서 각각 3일 연속 순매수가 있었는지 본다.

    누적 금액이 아니라 연속성을 보는 이유: 하루 대량 매수 뒤 계속 파는 패턴이
    누적으로는 양수로 보여 '수급이 좋다'고 오판하게 된다.
    """
    try:
        rows = kis.get_stock_investor_trend(code)
    except Exception as e:
        logger.debug(f"  {code} 수급 조회 실패: {e}")
        return None
    if not rows:
        return None

    window = rows[: settings.KR_FLOW_WINDOW_DAYS]

    def streak(field: str) -> int:
        """window 안에서 나온 최장 연속 순매수 일수."""
        best = cur = 0
        # 오래된 날부터 봐야 '연속'이 자연스럽다
        for r in reversed(window):
            try:
                val = float(r.get(field, 0) or 0)
            except (ValueError, TypeError):
                val = 0.0
            cur = cur + 1 if val > 0 else 0
            best = max(best, cur)
        return best

    def total(field: str) -> float:
        s = 0.0
        for r in window:
            try:
                s += float(r.get(field, 0) or 0)
            except (ValueError, TypeError):
                pass
        return round(s, 1)

    need = settings.KR_FLOW_STREAK_DAYS
    frgn_streak = streak("frgn_ntby_tr_pbmn")
    orgn_streak = streak("orgn_ntby_tr_pbmn")

    return {
        "foreign_streak": frgn_streak,
        "institution_streak": orgn_streak,
        "foreign_total": total("frgn_ntby_tr_pbmn"),
        "institution_total": total("orgn_ntby_tr_pbmn"),
        "passed": frgn_streak >= need and orgn_streak >= need,
        "days_observed": len(window),
    }


# ══════════════════════════════════════════════════════════════════
# ML 예측
# ══════════════════════════════════════════════════════════════════

def _load_ml_predictions() -> Dict[str, dict]:
    """kr_stock_analysis_results 의 최신 예측 (code → row)."""
    try:
        resp = (
            supabase.table(TABLE_ML)
            .select("*")
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
        )
    except Exception as e:
        logger.warning(f"  ML 예측 조회 실패: {e}")
        return {}

    latest: Dict[str, dict] = {}
    for row in resp.data or []:
        code = row.get("code")
        if code and code not in latest:
            latest[code] = row
    return latest


# ══════════════════════════════════════════════════════════════════
# 포트폴리오 상태
# ══════════════════════════════════════════════════════════════════

def get_portfolio_state(balance: Optional[dict] = None) -> dict:
    """
    현재 보유 종목 수 / 섹터 분포 / 신규 매수 여력.

    보유 판단은 KIS 원장(잔고)을 기준으로 한다. kr_trade_records 는 주문 접수 후
    체결 전 상태가 섞여 있어 '지금 몇 종목을 들고 있나'의 답으로는 부정확하다.
    다만 매수 주문이 나갔지만 아직 체결 안 된 종목도 슬롯을 차지하므로 함께 센다.
    """
    if balance is None:
        balance = kis.get_balance()

    held: List[dict] = []
    if balance.get("rt_cd") == "0":
        for h in balance.get("output1", []):
            try:
                qty = int(h.get("hldg_qty", 0) or 0)
            except (ValueError, TypeError):
                qty = 0
            code = h.get("pdno")
            if code and qty > 0:
                held.append(
                    {"code": code, "name": h.get("prdt_name", code),
                     "sector": kr_universe_service.sector(code),
                     "sector_key": kr_universe_service.sector_key(code)}
                )

    pending: List[str] = []
    try:
        resp = (
            supabase.table(TABLE_TRADES)
            .select("code")
            .eq("status", "buy_ordered")
            .eq("account_type", kis.current_account_type())
            .execute()
        )
        held_codes = {h["code"] for h in held}
        pending = [r["code"] for r in (resp.data or []) if r["code"] not in held_codes]
    except Exception as e:
        logger.warning(f"  미체결 매수 조회 실패: {e}")

    # 분산 규칙은 KRX 업종(sector_key)을 기준으로 센다 — 전 종목에 빠짐없이 있어서
    # '미분류' 한 덩어리에 서로 다른 업종이 섞이는 일이 없다.
    sectors: Dict[str, int] = {}
    for h in held:
        key = h["sector_key"]
        sectors[key] = sectors.get(key, 0) + 1

    occupied = len(held) + len(pending)
    return {
        "held": held,
        "held_count": len(held),
        "pending_codes": pending,
        "occupied_slots": occupied,
        "max_positions": settings.KR_MAX_POSITIONS,
        "free_slots": max(settings.KR_MAX_POSITIONS - occupied, 0),
        "sector_counts": sectors,
    }


def get_holdings_context(balance: Optional[dict] = None) -> List[dict]:
    """
    리밸런싱 프롬프트에 넣을 보유 종목 상세.

    잔고(수량·손익)에 더해, 기존 매도검토가 쓰는 점수·기술신호·손절선까지 붙여준다.
    LLM 이 "이 보유분을 팔고 후보를 담을 가치가 있나"를 판단하려면 후보와 같은 축의
    정보가 있어야 하기 때문이다. 부가 정보 조회가 실패해도 잔고 기반 항목은 남긴다 —
    점수를 못 구했다고 그 종목이 프롬프트에서 사라지면 LLM 이 보유 사실 자체를 모른다.
    """
    from app.services.kr import kr_recommendation_service as recommend

    if balance is None:
        balance = kis.get_balance()

    holdings: List[dict] = []
    if balance.get("rt_cd") != "0":
        logger.warning(f"  잔고 조회 실패: {balance.get('msg1', '')}")
        return holdings

    for h in balance.get("output1", []):
        code = h.get("pdno")
        try:
            qty = int(h.get("hldg_qty", 0) or 0)
            buy_price = float(h.get("pchs_avg_pric", 0) or 0)
            current = float(h.get("prpr", 0) or 0)
        except (ValueError, TypeError):
            continue
        if not code or qty <= 0:
            continue
        change = ((current - buy_price) / buy_price * 100) if buy_price > 0 else 0.0
        holdings.append(
            {
                "code": code,
                "name": h.get("prdt_name", code),
                "sector_key": kr_universe_service.sector_key(code),
                "quantity": qty,
                "buy_price": buy_price,
                "current_price": current,
                "price_change_percent": round(change, 2),
            }
        )

    if not holdings:
        return holdings

    # 점수·기술신호·손절선 보강 (실패해도 위 기본 정보는 유지)
    try:
        context = recommend.get_llm_sell_context(balance=balance)
        extra = {c["code"]: c for c in (context.get("holdings") or [])}
        for h in holdings:
            e = extra.get(h["code"])
            if not e:
                continue
            for key in (
                "composite_score", "score_rank", "score_universe_size",
                "technical_sell_signals", "technical_sell_details",
                "stop_loss_price", "take_profit_price", "sentiment_score",
            ):
                if e.get(key) is not None:
                    h[key] = e[key]
    except Exception as e:
        logger.warning(f"  보유 종목 상세 보강 실패(기본 정보로 진행): {e}")

    return holdings


def _apply_portfolio_rules(ranked: List[dict], state: dict) -> "tuple[List[dict], List[dict]]":
    """
    포트폴리오 규칙 적용 → (선정, 보류).

    1. 남은 슬롯(최대 KR_MAX_POSITIONS)을 넘지 않는다.
    2. 한 번에 KR_STAGE2_TOP_N 종목까지만 3단계로 넘긴다.
    3. 같은 섹터가 KR_MAX_PER_SECTOR 종목을 넘지 않도록 한다 (보유분 포함).

    3번은 '최대한 다양한 섹터'라는 요구를 코드로 옮긴 것이다. 점수 순으로 뽑되
    이미 채워진 섹터의 종목은 건너뛰므로, 상위권이 한 섹터에 몰려도 포트폴리오는
    분산된다.
    """
    picked: List[dict] = []
    skipped: List[dict] = []

    sector_counts = dict(state["sector_counts"])
    limit = min(state["free_slots"], settings.KR_STAGE2_TOP_N)

    for item in ranked:
        if len(picked) >= limit:
            skipped.append({**item, "skip_reason": "이번 회차 추천 한도 도달"})
            continue
        # 후보 dict 는 유니버스에서 온 sector_krx 를, 보유 종목 dict 는 sector_key 를 갖는다.
        # 둘 다 KRX 업종명이라 같은 버킷으로 떨어진다.
        sec = (
            item.get("sector_key")
            or item.get("sector_krx")
            or item.get("sector")
            or "미분류"
        )
        if sector_counts.get(sec, 0) >= settings.KR_MAX_PER_SECTOR:
            skipped.append(
                {**item, "skip_reason": f"섹터 집중 제한({sec} 이미 {sector_counts[sec]}종목)"}
            )
            continue
        picked.append(item)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    return picked, skipped


# ══════════════════════════════════════════════════════════════════
# 파이프라인
# ══════════════════════════════════════════════════════════════════

def run_stage1(
    candidates: Optional[List[dict]] = None, progress: bool = True
) -> dict:
    """
    0단계(최소필터) + 1단계(병렬: 재무/기술/수급 분석).

    "병렬"은 서로의 통과 여부에 의존하지 않는다는 뜻이다 — 0단계를 통과한 종목은
    재무 점수가 낮거나 기술 신호가 약해도 여기서 탈락하지 않는다(그런 저품질 신호는
    2단계 Feature 통합의 z-score 가중합에서 자연히 낮은 순위로 밀리고, 최종 판단은
    3단계 LLM 이 한다). 유일한 예외는 ATR — 신호 강도 판단이 아니라 "이 종목은 익절/
    손절선을 만들 수 없어 주문 자체가 불가능하다"는 운영 제약이라 그대로 탈락시킨다.
    """
    started = datetime.now(KST)
    if candidates is None:
        candidates = [dict(u) for u in universe.UNIVERSE]
    logger.info(f"[0단계] 후보 {len(candidates)}종목으로 시작")
    codes = [c["code"] for c in candidates]

    def _early_return(msg: str, fund_result: dict) -> dict:
        return {
            "stage": 1,
            "message": msg,
            "passed": [],
            "dropped": [],
            "fundamental": fund_result,
            "elapsed_sec": int((datetime.now(KST) - started).total_seconds()),
        }

    # 0-1. 퀀트데이터 확보 — 실패하면 파이프라인을 중단한다(Fail-Close). DART 등으로
    #   조용히 폴백하지 않는 이유는 회차마다 재무 점수 산출 기준이 섞이는 걸 막기 위해서다.
    try:
        kr_quant_data_service.ensure_fresh()
        meta = kr_quant_data_service.get_market_meta_bulk(codes)
        fundamentals = kr_quant_data_service.get_fundamentals_bulk(codes, progress=progress)
    except Exception as e:
        msg = f"퀀트데이터 확보 실패 — 파이프라인 중단: {e}"
        logger.error(f"  {msg}")
        return _early_return(msg, {"passed": [], "failed": [], "llm_failed": False, "message": msg})

    for c in candidates:
        m = meta.get(c["code"])
        if m:
            c["market_cap"] = m["market_cap"]
            c["sector_krx"] = m["sector_krx"]
            c["rank"] = m["rank"]
    logger.info(f"  퀀트데이터 재무 지표 확보: {len(fundamentals)}/{len(candidates)}종목")

    # 0-2. 최소필터 — 유동성(퀀트데이터 스크리닝 필터 컬럼) + 재무건전성(하드게이트).
    #   quant_data.xlsx 의 *_isFilter 컬럼(동전주/거래정지/관리종목)은 True 면 배제 대상.
    liquidity_dropped: List[dict] = []
    survivors: List[dict] = []
    liq_flags = kr_quant_data_service.get_liquidity_flags(codes)
    for c in candidates:
        flag = liq_flags.get(c["code"])
        if flag and flag.get("excluded"):
            liquidity_dropped.append({**c, "drop_reason": f"유동성 최소필터: {flag['reason']}"})
        else:
            survivors.append(c)

    # 재무건전성 하드게이트 — kr_fundamental_review_service.review() 안에서 이미 실행된다
    # (자본잠식/영업적자/순손실/영업CF음수/핵심지표 결측). LLM 판정은 이제 게이트가 아니라
    # 점수(fundamental_score)로만 쓴다 — PASS/FAIL 과 무관하게 점수를 받은 종목은 전부
    # 1단계로 넘어간다.
    fund_result = kr_fundamental_review_service.review(survivors, fundamentals)
    hard_gate_dropped = [f for f in fund_result["failed"] if f.get("stage") == "hard_gate"]
    hard_gate_codes = {f["code"] for f in hard_gate_dropped}
    fund_score_by_code = {
        c["code"]: (c.get("fundamental_score"), c.get("fundamental_reason"))
        for c in fund_result["passed"] + fund_result["failed"]
        if c.get("code") not in hard_gate_codes
    }
    min_filter_dropped = liquidity_dropped + hard_gate_dropped
    stage0_survivors = [c for c in survivors if c["code"] not in hard_gate_codes]

    logger.info(
        f"  0단계 최소필터: {len(candidates)}종목 중 {len(min_filter_dropped)}종목 탈락 "
        f"(유동성 {len(liquidity_dropped)}, 재무건전성 {len(hard_gate_dropped)}) "
        f"→ {len(stage0_survivors)}종목"
    )
    if not stage0_survivors:
        msg = f"0단계 최소필터에서 전원 탈락 ({len(min_filter_dropped)}종목)"
        result = _early_return(msg, fund_result)
        result["dropped"] = min_filter_dropped
        return result

    for c in stage0_survivors:
        score, reason = fund_score_by_code.get(c["code"], (None, None))
        c["fundamental_score"] = score
        c["fundamental_reason"] = reason
        # fundamentals(영업이익률/ROE/ROIC/PER/부채비율 원자료)도 같이 넘겨야
        # _print_pick() 등 화면 출력이 실제 수치를 보여준다 — score/reason 만 넘기면
        # "영업이익률 None%" 처럼 결측으로 보인다.
        c["fundamentals"] = fundamentals.get(c["code"])

    # 재무·기술·수급 병렬분석 (서로 의존하지 않고, 재무 점수와도 무관하게 전원 실행) —
    # "1단계"(재무+기술+수급+감성 → Feature 통합 → 상위 KR_FEATURE_TOP_N) 의 앞부분일
    # 뿐이다. 이 함수는 Feature 통합을 하지 않으므로, 아래 메시지를 "1단계 통과"라고
    # 부르면 "1단계가 상위 30개로 못 걸렀다"는 오해를 산다(실사용자 피드백으로 확인) —
    # 그래서 "예비분석"이라 부르고, 진짜 1단계 완료(Feature 통합 상위 N) 표시는
    # run_stage2() 쪽에서 한다.
    passed: List[dict] = []
    dropped: List[dict] = list(min_filter_dropped)
    for i, c in enumerate(stage0_survivors, 1):
        tech = analyze_technicals(c["code"])
        if tech is None:
            dropped.append({**c, "drop_reason": "일봉 데이터 부족/조회 실패"})
        elif tech["atr"] is None:
            dropped.append({**c, "drop_reason": "ATR 산출 불가 (익절/손절선을 만들 수 없음)"})
        else:
            flow = analyze_flow(c["code"])
            entry = {
                **c,
                **{k: v for k, v in tech.items() if k != "signals"},
                "buy_signals": tech["signals"],
                "flow": flow,
                "net_buy_score": (
                    (flow["foreign_total"] + flow["institution_total"])
                    if flow is not None else None
                ),
            }
            passed.append(entry)
        if progress and (i % 10 == 0 or i == len(stage0_survivors)):
            logger.info(f"  1단계 예비분석(재무·기술·수급) {i}/{len(stage0_survivors)} (통과 {len(passed)})")

    msg = (
        f"1단계 예비분석 통과 {len(passed)}종목 — Feature 통합 전 "
        f"(후보 {len(candidates)} → 0단계 {len(stage0_survivors)} → ATR/일봉 탈락 "
        f"{len(stage0_survivors) - len(passed)})"
    )
    logger.info(f"  {msg}")
    return {
        "stage": 1,
        "message": msg,
        "passed": passed,
        "dropped": dropped,
        "fundamental": fund_result,
        "elapsed_sec": int((datetime.now(KST) - started).total_seconds()),
    }


def run_stage2(stage1_passed: List[dict], fear_index: Optional[float] = None) -> dict:
    """
    1단계 마무리(감성 분석 → Feature 통합: 재무+기술+수급+감성) → 상위 KR_FEATURE_TOP_N →
    2단계(ML Filter: 상승확률+정확도) → 상위 KR_ML_FILTER_TOP_N.

    함수명은 run_stage2 지만, 실제로는 사용자 기준 "1단계"의 뒷부분(감성+Feature 통합)과
    "2단계"(ML Filter)를 한 함수 안에서 이어서 처리한다 — run_stage1() 이 이미 "1단계"
    라는 이름을 쓰고 있어서(재무·기술·수급 예비분석), 로그/메시지에서는 혼동을 피하려고
    "1단계 최종 통과"(Feature 통합 직후)와 "2단계 통과"(ML Filter 직후)를 따로 찍는다.

    ML 예측은 일부러 Feature 통합(composite_score)에 안 섞는다 — "재무·기술·수급·감성으로
    먼저 추리고, 그 다음에 ML 상승확률·정확도로 다시 거른다"는 순서라, Feature 통합 단계는
    `kr_scoring.compute_scores(..., include_rise=False)` 로 ML 팩터를 뺀다. Risk/Market
    Regime(변동성 국면)은 여기서 기계적으로 거르지 않고 3단계(kr_rebalance_service) LLM 의
    종합판단 재료로만 넘긴다 — fear_index 는 참고용으로만 후보에 붙여둔다.
    """
    started = datetime.now(KST)
    logger.info(f"[1단계 마무리] 예비분석 통과 {len(stage1_passed)}종목으로 감성·Feature통합 시작")

    if not stage1_passed:
        return {
            "stage": 2, "message": "2단계 통과 0종목 (1단계에서 후보가 없음)",
            "ranked": [], "feature_dropped": 0, "ml_dropped": [],
            "elapsed_sec": int((datetime.now(KST) - started).total_seconds()),
        }

    # 2-1. 감성 분석 (종목 + 섹터 업황) — 하드 게이트 없이 점수만 부착한다(z-score 팩터로만 씀).
    logger.info(f"  감성 분석 {len(stage1_passed)}종목")
    sentiment = kr_sentiment_service.score_items(
        [{"code": s["code"], "name": s["name"]} for s in stage1_passed]
    )
    sector_sentiment = kr_sentiment_service.score_sectors(
        [s.get("sector") for s in stage1_passed if s.get("sector")]
    )
    for s in stage1_passed:
        sent = sentiment.get(s["code"], {})
        sec_entry = sector_sentiment.get(s.get("sector") or "", {})
        s["sentiment_score"] = round(float(sent.get("score") or 0.0), 3)
        s["sentiment_summary"] = sent.get("summary")
        s["article_count"] = sent.get("article_count", 0)
        s["blog_buzz"] = sent.get("blog_buzz", 0)
        s["sector_sentiment"] = round(float(sec_entry.get("score") or 0.0), 3)

    # 2-2. Feature 통합 — 재무+기술+수급+감성만(ML 제외). kr_scoring 의 cross-sectional
    #   z-score 가중합. analyze_technicals() 는 ema20/ema50 을 주므로 kr_scoring 이
    #   기대하는 sma20/sma50 키에 그대로 매핑한다(같은 추세괴리 신호, 이름만 다르다).
    for c in stage1_passed:
        c.setdefault("sma20", c.get("ema20"))
        c.setdefault("sma50", c.get("ema50"))
        c.setdefault("signal", c.get("macd_signal"))
        c["fear_index"] = fear_index  # 3단계 LLM 참고용 — 여기서 거르는 데는 안 쓴다

    kr_scoring.compute_scores(stage1_passed, fear_index, include_rise=False)
    stage1_passed.sort(key=lambda x: x["composite_score"], reverse=True)
    feature_top = stage1_passed[: settings.KR_FEATURE_TOP_N]
    feature_dropped = len(stage1_passed) - len(feature_top)

    logger.info(
        f"  1단계 최종 통과(Feature 통합) {len(feature_top)}/{len(stage1_passed)}종목 "
        f"(상위 {settings.KR_FEATURE_TOP_N})"
    )

    # 2단계 — ML Filter: 정확도 하한 + 상승확률 양수만 남기고, 상승확률 내림차순 상위
    #   KR_ML_FILTER_TOP_N.
    ml = _load_ml_predictions()
    ranked: List[dict] = []
    ml_dropped: List[dict] = []
    for c in feature_top:
        pred = ml.get(c["code"])
        if not pred:
            ml_dropped.append(
                {**c, "drop_reason": "ML 예측 없음 (메뉴 11번으로 Kaggle 예측을 먼저 갱신하세요)"}
            )
            continue
        try:
            rise = float(pred.get("rise_probability") or 0)
            acc = float(pred.get("accuracy") or 0)
        except (ValueError, TypeError):
            ml_dropped.append({**c, "drop_reason": "ML 예측값 파싱 실패"})
            continue
        if acc < settings.KR_MIN_ML_ACCURACY:
            ml_dropped.append(
                {**c, "rise_probability": rise, "ml_accuracy": acc,
                 "drop_reason": f"ML 정확도 {acc:.1f}% < 하한 {settings.KR_MIN_ML_ACCURACY:.0f}%"}
            )
            continue
        if rise <= 0:
            ml_dropped.append(
                {**c, "rise_probability": rise, "ml_accuracy": acc,
                 "drop_reason": f"예측 상승률 {rise:+.2f}% (양수 아님)"}
            )
            continue
        if rise < settings.KR_MIN_RISE_PROBABILITY:
            ml_dropped.append(
                {**c, "rise_probability": rise, "ml_accuracy": acc,
                 "drop_reason": f"예측 상승률 {rise:.2f}% < 하한 {settings.KR_MIN_RISE_PROBABILITY:.1f}%"}
            )
            continue
        c["rise_probability"] = rise
        c["ml_accuracy"] = acc
        ranked.append(c)

    ranked.sort(key=lambda x: x["rise_probability"], reverse=True)
    over_limit = ranked[settings.KR_ML_FILTER_TOP_N:]
    ranked = ranked[: settings.KR_ML_FILTER_TOP_N]
    for c in over_limit:
        ml_dropped.append({**c, "drop_reason": f"ML 상승률 상위 {settings.KR_ML_FILTER_TOP_N}위 밖"})

    msg = (
        f"2단계 통과 {len(ranked)}종목 "
        f"(Feature 통합 상위 {len(feature_top)} → ML Filter 상승확률/정확도 상위 "
        f"{settings.KR_ML_FILTER_TOP_N})"
    )
    logger.info(f"  {msg}")
    return {
        "stage": 2,
        "message": msg,
        "ranked": ranked,
        "feature_dropped": feature_dropped,
        "ml_dropped": ml_dropped,
        "elapsed_sec": int((datetime.now(KST) - started).total_seconds()),
    }


def run_screening(
    progress: bool = True,
    balance: Optional[dict] = None,
    with_rebalance: bool = True,
) -> dict:
    """
    신규 종목 추천 전체 파이프라인 (0~1단계 병렬분석 → 2단계 Feature통합 → 3단계 LLM종합판단).

    매매를 실행하지는 않는다. 결과만 돌려주므로 메뉴에서 몇 번을 돌려도 안전하다.
    """
    started = datetime.now(KST)
    state = get_portfolio_state(balance)

    fear_index = None
    try:
        fear_index = kr_market_data_service.get_market_context().get("kospi_vol_20d")
    except Exception as e:
        logger.warning(f"  시장 국면(변동성) 조회 실패 — Feature 통합 임계값을 평온장 기준으로 진행: {e}")
    state["fear_index"] = fear_index
    logger.info(
        f"포트폴리오: 보유 {state['held_count']}종목 + 미체결 {len(state['pending_codes'])} "
        f"/ 최대 {state['max_positions']} → 여유 {state['free_slots']}슬롯"
    )

    if state["free_slots"] <= 0:
        return {
            "message": f"보유 종목이 이미 최대치({state['max_positions']}종목)입니다",
            "finalists": [], "skipped": [], "proposal": None, "portfolio": state,
            "stage1": None, "stage2": None,
            "elapsed_sec": 0,
        }

    buy_enabled = buy_switch_service.is_buy_enabled()
    if not buy_enabled:
        logger.warning("  신규 매수 스위치가 꺼져 있습니다 (분석은 계속 진행)")

    # 후보군은 고정 100종목(universe.py) — 시총/업종은 run_stage1 안에서 퀀트데이터로 채운다.
    universe_items = [dict(u) for u in universe.UNIVERSE]
    # 이미 보유/주문 중인 종목은 후보에서 뺀다
    exclude = {h["code"] for h in state["held"]} | set(state["pending_codes"])
    candidates = [u for u in universe_items if u["code"] not in exclude]

    s1 = run_stage1(candidates, progress=progress)
    if not s1["passed"]:
        return {
            "message": f"1단계에서 후보가 남지 않았습니다 — {s1['message']}",
            "finalists": [], "skipped": [], "proposal": None, "portfolio": state,
            "stage1": s1, "stage2": None, "buy_enabled": buy_enabled,
            "elapsed_sec": int((datetime.now(KST) - started).total_seconds()),
        }

    s2 = run_stage2(s1["passed"], fear_index=fear_index)
    finalists, skipped = _apply_portfolio_rules(s2["ranked"], state)

    # 3단계 — LLM 이 재무/기술/수급/감성/ML/시장국면(fear_index, state 에 실어 보냄)을
    # 종합판단해 매수를 추천하고 보유 종목과의 리밸런싱을 제안한다. 여기서도 주문은 나가지
    # 않는다. 승인은 사람이 메뉴에서 한다.
    proposal = None
    if finalists and with_rebalance:
        logger.info(f"[3단계] 후보 {len(finalists)}종목으로 LLM 종합판단·리밸런싱 제안 생성")
        holdings_ctx = get_holdings_context(balance)
        proposal = kr_rebalance_service.propose(finalists, holdings_ctx, state)

    msg = (
        f"2단계 통과 {len(finalists)}종목 "
        f"(후보 {len(candidates)} → 1단계 {len(s1['passed'])} → 2단계 {len(s2['ranked'])})"
    )
    if proposal:
        msg += f" / LLM 제안: 매수 {len(proposal['buy'])} · 매도 {len(proposal['sell'])}"
    logger.info(msg)
    return {
        "message": msg,
        "finalists": finalists,
        "skipped": skipped,
        "proposal": proposal,
        "portfolio": state,
        "stage1": s1,
        "stage2": s2,
        "buy_enabled": buy_enabled,
        "elapsed_sec": int((datetime.now(KST) - started).total_seconds()),
    }
