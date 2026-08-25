"""
3단계 — LLM 매수 추천 + 리밸런싱 제안.

2단계까지 통과한 후보(최대 KR_STAGE2_TOP_N=5)와 **현재 보유 종목**을 함께 놓고,
LLM 이 "무엇을 사고 무엇을 팔아 자리를 만들지"를 한 번에 판단한다.

## 왜 매수와 매도를 같이 판단하나

기존 매도 검토(kr_llm_sell_review_service)는 보유 종목만 보고 "팔 만한가"를 묻는다.
그런데 리밸런싱은 성격이 다르다 — 보유 종목 A 가 그 자체로는 나쁘지 않아도, 후보 B 가
확연히 낫고 슬롯이 없다면 A 를 팔고 B 를 사는 게 맞다. 이 판단은 양쪽을 같은 화면에
놓아야만 가능하다. 그래서 3단계는 매수 후보와 보유 종목을 한 프롬프트에 넣는다.

## 제약

LLM 은 다음을 반드시 지켜야 하고, 코드가 응답을 받은 뒤 **다시 한 번 검증**한다
(_validate). 프롬프트로만 제약을 걸면 지켜지지 않는 경우가 있어서다.

  · 매수 추천은 최대 KR_LLM_MAX_PICKS(기본 3) 종목
  · 매수 후보는 반드시 2단계 통과 목록 안에서만 고른다 (새 종목 창작 금지)
  · 매도 대상은 반드시 현재 보유 종목 안에서만 고른다
  · 집행 후 보유 종목수 ≤ KR_REBALANCE_MAX_POSITIONS(기본 8)
  · 같은 섹터 ≤ KR_MAX_PER_SECTOR(기본 2)

## 이 모듈은 주문을 내지 않는다

제안만 만든다. 실제 집행은 사람이 메뉴에서 승인한 뒤에 이뤄진다
(app/cli/menu.py 의 리밸런싱 승인 흐름). Fail-Close 는 "아무것도 하지 않음"이다 —
LLM 이 실패하면 빈 제안을 돌려주고, 사람은 승인할 것이 없다.
"""
import json
import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

import anthropic
import pytz

from app.core.config import settings

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

_FALLBACK_MODEL = "claude-sonnet-5"

# 설치된 anthropic SDK(1.x)의 messages.create() 에는 temperature 파라미터가 없다.

SYSTEM_PROMPT = """당신은 한국 주식시장 경력 20년의 포트폴리오 매니저입니다.

2단계 스크리닝(기본적 분석 → 감성 → 기술적 분석 → 수급 → ML 예측)을 모두 통과한 매수
후보와, 현재 보유 중인 종목을 함께 보고 **포트폴리오를 어떻게 재구성할지** 결정하세요.

## 당신이 결정할 것

1. **매수(BUY)** — 후보 중 실제로 담을 종목. 최대 개수는 아래 제약에 명시됩니다.
   후보가 전부 마음에 들지 않으면 하나도 안 골라도 됩니다. 억지로 채우지 마세요.
2. **매도(SELL)** — 자리를 만들기 위해, 또는 그 자체로 더 들고 있을 이유가 없어서
   정리할 보유 종목. 매도는 언제나 **전량**입니다(부분매도 없음).

## 판단 기준

- **상대 비교가 핵심입니다.** 보유 종목이 절대적으로 나빠서가 아니라, 후보가 확연히
  나을 때 교체하세요. 비슷하면 그냥 두는 게 낫습니다 — 교체에는 거래비용과 세금이 듭니다.
- **섹터 분산**을 지키세요. 이미 같은 섹터가 한도까지 찼으면 그 섹터 후보는 담을 수 없고,
  정말 담고 싶다면 같은 섹터의 기존 종목을 파는 제안을 함께 내야 합니다.
- **수익 중인 종목을 이유 없이 팔지 마세요.** 손실 중이라고 무조건 파는 것도 아닙니다.
  파는 근거는 '앞으로의 기대'여야지 '지금까지의 손익'이 아닙니다.
- 후보의 ML 예측 상승률은 참고치일 뿐 확정이 아닙니다. 기본적 분석 점수·감성·기술 신호와
  함께 보세요.

## 응답 원칙

- 매수/매도 각각에 **구체적 근거**를 1~2문장으로 쓰세요. 어떤 수치를 보고 그렇게
  판단했는지 인용하세요.
- rebalance_summary 에는 이번 재구성의 의도를 2~3문장으로 요약하세요.
- 제약을 어기면 제안 전체가 기각됩니다. 반드시 지키세요."""


