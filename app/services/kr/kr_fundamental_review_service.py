"""
1단계 기본적 분석 — LLM 판정.

DART 에서 뽑은 재무 지표(dart_service)를 Claude 에게 보여주고 종목별로
PASS / FAIL 을 받는다. 숫자 임계값을 코드에 박지 않고 LLM 에게 맡기는 이유는
**업종마다 정상 범위가 완전히 다르기 때문**이다.

  · 은행·보험은 부채비율이 1,000% 를 넘는 게 정상이다. 일률적으로 200% 컷을 걸면
    금융주가 통째로 사라진다.
  · 조선·건설은 수주산업이라 특정 해 영업이익률이 음수여도 수주잔고가 받쳐주면 문제가
    아니다.
  · 성장주는 PER 이 높은 게 당연하고, 경기민감주는 이익 정점에서 PER 이 낮아진다
    (오히려 고점 신호다).

그래서 코드는 **명백한 하드 게이트만** 미리 걸러 LLM 호출량을 줄이고(_hard_gate),
나머지 정성 판단은 업종 맥락과 함께 LLM 에게 넘긴다.

Fail-Close: LLM 호출이 전부 실패하면 그 배치는 **전원 탈락** 처리한다. 매수 후보를
좁히는 단계이므로, 근거 없이 통과시키는 것보다 이번 회차를 건너뛰는 편이 안전하다.
(매도 검토의 Fail-Close 가 'HOLD' 인 것과 방향이 같다 — 어느 쪽이든 새 위험을 만들지
않는 쪽으로 실패한다.)
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

# 한 번의 Claude 호출에 묶을 종목 수. 종목당 지표가 10여 개라 감성 채점(6종목)보다 넉넉하다.
TICKERS_PER_CALL = 10

_FALLBACK_MODEL = "claude-sonnet-5"

# 설치된 anthropic SDK(1.x)의 messages.create() 에는 temperature 파라미터가 없다.
# 판정 작업이라 어차피 결정론적 출력이 필요하지만, 모델 기본값을 그대로 쓴다.

SYSTEM_PROMPT = """당신은 한국 주식시장 경력 20년의 펀더멘털 애널리스트입니다.

주어진 종목들의 재무 지표를 보고 **중장기 매수 후보로 올릴 만한 재무 체력이 있는지**만
판정하세요. 주가 수준이나 매매 타이밍은 판단하지 마세요 — 그건 뒤 단계(기술적 분석)가 합니다.

## 판정 기준

1. **수익성** — 영업이익률, ROE, ROIC
   - ROIC 가 자본비용(한국 기준 대략 7~8%)을 넘는지가 핵심입니다. ROE 가 높아도
     부채 레버리지로 만든 것이면 ROIC 는 낮게 나옵니다.
2. **성장성** — PER
   - 절대 수치가 아니라 업종 대비/이익 추세 대비로 보세요. 경기민감주(철강·해운·화학)의
     낮은 PER 은 이익 정점 신호일 수 있고, 성장주의 높은 PER 은 정상일 수 있습니다.
3. **안정성** — 부채비율, 유동비율
   - **업종을 반드시 고려하세요.** 은행·보험·증권은 부채비율 1,000% 이상이 정상입니다.
     금융업을 부채비율만 보고 탈락시키지 마세요.
4. **현금흐름** — 영업/투자/재무 현금흐름과 FCF
   - 영업현금흐름이 순이익을 뒷받침하는지(이익의 질), FCF 가 양수인지 보세요.
   - 다만 대규모 증설 국면(반도체·2차전지)에서는 CAPEX 때문에 FCF 가 음수인 게
     정상일 수 있습니다. 영업현금흐름이 튼튼하면 그 점을 감안하세요.

## 판정 원칙

- 애매하면 FAIL 하세요. 이 단계는 고정 100종목 후보군을 걸러내는 단계이므로 **놓치는
  비용보다 잘못 통과시키는 비용이 큽니다.**
