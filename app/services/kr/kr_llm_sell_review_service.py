"""
국내주식 보유 종목 LLM 매도 검토.

매수 검토(kr_llm_review_service.py)와 같은 모델/재시도/Fail-Close 구조를 쓰지만 방향이 다르다:
  - 매수 LLM 은 거부권만 가진다 (BUY → HOLD). 실패 시 매수를 막는 것이 안전한 기본값이다.
  - 이 매도 LLM 은 HOLD/SELL_ALL 을 직접 결정한다 (매도는 언제나 전량이다 — 부분매도는 없다).
    하지만 ATR 익절/손절선·기술신호개수·공포장 규칙은 이 판단과 무관하게 항상 기계적으로 따로 실행되므로
    (app/services/kr/kr_recommendation_service.py::get_mechanical_sell_candidates), 이 LLM 이
    전부 실패해도 자금은 이미 보호되고 있다. 그래서 Fail-Close 기본값은 "추가 매도 안 함(HOLD)"이다
    — 매수 쪽 Fail-Close("매수 차단")와 방향이 반대인 것은 의도된 설계다.
"""
import json
import logging
import re
import time
from datetime import datetime
from typing import List, Optional

import anthropic
import pytz

from app.core.config import settings
from app.db.supabase import supabase
from app.services.kr import universe
from app.services.kr.kr_notification_service import notify_llm_sell_failure

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]
MODELS = ["claude-opus-5", "claude-sonnet-5"]
MODELS_WITHOUT_TEMPERATURE = {
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-fable-5",
}


def _save_sell_decision_logs(
    holdings_context: List[dict],
    decision_map: dict,
    market_analysis: str,
    market: Optional[dict],
):
    """
    LLM 매도 판단을 kr_llm_sell_decision_logs 에 저장한다.

    append-only — 항상 새 행을 INSERT 한다(upsert 아님). 같은 날 재점검이 여러 번 돌 수 있는데
    (장중 추가 매도검토), 덮어쓰면 하루 안에서 판단이 바뀐 과정이 사라진다. 읽는 쪽은
    (decision_date, code) 별 가장 최신(created_at) 행만 "오늘의 유효 판단"으로 취급한다.
    """
    today = datetime.now(KST).strftime("%Y-%m-%d")
    market = market or {}
    try:
        rows = []
        for h in holdings_context:
            code = h["code"]
            d = decision_map.get(code, {})
            rotation = h.get("rotation_candidate") or {}
            rows.append(
                {
                    "decision_date": today,
                    "code": code,
                    "stock_name": h.get("stock_name"),
                    "decision": d.get("decision", "N/A"),
                    "reason": d.get("reason", ""),
                    "market_analysis": market_analysis,
                    "composite_score": h.get("composite_score"),
                    "score_rank": h.get("score_rank"),
                    "score_universe_size": h.get("score_universe_size"),
                    "factor_reversals": ", ".join(h.get("technical_sell_details") or []),
                    "sentiment_score": h.get("sentiment_score"),
                    "rotation_flag": bool(h.get("rotation_flag")),
                    "rotation_candidate_code": rotation.get("code"),
                    "rotation_candidate_score": rotation.get("composite_score"),
                    "price_at_decision": h.get("current_price"),
                    "signal_count_at_decision": h.get("technical_sell_signals"),
                    "status": "pending",
                }
            )
        supabase.table("kr_llm_sell_decision_logs").insert(rows).execute()
        logger.info(f"  LLM 매도 판단 로그 저장 완료: {len(rows)}건")
    except Exception as e:
        logger.warning(f"  LLM 매도 판단 로그 저장 실패: {e}")