def _num(v, unit: str = "") -> str:
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:,.2f}{unit}"
    return f"{v:,}{unit}"


def _format_candidates(finalists: List[dict]) -> str:
    parts = []
    for i, c in enumerate(finalists, 1):
        f = c.get("fundamentals") or {}
        parts.append(
            f"\n{i}. {c['name']} ({c['code']}) — 섹터: {c.get('sector_krx') or c.get('sector') or '미상'}"
        )
        parts.append(
            f"   ML 예측 상승률 {c.get('rise_probability', 0):+.2f}% "
            f"(정확도 {c.get('ml_accuracy', 0):.1f}%)"
        )
        parts.append(
            f"   기본적 분석 {c.get('fundamental_score', 0)}점 — {c.get('fundamental_reason', '')}"
        )
        parts.append(
            f"     영업이익률 {_num(f.get('operating_margin'), '%')} / "
            f"ROE {_num(f.get('roe'), '%')} / ROIC {_num(f.get('roic'), '%')} / "
            f"PER {_num(f.get('per'))} / 부채비율 {_num(f.get('debt_ratio'), '%')}"
        )
        parts.append(
            f"   감성 {c.get('sentiment_score', 0):+.2f} "
            f"(기사 {c.get('article_count', 0)}건, 섹터 업황 {c.get('sector_sentiment', 0):+.2f})"
        )
        parts.append(
            f"   매수 신호 {c.get('signal_count', 0)}개: {', '.join(c.get('buy_signals') or [])}"
        )
        flow = c.get("flow") or {}
        parts.append(
            f"   수급: 외국인 {flow.get('foreign_streak', 0)}일 연속 / "
            f"기관 {flow.get('institution_streak', 0)}일 연속 순매수"
        )
        parts.append(f"   현재가 {_num(c.get('current_price'))}원")
    return "\n".join(parts) if parts else "(후보 없음)"


def _format_holdings(holdings: List[dict]) -> str:
    if not holdings:
        return "(현재 보유 종목 없음)"
    parts = []
    for i, h in enumerate(holdings, 1):
        parts.append(
            f"\n{i}. {h['name']} ({h['code']}) — 섹터: {h.get('sector_key') or '미상'}"
        )
        parts.append(
            f"   {h.get('quantity', 0):,}주 / 매입 {_num(h.get('buy_price'))}원 → "
            f"현재 {_num(h.get('current_price'))}원 "
            f"({h.get('price_change_percent', 0):+.2f}%)"
        )
        if h.get("composite_score") is not None:
            parts.append(
                f"   현재 점수 {h['composite_score']:+.3f} "
                f"(순위 {h.get('score_rank')}/{h.get('score_universe_size')})"
            )
        if h.get("technical_sell_details"):
            parts.append(f"   매도 신호: {', '.join(h['technical_sell_details'])}")
        if h.get("stop_loss_price"):
            parts.append(
                f"   손절선 {_num(h.get('stop_loss_price'))}원 / "
                f"익절선 {_num(h.get('take_profit_price'))}원"
            )
    return "\n".join(parts)