- 결측치(N/A)가 많아 판단 근거가 부족하면 FAIL 하세요.
- reason 은 반드시 **구체적 수치를 인용**해서 1~2문장으로 쓰세요.
- score 는 재무 체력을 0~100 으로 매긴 값입니다. PASS 는 보통 60 이상입니다."""


def _num(v, unit: str = "", digits: int = 2) -> str:
    if v is None:
        return "N/A"
    if abs(v) >= 1e11:
        return f"{v / 1e12:.2f}조원"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.0f}억원"
    return f"{v:,.{digits}f}{unit}"


def _build_user_prompt(batch: List[dict]) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    parts = [f"오늘 날짜: {today}\n"]
    for item in batch:
        f = item["fundamentals"]
        parts.append(
            f"\n### {item['name']} ({item['code']}) — 업종: {item.get('sector') or '미상'}"
            f" / 시가총액 {_num((item.get('market_cap') or 0) * 1e8)}"
            f" / 회계연도 FY{f.get('fiscal_year', '?')}"
        )
        parts.append(
            f"- 수익성: 영업이익률 {_num(f.get('operating_margin'), '%')}"
            f" / ROE {_num(f.get('roe'), '%')}"
            f" / ROIC {_num(f.get('roic'), '%')}"
        )
        parts.append(f"- 성장성: PER {_num(f.get('per'))}")
        parts.append(
            f"- 안정성: 부채비율 {_num(f.get('debt_ratio'), '%')}"
            f" / 유동비율 {_num(f.get('current_ratio'), '%')}"
        )
        parts.append(
            f"- 현금흐름: 영업 {_num(f.get('cf_operating'))}"
            f" / 투자 {_num(f.get('cf_investing'))}"
            f" / 재무 {_num(f.get('cf_financing'))}"
            f" / CAPEX {_num(f.get('capex'))} / FCF {_num(f.get('fcf'))}"
        )
        parts.append(
            f"- 규모: 매출 {_num(f.get('revenue'))}"
            f" / 영업이익 {_num(f.get('operating_income'))}"
            f" / 순이익 {_num(f.get('net_income'))}"
        )
    parts.append(
        f"\n\n위 {len(batch)}개 종목 각각을 판정하세요. "
        "results 배열에 정확히 이 종목들만, 종목코드와 함께 반환하세요."
    )
    return "\n".join(parts)


RESPONSE_FORMAT = """
반드시 아래 JSON 만 출력하세요. 다른 텍스트나 코드펜스를 덧붙이지 마세요.
{
  "results": [
    {"code": "종목코드 6자리", "verdict": "PASS 또는 FAIL", "score": 0-100 정수,
     "reason": "구체적 수치를 인용한 1~2문장"}
  ]
}"""


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


def _review_batch(client: anthropic.Anthropic, batch: List[dict]) -> Dict[str, dict]:
    """종목 묶음 하나를 판정. 실패하면 빈 dict (호출부가 Fail-Close 처리)."""
    user_prompt = _build_user_prompt(batch) + "\n" + RESPONSE_FORMAT
    codes_in_batch = {b["code"] for b in batch}

    for model in (settings.KR_FUNDAMENTAL_MODEL, _FALLBACK_MODEL):
        for attempt in range(2):
            try:
                kwargs = {
                    "model": model,
                    "max_tokens": 8000,
                    "system": [
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            # 판정 기준은 호출마다 동일 → 프리픽스 캐시 대상
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                resp = client.messages.create(**kwargs)
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                data = _extract_json(text)
                if not data or "results" not in data:
                    raise ValueError("JSON 파싱 실패")

                out: Dict[str, dict] = {}
                for r in data["results"]:
                    code = str(r.get("code", "")).strip()
                    if code not in codes_in_batch:
                        continue
                    verdict = str(r.get("verdict", "")).upper().strip()
                    if verdict not in ("PASS", "FAIL"):
                        continue
                    try:
                        score = int(r.get("score", 0))
                    except (ValueError, TypeError):
                        score = 0
                    out[code] = {
                        "verdict": verdict,
                        "score": max(0, min(score, 100)),
                        "reason": str(r.get("reason", "")).strip(),
                    }
                if out:
                    return out
                raise ValueError("판정 결과가 비었습니다")

            except Exception as e:
                logger.warning(
                    f"  기본적 분석 판정 실패 ({model}, 시도 {attempt + 1}/2): {e}"
                )
                time.sleep(1.5)

        if model != _FALLBACK_MODEL:
            logger.info(f"  {model} 실패 → 폴백 모델 {_FALLBACK_MODEL} 로 전환")

    return {}


# ══════════════════════════════════════════════════════════════════
# 하드 게이트 (LLM 호출 전 사전 컷)
# ══════════════════════════════════════════════════════════════════

def _hard_gate(fundamentals: Optional[dict]) -> Optional[str]:
    """
    LLM 에게 물어볼 것도 없이 탈락인 경우만 사유를 돌려준다. 통과면 None.

    업종 특수성이 개입할 여지가 없는 것만 본다 — 어느 업종이든 적자이거나 자본잠식이면
    중장기 매수 후보가 아니다. 부채비율·PER 처럼 업종마다 정상 범위가 다른 지표는
    여기서 건드리지 않고 LLM 에게 맡긴다.
    """
    if not fundamentals:
        return "재무제표 조회 실패 (DART 미등록 또는 보고서 없음)"

    op = fundamentals.get("operating_income")
    net = fundamentals.get("net_income")
    equity = fundamentals.get("equity")
    cf_op = fundamentals.get("cf_operating")

    if equity is not None and equity <= 0:
        return "자본잠식 (자본총계 ≤ 0)"
    if op is not None and op <= 0:
        return f"영업적자 (영업이익 {_num(op)})"
    if net is not None and net <= 0:
        return f"당기순손실 (순이익 {_num(net)})"
    if cf_op is not None and cf_op <= 0:
        return f"영업활동 현금흐름 음수 ({_num(cf_op)})"

    # 판단 근거가 너무 없으면 LLM 에게 물어도 FAIL 이 나온다 — 호출을 아낀다
    key_metrics = [
        fundamentals.get("operating_margin"),
        fundamentals.get("roe"),
        fundamentals.get("roic"),
    ]
    if sum(1 for m in key_metrics if m is None) >= 2:
        return "핵심 수익성 지표 결측 (영업이익률/ROE/ROIC 중 2개 이상)"

    return None


# ══════════════════════════════════════════════════════════════════
# 진입점
# ══════════════════════════════════════════════════════════════════

def review(candidates: List[dict], fundamentals: Dict[str, dict]) -> dict:
    """
    후보군 기본적 분석 판정.

    candidates: [{"code","name","sector","market_cap"}]
    fundamentals: {code: dart_service 지표 dict}

    Returns:
      {"passed": [...], "failed": [...], "llm_failed": bool, "message": str}
      passed 각 항목에 fundamental_score / fundamental_reason 이 붙는다.
    """
    passed: List[dict] = []
    failed: List[dict] = []
    to_ask: List[dict] = []

    for c in candidates:
        fund = fundamentals.get(c["code"])
        blocked = _hard_gate(fund)
        if blocked:
            failed.append({**c, "fundamental_reason": blocked, "fundamental_score": 0,
                           "stage": "hard_gate"})
            continue
        to_ask.append({**c, "fundamentals": fund})

    logger.info(
        f"  하드 게이트: {len(candidates)}종목 중 {len(failed)}종목 탈락 → "
        f"LLM 판정 대상 {len(to_ask)}종목"
    )

    if not to_ask:
        return {
            "passed": [], "failed": failed, "llm_failed": False,
            "message": f"하드 게이트에서 전원 탈락 ({len(failed)}종목)",
        }

    if not settings.ANTHROPIC_API_KEY:
        logger.error("  ANTHROPIC_API_KEY 미설정 — 기본적 분석 Fail-Close (전원 탈락)")
        return {
            "passed": [], "failed": failed + to_ask, "llm_failed": True,
            "message": "ANTHROPIC_API_KEY 미설정 — Fail-Close",
        }

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    verdicts: Dict[str, dict] = {}
    batches = -(-len(to_ask) // TICKERS_PER_CALL)
    for i in range(0, len(to_ask), TICKERS_PER_CALL):
        batch = to_ask[i : i + TICKERS_PER_CALL]
        logger.info(
            f"  기본적 분석 LLM {i // TICKERS_PER_CALL + 1}/{batches} 배치 ({len(batch)}종목)"
        )
        verdicts.update(_review_batch(client, batch))

    if not verdicts:
        logger.error("  기본적 분석 LLM 전체 실패 — Fail-Close (전원 탈락)")
        return {
            "passed": [], "failed": failed + to_ask, "llm_failed": True,
            "message": "LLM 판정 전체 실패 — Fail-Close",
        }

    for c in to_ask:
        v = verdicts.get(c["code"])
        entry = {k: val for k, val in c.items() if k != "fundamentals"}
        entry["fundamentals"] = c["fundamentals"]
        if v is None:
            # 판정을 못 받은 종목도 통과시키지 않는다 (Fail-Close 원칙)
            failed.append({**entry, "fundamental_reason": "LLM 판정 누락",
                           "fundamental_score": 0, "stage": "llm"})
            continue
        entry["fundamental_score"] = v["score"]
        entry["fundamental_reason"] = v["reason"]
        entry["stage"] = "llm"
        (passed if v["verdict"] == "PASS" else failed).append(entry)

    passed.sort(key=lambda x: x["fundamental_score"], reverse=True)
    msg = (
        f"기본적 분석: {len(candidates)}종목 → PASS {len(passed)}종목 "
        f"(하드게이트 탈락 {sum(1 for f in failed if f.get('stage') == 'hard_gate')}, "
        f"LLM 탈락 {sum(1 for f in failed if f.get('stage') == 'llm')})"
    )
    logger.info(f"  {msg}")
    return {"passed": passed, "failed": failed, "llm_failed": False, "message": msg}
