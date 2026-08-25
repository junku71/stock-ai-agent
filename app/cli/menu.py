"""
번호 입력식 운영 메뉴.

매매 스케줄러가 백그라운드에서 도는 동안 같은 터미널에서 상시로 쓴다. 조회는 물론
신규 종목 추천 파이프라인도 여기서 손으로 돌려볼 수 있다.

## 설계

- **서비스 함수를 직접 부른다** (HTTP 경유 X). 같은 프로세스라 포트·인증이 필요 없고,
  FastAPI 를 안 띄운 상태에서도 메뉴만 단독으로 돌릴 수 있다.
- **매매를 직접 실행하는 항목은 확인 입력을 받는다.** 조회와 실행이 같은 번호판에
  섞여 있어서, 오타 하나로 주문이 나가면 안 된다.
- **오래 걸리는 작업은 console_logging() 으로 진행 상황을 보여준다.** 스크리닝 한 번에
  몇 분이 걸리는데 화면이 멈춰 있으면 죽은 것처럼 보인다.
- 어떤 항목에서 예외가 나도 메뉴는 죽지 않는다. 매매 스케줄러와 한 프로세스라
  메뉴가 죽으면 사용자가 프로세스를 통째로 재시작하게 되고, 그게 더 위험하다.
"""
import logging
import traceback
import unicodedata
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import pytz

from app.core.config import settings
from app.core.logging_config import console_logging, log_path

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

_LINE = "─" * 72

# 직전 실행 결과 보관 (2단계만 따로 돌리거나 결과를 다시 보기 위해)
_last: dict = {"stage1": None, "screening": None}


# ══════════════════════════════════════════════════════════════════
# 출력 헬퍼
# ══════════════════════════════════════════════════════════════════

def _won(v) -> str:
    if v is None:
        return "N/A"
    return f"{v:,.0f}원"


def _pct(v, digits: int = 2) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.{digits}f}%"


def _title(text: str) -> None:
    print()
    print(_LINE)
    print(f"  {text}")
    print(_LINE)


def _pause() -> None:
    try:
        input("\n[Enter] 메뉴로 돌아가기 ")
    except (EOFError, KeyboardInterrupt):
        pass