def _build_prompt(finalists: List[dict], holdings: List[dict], state: dict) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    sector_lines = "\n".join(
        f"  - {sec}: {n}종목" for sec, n in sorted(state["sector_counts"].items(), key=lambda x: -x[1])
    ) or "  (없음)"

    max_after = settings.KR_REBALANCE_MAX_POSITIONS
    held_n = len(holdings)

    fear_index = state.get("fear_index")
    market_line = (
        f"\n## 시장 국면\n코스피 20일 실현변동성 {fear_index:.1f}% — "
        f"{'공포장, 신중하게 판단하세요' if fear_index > 40 else '평온~약간 불안'}\n"
        if fear_index is not None else ""
    )

    return f"""오늘 날짜: {today}
{market_line}
## 현재 포트폴리오
- 보유 {held_n}종목 (미체결 매수 {len(state.get('pending_codes') or [])}종목)
- 섹터 분포:
{sector_lines}

## 제약 (반드시 지킬 것)
- 매수 추천은 **최대 {settings.KR_LLM_MAX_PICKS}종목**.
- 매수는 아래 '매수 후보' 목록 안에서만 고르세요. 목록에 없는 종목을 만들면 안 됩니다.
- 매도는 아래 '현재 보유 종목' 안에서만 고르세요.
- **집행 후 보유 종목수가 {max_after}종목을 넘으면 안 됩니다.**
  (현재 {held_n}종목 → 매수 N개, 매도 M개면 집행 후 {held_n} + N − M ≤ {max_after})
- 같은 섹터는 **최대 {settings.KR_MAX_PER_SECTOR}종목**까지만 보유할 수 있습니다(집행 후 기준).

## 매수 후보 (2단계 스크리닝 통과)
{_format_candidates(finalists)}

## 현재 보유 종목
{_format_holdings(holdings)}

## 응답 형식
반드시 아래 JSON 만 출력하세요. 다른 텍스트나 코드펜스를 덧붙이지 마세요.
{{
  "rebalance_summary": "이번 재구성의 의도 2~3문장",
  "buy": [
    {{"code": "종목코드 6자리", "stock_name": "종목명", "conviction": 1-10 정수,
      "reason": "구체적 근거 1~2문장"}}
  ],
  "sell": [
    {{"code": "종목코드 6자리", "stock_name": "종목명",
      "reason": "구체적 근거 1~2문장"}}
  ]
}}"""


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _validate(
    proposal: dict, finalists: List[dict], holdings: List[dict], state: dict
) -> "tuple[List[dict], List[dict], List[str]]":
    """
    LLM 응답을 제약에 맞춰 정리한다 → (buy, sell, 경고 목록).

    프롬프트로 제약을 걸어도 지켜지지 않는 경우가 있어 코드가 다시 자른다.
    잘라낸 항목은 경고로 남겨 사람이 볼 수 있게 한다 — 조용히 버리면 LLM 이 왜 그
    종목을 골랐는지 검토할 기회가 사라진다.
    """
    warnings: List[str] = []
    by_code = {c["code"]: c for c in finalists}
    held_by_code = {h["code"]: h for h in holdings}

    # 1) 매도 — 보유 종목 안에서만
    sell: List[dict] = []
    for item in proposal.get("sell") or []:
        code = str(item.get("code", "")).strip()
        if code not in held_by_code:
            warnings.append(f"매도 제안 {code} 는 보유 종목이 아니라 제외")
            continue
        if any(s["code"] == code for s in sell):
            continue
        sell.append(
            {
                "code": code,
                "stock_name": held_by_code[code]["name"],
                "reason": str(item.get("reason", "")).strip(),
                "holding": held_by_code[code],
            }
        )

    # 2) 매수 — 후보 안에서만, 최대 KR_LLM_MAX_PICKS
    buy: List[dict] = []
    for item in proposal.get("buy") or []:
        code = str(item.get("code", "")).strip()
        if code not in by_code:
            warnings.append(f"매수 제안 {code} 는 2단계 통과 후보가 아니라 제외")
            continue
        if any(b["code"] == code for b in buy):
            continue
        if len(buy) >= settings.KR_LLM_MAX_PICKS:
            warnings.append(
                f"매수 제안이 상한({settings.KR_LLM_MAX_PICKS}종목)을 넘어 {code} 제외"
            )
            continue
        try:
            conviction = int(item.get("conviction", 0))
        except (ValueError, TypeError):
            conviction = 0
        buy.append(
            {
                "code": code,
                "stock_name": by_code[code]["name"],
                "conviction": max(0, min(conviction, 10)),
                "reason": str(item.get("reason", "")).strip(),
                "candidate": by_code[code],
            }
        )

    # 3) 집행 후 보유 종목수 상한
    sell_codes = {s["code"] for s in sell}
    after = len(holdings) - len(sell_codes) + len(buy)
    limit = settings.KR_REBALANCE_MAX_POSITIONS
    if after > limit:
        # 확신도가 낮은 매수부터 잘라낸다 — 파는 쪽을 늘리면 사람이 승인하지 않은
        # 매도를 시스템이 만들어내는 셈이라 더 위험하다.
        excess = after - limit
        buy.sort(key=lambda b: b["conviction"], reverse=True)
        dropped = buy[len(buy) - excess :]
        buy = buy[: len(buy) - excess]
        for d in dropped:
            warnings.append(
                f"집행 후 보유 {after}종목 > 상한 {limit} → 확신도 낮은 매수 "
                f"{d['stock_name']}({d['code']}) 제외"
            )

    # 4) 섹터 한도 (집행 후 기준)
    sector_counts = dict(state["sector_counts"])
    for s in sell:
        key = s["holding"].get("sector_key") or "미분류"
        if sector_counts.get(key):
            sector_counts[key] -= 1
    kept: List[dict] = []
    for b in buy:
        cand = b["candidate"]
        key = cand.get("sector_key") or cand.get("sector_krx") or cand.get("sector") or "미분류"
        if sector_counts.get(key, 0) >= settings.KR_MAX_PER_SECTOR:
            warnings.append(
                f"섹터 한도 초과 → 매수 {b['stock_name']}({b['code']}) 제외 "
                f"({key} 이미 {sector_counts[key]}종목)"
            )
            continue
        sector_counts[key] = sector_counts.get(key, 0) + 1
        kept.append(b)
    buy = kept

    return buy, sell, warnings


