"""
국내주식 매수 후보 점수 산출.

미국 트랙(app/services/scoring_service.py)의 cross-sectional z-score 방식을 그대로
쓰되, 한국 시장 특유의 팩터를 하나 추가한다.

  + 수급(flow): 외국인·기관 순매수. 한국 시장에서 가장 검증된 단기 신호로,
    개인 대비 정보 우위가 크고 코스피 대형주는 외국인 수급에 강하게 연동된다.

의도적으로 점수에 넣지 '않은' 것:
  - 네이버 검색 관심도(search_interest): 한국 시장에서 개인 관심 급증은
    고점 신호로 작동하는 경우가 많아 부호가 불안정하다. 점수에서는 빼고
    LLM 최종 검토에 참고 정보로만 넘긴다.

공포 게이트는 미국의 VIX 대신 코스피 20일 실현변동성(연율%)을 쓴다.

2006~2026 실측 분포:
    중위 15.0% / 75%ile 20.4% / 90%ile 29.7% / 95%ile 41.7% / 99%ile 75.6%
    (리먼 2008-10 = 88.5%, 코로나 2020-03 = 69.9%)

하드블록은 90% 다 (운영자 지정). 실측 분포상 99.9%ile 근처라 매수를 멈추는 국면은
사실상 리먼을 넘어서는 극단뿐이고, 리먼·코로나급 폭락장에서도 매수는 진행된다.
대신 30% 위 구간에서 임계값을 계단식으로 올려 선별도를 높이는 것으로 위험을 흡수한다.
"""
from typing import List, Optional

# z-score 정규화와 사전 필터는 미국 트랙과 완전히 동일하므로 재사용한다
from app.services.scoring_service import apply_prefilters, cross_sectional_zscore

# ── 가중치 (z-score 기준이라 명목 = 실효 영향력) ──────────────────
W_RISE = 0.18   # ML 예측 상승률 — 보수적
W_TECH = 0.27   # 기술적 모멘텀 (MACD diff + SMA diff + RSI 평균)
W_FLOW = 0.15   # 외국인·기관 순매수 ★ 국내 전용
W_VOL = 0.15    # 거래량 비율 (가격-거래량 컨펌)
W_ADX = 0.15    # 추세 강도
W_SENT = 0.10   # 뉴스 감성 (학술 IC 가 약해 낮게)

BASE_THRESHOLD = 0.40

# 코스피 20일 실현변동성(연율%) 기준. 이 값 이하면 매수 추천이 가능하다.
# (실측 분포상 약 99.9%ile — 리먼 88.5% / 코로나 69.9% 도 통과한다)
FEAR_HARD_BLOCK = 90.0  # 초과 시에만 매수 전면 중단


def get_threshold(fear_index: Optional[float]) -> float:
    """
    변동성 기반 적응형 임계값.

    하드블록이 90% 로 올라가면서 매수 허용 구간이 크게 넓어졌다. 30~90% 를 한 칸으로
    두면 리먼급(85%) 장세를 '약간 불안'(31%)과 똑같은 선별도로 사게 되므로,
    그 구간을 계단으로 쪼개 변동성이 오를수록 상위 종목만 남도록 했다.

    | 20일 실현변동성 | 백분위      | 임계값 | 의미                     |
    |-----------------|-------------|--------|--------------------------|
    | None 또는 <20   | ~하위 74%   | 0.40   | 평온장                   |
    | 20~25           | 74~82%      | 0.45   | 약간 불안                |
    | 25~30           | 82~90%      | 0.55   | 불안 — 선별적            |
    | 30~40           | 90~95%      | 0.70   | 공포 — 매우 선별적       |
    | 40~55           | 95~98%      | 0.90   | 심한 공포 — 극도로 선별적|
    | 55~90           | 98~99.9%    | 1.10   | 폭락장 — 최상위만        |
    | >90             | 상위 0.1%   | 하드블록 (get_threshold 이전 단계에서 차단) |
    """
    if fear_index is None or fear_index < 20:
        return BASE_THRESHOLD
    if fear_index < 25:
        return BASE_THRESHOLD + 0.05
    if fear_index < 30:
        return BASE_THRESHOLD + 0.15
    if fear_index < 40:
        return BASE_THRESHOLD + 0.30
    if fear_index < 55:
        return BASE_THRESHOLD + 0.50
    return BASE_THRESHOLD + 0.70