def _format_holdings(holdings_context: List[dict], is_intraday: bool = False) -> str:
    score_asof = "전날 마감 기준" if is_intraday else "오늘"
    lines = []
    for i, h in enumerate(holdings_context, 1):
        score = h.get("composite_score")
        score_str = (
            f"{score:+.4f} (순위 {h.get('score_rank')}/{h.get('score_universe_size')})"
            if score is not None
            else f"산출 불가 ({h.get('score_note') or '데이터 부족'})"
        )
        mech_bits = []
        if h.get("atr"):
            mech_bits.append(f"ATR={h['atr']:,.0f}")
        if h.get("stop_loss_price"):
            mech_bits.append(f"손절가 {h['stop_loss_price']:,.0f}원")
        if h.get("stop_distance_pct") is not None:
            mech_bits.append(f"손절선까지 {h['stop_distance_pct']:+.2f}% 여유")
        if h.get("take_profit_distance_pct") is not None:
            mech_bits.append(f"익절선까지 {h['take_profit_distance_pct']:+.2f}% 남음")
        mech_str = ", ".join(mech_bits) if mech_bits else "레거시 보유분(ATR 정보 없음)"

        trend = h.get("score_trend") or []
        if len(trend) >= 2:
            trend_str = " → ".join(
                f"{t['decision_date'][5:]} {t['score_rank']}위({t['composite_score']:+.2f})"
                for t in trend
                if t.get("score_rank") is not None and t.get("composite_score") is not None
            )
            if not trend_str:
                trend_str = "이력은 있으나 점수 산출 불가 기록뿐"
        else:
            trend_str = "이력 부족(오늘이 사실상 첫 기록 — 하루짜리 신호일 수 있으니 과신하지 마세요)"

        details = h.get("technical_sell_details") or []
        details_str = ", ".join(details) if details else "없음"
        sent = h.get("sentiment_score")
        sent_str = f"{sent:+.2f}" if sent is not None else "데이터 없음"
        adx = h.get("adx")
        rotation = h.get("rotation_candidate")
        rotation_str = (
            f"\n   - ⚠️ 교체매매 후보 있음: 대기 중인 {rotation.get('stock_name')}"
            f"({rotation.get('code')}) 점수 {rotation.get('composite_score'):+.4f} "
            f"(보유 슬롯이 가득 찬 상태에서 이 종목보다 점수가 KR_ROTATION_MIN_SCORE_GAP 이상 높습니다)"
            if h.get("rotation_flag") and rotation
            else ""
        )

        lines.append(
            f"""
{i}. {h.get('stock_name')} ({h.get('code')}) — 보유 {h.get('quantity')}주
   - 매입가 {h.get('buy_price', 0):,.0f}원 → 현재가 {h.get('current_price', 0):,.0f}원
     ({h.get('price_change_percent', 0):+.2f}%)
   - 기계적 상태: {mech_str}
   - 종합점수({score_asof}, 전체 유니버스 대비): {score_str}
   - 최근 순위/점수 추이(오래된 순): {trend_str}
   - 기술적 매도신호 {h.get('technical_sell_signals', 0)}개: {details_str} (ADX={adx if adx is not None else 'N/A'})
   - 뉴스 감성: {sent_str}{rotation_str}"""
        )
    return "\n".join(lines)