def _confirm(prompt: str) -> bool:
    """실주문처럼 되돌리기 어려운 동작 전 확인. 'y' 정확 입력만 통과."""
    try:
        answer = input(f"{prompt} (진행하려면 y 입력) ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"


def _mode_tag() -> str:
    if settings.KR_DRY_RUN:
        return "드라이런"
    return "모의투자" if settings.KIS_USE_MOCK else "실전투자"


# ══════════════════════════════════════════════════════════════════
# 1. 시스템 상태
# ══════════════════════════════════════════════════════════════════

def show_status() -> None:
    from app.services.kr import kis_domestic_service as kis
    from app.services.kr import kr_quant_data_service, kr_universe_service
    from app.services import buy_switch_service
    from app.utils import kr_scheduler

    _title("시스템 상태")
    now = datetime.now(KST)
    print(f"  현재 시각      : {now:%Y-%m-%d %H:%M:%S} KST")
    print(f"  계좌 모드      : {_mode_tag()}")
    print(f"  영업일 여부    : {'예' if kis.is_business_day(now) else '아니오 (휴장)'}")
    print(f"  장 운영 중     : {'예' if kis.is_market_open(now) else '아니오'}")
    print(f"  매도 감시 구간 : {'예' if kis.is_sell_window(now) else '아니오'}")

    try:
        status = kr_scheduler.get_kr_scheduler_status()
        print(f"\n  스케줄러 스레드: {'살아있음' if status.get('thread_alive') else '중지'}")
        for job in status.get("jobs", []) or []:
            print(f"    - {job.get('name', '?')}: 다음 {job.get('next_run', '?')}")
    except Exception as e:
        print(f"\n  스케줄러 상태 조회 실패: {e}")

    try:
        sw = buy_switch_service.get_status()
        state = "허용" if sw["buy_enabled"] else "중단"
        detail = "" if sw["buy_enabled"] else f" (사유: {sw.get('reason') or '없음'})"
        print(f"\n  신규 매수      : {state}{detail}")
    except Exception as e:
        print(f"\n  매수 스위치 조회 실패: {e}")

    quant = kr_quant_data_service.cache_status()
    if quant is None:
        print("\n  퀀트데이터 캐시: 없음 (1단계 첫 실행 시 자동 다운로드)")
    else:
        age = quant.get("age_days")
        age_text = f"{age:.1f}일 전 확인" if age is not None else "확인 시각 불명"
        print(f"\n  퀀트데이터 캐시: {quant.get('source_date') or '?'}자 파일, {age_text} "
              f"(1~2단계 재무/시총 소스, {settings.KR_QUANT_CACHE_DAYS:.0f}일 주기 갱신)")

    age = kr_universe_service.cache_age_days()
    print(f"  동적 유니버스 캐시: "
          f"{'없음' if age is None else f'{age:.1f}일 전 생성'}"
          f" / 목표 {settings.KR_UNIVERSE_SIZE}종목 "
          f"(메뉴 8·9번 전용 — 신규종목 추천 파이프라인은 고정 100종목+퀀트데이터를 씀)")
    print(f"  포트폴리오 한도: 최대 {settings.KR_MAX_POSITIONS}종목, "
          f"섹터당 {settings.KR_MAX_PER_SECTOR}종목")
    print(f"  추천 파이프라인 : 1단계 Feature통합 상위 {settings.KR_FEATURE_TOP_N}종목 → "
          f"2단계 ML필터 상위 {settings.KR_ML_FILTER_TOP_N}종목 → "
          f"3단계 LLM 매수 최대 {settings.KR_LLM_MAX_PICKS}종목, "
          f"리밸런싱 후 보유 {settings.KR_REBALANCE_MAX_POSITIONS}종목 이하")
    print(f"  로그 파일      : {log_path()}")
    _pause()


# ══════════════════════════════════════════════════════════════════
# 2. 잔고 / 보유 종목
# ══════════════════════════════════════════════════════════════════

def show_balance() -> None:
    from app.services.kr import kis_domestic_service as kis

    _title("잔고 · 보유 종목")
    with console_logging():
        balance = kis.get_balance()
        summary = kis.get_account_summary()

    if balance.get("rt_cd") != "0":
        print(f"  잔고 조회 실패: {balance.get('msg1', '')}")
        _pause()
        return

    holdings = balance.get("output1", [])
    if not holdings:
        print("  보유 종목이 없습니다.")
    else:
        print(f"  {'종목':<14}{'수량':>7}{'평단가':>12}{'현재가':>12}"
              f"{'평가손익':>15}{'수익률':>9}")
        print("  " + "─" * 70)
        for h in holdings:
            try:
                qty = int(h.get("hldg_qty", 0) or 0)
                buy = float(h.get("pchs_avg_pric", 0) or 0)
                cur = float(h.get("prpr", 0) or 0)
                pnl = float(h.get("evlu_pfls_amt", 0) or 0)
                rate = float(h.get("evlu_pfls_rt", 0) or 0)
            except (ValueError, TypeError):
                continue
            name = (h.get("prdt_name") or "")[:7]
            sign = "+" if pnl >= 0 else ""
            print(f"  {name:<14}{qty:>7,}{buy:>12,.0f}{cur:>12,.0f}"
                  f"{sign}{pnl:>14,.0f}{sign}{rate:>8.2f}%")

    if summary and summary.get("stock_purchase_amount", 0) > 0:
        pnl = summary["eval_profit_loss"]
        sign = "+" if pnl >= 0 else ""
        print(f"\n  주식 평가액 : {_won(summary['stock_eval_amount'])}")
        print(f"  매입 원금   : {_won(summary['stock_purchase_amount'])}")
        print(f"  평가 손익   : {sign}{pnl:,.0f}원 ({sign}{summary['eval_profit_loss_pct']:.2f}%)")
        print(f"  예수금(D+2) : {_won(summary['d2_deposit'])}")
    _pause()


def show_portfolio() -> None:
    from app.services.kr import kr_screening_service as screen

    _title("포트폴리오 현황")
    with console_logging():
        state = screen.get_portfolio_state()

    print(f"  보유 종목      : {state['held_count']}종목")
    print(f"  미체결 매수    : {len(state['pending_codes'])}종목")
    print(f"  점유 슬롯      : {state['occupied_slots']} / {state['max_positions']}")
    print(f"  신규 매수 여력 : {state['free_slots']}슬롯 "
          f"(3단계 LLM 진입 후보 최대 {min(state['free_slots'], settings.KR_STAGE2_TOP_N)}종목)")

    if state["sector_counts"]:
        print(f"\n  섹터 분포 (한 섹터 최대 {settings.KR_MAX_PER_SECTOR}종목)")
        for sec, n in sorted(state["sector_counts"].items(), key=lambda x: -x[1]):
            full = " ← 한도 도달" if n >= settings.KR_MAX_PER_SECTOR else ""
            print(f"    {sec:<24} {n}종목{full}")
    _pause()


# ══════════════════════════════════════════════════════════════════
# 3. 신규 종목 추천
# ══════════════════════════════════════════════════════════════════

def _print_pick(i: int, p: dict) -> None:
    print(f"\n  [{i}] {p['name']} ({p['code']})  섹터: {p.get('sector') or '미상'}")
    print(f"      종합점수(Feature 통합) {p.get('composite_score', 0):+.3f}")
    kf = p.get("kr_factors") or {}
    if kf:
        print(f"        재무 {kf.get('z_fund', 0):+.2f}  기술 {kf.get('z_tech', 0):+.2f}  "
              f"수급 {kf.get('z_flow', 0):+.2f}  거래량 {kf.get('z_vol', 0):+.2f}  "
              f"ADX {kf.get('z_adx', 0):+.2f}  감성 {kf.get('z_sent', 0):+.2f}  (z-score)")
    print(f"      ML 예측 상승률 {p.get('rise_probability', 0):+.2f}% "
          f"(정확도 {p.get('ml_accuracy', 0):.1f}%) — 2단계 ML필터 통과 기준, "
          f"1단계 종합점수에는 미반영")
    print(f"      기본적 분석 {p.get('fundamental_score', 0)}점 — {p.get('fundamental_reason', '')}")
    f = p.get("fundamentals") or {}
    print(f"        영업이익률 {f.get('operating_margin')}%  ROE {f.get('roe')}%  "
          f"ROIC {f.get('roic')}%  PER {f.get('per')}  부채비율 {f.get('debt_ratio')}%")
    print(f"      감성 {p.get('sentiment_score', 0):+.2f} "
          f"(기사 {p.get('article_count', 0)}건, 섹터 업황 {p.get('sector_sentiment', 0):+.2f})")
    print(f"      기술 신호 {p.get('signal_count', 0)}개: {', '.join(p.get('buy_signals') or [])}")
    flow = p.get("flow") or {}
    print(f"      수급: 외국인 {flow.get('foreign_streak', 0)}일 연속 / "
          f"기관 {flow.get('institution_streak', 0)}일 연속 "
          f"(순매수 강도 {p.get('net_buy_score', 0):,.0f}백만원)")
    print(f"      현재가 {_won(p.get('current_price'))}  ATR {p.get('atr')}")


def _print_proposal(proposal: dict, portfolio: dict) -> None:
    print("\n  " + "═" * 68)
    print("  ▶ 3단계 — LLM 매수 추천 + 리밸런싱 제안")
    print("  " + "═" * 68)

    if proposal.get("llm_failed"):
        print(f"  {proposal.get('summary')}")
        print("  ※ 제안을 만들지 못했습니다 (Fail-Close — 아무 조치도 취하지 않습니다).")
        return

    if proposal.get("summary"):
        print(f"\n  {proposal['summary']}")

    buy, sell = proposal.get("buy") or [], proposal.get("sell") or []
    if not buy and not sell:
        print("\n  LLM 이 이번 회차에는 매수·매도 모두 권하지 않았습니다.")
    for b in buy:
        cand = b.get("candidate") or {}
        print(f"\n  [매수] {b['stock_name']} ({b['code']})  확신도 {b['conviction']}/10")
        print(f"         섹터 {cand.get('sector_krx') or cand.get('sector') or '미상'} / "
              f"ML {cand.get('rise_probability', 0):+.2f}% / "
              f"재무 {cand.get('fundamental_score', 0)}점 / "
              f"감성 {cand.get('sentiment_score', 0):+.2f}")
        print(f"         {b.get('reason')}")
    for sl in sell:
        h = sl.get("holding") or {}
        print(f"\n  [매도] {sl['stock_name']} ({sl['code']})  전량 {h.get('quantity', 0):,}주")
        print(f"         매입 {_won(h.get('buy_price'))} → 현재 {_won(h.get('current_price'))} "
              f"({_pct(h.get('price_change_percent'))})")
        print(f"         {sl.get('reason')}")

    held = portfolio.get("held_count", 0)
    print(f"\n  집행 후 보유: {held} − {len(sell)} + {len(buy)} = "
          f"{proposal.get('after_positions', held)}종목 "
          f"(리밸런싱 상한 {settings.KR_REBALANCE_MAX_POSITIONS} / "
          f"하드 상한 {settings.KR_MAX_POSITIONS})")

    for w in proposal.get("warnings") or []:
        print(f"  ⚠ 제안 보정: {w}")


def _print_screening(result: dict) -> None:
    print(f"\n  {result['message']}  ({result.get('elapsed_sec', 0)}초 소요)")
    if result.get("buy_enabled") is False:
        print("  ※ 신규 매수 스위치가 꺼져 있습니다 — 승인해도 매수 주문은 나가지 않습니다.")

    finalists = result.get("finalists") or []
    if not finalists:
        print("\n  2단계(ML필터)를 통과한 후보가 없습니다.")
    else:
        print(f"\n  ▶ 2단계 ML필터 통과 {len(finalists)}종목 (3단계 LLM 진입 후보, "
              f"ML 상승확률 내림차순)")
        for i, p in enumerate(finalists, 1):
            _print_pick(i, p)

    skipped = result.get("skipped") or []
    if skipped:
        print(f"\n  ▶ 보류 {len(skipped)}종목 (2단계는 통과했으나 포트폴리오 규칙에 걸림)")
        for sk in skipped:
            print(f"    - {sk['name']} ({sk['code']}): {sk.get('skip_reason')}")

    s1, s2 = result.get("stage1"), result.get("stage2")
    print("\n  ▶ 단계별 통과 현황")
    if s1:
        print(f"    0단계 최소필터+1단계 병렬분석 : {len(s1.get('passed') or [])}종목 "
              f"({s1.get('elapsed_sec', 0)}초, 탈락 {len(s1.get('dropped') or [])}종목)")
    if s2:
        print(f"    1단계 Feature통합(상위{settings.KR_FEATURE_TOP_N}) "
              f"→ 2단계 ML필터(상위{settings.KR_ML_FILTER_TOP_N}) : "
              f"{len(s2.get('ranked') or [])}종목 ({s2.get('elapsed_sec', 0)}초, "
              f"Feature 통합 탈락 {s2.get('feature_dropped', 0)}종목, "
              f"ML 필터 탈락 {len(s2.get('ml_dropped') or [])}종목)")

    proposal = result.get("proposal")
    if proposal:
        _print_proposal(proposal, result.get("portfolio") or {})


def _select_indices(prompt: str, total: int) -> Optional[List[int]]:
    """
    '1,3' / 'a'(전체) / 'n'(없음) 입력을 인덱스 목록으로. 취소면 None.

    잘못 입력했을 때 되묻지 않고 바로 취소로 처리한다 — 승인 화면에서 애매한 입력을
    '전체 승인'으로 해석하면 사고가 난다.
    """
    try:
        raw = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw in ("n", "none", ""):
        return []
    if raw in ("a", "all"):
        return list(range(total))
    picked = []
    for token in raw.replace(" ", "").split(","):
        if not token.isdigit():
            print(f"    '{token}' 은 번호가 아닙니다 — 취소합니다.")
            return None
        idx = int(token) - 1
        if not (0 <= idx < total):
            print(f"    {token} 번은 목록에 없습니다 — 취소합니다.")
            return None
        picked.append(idx)
    return sorted(set(picked))


def _approve_proposal(result: dict) -> None:
    """
    리밸런싱 제안을 사람이 승인하는 흐름.

    매수/매도를 따로 물어본다. 하나는 동의하고 하나는 아닌 경우가 흔한데, 한 번에
    묶어서 물으면 그런 선택을 할 수 없다.
    """
    from app.services.kr import kr_rebalance_service

    proposal = result.get("proposal")
    if not proposal or proposal.get("llm_failed"):
        return
    buy, sell = proposal.get("buy") or [], proposal.get("sell") or []
    if not buy and not sell:
        return

    print("\n  " + "─" * 68)
    print("  리밸런싱 승인 — 집행할 항목을 고르세요")
    print("  (번호는 쉼표로 여러 개, a=전체, n=없음, 그 외 입력은 취소)")
    print("  " + "─" * 68)

    approved_buy: List[dict] = []
    if buy:
        print("\n  매수 후보:")
        for i, b in enumerate(buy, 1):
            print(f"    {i}. {b['stock_name']} ({b['code']}) 확신도 {b['conviction']}/10")
        idx = _select_indices("\n  매수 승인 > ", len(buy))
        if idx is None:
            print("  취소했습니다. 아무것도 집행하지 않습니다.")
            return
        approved_buy = [buy[i] for i in idx]

    approved_sell: List[dict] = []
    if sell:
        print("\n  매도 후보 (전량매도):")
        for i, sl in enumerate(sell, 1):
            h = sl.get("holding") or {}
            print(f"    {i}. {sl['stock_name']} ({sl['code']}) "
                  f"{h.get('quantity', 0):,}주 {_pct(h.get('price_change_percent'))}")
        idx = _select_indices("\n  매도 승인 > ", len(sell))
        if idx is None:
            print("  취소했습니다. 아무것도 집행하지 않습니다.")
            return
        approved_sell = [sell[i] for i in idx]

    if not approved_buy and not approved_sell:
        print("\n  승인된 항목이 없습니다. 아무것도 집행하지 않습니다.")
        return

    print(f"\n  승인: 매수 {len(approved_buy)}종목 / 매도 {len(approved_sell)}종목")
    print("  매수는 매수 큐에, 매도는 매도 판정에 적재됩니다.")
    print(f"  집행 시점 — 매수: 다음 {settings.KR_EXECUTION_TIME} 또는 메뉴 13번 / "
          f"매도: 다음 매도 감시 사이클 또는 메뉴 14번")
    if not _confirm("\n  적재할까요?"):
        print("  취소했습니다.")
        return

    with console_logging():
        outcome = kr_rebalance_service.apply_approved(approved_buy, approved_sell)
    print(f"\n  매수 큐 {outcome['queued_buy']}건 / 매도 판정 {outcome['queued_sell']}건 적재")
    for err in outcome.get("errors") or []:
        print(f"  ⚠ {err}")


def run_full_screening() -> None:
    from app.services.kr import kr_screening_service as screen

    _title("신규 종목 추천 — 전체 스크리닝")
    print("  고정 100종목 → 최소필터(유동성·재무건전성) → 1단계(재무+기술+수급 병렬분석 →")
    print(f"  감성분석 → Feature통합 상위 {settings.KR_FEATURE_TOP_N}종목) → 2단계(ML필터,")
    print(f"  상승확률·정확도 상위 {settings.KR_ML_FILTER_TOP_N}종목) → 3단계(LLM 종합판단,")
    print(f"  Risk/Market국면 감안 최대 {settings.KR_LLM_MAX_PICKS}종목 매수 추천 + 리밸런싱 제안)")
    print("  → 사용자 승인 순으로 진행합니다. 승인하기 전에는 어떤 주문도 나가지 않습니다.")
    print("  퀀트데이터 캐시가 없거나 만료됐으면 첫 다운로드가 추가로 걸립니다"
          "(보통 수십 초, 매수 신호 계산은 종목당 KIS 조회가 있어 전체 수 분 소요).\n")
    if not _confirm("  시작할까요?"):
        print("  취소했습니다.")
        _pause()
        return

    try:
        with console_logging():
            result = screen.run_screening(progress=True)
        _last["screening"] = result
        _last["stage1"] = result.get("stage1")
        _print_screening(result)
        _approve_proposal(result)
    except Exception as e:
        print(f"\n  스크리닝 실패: {e}")
        logger.error("스크리닝 실패", exc_info=True)
        traceback.print_exc()
    _pause()


def run_stage1_only() -> None:
    from app.services.kr import kr_screening_service as screen

    _title("0~1단계만 실행 — 최소필터 + 병렬분석(재무·기술·수급)")
    if not _confirm("  시작할까요?"):
        print("  취소했습니다.")
        _pause()
        return
    try:
        with console_logging():
            result = screen.run_stage1(progress=True)
        _last["stage1"] = result
        print(f"\n  {result['message']}  ({result.get('elapsed_sec', 0)}초)")
        passed = result.get("passed") or []
        for i, p in enumerate(passed[:30], 1):
            print(f"    {i:>2}. {p['name']:<12} ({p['code']}) "
                  f"재무 {p.get('fundamental_score') if p.get('fundamental_score') is not None else 'N/A'}점  "
                  f"기술신호 {p.get('signal_count', 0)}개  "
                  f"{p.get('sector') or '미상'}")
        if len(passed) > 30:
            print(f"    ... 외 {len(passed) - 30}종목")
        print("\n  → 메뉴 6번으로 이 결과에 2단계를 이어서 돌릴 수 있습니다.")
    except Exception as e:
        print(f"\n  0~1단계 실패: {e}")
        logger.error("0~1단계 실패", exc_info=True)
    _pause()


def run_stage2_only() -> None:
    from app.services.kr import kr_screening_service as screen

    _title("1단계 마무리(감성·Feature통합) → 2단계(ML필터) → 3단계(LLM종합판단) 실행")
    s1 = _last.get("stage1")
    if not s1 or not s1.get("passed"):
        print("  먼저 5번(0~1단계)을 실행해 주세요. 직전 1단계 결과가 없습니다.")
        _pause()
        return
    print(f"  직전 1단계 통과 {len(s1['passed'])}종목으로 진행합니다.")
    if not _confirm("  시작할까요?"):
        print("  취소했습니다.")
        _pause()
        return
    try:
        from app.services.kr import kr_market_data_service, kr_rebalance_service

        with console_logging():
            fear_index = None
            try:
                fear_index = kr_market_data_service.get_market_context().get("kospi_vol_20d")
            except Exception as e:
                logger.warning(f"  시장 국면 조회 실패(평온장 기준으로 진행): {e}")
            result = screen.run_stage2(s1["passed"], fear_index=fear_index)
            state = screen.get_portfolio_state()
            state["fear_index"] = fear_index
            finalists, skipped = screen._apply_portfolio_rules(result["ranked"], state)
            proposal = None
            if finalists:
                holdings_ctx = screen.get_holdings_context()
                proposal = kr_rebalance_service.propose(finalists, holdings_ctx, state)
        merged = {
            "message": result["message"], "finalists": finalists, "skipped": skipped,
            "proposal": proposal, "portfolio": state, "stage1": s1, "stage2": result,
            "elapsed_sec": result.get("elapsed_sec", 0),
        }
        _last["screening"] = merged
        _print_screening(merged)
        _approve_proposal(merged)
    except Exception as e:
        print(f"\n  2단계 실패: {e}")
        logger.error("2단계 실패", exc_info=True)
    _pause()


def show_last_result() -> None:
    _title("마지막 추천 결과")
    result = _last.get("screening")
    if not result:
        print("  아직 스크리닝을 실행하지 않았습니다.")
    else:
        _print_screening(result)
        _approve_proposal(result)
    _pause()


# ══════════════════════════════════════════════════════════════════
# 4. 유니버스 / 데이터
# ══════════════════════════════════════════════════════════════════

def refresh_universe() -> None:
    from app.services.kr import kr_universe_service

    _title("유니버스 갱신 — KOSPI 시가총액 상위 재조회")
    print(f"  KOSPI 보통주 전 종목의 시가총액을 조회해 상위 {settings.KR_UNIVERSE_SIZE}종목을 뽑습니다.")
    print("  ※ 신규종목 추천 파이프라인(메뉴 4~6번)은 이제 이 결과를 안 씁니다 — 고정")
    print("    100종목(universe.py) + 퀀트데이터로 시총·업종을 따로 채웁니다. 이 메뉴는")
    print("    현재 KOSPI 시총 순위를 직접 확인하거나, 메뉴 9번(유니버스 동기화) 전에")
    print("    최신 순위를 받아둘 때만 필요합니다.")
    print("  종목당 1회 호출이라 8분 안팎 걸립니다. 그동안 매매 스케줄러는 계속 동작합니다.\n")
    if not _confirm("  시작할까요?"):
        print("  취소했습니다.")
        _pause()
        return
    try:
        with console_logging():
            items = kr_universe_service.get_universe(force_refresh=True, progress=True)
        print(f"\n  갱신 완료: {len(items)}종목")
        for it in items[:15]:
            cap = it.get("market_cap")
            print(f"    {it.get('rank', 0):>3}. {it['name']:<14} ({it['code']}) "
                  f"{cap:>12,.0f}억원" if cap else f"    {it['name']}")
        print(f"    ... 외 {max(len(items) - 15, 0)}종목")
    except Exception as e:
        print(f"\n  유니버스 갱신 실패: {e}")
        logger.error("유니버스 갱신 실패", exc_info=True)
    _pause()


def sync_universe_files() -> None:
    from app.services.kr import kr_universe_service

    _title("유니버스 동기화 산출물 생성")
    print("  ML 학습 대상(고정 유니버스)을 현재 시총 상위 목록에 맞추려면 세 파일을")
    print("  함께 고쳐야 합니다. 그 조각을 파일로 만들어 드립니다 (자동 반영하지 않습니다).")
    print("    1) app/services/kr/universe.py 의 UNIVERSE")
    print("    2) sql/kr/setup_kr.sql 의 종가 컬럼")
    print("    3) kaggle_notebook_kr/predict_kr.py 의 TARGET_COLUMNS\n")
    try:
        with console_logging():
            out = kr_universe_service.sync_static_universe()
        print(f"  생성 위치: {out['out_dir']}  ({out['size']}종목)")
        for f in out["files"]:
            print(f"    - {f}")
        print("\n  ※ ML 학습 대상을 바꾸면 과거 학습 데이터의 컬럼과 어긋날 수 있습니다.")
        print("     내용을 확인한 뒤 반영하고, setup_kr.sql 재실행 → Kaggle 재학습을 하세요.")
    except Exception as e:
        print(f"\n  산출물 생성 실패: {e}")
        logger.error("유니버스 동기화 실패", exc_info=True)
    _pause()


def collect_market_data() -> None:
    from app.services.kr import kr_market_data_service as md

    _title("시장 데이터 수집")
    full = _confirm("  전체 백필로 실행할까요? (아니면 증분 수집)")
    try:
        with console_logging():
            result = md.collect_market_data(force_full=full)
        print(f"\n  {result.get('message', result)}")
    except Exception as e:
        print(f"\n  수집 실패: {e}")
        logger.error("시장 데이터 수집 실패", exc_info=True)
    _pause()


def trigger_ml() -> None:
    from app.services import ml_trigger_service

    _title("ML 예측 트리거 (Kaggle)")
    print("  Kaggle 커널을 push 하고 완료까지 최대 15분 대기합니다.\n")
    if not _confirm("  시작할까요?"):
        print("  취소했습니다.")
        _pause()
        return
    try:
        with console_logging():
            ok, msg, meta = ml_trigger_service.trigger_and_wait()
        print(f"\n  {'성공' if ok else '실패'}: {msg}")
        print(f"  소요 {meta.get('elapsed_sec', 0)}초 / 최종 상태 {meta.get('final_status')}")
    except Exception as e:
        print(f"\n  ML 트리거 실패: {e}")
        logger.error("ML 트리거 실패", exc_info=True)
    _pause()


# ══════════════════════════════════════════════════════════════════
# 5. 매매
# ══════════════════════════════════════════════════════════════════

def show_sell_candidates() -> None:
    from app.services.kr import kr_recommendation_service as recommend

    _title("매도 후보 (기계적 규칙)")
    try:
        with console_logging():
            result = recommend.get_mechanical_sell_candidates()
        print(f"\n  {result.get('message')}")
        for c in result.get("sell_candidates", []):
            print(f"\n    {c['stock_name']} ({c['code']})  {c['quantity']}주 전량")
            print(f"      매입 {_won(c['buy_price'])} → 현재 {_won(c['current_price'])} "
                  f"({_pct(c['price_change_percent'])})")
            for r in c["sell_reasons"]:
                print(f"      · {r}")
    except Exception as e:
        print(f"\n  매도 후보 조회 실패: {e}")
        logger.error("매도 후보 조회 실패", exc_info=True)
    _pause()


def execute_buy_queue() -> None:
    from app.utils import kr_scheduler

    _title("매수 집행 (kr_buy_queue)")
    print(f"  계좌 모드: {_mode_tag()}")
    print("  큐에 쌓인 매수 예약을 지금 집행합니다. 실제 주문이 나갈 수 있습니다.\n")
    if not _confirm("  정말 집행할까요?"):
        print("  취소했습니다.")
        _pause()
        return
    try:
        with console_logging():
            result = kr_scheduler.run_buy_execution_now(force=True)
        print(f"\n  결과: {result}")
    except Exception as e:
        print(f"\n  매수 집행 실패: {e}")
        logger.error("매수 집행 실패", exc_info=True)
    _pause()


def execute_sell_check() -> None:
    from app.utils import kr_scheduler

    _title("매도 감시 1회 실행")
    print(f"  계좌 모드: {_mode_tag()}")
    print("  기계적 매도 규칙 + LLM 매도판정을 지금 한 번 돌립니다. 실제 주문이 나갈 수 있습니다.\n")
    if not _confirm("  정말 실행할까요?"):
        print("  취소했습니다.")
        _pause()
        return
    try:
        with console_logging():
            result = kr_scheduler.run_auto_sell_now()
        print(f"\n  결과: {result}")
    except Exception as e:
        print(f"\n  매도 감시 실패: {e}")
        logger.error("매도 감시 실패", exc_info=True)
    _pause()


def toggle_buy_switch() -> None:
    from app.services import buy_switch_service

    _title("신규 매수 스위치")
    try:
        status = buy_switch_service.get_status()
    except Exception as e:
        print(f"  상태 조회 실패: {e}")
        _pause()
        return

    print(f"  현재: {'허용' if status['buy_enabled'] else '중단'}")
    if status.get("reason"):
        print(f"  사유: {status['reason']}")
    print(f"  변경: {status.get('updated_at') or '-'}")
    print("\n  이 스위치는 신규 매수만 막습니다. 매도 감시·정합성 확인은 계속 동작합니다.")

    if status["buy_enabled"]:
        if _confirm("\n  신규 매수를 중단할까요?"):
            try:
                reason = input("  중단 사유 (감사 로그에 남습니다): ").strip()
            except (EOFError, KeyboardInterrupt):
                reason = ""
            if not reason:
                print("  사유가 없어 취소했습니다.")
            else:
                buy_switch_service.set_buy_enabled(False, reason=reason)
                print("  신규 매수를 중단했습니다.")
    else:
        if _confirm("\n  신규 매수를 재개할까요?"):
            buy_switch_service.set_buy_enabled(True)
            print("  신규 매수를 재개했습니다.")
    _pause()


def manage_fear_gate() -> None:
    """
    변동성 게이트(공포장 매수 하드블록) 상태 조회 + 수동 오버라이드 발급/해제.

    기존에는 kr_override_service 를 API(POST /kr/fear-gate/override)로만 건드릴 수
    있었다. 운영자가 메뉴만 쓰는 상황에서 공포장에 막혀도 승인 경로가 없었으므로,
    같은 안전장치(확인 문구·사유 필수·자동 만료)를 메뉴에도 그대로 노출한다.
    """
    from app.services.kr import kr_market_data_service, kr_override_service, kr_scoring

    _title("변동성 게이트 (공포장 매수 하드블록)")
    try:
        market = kr_market_data_service.get_market_context()
        fear = market.get("kospi_vol_20d")
    except Exception as e:
        print(f"  시장 상태 조회 실패: {e}")
        fear = None

    try:
        override = kr_override_service.describe()
    except Exception as e:
        print(f"  오버라이드 상태 조회 실패: {e}")
        _pause()
        return

    blocked = fear is not None and fear > kr_scoring.FEAR_HARD_BLOCK and not override["active"]
    fear_text = f"{fear:.1f}%" if fear is not None else "조회 불가"
    print(f"  코스피 20일 실현변동성 : {fear_text}  (하드블록 기준 {kr_scoring.FEAR_HARD_BLOCK:.0f}%)")
    print(f"  매수 하드블록 상태     : {'차단됨' if blocked else '정상(매수 가능)'}")
    print(f"  오버라이드             : {override['message']}")

    if override["active"]:
        print(f"\n  발급 사유: {override.get('reason')}")
        print(f"  잔여 시간: {override.get('remaining_minutes')}분")
        print(f"  임계값 완화(relax_threshold): {override.get('relax_threshold')}")
        if _confirm("\n  지금 오버라이드를 해제하고 게이트를 정상 복구할까요?"):
            try:
                note = input("  해제 사유 (선택, Enter 시 '수동 해제'): ").strip() or "수동 해제"
            except (EOFError, KeyboardInterrupt):
                note = "수동 해제"
            result = kr_override_service.revoke_override(note=note)
            print(f"  {'해제했습니다.' if result['revoked'] else result['message']}")
        _pause()
        return

    print(
        "\n  이 오버라이드는 '하드블록만' 해제합니다 — 변동성 기반 선별 임계값은 그대로라\n"
        "  폭락장에서는 여전히 상위 종목만 통과합니다. 발급/해제 시 Slack 으로 즉시 통지되고\n"
        "  DB 에 사유·발급자 행위가 감사 기록으로 남으며, 지정 시간이 지나면 자동 복구됩니다."
    )
    if not _confirm("\n  공포장이어도 매수를 열어두도록 지금 오버라이드를 발급할까요?"):
        print("  취소했습니다.")
        _pause()
        return

    try:
        confirm = input(
            f'  확인 문구 "{kr_override_service.CONFIRM_PHRASE}" 를 정확히 입력하세요: '
        ).strip()
        reason = input("  발급 사유 (5자 이상, 감사 로그에 남습니다): ").strip()
        minutes_raw = input("  유효 시간(분, Enter 시 120분): ").strip()
        relax_raw = input("  변동성 선별 임계값도 함께 완화할까요? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  취소했습니다.")
        _pause()
        return

    try:
        minutes = int(minutes_raw) if minutes_raw else kr_override_service.DEFAULT_MINUTES
    except ValueError:
        print(f"  '{minutes_raw}' 은(는) 숫자가 아닙니다. 취소했습니다.")
        _pause()
        return

    try:
        record = kr_override_service.create_override(
            confirm=confirm,
            reason=reason,
            minutes=minutes,
            relax_threshold=(relax_raw == "y"),
        )
        print(
            f"\n  발급했습니다. {record['expires_at']} 까지 매수 하드블록이 해제됩니다."
            f" (사유: {record['reason']})"
        )
    except kr_override_service.OverrideError as e:
        print(f"\n  발급 실패: {e}")
    _pause()


def send_report() -> None:
    from app.services.kr import kr_report_service

    _title("분석 리포트 전송")
    if not _confirm("  직전 파이프라인 결과로 리포트를 만들어 Slack 에 보낼까요?"):
        print("  취소했습니다.")
        _pause()
        return
    try:
        with console_logging():
            result = kr_report_service.build_and_send_report()
        print(f"\n  결과: {result}")
    except Exception as e:
        print(f"\n  리포트 전송 실패: {e}")
        logger.error("리포트 전송 실패", exc_info=True)
    _pause()


# ══════════════════════════════════════════════════════════════════
# 메뉴 정의 / 루프
# ══════════════════════════════════════════════════════════════════

MenuItem = Tuple[str, str, Optional[Callable[[], None]]]

MENU: List[MenuItem] = [
    ("", "── 상태 ──", None),
    ("1", "시스템 상태 (스케줄러 · 장 운영 · 설정)", show_status),
    ("2", "잔고 · 보유 종목", show_balance),
    ("3", "포트폴리오 현황 (슬롯 · 섹터 분포)", show_portfolio),
    ("", "── 신규 종목 추천 ──", None),
    ("4", "전체 스크리닝(1~3단계) + LLM 리밸런싱 제안 → 승인", run_full_screening),
    ("5", "1단계 앞부분만 (최소필터 + 재무·기술·수급 병렬분석)", run_stage1_only),
    ("6", "1단계 마무리(감성)+2단계(ML필터)+3단계(LLM), 직전 5번 결과로", run_stage2_only),
    ("7", "마지막 추천 결과 다시 보기 / 재승인", show_last_result),
    ("", "── 유니버스 · 데이터 ──", None),
    ("8", "유니버스 갱신 (KOSPI 시총 상위 재조회)", refresh_universe),
    ("9", "유니버스 동기화 산출물 생성 (ML 컬럼 3곳)", sync_universe_files),
    ("10", "시장 데이터 수집", collect_market_data),
    ("11", "ML 예측 트리거 (Kaggle)", trigger_ml),
    ("", "── 매매 ──", None),
    ("12", "매도 후보 조회", show_sell_candidates),
    ("13", "매수 집행 (큐)", execute_buy_queue),
    ("14", "매도 감시 1회 실행", execute_sell_check),
    ("15", "신규 매수 스위치 on/off", toggle_buy_switch),
    ("16", "변동성 게이트 조회 · 공포장 매수 승인/해제", manage_fear_gate),
    ("", "── 기타 ──", None),
    ("17", "분석 리포트 전송", send_report),
    ("0", "종료 (매매 시스템도 함께 종료됩니다)", None),
]

_ACTIONS = {key: fn for key, _, fn in MENU if key and fn}


def _width(text: str) -> int:
    """한글·전각 문자를 2칸으로 세는 표시 폭. 박스 정렬이 어긋나는 것을 막는다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _render_menu() -> None:
    print()
    print("╔" + "═" * 70 + "╗")
    title = f"국내주식 자동매매 운영 메뉴  [{_mode_tag()}]"
    print("║ " + title + " " * max(69 - _width(title), 0) + "║")
    print("╚" + "═" * 70 + "╝")
    for key, label, _ in MENU:
        if not key:
            print(f"\n  {label}")
        else:
            print(f"   {key:>2}. {label}")


def run_menu(on_exit: Optional[Callable[[], None]] = None) -> None:
    """
    메뉴 루프. 0 을 고르거나 Ctrl+C 를 누르면 on_exit 을 부르고 빠져나온다.

    입력 스트림이 없는 환경(nohup, 서비스 등록 등)에서는 EOF 가 즉시 떨어진다.
    그때는 메뉴를 포기하고 조용히 반환한다 — 서버는 계속 돌아야 하므로 예외로
    올리지 않는다.
    """
    print(f"\n  로그는 화면 대신 파일에 쌓입니다: {log_path()}")
    print("  (오래 걸리는 작업을 고르면 그동안만 진행 상황이 화면에 표시됩니다)")

    while True:
        try:
            _render_menu()
            choice = input("\n  번호 선택 > ").strip()
        except EOFError:
            print("\n  입력을 받을 수 없는 환경입니다 — 메뉴를 종료합니다 (서버는 계속 동작).")
            return
        except KeyboardInterrupt:
            print()
            choice = "0"

        if choice == "0":
            if on_exit:
                on_exit()
            print("  종료합니다.")
            return

        action = _ACTIONS.get(choice)
        if action is None:
            print("  없는 번호입니다.")
            continue

        try:
            action()
        except KeyboardInterrupt:
            print("\n  작업을 중단했습니다.")
        except Exception as e:
            print(f"\n  처리 중 오류: {e}")
            logger.error(f"메뉴 항목 {choice} 처리 실패", exc_info=True)
            _pause()