def propose(finalists: List[dict], holdings: List[dict], state: dict) -> dict:
    """
    리밸런싱 제안 생성. **주문은 내지 않는다.**

    finalists: 2단계 통과 후보 (≤ KR_STAGE2_TOP_N)
    holdings:  현재 보유 종목 컨텍스트 (kr_screening_service.get_holdings_context)
    state:     포트폴리오 상태 (kr_screening_service.get_portfolio_state)

    Returns:
      {"buy": [...], "sell": [...], "summary": str, "warnings": [...],
       "llm_failed": bool, "after_positions": int}
    """
    empty = {
        "buy": [], "sell": [], "summary": "", "warnings": [],
        "llm_failed": False,
        "after_positions": len(holdings),
    }

    if not finalists:
        empty["summary"] = "2단계를 통과한 매수 후보가 없어 제안할 것이 없습니다."
        return empty

    if not settings.ANTHROPIC_API_KEY:
        logger.error("  ANTHROPIC_API_KEY 미설정 — 리밸런싱 제안 생략")
        empty["llm_failed"] = True
        empty["summary"] = "ANTHROPIC_API_KEY 미설정 — 제안을 만들 수 없습니다."
        return empty

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    prompt = _build_prompt(finalists, holdings, state)

    for model in (settings.KR_REBALANCE_MODEL, _FALLBACK_MODEL):
        for attempt in range(2):
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=8000,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                )
                data = _extract_json(text)
                if not data:
                    raise ValueError("JSON 파싱 실패")

                buy, sell, warnings = _validate(data, finalists, holdings, state)
                after = len(holdings) - len({s["code"] for s in sell}) + len(buy)
                logger.info(
                    f"  리밸런싱 제안: 매수 {len(buy)}종목 / 매도 {len(sell)}종목 "
                    f"→ 집행 후 {after}종목"
                )
                for w in warnings:
                    logger.warning(f"    제안 보정: {w}")
                return {
                    "buy": buy,
                    "sell": sell,
                    "summary": str(data.get("rebalance_summary", "")).strip(),
                    "warnings": warnings,
                    "llm_failed": False,
                    "after_positions": after,
                    "model": model,
                }

            except Exception as e:
                logger.warning(f"  리밸런싱 제안 실패 ({model}, 시도 {attempt + 1}/2): {e}")
                time.sleep(1.5)

        if model != _FALLBACK_MODEL:
            logger.info(f"  {model} 실패 → 폴백 모델 {_FALLBACK_MODEL} 로 전환")

    logger.error("  리밸런싱 LLM 전체 실패 — 제안 없음 (Fail-Close)")
    empty["llm_failed"] = True
    empty["summary"] = "LLM 호출이 모두 실패해 제안을 만들지 못했습니다."
    return empty