def _build_prompt(holdings_context: List[dict], market: dict, is_intraday: bool = False) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d (%a)")
    kospi = market.get("kospi")
    vol = market.get("kospi_vol_20d")
    usdkrw = market.get("usdkrw")

    intraday_note = ""
    if is_intraday:
        intraday_note = """

## ⏱ 장중 재점검 안내
지금은 장 마감 후 정기 검토(하루 1회)가 아니라, 공포지수가 높게 지속되는 장중에 추가로 도는
재점검입니다. **아래 종합점수/순위/기술신호/뉴스감성은 전날 마감 기준 스냅샷 그대로이고 장중에
갱신되지 않습니다** — 새로운 정보가 아니라 참고용 배경입니다. 실질적으로 바뀐 건 현재가와
손절/익절선까지의 거리뿐입니다. 판단 기준은 비대칭입니다 — **마지막 판단(전날 또는 오늘 이전
재점검) 이후 가격이 불리한 방향으로 눈에 띄게 더 움직였다면 그건 예외이니 주저 말고 판정을
바꾸세요. 반대로 별다른 변화가 없다면(가격이 그대로거나 유리하게 움직였다면) 근거 없이 판정을
바꾸지 마세요** — "애매하면 유지"가 아니라 "나빠졌으면 반응, 그대로면 유지"입니다."""

    stale_tag = " _(장중엔 갱신 안 됨 — 새 근거로 쓰지 마세요)_" if is_intraday else ""

    if is_intraday:
        hold_or_react_principle = (
            "- 가격이 판단 시점보다 불리한 방향으로 눈에 띄게 더 움직였다면 주저 말고 판정을 "
            "바꾸세요. 반대로 별다른 변화가 없다면(그대로거나 유리하게 움직였다면) 근거 없이 "
            "판정을 바꾸지 마세요 — 기계적 손절선이 이미 하방을 지키고 있으니 애매한데 "
            "굳이 팔 이유는 없습니다."
        )
    else:
        hold_or_react_principle = (
            "- 애매하면 HOLD 하세요 — 기계적 손절선이 이미 하방을 지키고 있으므로 "
            "무리하게 팔 이유가 없습니다."
        )

    return f"""당신은 한국 주식시장 경력 20년의 포트폴리오 매니저입니다.

## 당신의 역할
아래는 이미 보유 중인 국내 종목입니다. **ATR 익절/손절선과 기술신호개수·공포장 자동매도 규칙은
이 판단과 무관하게 별도로 항상 기계적으로 실행됩니다** (이번 사이클에 이미 매도 주문이 나갔을 수도
있습니다). 당신은 그 위에 추가로, 아래 정성적 근거만 보고 종목별로
HOLD(계속 보유) / SELL_ALL(전량매도) 를 결정하는 팀장입니다.

**이 시스템에 부분매도는 없습니다.** 팔기로 하면 보유수량 전부를 팝니다. 그러니 "조금 줄이고
싶다" 는 애매한 상태는 SELL_ALL 이 아니라 HOLD 입니다 — 전량을 정리할 만큼 근거가 분명할 때만
SELL_ALL 을 내리세요.{intraday_note}

## 판단 근거로 삼을 것
- **점수 추이(감쇠)**{stale_tag}: 각 종목마다 "최근 순위/점수 추이"를 며칠치 함께 줍니다. 순위가
  하루만 나빴다가 회복됐다면 노이즈, 여러 날에 걸쳐 계속 밀리고 있다면 추세적 악화입니다 — 반드시
  이 추이를 보고 판단하고, 이력이 짧다고 표시된 종목은 단정하지 마세요.
- **개별 팩터반전**{stale_tag}: 데드크로스/RSI 과매수/MACD 매도신호/수급이탈 등이 몇 개나 겹쳤는지,
  그리고 왜 겹쳤는지(실적 이슈, 업황 등 알고 있는 맥락이 있다면 반영). 단, 이 신호들이 일정 개수
  이상 겹치면 이미 기계적 규칙이 전량매도를 별도로 실행하고 있으므로, 당신의 판단은 주로 "아직
  기계적 문턱에는 못 미치지만 조짐이 보이는" 구간에서 가치가 있습니다.
- **교체매매**{stale_tag}: 보유 슬롯이 가득 찬 상태에서 대기 중인 후보가 이 종목보다 점수가
  뚜렷하게 높다면, 이 종목을 팔아 슬롯을 넘겨줄 가치가 있는지 판단하세요. 슬롯 여유가 있거나
  점수 차가 크지 않다면 교체할 필요 없습니다.
- **하방 여유**: "기계적 상태"에 손절선까지 남은 폭이 나와 있습니다(실시간 반영).
  여유가 거의 없다면 어차피 곧 기계적으로 정리될 테니 당신이 무리해서 팔 필요는 적고, 여유가
  크다면 기계적 안전망이 당분간 작동하지 않는다는 뜻이라 당신의 판단이 더 중요해집니다.

## 오늘 날짜
{today}

## 시장 환경
- 코스피: {f'{kospi:,.2f}' if kospi else 'N/A'}
- 코스피 20일 실현변동성(연율): {f'{vol:.1f}%' if vol else 'N/A'}
- 원/달러 환율: {f'{usdkrw:,.1f}원' if usdkrw else 'N/A'}

## 보유 종목
{_format_holdings(holdings_context, is_intraday=is_intraday)}

## 판정 원칙
{hold_or_react_principle}
- SELL_ALL 은 구체적 근거(점수감쇠 추세, 팩터반전 개수, 교체매매 등)를 명시하세요.
- 판정은 HOLD 아니면 SELL_ALL 둘뿐입니다. 중간값은 없습니다.

## 응답 형식
반드시 아래 JSON 만 출력하세요. 다른 텍스트나 코드펜스를 덧붙이지 마세요.
{{
  "market_analysis": "오늘 한국 시장 전반에 대한 간단한 분석 (1~2문장)",
  "decisions": [
    {{
      "code": "종목코드 6자리",
      "stock_name": "종목명",
      "decision": "HOLD 또는 SELL_ALL",
      "reason": "판정 이유 (1~2문장)"
    }}
  ]
}}"""


