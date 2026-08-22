"""
확신도 가중 포지션 사이징 (시장 무관 공용 모듈).

기존 방식은 통과한 모든 종목에 총자산의 같은 비율(기본 10%)을 배분했다.
이 모듈은 **종합점수가 높은 종목에 더 많이 배분**한다.

설계 원칙 — composite_score 를 비중에 그대로 쓰지 않는다:
  - z-score 기반이라 음수가 나올 수 있다 (음수 비중은 의미가 없다)
  - 후보군·시장 국면마다 스케일이 달라진다. 어떤 날은 점수가 0.4~1.3, 어떤 날은
    0.4~0.5 로 몰린다. 원점수에 비례시키면 전자에서는 한 종목에 과도하게 쏠리고
    후자에서는 사실상 균등이 된다.
  → 그래서 **후보군 내 상대 위치(0~1)** 로 먼저 정규화한 뒤 배율을 씌운다.

배율 공식:
    mult_i = 1 + tilt × (2×rel_i - 1)             # tilt=0.5 → 0.5배 ~ 1.5배
    slot_i = base_ratio × mult_i                  # 총자산 대비 비율

  tilt = 0    → 전부 base_ratio (기존 균등 배분과 완전히 동일)
  tilt = 0.5  → 최고 1.5×base, 최저 0.5×base, 중간 1.0×base
  tilt = 1.0  → 최고 2.0×base, 최저 0×base

rel_i(후보군 내 상대 강도 0~1)를 구하는 방식이 두 가지다.

  method="rank" (기본) — 점수 순위로 등간격 배치
      rel_i = (N-1-순위) / (N-1)      # 동점은 평균 순위
      · 배율 평균이 정확히 1 이라 **총 투입액이 종목 수 × base 로 고정**된다
      · 이상치에 흔들리지 않는다
      · 대신 점수 차의 크기는 무시한다 (1등이 압도적이어도 2등과 한 칸 차이)

  method="score" — 점수 값을 min-max 정규화
      rel_i = (score_i - min) / (max - min)
      · 점수 차의 크기를 반영한다
      · 단점: 이상치 하나가 나머지를 전부 바닥으로 눌러버린다. 실측 예로
        [1.301, 0.552, 0.531, 0.528, 0.517, 0.510, 0.464] 에 tilt=0.5 를 주면
        1등만 15%, 나머지 6종목이 하한에 붙어 총 투입이 70%→48.8% 로 급감했다.
        "얼마나 살지"는 임계값이 이미 결정한 몫인데 사이징이 이를 흔드는 셈이다.

기본값을 rank 로 둔 이유가 이것이다 — 배분 강도는 tilt 로 조절하고,
총 투입액은 종목 수가 결정하도록 역할을 분리한다.

안전장치:
  - min_ratio / max_ratio 로 개별 종목 비중을 클램프 (한 종목 몰빵·먼지 주문 방지)
  - max_total_exposure 로 총 투입 비율 상한 (현금 소진 방지). 초과 시 비례 축소하며,
    이 상한은 min_ratio 보다 우선한다.
  - 후보가 1개이거나 점수가 모두 같으면 rel 을 0.5 로 봐서 전부 base_ratio 를 받는다
    (분모 0 방지 + '비교 대상이 없으면 기울이지 않는다'는 원칙)
"""
from typing import List, Optional, Sequence


def _relative_strength(values: List[float], method: str) -> List[float]:
    """후보군 내 상대 강도 0~1. 자세한 차이는 모듈 docstring 참조."""
    n = len(values)
    if n == 1:
        return [0.5]

    if method == "score":
        lo, hi = min(values), max(values)
        span = hi - lo
        if span < 1e-9:
            return [0.5] * n
        return [(v - lo) / span for v in values]

    # rank (기본) — 동점은 평균 순위를 주어 같은 비중을 받게 한다
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return [r / (n - 1) for r in ranks]


def compute_weighted_slots(
    scores: Sequence[Optional[float]],
    base_ratio: float,
    tilt: float = 0.0,
    min_ratio: float = 0.0,
    max_ratio: float = 1.0,
    max_total_exposure: float = 1.0,
    method: str = "rank",
) -> List[float]:
    """
    종합점수 → 종목별 투자 비율(총자산 대비) 리스트.

    Args:
        scores:             후보별 composite_score. None 은 최저 점수로 취급한다.
        base_ratio:         기준 슬롯 비율 (예: 0.10)
        tilt:               가중 강도 0~1. 0 이면 균등 배분.
        min_ratio:          종목별 최소 비율 (먼지 주문 방지)
        max_ratio:          종목별 최대 비율 (한 종목 쏠림 방지)
        max_total_exposure: 총 투입 비율 상한 (예: 0.80)
        method:             "rank"(기본, 총 투입액 고정) 또는 "score"(점수 차 반영)

    Returns:
        입력과 같은 길이·순서의 비율 리스트.
    """
    n = len(scores)
    if n == 0:
        return []

    tilt = max(0.0, min(float(tilt), 1.0))

    # None 은 비교에서 최저로 — 점수를 못 구한 종목에 더 주지 않는다
    valid = [s for s in scores if s is not None]
    fallback = min(valid) if valid else 0.0
    values = [float(s) if s is not None else fallback for s in scores]

    if tilt == 0.0:
        ratios = [base_ratio] * n
    else:
        rel = _relative_strength(values, method)
        ratios = [base_ratio * (1.0 + tilt * (2.0 * r - 1.0)) for r in rel]

    # 개별 상·하한
    ratios = [max(min_ratio, min(r, max_ratio)) for r in ratios]

    # 총 노출 상한 — 개별 하한보다 우선한다 (현금 부족이 더 큰 문제)
    total = sum(ratios)
    if max_total_exposure > 0 and total > max_total_exposure:
        scale = max_total_exposure / total
        ratios = [r * scale for r in ratios]

    return ratios


def describe_allocation(
    names: Sequence[str],
    scores: Sequence[Optional[float]],
    ratios: Sequence[float],
    total_assets: float,
) -> List[str]:
    """로그·알림용 배분 내역 문자열. 사이징 결과를 눈으로 확인할 때 쓴다."""
    lines = []
    for name, score, ratio in zip(names, scores, ratios):
        score_str = f"{score:+.3f}" if score is not None else "  N/A"
        lines.append(
            f"    {str(name)[:14]:16s} score={score_str} "
            f"비중 {ratio * 100:5.2f}%  {total_assets * ratio:>13,.0f}"
        )
    return lines
