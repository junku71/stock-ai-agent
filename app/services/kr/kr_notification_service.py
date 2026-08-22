"""
국내주식 트랙 Slack 알림.

전송 함수(_send)는 미국 트랙(app/services/notification_service.py)의 것을 그대로 쓰고,
메시지 포맷만 원화·한국 시장 기준으로 다시 작성했다.
SLACK_WEBHOOK_URL 이 비어 있으면 모든 함수가 조용히 no-op 이다.

알림 종류:
  ① notify_data_ready       — 분석 파이프라인(데이터→ML→기술·감성) 완료
  ② notify_llm_decisions    — 오늘 매수/홀드 결정 종목
  ③ notify_buy_queued       — 다음 영업일 매수 예약 목록 (장 마감 후 분석 결과)
  ④ notify_buy_ordered      — 매수 주문 접수
  ⑤ notify_buy_filled       — 매수 체결 (계좌 요약 + 보유 현황표)
  ⑥ notify_sell_ordered     — 매도 주문 접수
  ⑦ notify_sell_filled      — 매도 체결 (손익 + 보유 현황표)
  ⑧ notify_pipeline_failure — 파이프라인 실패
  ⑨ notify_llm_failure      — LLM 검토 전체 실패 (Fail-Close 매수 차단)
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

import pytz

from app.core.config import settings
from app.services.notification_service import _send  # 저수준 Webhook 전송 재사용

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")


def _mode_tag() -> str:
    """알림 제목에 붙일 계좌 모드 태그 — 모의/실전을 한눈에 구분."""
    if settings.KR_DRY_RUN:
        return "[국내·드라이런]"
    return "[국내·모의]" if settings.KIS_USE_MOCK else "[국내·실전]"


def _won(amount: Optional[float]) -> str:
    if amount is None:
        return "N/A"
    return f"{amount:,.0f}원"


# ══════════════════════════════════════════════════════════════════
# 보유 현황표
# ══════════════════════════════════════════════════════════════════

def format_holdings_table() -> str:
    """KIS 국내 잔고 → 모노스페이스 표 (Slack 코드블록)."""
    try:
        from app.services.kr import kis_domestic_service as kis

        balance = kis.get_balance()
    except Exception as e:
        return f"_(보유 종목 조회 실패: {e})_"

    if balance.get("rt_cd") != "0":
        return f"_(보유 종목 조회 실패: {balance.get('msg1', '')})_"

    holdings = balance.get("output1", [])
    if not holdings:
        return "_(현재 보유 종목 없음)_"

    lines = ["```"]
    lines.append(f"{'종목':<14}{'수량':>6} {'평단가':>10} {'현재가':>10} {'평가손익':>13} {'수익률':>8}")
    lines.append("─" * 66)

    total_buy = 0.0
    total_pnl = 0.0

    for h in holdings:
        name = (h.get("prdt_name") or "")[:7]
        try:
            qty = int(h.get("hldg_qty", 0) or 0)
            buy = float(h.get("pchs_avg_pric", 0) or 0)
            now = float(h.get("prpr", 0) or 0)
            pnl = float(h.get("evlu_pfls_amt", 0) or 0)
            pnl_pct = float(h.get("evlu_pfls_rt", 0) or 0)
        except (ValueError, TypeError):
            continue

        total_buy += buy * qty
        total_pnl += pnl
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"{name:<14}{qty:>6} {buy:>10,.0f} {now:>10,.0f} "
            f"{sign}{pnl:>12,.0f} {sign}{pnl_pct:>6.2f}%"
        )

    lines.append("─" * 66)
    total_sign = "+" if total_pnl >= 0 else ""
    total_pct = (total_pnl / total_buy * 100) if total_buy > 0 else 0.0
    lines.append(
        f"{'합계':<14}{'':>6} {total_buy:>21,.0f} → "
        f"{total_sign}{total_pnl:,.0f}원 ({total_sign}{total_pct:.2f}%)"
    )
    lines.append("```")
    return "\n".join(lines)


def _account_summary_block() -> str:
    try:
        from app.services.kr import kis_domestic_service as kis

        s = kis.get_account_summary()
    except Exception as e:
        logger.warning(f"계좌 요약 조회 실패: {e}")
        return ""

    if not s or s.get("stock_purchase_amount", 0) <= 0:
        return ""

    pnl = s["eval_profit_loss"]
    sign = "+" if pnl >= 0 else ""
    return (
        f"*💼 계좌 현황*\n"
        f"  주식 평가액: {_won(s['stock_eval_amount'])}\n"
        f"  매입 원금:   {_won(s['stock_purchase_amount'])}\n"
        f"  평가 손익:   {sign}{s['eval_profit_loss']:,.0f}원 "
        f"({sign}{s['eval_profit_loss_pct']:.2f}%)\n"
        f"  예수금(D+2): {_won(s['d2_deposit'])}\n"
    )


def _today_trade_summary() -> str:
    """오늘(KST) 국내 거래 통계."""
    try:
        from app.db.supabase import supabase

        now = datetime.now(KST)
        start_utc = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(pytz.UTC)
            .strftime("%Y-%m-%dT%H:%M:%S")
        )
        resp = (
            supabase.table("kr_trade_records")
            .select("status, buy_price, quantity, profit_loss")
            .gte("created_at", start_utc)
            .execute()
        )
    except Exception as e:
        logger.warning(f"오늘 거래 요약 조회 실패: {e}")
        return ""

    rows = resp.data or []
    buy_count = buy_amount = sell_count = realized = 0
    for r in rows:
        status = r.get("status")
        if status in ("buy_ordered", "holding"):
            buy_count += 1
            buy_amount += float(r.get("buy_price") or 0) * (r.get("quantity") or 0)
        elif status == "sold":
            sell_count += 1
            realized += float(r.get("profit_loss") or 0)

    if buy_count == 0 and sell_count == 0:
        return ""

    sign = "+" if realized >= 0 else ""
    return (
        f"*📊 오늘 거래 요약 ({datetime.now(KST).strftime('%Y-%m-%d')})*\n"
        f"  매수: {buy_count}건 / {buy_amount:,.0f}원\n"
        f"  매도: {sell_count}건 / 실현손익 {sign}{realized:,.0f}원\n"
    )


# ══════════════════════════════════════════════════════════════════
# ① 데이터 수집 완료
# ══════════════════════════════════════════════════════════════════

def notify_data_ready(elapsed_sec: int, steps_summary: dict):
    _send(
        title=f"📥 {_mode_tag()} 데이터 수집 완료",
        message=f"오늘 매수 판단을 위한 국내 데이터가 모두 갱신됐습니다. ({elapsed_sec}초)",
        color="#2eb886",
        fields={
            "1 시장데이터": f"{steps_summary.get('1_market', '?')}초",
            "2 ML 예측": f"{steps_summary.get('2_ml', '?')}초",
            "3 기술+감성": f"{steps_summary.get('3_tech_sent', '?')}초",
        },
    )


# ══════════════════════════════════════════════════════════════════
# ② 매수 / 홀드 결정
# ══════════════════════════════════════════════════════════════════

def notify_llm_decisions(
    buy_candidates: List[dict],
    held_candidates: List[dict],
    market_analysis: str = "",
    market: Optional[dict] = None,
):
    market = market or {}

    if not buy_candidates and not held_candidates:
        _send(
            title=f"📋 {_mode_tag()} 오늘 매수 후보 없음",
            message="ML·기술·감성·수급 필터를 통과한 종목이 없습니다.",
            color="#888888",
        )
        return

    buy_lines = [
        f"• *{c.get('stock_name')}* ({c.get('code')}) "
        f"score={c.get('composite_score', 0):+.3f} "
        f"상승률예측={c.get('rise_probability', 0):.2f}% "
        f"— _{c.get('llm_reason', '')}_"
        for c in buy_candidates
    ]
    hold_lines = [
        f"• {c.get('stock_name')} ({c.get('code')}) "
        f"score={c.get('composite_score', 0):+.3f} "
        f"— _{c.get('llm_reason', '')}_"
        for c in held_candidates
    ]

    parts = []
    vol = market.get("kospi_vol_20d")
    kospi = market.get("kospi")
    fx = market.get("usdkrw")
    env_bits = []
    if kospi:
        env_bits.append(f"코스피 {kospi:,.2f}")
    if vol is not None:
        env_bits.append(f"변동성 {vol:.1f}%")
    if fx:
        env_bits.append(f"원/달러 {fx:,.1f}")
    if env_bits:
        parts.append("🌐 *시장 환경:* " + " · ".join(env_bits))
    ov = market.get("override") or {}
    if ov.get("active"):
        parts.append(
            f"⚠️ *변동성 게이트 수동 해제 중* (잔여 {ov.get('remaining_minutes')}분) "
            f"— {ov.get('reason')}"
        )
    if market_analysis:
        parts.append(f"💬 *시장 분석:* {market_analysis}")
    if buy_lines:
        parts.append(f"🟢 *BUY ({len(buy_lines)}건)*\n" + "\n".join(buy_lines))
    if hold_lines:
        parts.append(f"🟡 *HOLD ({len(hold_lines)}건)*\n" + "\n".join(hold_lines))

    _send(
        title=f"📋 {_mode_tag()} 오늘 결정 — BUY {len(buy_candidates)} / HOLD {len(held_candidates)}",
        message="\n\n".join(parts),
        color="#3b82f6",
    )


# ══════════════════════════════════════════════════════════════════
# ③ 다음 영업일 매수 예약
# ══════════════════════════════════════════════════════════════════

def notify_buy_queued(queued: List[dict], execute_at: str):
    """
    장 마감 후 분석에서 확정된 '다음 영업일 매수 예정' 목록.
    한국 증시는 15:30 에 닫히므로 분석 시점에 주문을 낼 수 없다 —
    큐에 넣고 다음 개장일 아침에 집행한다.
    """
    if not queued:
        _send(
            title=f"🗓 {_mode_tag()} 내일 매수 예정 없음",
            message="LLM 검토를 통과한 종목이 없어 매수 예약이 없습니다.",
            color="#888888",
        )
        return

    lines = [
        f"• *{q.get('stock_name')}* ({q.get('code')}) "
        f"score={q.get('composite_score', 0):+.3f}"
        for q in queued
    ]
    _send(
        title=f"🗓 {_mode_tag()} 다음 개장일 매수 예약 {len(queued)}건",
        message=(
            f"아래 종목을 다음 영업일 *{execute_at} KST* 에 시세 조회 후 매수 주문합니다.\n"
            + "\n".join(lines)
            + "\n\n_집행 직전 현재가로 수량을 다시 계산하며, 이미 보유 중이면 건너뜁니다._"
        ),
        color="#8b5cf6",
    )


# ══════════════════════════════════════════════════════════════════
# ④⑤ 매수
# ══════════════════════════════════════════════════════════════════

def notify_buy_ordered(
    code: str, stock_name: str, qty: int, price: int, composite_score: Optional[float]
):
    score_line = (
        f"*종합점수:* {composite_score:+.4f}\n" if composite_score is not None else ""
    )
    _send(
        title=f"📋 {_mode_tag()} 매수 주문 접수: {stock_name} ({code})",
        message=(
            f"*수량:* {qty:,}주  *주문가(지정가):* {price:,}원  "
            f"*주문금액:* {qty * price:,}원\n"
            f"{score_line}"
            f"_⏳ 체결은 정규장(09:00~15:30 KST) 매칭 후 별도 '체결' 알림으로 안내됩니다._"
        ),
        color="#3b82f6",
    )


def notify_buy_filled(
    code: str,
    stock_name: str,
    qty: int,
    fill_price: float,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    composite_score: Optional[float] = None,
):
    parts = [
        "*이번 거래*",
        f"  {qty:,}주 @ {fill_price:,.0f}원 = *{qty * fill_price:,.0f}원*",
    ]
    if composite_score is not None:
        parts.append(f"  종합점수: {composite_score:+.4f} (LLM BUY)")

    if take_profit_price and stop_loss_price and fill_price > 0:
        tp_pct = (take_profit_price - fill_price) / fill_price * 100
        sl_pct = (stop_loss_price - fill_price) / fill_price * 100
        rr = tp_pct / abs(sl_pct) if sl_pct else 0
        parts += [
            "",
            "*🎯 자동 청산 라인 (ATR 기반)*",
            f"  익절가: {take_profit_price:,.0f}원 ({tp_pct:+.2f}%)  ← 도달 시 자동 매도",
            f"  손절가: {stop_loss_price:,.0f}원 ({sl_pct:+.2f}%)  ← 도달 시 자동 손절",
            f"  보상/위험: {rr:.2f} : 1",
        ]

    today = _today_trade_summary()
    if today:
        parts += ["", today.rstrip()]
    account = _account_summary_block()
    if account:
        parts += ["", account.rstrip()]
    parts += ["", "*📊 현재 보유 종목*", format_holdings_table()]

    _send(
        title=f"✅ {_mode_tag()} 매수 체결: {stock_name} ({code})",
        message="\n".join(parts),
        color="#36a64f",
    )


# ══════════════════════════════════════════════════════════════════
# ⑥⑦ 매도
# ══════════════════════════════════════════════════════════════════

_SELL_REASON_KR = {
    "take_profit": "익절 (ATR 목표가 도달)",
    "stop_loss": "손절 (ATR 손실 한도 도달)",
    "signal": "기술 신호 매도",
    "panic_sell": "패닉셀 (급락 + 거래량 폭증)",
    "flow_out": "수급 이탈 (외국인·기관 순매도)",
}


def notify_sell_ordered(code: str, stock_name: str, qty: int, price: int, sell_reason: str):
    _send(
        title=f"📋 {_mode_tag()} 매도 주문 접수: {stock_name} ({code})",
        message=(
            f"*수량:* {qty:,}주  *주문가(지정가):* {price:,}원  "
            f"*사유:* `{_SELL_REASON_KR.get(sell_reason, sell_reason)}`\n"
            f"_⏳ 체결은 정규장(09:00~15:30 KST) 매칭 후 별도 '체결' 알림으로 안내됩니다._"
        ),
        color="#3b82f6",
    )


def notify_sell_filled(
    code: str,
    stock_name: str,
    qty: int,
    fill_price: float,
    sell_reason: str,
    profit_loss: float,
    profit_loss_pct: float,
    buy_price: Optional[float] = None,
    buy_date: Optional[str] = None,
):
    is_profit = profit_loss >= 0
    icon = "💰" if is_profit else "🩸"
    color = "#2eb886" if is_profit else "#ff9800"
    sign = "+" if is_profit else ""

    parts = ["*이번 거래*", f"  {qty:,}주 @ {fill_price:,.0f}원"]
    if buy_price:
        parts.append(f"  매수가 {buy_price:,.0f}원 → 매도가 {fill_price:,.0f}원")
    parts.append(f"  손익: *{sign}{profit_loss:,.0f}원* ({sign}{profit_loss_pct:.2f}%)")
    parts.append(f"  사유: `{_SELL_REASON_KR.get(sell_reason, sell_reason)}`")

    if buy_date:
        try:
            bought = datetime.strptime(buy_date[:10], "%Y-%m-%d").date()
            parts.append(f"  보유 기간: {max((datetime.now(KST).date() - bought).days, 0)}일")
        except (ValueError, TypeError):
            pass

    # 국내주식은 T+2 결제 — 매도 대금은 2영업일 뒤에 출금 가능해진다
    parts.append("  _결제: T+2 (매도 대금은 2영업일 후 인출 가능)_")

    today = _today_trade_summary()
    if today:
        parts += ["", today.rstrip()]
    account = _account_summary_block()
    if account:
        parts += ["", account.rstrip()]
    parts += ["", "*📊 매도 후 보유 종목*", format_holdings_table()]

    _send(
        title=(
            f"{icon} {_mode_tag()} 매도 체결: {stock_name} ({code})  "
            f"{sign}{profit_loss:,.0f}원 ({sign}{profit_loss_pct:.2f}%)"
        ),
        message="\n".join(parts),
        color=color,
    )


# ══════════════════════════════════════════════════════════════════
# ⑧⑨ 장애 알림
# ══════════════════════════════════════════════════════════════════

def notify_pipeline_failure(
    failed_step: str,
    step_name: str,
    error: str,
    completed_steps: Optional[Dict[str, dict]] = None,
):
    fields = {"❌ 실패 단계": f"{step_name}\n({failed_step})"}
    for k, v in (completed_steps or {}).items():
        fields[f"✅ {v.get('step_name', k)}"] = f"{v.get('elapsed_sec', '?')}초"

    _send(
        title=f"❌ {_mode_tag()} 파이프라인 실패 — {step_name}",
        message=(
            f"국내 자동매매 파이프라인이 *{step_name}* 단계에서 실패했습니다.\n"
            f"이번 사이클의 매수는 진행되지 않습니다.\n\n"
            f"*에러:*\n```{(error or '')[:500]}```"
        ),
        color="#ff0000",
        fields=fields,
    )


def notify_override_created(record: dict):
    """
    변동성 게이트 수동 해제 알림.

    조용히 열려 있는 상태가 가장 위험하므로, 발급 즉시 그리고 눈에 띄게 알린다.
    """
    relax = "예 (임계값도 평온장 기준)" if record.get("relax_threshold") else "아니오 (하드블록만 해제)"
    fear = record.get("fear_index_at_creation")
    body = chr(10).join([
        "운영자 개입으로 공포장 매수 차단이 *한시적으로 해제*됐습니다.",
        "",
        f"*사유:* {record.get('reason')}",
        "",
        "_만료되면 게이트는 자동으로 복구됩니다._",
        "_즉시 되돌리려면 `POST /kr/fear-gate/revoke` 를 호출하세요._",
    ])
    _send(
        title=f"⚠️ {_mode_tag()} 변동성 게이트 수동 해제",
        message=body,
        color="#f59e0b",
        fields={
            "발급 시 변동성": f"{fear:.1f}%" if fear is not None else "N/A",
            "유효 시간": f"{record.get('minutes')}분",
            "만료 시각": str(record.get("expires_at", ""))[:16].replace("T", " "),
            "임계값 완화": relax,
        },
    )


def notify_override_revoked(record: dict, note: str):
    """변동성 게이트 오버라이드 해제 알림."""
    body = chr(10).join([
        "수동 해제가 종료되어 공포장 매수 차단이 다시 적용됩니다.",
        "",
        f"*원래 사유:* {record.get('reason')}",
        f"*종료:* {note}",
    ])
    _send(
        title=f"✅ {_mode_tag()} 변동성 게이트 복구",
        message=body,
        color="#2eb886",
    )


def notify_llm_failure(reason: str, candidate_count: int = 0):
    _send(
        title=f"❌ {_mode_tag()} LLM 검토 실패 — 매수 차단",
        message=(
            f"Claude API 호출이 전부 실패했습니다 (Opus + Sonnet 폴백 포함).\n"
            f"Fail-Close 정책에 따라 *오늘 매수를 진행하지 않습니다*.\n\n"
            f"검토 시도 후보: *{candidate_count}개*\n\n"
            f"*에러:*\n```{(reason or '')[:500]}```"
        ),
        color="#ff0000",
        fields={"조치 권장": "ANTHROPIC_API_KEY / 잔액 / 서비스 상태 확인"},
    )
