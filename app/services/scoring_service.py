"""
매수 후보 채점의 공용 원시 함수 (시장 무관).

여기에는 "어떤 후보를 아예 볼 것도 없이 버릴지"(사전 필터)와 "후보군 안에서 상대
위치를 어떻게 재는지"(cross-sectional z-score) 두 가지만 둔다. 팩터 가중치·임계값처럼
시장마다 달라지는 결정은 여기 없다 — 국내 트랙의 그 부분은
app/services/kr/kr_scoring.py 에 있다.

z-score 를 쓰는 이유: 팩터의 절대값은 종목·국면마다 스케일이 제각각이라 그대로
가중합하면 명목 가중치와 실효 영향력이 어긋난다. 후보군 안에서 표준화한 뒤 더하면
가중치가 곧 실효 영향력이 된다.

참조: documents/10_멀티팩터_변별력_개선_기획.md
"""
from typing import List, Optional

import numpy as np


def apply_prefilters(item: dict) -> bool:
    """
    매수 후보 사전 필터.
      1. RSI > 80 하드블록 (과매수 제외)
      2. 기술 신호 2개 이상 충족 (골든크로스 / RSI 매수구간 / MACD 매수)

    Returns:
        True  → 후보 유지
        False → 후보 제외
    """
    rsi = item.get("rsi", 50)
    if rsi > 80:
        return False

    rsi_buy = rsi <= 65
    tech_signals = [
        bool(item.get("golden_cross")),
        rsi_buy,
        bool(item.get("macd_buy_signal")),
    ]
    if sum(tech_signals) < 2:
        return False

    return True


WINSOR_LIMIT = 3.0  # ±3σ 클립


def cross_sectional_zscore(values: List[Optional[float]]) -> List[float]:
    """후보군 내 z-score 정규화. None 은 평균(0) 처리, ±3σ winsorize."""
    arr = np.array(
        [float(v) if v is not None else np.nan for v in values],
        dtype=float,
    )
    mean = np.nanmean(arr)
    std = np.nanstd(arr)
    if not np.isfinite(std) or std < 1e-9:
        return [0.0] * len(values)
    z = (arr - mean) / std
    z = np.nan_to_num(z, nan=0.0, posinf=WINSOR_LIMIT, neginf=-WINSOR_LIMIT)
    z = np.clip(z, -WINSOR_LIMIT, WINSOR_LIMIT)
    return [float(x) for x in z]