def compute_scores(candidates: List[dict], fear_index: Optional[float]) -> None:
    """
    후보 리스트 전체를 cross-sectional 로 채점 (in-place).
    각 candidate 에 composite_score, kr_factors, fear_index, scoring_version 을 추가한다.
    """
    n = len(candidates)
    if n == 0:
        return

    rise_raw = [c.get("rise_probability", 0) or 0 for c in candidates]
    rsi_raw = [c.get("rsi", 50) or 50 for c in candidates]
    macd_diff_raw = [
        (c.get("macd", 0) or 0) - (c.get("signal", 0) or 0) for c in candidates
    ]
    # SMA 괴리율 — 절대가격 스케일 차이를 없애기 위해 비율로
    sma_diff_raw = [
        ((c.get("sma20", 0) or 0) / max(c.get("sma50", 1) or 1, 1e-9) - 1)
        for c in candidates
    ]
    sent_raw = [c.get("sentiment_score") for c in candidates]
    vol_raw = [c.get("volume_ratio") for c in candidates]
    adx_raw = [c.get("adx") for c in candidates]
    # 순매수 대금을 시가총액이 아닌 거래대금 대비 비율로 넣으면 대형주 편향이 줄지만,
    # 무료 소스로 일별 시총을 안정적으로 얻기 어려워 순매수 강도(원)를 그대로 z-score 화한다.
    flow_raw = [c.get("net_buy_score") for c in candidates]

    z_rise = cross_sectional_zscore(rise_raw)
    z_rsi = cross_sectional_zscore(rsi_raw)
    z_macd = cross_sectional_zscore(macd_diff_raw)
    z_sma = cross_sectional_zscore(sma_diff_raw)
    z_sent = cross_sectional_zscore(sent_raw)
    z_vol = cross_sectional_zscore(vol_raw)
    z_adx = cross_sectional_zscore(adx_raw)
    z_flow = cross_sectional_zscore(flow_raw)

    z_tech = [(z_macd[i] + z_sma[i] + z_rsi[i]) / 3.0 for i in range(n)]

    for i, c in enumerate(candidates):
        composite = (
            W_RISE * z_rise[i]
            + W_TECH * z_tech[i]
            + W_FLOW * z_flow[i]
            + W_VOL * z_vol[i]
            + W_ADX * z_adx[i]
            + W_SENT * z_sent[i]
        )
        c["composite_score"] = round(composite, 4)
        c["kr_factors"] = {
            "z_rise": round(z_rise[i], 3),
            "z_tech": round(z_tech[i], 3),
            "z_macd": round(z_macd[i], 3),
            "z_sma": round(z_sma[i], 3),
            "z_rsi": round(z_rsi[i], 3),
            "z_flow": round(z_flow[i], 3),
            "z_vol": round(z_vol[i], 3),
            "z_adx": round(z_adx[i], 3),
            "z_sent": round(z_sent[i], 3),
        }
        c["fear_index"] = fear_index
        c["scoring_version"] = "kr-v1"


def score_and_filter(
    candidates: List[dict], fear_index: Optional[float]
) -> List[dict]:
    """
    사전 필터 → 채점 → 임계값 통과분만 composite_score 내림차순 반환.

    사전 필터는 미국 트랙과 공유한다 (RSI>80 하드블록 + 기술 신호 2개 이상).
    """
    passed = [c for c in candidates if apply_prefilters(c)]
    if not passed:
        return []

    compute_scores(passed, fear_index)
    threshold = get_threshold(fear_index)

    final = [c for c in passed if c["composite_score"] >= threshold]
    final.sort(key=lambda x: x["composite_score"], reverse=True)
    return final