def _extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def review_sell_candidates(
    holdings_context: List[dict], market: Optional[dict] = None, is_intraday: bool = False
) -> dict:
    """
    보유 종목을 Claude 로 종합 검토한다.

    is_intraday=True 면 장중 추가 매도검토(조건부 주기체크)임을 프롬프트에 명시해, 전날 마감
    스냅샷인 점수/신호/감성을 새 정보처럼 재해석하지 않고 가격 변화 위주로만 재확인하게 한다.

    Returns:
        {"decisions": [{"code","stock_name","decision","reason"}, ...], "market_analysis": str}
    """
    market = market or {}

    if not holdings_context:
        return {"decisions": [], "market_analysis": "보유 종목 없음"}

    if not settings.ANTHROPIC_API_KEY:
        msg = "ANTHROPIC_API_KEY 미설정 — LLM 매도검토 불가, 전 종목 HOLD 유지(Fail-Close)"
        logger.error(f"  {msg}")
        try:
            notify_llm_sell_failure(reason=msg, held_count=len(holdings_context))
        except Exception as e:
            logger.warning(f"  LLM 매도검토 실패 알림 발송 실패: {e}")
        fail_map = {h["code"]: {"decision": "FAIL", "reason": msg} for h in holdings_context}
        _save_sell_decision_logs(holdings_context, fail_map, msg, market)
        return {"decisions": [], "market_analysis": msg}

    prompt = _build_prompt(holdings_context, market, is_intraday=is_intraday)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    last_error = None

    for model in MODELS:
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(f"  LLM 매도검토 시도 {attempt + 1}/{MAX_RETRIES} (모델: {model})")
                kwargs = {
                    "model": model,
                    "max_tokens": 16000,
                    "output_config": {"effort": "low"},
                    "messages": [{"role": "user", "content": prompt}],
                }
                if model not in MODELS_WITHOUT_TEMPERATURE:
                    kwargs["temperature"] = 0

                message = client.messages.create(**kwargs)

                if message.stop_reason == "refusal":
                    raise ValueError(
                        f"모델이 응답을 거부 "
                        f"(category={getattr(message.stop_details, 'category', None)})"
                    )
                if message.stop_reason == "max_tokens":
                    raise ValueError("max_tokens 도달로 응답이 잘렸습니다")

                text_block = next((b for b in message.content if b.type == "text"), None)
                if text_block is None:
                    raise ValueError("응답에 텍스트 블록이 없습니다 (thinking 만 반환)")

                data = _extract_json(text_block.text)
                if not data:
                    raise ValueError(f"JSON 파싱 실패: {text_block.text[:400]}")

                raw_decisions = data.get("decisions", [])
                market_analysis = data.get("market_analysis", "")
                fallback_note = f" (폴백: {model})" if model != MODELS[0] else ""

                decision_map = {}
                for d in raw_decisions:
                    code = str(d.get("code", "")).zfill(6)
                    if code in universe.CODE_TO_NAME or code in {h["code"] for h in holdings_context}:
                        decision_map[code] = d

                decisions = []
                for h in holdings_context:
                    d = decision_map.get(h["code"], {})
                    verdict = str(d.get("decision", "HOLD")).upper()
                    if verdict not in ("HOLD", "SELL_ALL"):
                        verdict = "HOLD"
                    reason = d.get("reason", "LLM 응답에 해당 종목 판정 없음")
                    decisions.append(
                        {
                            "code": h["code"],
                            "stock_name": h.get("stock_name"),
                            "decision": verdict,
                            "reason": reason,
                        }
                    )
                    if verdict != "HOLD":
                        logger.info(f"  LLM {verdict}: {universe.display(h['code'])} — {reason}")

                sell_count = sum(1 for d in decisions if d["decision"] != "HOLD")
                logger.info(
                    f"  LLM 매도검토 완료{fallback_note}: "
                    f"HOLD {len(decisions) - sell_count} / 매도판정 {sell_count}"
                )
                logger.info(f"  시장 분석: {market_analysis}")

                _save_sell_decision_logs(holdings_context, decision_map, market_analysis, market)

                return {
                    "decisions": decisions,
                    "market_analysis": market_analysis + fallback_note,
                }

            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                last_error = e
                status = getattr(e, "status_code", 0)
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                if status in (429, 500, 502, 503, 529):
                    logger.warning(
                        f"  LLM 과부하/속도제한 ({model}, {status}) — {delay}초 후 재시도"
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"  LLM API 에러 ({model}, {status}): {e}")
                break

            except anthropic.APIConnectionError as e:
                last_error = e
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(f"  LLM 네트워크 오류 ({model}) — {delay}초 후 재시도: {e}")
                time.sleep(delay)
                continue

            except Exception as e:
                last_error = e
                logger.warning(f"  LLM 매도검토 실패 ({model}, 시도 {attempt + 1}): {e}")
                break

        if model != MODELS[-1]:
            logger.info(f"  {model} 실패 → 폴백 모델 {MODELS[MODELS.index(model) + 1]} 로 전환")

    fail_reason = f"LLM 매도검토 전체 실패 (Opus/Sonnet 각 {MAX_RETRIES}회): {last_error}"
    logger.error(f"  {fail_reason}")

    fail_map = {h["code"]: {"decision": "FAIL", "reason": fail_reason} for h in holdings_context}
    _save_sell_decision_logs(holdings_context, fail_map, fail_reason, market)

    try:
        notify_llm_sell_failure(reason=fail_reason, held_count=len(holdings_context))
    except Exception as e:
        logger.warning(f"  LLM 매도검토 실패 알림 발송 실패: {e}")

    # Fail-Close: 실패해도 SELL 결정을 만들지 않는다. 기계적 손절선이 이미 자금을 보호한다.
    return {"decisions": [], "market_analysis": fail_reason}