# ══════════════════════════════════════════════════════════════════
# 승인된 제안 적재
# ══════════════════════════════════════════════════════════════════

def apply_approved(
    buy: List[dict], sell: List[dict], note: str = "사용자 승인 리밸런싱"
) -> dict:
    """
    사람이 승인한 항목만 실행 대기열에 넣는다. **여기서도 주문은 직접 내지 않는다.**

    기존 집행 경로를 그대로 태우는 게 핵심이다 — 매수는 kr_buy_queue 로, 매도는
    kr_llm_sell_decision_logs 의 pending 판정으로 넣으면, 이미 검증된 집행 로직
    (현재가 재조회 · 호가단위 정규화 · 수량 재계산 · 중복 방지 · 정합성 확인)이
    그대로 적용된다. 여기서 주문 API 를 직접 부르면 그 안전장치를 전부 우회하게 된다.

    집행 시점:
      · 매수 — 다음 KR_EXECUTION_TIME(기본 09:05) 또는 메뉴 '매수 집행'
      · 매도 — 다음 매도 감시 사이클(1분 주기) 또는 메뉴 '매도 감시 1회 실행'

    Returns: {"queued_buy", "queued_sell", "errors"}
    """
    from app.db.supabase import supabase
    from app.services.kr import kis_domestic_service as kis

    today = datetime.now(KST).strftime("%Y-%m-%d")
    account = kis.current_account_type()
    errors: List[str] = []
    queued_buy = queued_sell = 0

    if buy:
        rows = []
        for b in buy:
            cand = b.get("candidate") or {}
            rows.append(
                {
                    "queued_date": today,
                    "code": b["code"],
                    "stock_name": b["stock_name"],
                    "composite_score": cand.get("fundamental_score"),
                    "rise_probability": cand.get("rise_probability"),
                    "llm_reason": f"[{note}] {b.get('reason', '')}",
                    "atr": cand.get("atr"),
                    "status": "pending",
                    "account_type": account,
                }
            )
        try:
            supabase.table("kr_buy_queue").insert(rows).execute()
            queued_buy = len(rows)
            logger.info(f"  리밸런싱 매수 {queued_buy}건을 kr_buy_queue 에 저장")
        except Exception as e:
            errors.append(f"매수 큐 저장 실패: {e}")
            logger.error(f"  매수 큐 저장 실패: {e}", exc_info=True)

    if sell:
        rows = []
        for s in sell:
            holding = s.get("holding") or {}
            rows.append(
                {
                    "decision_date": today,
                    "code": s["code"],
                    "stock_name": s["stock_name"],
                    "decision": "SELL_ALL",
                    "reason": f"[{note}] {s.get('reason', '')}",
                    "price_at_decision": holding.get("current_price"),
                    "signal_count_at_decision": holding.get("technical_sell_signals"),
                    "composite_score": holding.get("composite_score"),
                    "score_rank": holding.get("score_rank"),
                    "score_universe_size": holding.get("score_universe_size"),
                    # kr_llm_sell_decision_logs 에는 account_type 컬럼이 없다 —
                    # 집행부가 kr_trade_records 쪽에서 계좌를 판별한다.
                    "status": "pending",
                }
            )
        try:
            supabase.table("kr_llm_sell_decision_logs").insert(rows).execute()
            queued_sell = len(rows)
            logger.info(f"  리밸런싱 매도 {queued_sell}건을 매도 판정으로 저장")
        except Exception as e:
            errors.append(f"매도 판정 저장 실패: {e}")
            logger.error(f"  매도 판정 저장 실패: {e}", exc_info=True)

    return {"queued_buy": queued_buy, "queued_sell": queued_sell, "errors": errors}
