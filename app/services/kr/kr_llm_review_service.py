"""
국내주식 매수 후보 LLM 최종 검토.

미국 트랙(llm_review_service.py)과 동일한 원칙:
  - LLM 은 거부권만 가진다 (BUY → HOLD 로만 바꿀 수 있고, 새 종목을 추가할 수 없다)
  - LLM 호출이 전부 실패하면 매수를 차단한다 (Fail-Close) + Slack 즉시 알림

프롬프트만 한국 시장 맥락으로 다시 썼다 (외국인 수급, 환율, 실적 시즌, 지주사 등).
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
from app.services.kr.kr_notification_service import notify_llm_failure

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]
MODELS = ["claude-opus-5", "claude-sonnet-5"]
# sampling 파라미터를 받지 않는 모델 (Opus 4.7부터 제거)
MODELS_WITHOUT_TEMPERATURE = {
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-fable-5",
}


def _save_decision_logs(
    candidates: List[dict],
    decision_map: dict,
    market_analysis: str,
    market: Optional[dict],
):
    """LLM 판단 결과를 kr_llm_decision_logs 에 저장 (동일 날짜+종목이면 업데이트)."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    market = market or {}
    try:
        for c in candidates:
            code = c["code"]
            d = decision_map.get(code, {})
            supabase.table("kr_llm_decision_logs").upsert(
                {
                    "decision_date": today,
                    "code": code,
                    "stock_name": c.get("stock_name"),
                    "decision": d.get("decision", "N/A"),
                    "reason": d.get("reason", ""),
                    "market_analysis": market_analysis,
                    "composite_score": c.get("composite_score"),
                    "rise_probability": c.get("rise_probability"),
                    "rsi": c.get("rsi"),
                    "adx": c.get("adx"),
                    "sentiment_score": c.get("sentiment_score"),
                    "net_buy_5d": c.get("net_buy_score"),
                    "kospi_vol_20d": market.get("kospi_vol_20d"),
                    "usdkrw": market.get("usdkrw"),
                    "updated_at": datetime.now(KST).isoformat(),
                },
                on_conflict="decision_date,code",
            ).execute()
        logger.info(f"  LLM 판단 로그 저장 완료: {len(candidates)}건")
    except Exception as e:
        logger.warning(f"  LLM 판단 로그 저장 실패: {e}")


def _format_candidates(candidates: List[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates, 1):
        f = c.get("kr_factors", {})
        sent = c.get("sentiment_score")
        sent_str = (
            f"{sent:+.2f} (관련기사 {c.get('article_count', 0)}건)"
            if sent is not None
            else "데이터 없음"
        )
        summary = c.get("sentiment_summary")
        net_buy = c.get("net_buy_score")
        net_buy_str = (
            f"{net_buy:+,.0f}백만원" if net_buy is not None else "데이터 없음"
        )
        interest = c.get("search_interest")
        interest_str = f"{interest:.2f}배" if interest is not None else "데이터 없음"

        rsi = c.get("rsi", 50) or 50
        rsi_note = (
            "과매도 반등 구간" if rsi < 30
            else "정상 매수 구간" if rsi <= 65
            else "과열"
        )
        adx = c.get("adx")
        adx_note = (
            "강한 추세" if adx and adx > 25
            else "추세 약함" if adx and adx < 20
            else "보통"
        )

        lines.append(
            f"""
{i}. {c.get('stock_name')} ({c.get('code')}) — 섹터: {c.get('sector', '?')}
   - ML 예측: 상승률 +{c.get('rise_probability', 0):.2f}% (현재가 {c.get('last_price') or 0:,.0f}원 → 예측가 {c.get('predicted_price') or 0:,.0f}원)
   - 기술적 지표:
     골든크로스 {'O' if c.get('golden_cross') else 'X'} (SMA20 {c.get('sma20', 0):,.0f} / SMA50 {c.get('sma50', 0):,.0f})
     RSI {rsi:.1f} ({rsi_note})
     MACD {c.get('macd', 0):.2f} / Signal {c.get('signal', 0):.2f} → 매수신호 {'O' if c.get('macd_buy_signal') else 'X'}
     ADX {adx if adx is not None else 'N/A'} ({adx_note})
   - 거래량: 5일 평균 대비 {c.get('volume_ratio', 'N/A')}배
   - 수급(외국인+기관 5일 누적 순매수): {net_buy_str}
   - 뉴스 감성: {sent_str}{f" — {summary}" if summary else ""}
   - 네이버 검색 관심도(평시 대비): {interest_str}
   - 종합점수: {c.get('composite_score', 0):+.4f}
     (수급 z={f.get('z_flow', 0):+.2f}, 기술 z={f.get('z_tech', 0):+.2f}, ML z={f.get('z_rise', 0):+.2f}, 거래량 z={f.get('z_vol', 0):+.2f}, ADX z={f.get('z_adx', 0):+.2f}, 감성 z={f.get('z_sent', 0):+.2f})"""
        )
    return "\n".join(lines)


def _build_prompt(candidates: List[dict], market: dict) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d (%a)")
    kospi = market.get("kospi")
    vol = market.get("kospi_vol_20d")
    usdkrw = market.get("usdkrw")
    fx_chg = market.get("usdkrw_chg_20d")
    vix = market.get("vix")

    return f"""당신은 한국 주식시장 경력 20년의 애널리스트이자 최종 의사결정자입니다.

## 당신의 역할
아래 종목들은 자동매매 시스템(팀원)이 ML 예측, 기술적 분석, 뉴스 감성, 외국인·기관 수급을
종합해 매수 후보로 올린 KOSPI 대형주입니다.
당신은 팀장으로서 이를 최종 검토하고 각 종목에 BUY 또는 HOLD 를 판정합니다.
팀원의 분석이 맞을 수도, 틀릴 수도 있습니다. 제공된 데이터와 당신의 시장 지식으로 독립적으로 판단하세요.
당신은 거부권만 가집니다 — 목록에 없는 종목을 추가할 수는 없습니다.

## 오늘 날짜
{today}

## 시장 환경
- 코스피: {f'{kospi:,.2f}' if kospi else 'N/A'}
- 코스피 20일 실현변동성(연율): {f'{vol:.1f}%' if vol else 'N/A'}  ← 한국판 공포지수(2006~2026 실측: 중위 15%, 90%ile 30%, 95%ile 42%)
- 원/달러 환율: {f'{usdkrw:,.1f}원' if usdkrw else 'N/A'} (최근 20일 {f'{fx_chg:+.2f}%' if fx_chg is not None else 'N/A'})
- 미국 VIX: {vix if vix else 'N/A'}

## 매수 후보 종목
{_format_candidates(candidates)}

## 검토 기준

### 한국 시장 특유의 리스크
- 원화 약세(원/달러 상승)가 가파르면 외국인 순매도 압력이 커집니다. 수급 지표가 약한 종목은 더 보수적으로 보세요.
- 외국인·기관 순매수가 마이너스인데 다른 지표만으로 후보에 올라온 종목은 신중히 검토하세요. 한국 대형주는 수급이 방향을 좌우하는 경우가 많습니다.
- 분기 실적 시즌(1·4·7·10월 하순~다음 달 중순) 직전이면 어닝 쇼크 변동성을 감안하세요.
- 유상증자·전환사채·블록딜 등 물량 부담 이슈가 감성 요약에 있으면 HOLD 를 강하게 고려하세요.
- 지주회사(SK, 삼성물산 등)는 자회사 실적이 지표에 지연 반영되는 특성이 있습니다.

### 기술적 지표 검증
- 골든크로스가 떴지만 SMA20 과 SMA50 의 차이가 미미하면 노이즈일 수 있습니다.
- RSI 과매도(<30)는 반등 기회일 수 있으나 ADX 가 20 미만이면 추세 없는 횡보입니다.
- RSI 70 초과 종목이 후보에 있다면 시스템 오류 가능성 → HOLD.

### 개인 관심도 해석 (주의)
- 네이버 검색 관심도가 3배 이상으로 치솟은 종목은 개인 매수세가 이미 몰린 뒤일 가능성이 있습니다.
  수급(외국인·기관)이 함께 유입 중이면 긍정적이지만, 수급이 빠지는데 검색만 폭증했다면 고점 신호로 보세요.

### 포트폴리오 균형
- 같은 섹터가 3개 이상 몰리면 가장 약한 종목을 HOLD 하세요 (전부 HOLD 하지는 마세요).

## 판정 원칙
- BUY 와 HOLD 모두 구체적 근거를 제시하세요.
- 막연한 불안감이 아니라 제시된 데이터와 사실에 기반해 판단하세요.
- 살 만한 종목은 사고 위험한 종목은 거부하는 균형 잡힌 판단을 하세요.

## 응답 형식
반드시 아래 JSON 만 출력하세요. 다른 텍스트나 코드펜스를 덧붙이지 마세요.
{{
  "market_analysis": "오늘 한국 시장 전반에 대한 간단한 분석 (1~2문장)",
  "decisions": [
    {{
      "code": "종목코드 6자리",
      "stock_name": "종목명",
      "decision": "BUY 또는 HOLD",
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


def review_buy_candidates(candidates: List[dict], market: Optional[dict] = None) -> dict:
    """
    매수 후보를 Claude 로 최종 검토한다.

    Returns:
        {"reviewed_candidates": [BUY], "held_candidates": [HOLD],
         "llm_reasoning": str, "raw_response": [...]}
    """
    market = market or {}

    if not candidates:
        return {
            "reviewed_candidates": [],
            "held_candidates": [],
            "llm_reasoning": "매수 후보 없음",
            "raw_response": [],
        }

    if not settings.ANTHROPIC_API_KEY:
        msg = "ANTHROPIC_API_KEY 미설정 — LLM 검토 불가로 매수 차단"
        logger.error(f"  {msg}")
        try:
            notify_llm_failure(reason=msg, candidate_count=len(candidates))
        except Exception as e:
            logger.warning(f"  LLM 실패 알림 발송 실패: {e}")
        return {
            "reviewed_candidates": [],
            "held_candidates": candidates,
            "llm_reasoning": msg,
            "raw_response": [],
        }

    prompt = _build_prompt(candidates, market)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    last_error = None

    for model in MODELS:
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(f"  LLM 검토 시도 {attempt + 1}/{MAX_RETRIES} (모델: {model})")
                kwargs = {
                    "model": model,
                    # Opus 5 / Sonnet 5 는 thinking 과 답변이 같은 max_tokens 예산을 쓴다.
                    # effort 를 낮추고 max_tokens 을 넉넉히 잡아 답변이 잘리지 않게 한다.
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

                decisions = data.get("decisions", [])
                market_analysis = data.get("market_analysis", "")
                fallback_note = f" (폴백: {model})" if model != MODELS[0] else ""

                decision_map = {}
                for d in decisions:
                    code = str(d.get("code", "")).zfill(6)
                    if code in universe.CODE_TO_NAME or code in {c["code"] for c in candidates}:
                        decision_map[code] = d

                reviewed, held = [], []
                for c in candidates:
                    d = decision_map.get(c["code"], {})
                    verdict = str(d.get("decision", "HOLD")).upper()
                    reason = d.get("reason", "LLM 응답에 해당 종목 판정 없음")
                    c["llm_decision"] = verdict
                    c["llm_reason"] = reason
                    if verdict == "BUY":
                        reviewed.append(c)
                    else:
                        held.append(c)
                        logger.info(f"  LLM HOLD: {universe.display(c['code'])} — {reason}")

                logger.info(
                    f"  LLM 검토 완료{fallback_note}: BUY {len(reviewed)} / HOLD {len(held)}"
                )
                logger.info(f"  시장 분석: {market_analysis}")

                _save_decision_logs(candidates, decision_map, market_analysis, market)

                return {
                    "reviewed_candidates": reviewed,
                    "held_candidates": held,
                    "llm_reasoning": market_analysis + fallback_note,
                    "raw_response": decisions,
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
                logger.warning(f"  LLM 검토 실패 ({model}, 시도 {attempt + 1}): {e}")
                # JSON 파싱 실패는 재시도해도 같은 결과일 확률이 높아 바로 폴백
                break

        if model != MODELS[-1]:
            logger.info(f"  {model} 실패 → 폴백 모델 {MODELS[MODELS.index(model) + 1]} 로 전환")

    fail_reason = f"LLM 검토 전체 실패 (Opus/Sonnet 각 {MAX_RETRIES}회): {last_error}"
    logger.error(f"  {fail_reason}")

    fail_map = {c["code"]: {"decision": "FAIL", "reason": fail_reason} for c in candidates}
    _save_decision_logs(candidates, fail_map, fail_reason, market)

    try:
        notify_llm_failure(reason=fail_reason, candidate_count=len(candidates))
    except Exception as e:
        logger.warning(f"  LLM 실패 알림 발송 실패: {e}")

    return {
        "reviewed_candidates": [],
        "held_candidates": candidates,
        "llm_reasoning": fail_reason,
        "raw_response": [],
    }
