"""
국내주식 분석 리포트 생성 + Slack 전송.

Phase A(16:30 분석 파이프라인)가 끝난 뒤 그 결과물을 모아
  ① 매수 견적서(집행 예정 종목·수량·금액)를 기계적으로 계산하고
  ② Claude 에게 리포트 서술(시장 진단·종목별 논거·리스크·체크리스트)을 맡긴 뒤
  ③ PDF 로 렌더링해 Slack 채널에 업로드한다.

설계 원칙 — **리포트는 매매에 절대 영향을 주지 않는다**:
  - 이 모듈의 어떤 실패도 예외를 밖으로 던지지 않는다 (호출부에서 한 번 더 감싸지만 이중 방어)
  - LLM 호출이 실패해도 기계적 데이터만으로 PDF 를 만들어 보낸다 (Fail-Open)
  - Slack Bot Token 이 없으면 PDF 는 로컬에 남기고 Webhook 으로 경로만 알린다

매수 견적서의 수량은 **참고치**다. 실제 주문은 Phase B(09:05)에서 현재가를 재조회해
다시 계산하므로, 여기서는 분석 시점 종가를 기준으로 같은 배분 로직을 돌린 값을 보여준다.
"""
import glob
import json
import logging
import os
import time
from datetime import datetime
from typing import List, Optional

import anthropic
import pytz

from app.core.config import settings
from app.services.kr import kis_domestic_service as kis
from app.services.kr import slack_file_service, universe
from app.services.kr import kr_pdf_service
from app.services.slack_service import _send
from app.services.position_sizing import compute_weighted_slots

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

FALLBACK_MODEL = "claude-sonnet-5"
MAX_RETRIES = 2
RETRY_DELAYS = [5, 15]

# LLM 서술 스키마 — structured outputs 로 강제해 파싱 실패 자체를 없앤다
NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "리포트 첫 줄 한 문장 요약 (60자 이내)",
        },
        "market_view": {
            "type": "string",
            "description": "시장 진단 2~4문단. 문단은 개행(\\n)으로 구분.",
        },
        "buy_thesis": {
            "type": "array",
            "description": "매수 예약된 종목에 대해서만 작성. 예약이 없으면 빈 배열.",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "stock_name": {"type": "string"},
                    "thesis": {"type": "string", "description": "매수 논거 2~4문장"},
                    "risk": {"type": "string", "description": "이 종목의 핵심 리스크 1~2문장"},
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["code", "stock_name", "thesis", "risk", "confidence"],
                "additionalProperties": False,
            },
        },
        "portfolio_action": {
            "type": "string",
            "description": "보유 포지션 관점의 조치 요약 1~2문단. 보유가 없으면 그 사실을 적는다.",
        },
        "risk_factors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "포트폴리오 전체 리스크 3~5개",
        },
        "tomorrow_checklist": {
            "type": "array",
            "items": {"type": "string"},
            "description": "다음 영업일 개장 전/중 확인할 항목 3~5개",
        },
    },
    "required": [
        "headline",
        "market_view",
        "buy_thesis",
        "portfolio_action",
        "risk_factors",
        "tomorrow_checklist",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 한국 주식시장 전담 애널리스트다. 자동매매 시스템이 장 마감 후 산출한
분석 결과를 받아, 운용자가 다음 영업일 개장 전에 읽을 일일 리포트를 한국어로 작성한다.

규칙:
- 주어진 데이터에 없는 사실(뉴스, 실적 수치, 목표주가 등)을 지어내지 않는다.
  데이터가 부족하면 "데이터 없음"이라고 쓴다.
- 시스템의 판정을 그대로 옮겨 적지 말고, 지표들이 서로 무엇을 뜻하는지 해석한다.
  (예: ML 상승률은 높은데 RSI 과열 + 외국인 순매도라면 그 긴장 관계를 지적한다)
- 매수 논거는 이미 예약된 종목에 대해서만 쓴다. 새 종목을 추천하지 않는다.
- 확신도(confidence)는 지표 간 정합성으로 판단한다. 신호가 엇갈리면 LOW 를 준다.
- 문체는 간결한 평서문. 과장·감탄사·이모지를 쓰지 않는다.
- 리스크 항목은 "무엇이 어떻게 되면 손실인가"를 구체적으로 쓴다."""


# ══════════════════════════════════════════════════════════════════
# 매수 견적서
# ══════════════════════════════════════════════════════════════════

def build_buy_quote(approved: List[dict]) -> dict:
    """
    매수 예약 종목 → 견적서(종목별 예상 단가·수량·금액·비중).

    Phase B(execute_buy_queue)와 같은 배분 로직(compute_weighted_slots)을 쓰되,
    단가만 '분석 시점 종가'를 쓴다. 계좌 조회가 실패하면 금액 없이 비중만 채운다.
    """
    quote = {
        "rows": [],
        "total_quantity": 0,
        "total_amount": 0.0,
        "total_ratio_pct": 0.0,
        "total_assets": None,
        "cash": None,
        "method": settings.KR_SLOT_METHOD,
        "tilt": settings.KR_SLOT_TILT,
        "base_ratio_pct": settings.KR_SLOT_RATIO * 100,
        "min_ratio_pct": settings.KR_MIN_SLOT_RATIO * 100,
        "max_ratio_pct": settings.KR_MAX_SLOT_RATIO * 100,
        "note": None,
    }
    if not approved:
        quote["note"] = "오늘 예약된 매수 종목이 없습니다."
        return quote

    # 점수 내림차순 — Phase B 의 큐 정렬(composite_score desc)과 순서를 맞춘다
    rows_src = sorted(
        approved, key=lambda c: (c.get("composite_score") is None, -(c.get("composite_score") or 0))
    )
    ratios = compute_weighted_slots(
        scores=[c.get("composite_score") for c in rows_src],
        base_ratio=settings.KR_SLOT_RATIO,
        tilt=settings.KR_SLOT_TILT,
        min_ratio=settings.KR_MIN_SLOT_RATIO,
        max_ratio=settings.KR_MAX_SLOT_RATIO,
        max_total_exposure=settings.KR_MAX_TOTAL_EXPOSURE,
        method=settings.KR_SLOT_METHOD,
    )

    total_assets = None
    cash = None
    try:
        summary = kis.get_account_summary()
        if summary:
            cash = summary.get("d2_deposit", 0.0) or summary.get("deposit", 0.0)
            total_assets = cash + summary.get("stock_eval_amount", 0.0)
    except Exception as e:
        logger.warning(f"  견적서용 계좌 조회 실패(금액 생략): {e}")

    if not total_assets or total_assets <= 0:
        total_assets = None
        quote["note"] = (
            "계좌 총자산을 조회하지 못해 금액·수량은 비워두고 배분 비중만 표시합니다."
        )
    quote["total_assets"] = total_assets
    quote["cash"] = cash

    for c, ratio in zip(rows_src, ratios):
        code = c["code"]
        ref_price = _reference_price(c)
        quantity = amount = None
        if total_assets and ref_price:
            quantity = int((total_assets * ratio) // ref_price)
            amount = quantity * ref_price

        quote["rows"].append({
            "code": code,
            "stock_name": c.get("stock_name") or universe.CODE_TO_NAME.get(code, code),
            "sector": c.get("sector") or universe.CODE_TO_SECTOR.get(code, ""),
            "composite_score": c.get("composite_score"),
            "rise_probability": c.get("rise_probability"),
            "ref_price": ref_price,
            "quantity": quantity,
            "amount": amount,
            "ratio_pct": ratio * 100,
            "llm_reason": c.get("llm_reason"),
        })

    quote["total_quantity"] = sum(r["quantity"] or 0 for r in quote["rows"])
    quote["total_amount"] = sum(r["amount"] or 0 for r in quote["rows"])
    quote["total_ratio_pct"] = (
        quote["total_amount"] / total_assets * 100 if total_assets else sum(ratios) * 100
    )
    return quote


def _reference_price(candidate: dict) -> Optional[float]:
    """견적 기준 단가 — KIS 현재가(장 마감 후엔 종가) 우선, 실패 시 ML 최종 실거래가."""
    try:
        price = kis.get_current_price_value(candidate["code"])
        if price:
            return float(kis.round_to_tick(price, mode="up"))
    except Exception as e:
        logger.debug(f"  {candidate.get('code')} 현재가 조회 실패(종가 대체): {e}")
    last = candidate.get("last_price")
    try:
        return float(last) if last else None
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════
# LLM 서술
# ══════════════════════════════════════════════════════════════════

def _fmt_candidates(candidates: List[dict], limit: int = 40) -> str:
    if not candidates:
        return "(없음)"
    lines = []
    for c in candidates[:limit]:
        lines.append(
            f"- {c.get('stock_name')}({c.get('code')}, {c.get('sector') or '섹터미상'}) "
            f"판정={c.get('llm_decision') or 'N/A'} "
            f"종합점수={_r(c.get('composite_score'))} "
            f"ML상승률={_r(c.get('rise_probability'))}% "
            f"ML정확도={_r(c.get('accuracy'))}% "
            f"ML기준종가={_r(c.get('last_price'), 0)}원 예측가={_r(c.get('predicted_price'), 0)}원 "
            f"RSI={_r(c.get('rsi'), 1)} ADX={_r(c.get('adx'), 1)} "
            f"골든크로스={'O' if c.get('golden_cross') else 'X'} "
            f"MACD매수={'O' if c.get('macd_buy_signal') else 'X'} "
            f"거래량비={_r(c.get('volume_ratio'), 2)} "
            f"감성={_r(c.get('sentiment_score'))}(기사 {c.get('article_count') or 0}건) "
            f"5일수급={_r(c.get('net_buy_score'), 0)}백만원"
        )
        if c.get("sentiment_summary"):
            lines.append(f"    뉴스요약: {c['sentiment_summary']}")
        if c.get("llm_reason"):
            lines.append(f"    시스템 판정사유: {c['llm_reason']}")
    if len(candidates) > limit:
        lines.append(f"- (외 {len(candidates) - limit}종목 생략)")
    return "\n".join(lines)


def _r(v, digits: int = 2) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _build_prompt(ctx: dict) -> str:
    market = ctx.get("market") or {}
    quote = ctx.get("quote") or {}

    quote_lines = [
        f"- {r['stock_name']}({r['code']}) 예상단가 {_r(r['ref_price'], 0)}원 × "
        f"{r['quantity'] if r['quantity'] is not None else '?'}주 = "
        f"{_r(r['amount'], 0)}원 (총자산 대비 {_r(r['ratio_pct'], 1)}%)"
        for r in quote.get("rows", [])
    ] or ["- (매수 예약 없음)"]

    sell_lines = [
        f"- {d.get('stock_name')}({d.get('code')}) {d.get('decision')} — {d.get('reason')}"
        for d in (ctx.get("sell_decisions") or [])
    ] or ["- (보유 종목 없음 또는 매도검토 미실행)"]

    return f"""오늘({ctx.get('date')}) 장 마감 후 자동매매 시스템이 산출한 국내주식 분석 결과다.
이 데이터만 근거로 일일 리포트를 작성하라.

# 계좌·전략 설정
- 계좌 모드: {ctx.get('mode')}
- 종목당 기준 비중 {settings.KR_SLOT_RATIO:.0%} (배분 {settings.KR_SLOT_METHOD}, tilt {settings.KR_SLOT_TILT}), 최대 보유 {settings.KR_MAX_POSITIONS}종목
- 총 노출 상한 {settings.KR_MAX_TOTAL_EXPOSURE:.0%}, 종목당 {settings.KR_MIN_SLOT_RATIO:.0%}~{settings.KR_MAX_SLOT_RATIO:.0%}
- 매도 규칙: 2.5×ATR 도달 시 전량 익절 / 1.5×ATR 도달 시 전량 손절 (부분매도 없음)
- 매수 집행 예정 시각: 다음 영업일 {settings.KR_EXECUTION_TIME} KST (지정가, 집행 시점 현재가 재계산)
- 총자산 {_r(quote.get('total_assets'), 0)}원 / D+2 예수금 {_r(quote.get('cash'), 0)}원

# 시장 환경 (기준일 {market.get('date', 'N/A')})
- 코스피 {_r(market.get('kospi'))} / 코스피200 {_r(market.get('kospi200'))}
- 코스피 20일 실현변동성(공포지표) {_r(market.get('kospi_vol_20d'), 1)}%
  · 20% 미만 평온 / 25~35% 경계 / 35% 초과면 매수 하드블록
- 원/달러 {_r(market.get('usdkrw'), 1)}원 (20일 변화 {_r(market.get('usdkrw_chg_20d'))}%)
- 미국 VIX {_r(market.get('vix'))}
- 변동성 게이트 수동해제: {market.get('override') or '없음'}

# 매수 최종 검토 결과
시스템 시장 코멘트: {ctx.get('llm_reasoning') or '(없음)'}

## 매수 승인 ({len(ctx.get('approved') or [])}종목)
{_fmt_candidates(ctx.get('approved') or [])}

## 보류 HOLD ({len(ctx.get('held') or [])}종목)
{_fmt_candidates(ctx.get('held') or [])}

# 매수 견적서 (다음 영업일 집행 예정)
{chr(10).join(quote_lines)}
※ 견적 예상단가는 KIS 에서 조회한 최신 체결가를 호가단위로 올림한 값이고, 위 후보 목록의
  'ML기준종가'는 ML 학습에 쓰인 시점의 종가다. 데이터 갱신 시점이 달라 두 값은 다를 수 있으며
  집행은 항상 예상단가 쪽 계보(집행 시각 현재가 재조회)를 따른다. 두 값의 차이 자체를
  오류로 지적하지 말 것 — 단, 차이가 배수 수준(2배 이상)이면 데이터 이상으로 언급해도 된다.

# 보유 포지션 매도검토
시스템 코멘트: {ctx.get('sell_market_analysis') or '(없음)'}
{chr(10).join(sell_lines)}

위 내용을 바탕으로 지정된 JSON 스키마에 맞춰 리포트를 작성하라."""


def generate_narrative(ctx: dict) -> dict:
    """
    Claude 에게 리포트 서술을 요청한다.

    Returns:
        {"narrative": dict|None, "error": str|None, "model": str|None}
        실패해도 예외를 던지지 않는다 — 호출부는 서술 없이 PDF 를 만든다.
    """
    if not settings.ANTHROPIC_API_KEY:
        return {"narrative": None, "error": "ANTHROPIC_API_KEY 미설정", "model": None}

    prompt = _build_prompt(ctx)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    models = [settings.KR_REPORT_MODEL]
    if FALLBACK_MODEL not in models:
        models.append(FALLBACK_MODEL)

    last_error = None
    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(f"  리포트 서술 생성 시도 {attempt + 1}/{MAX_RETRIES} (모델: {model})")
                # 리포트는 출력이 길어 스트리밍으로 받는다 (HTTP 타임아웃 회피)
                with client.messages.stream(
                    model=model,
                    max_tokens=32000,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    output_config={
                        "effort": "medium",
                        "format": {"type": "json_schema", "schema": NARRATIVE_SCHEMA},
                    },
                ) as stream:
                    message = stream.get_final_message()

                if message.stop_reason == "refusal":
                    raise ValueError(
                        f"모델이 응답을 거부 "
                        f"(category={getattr(message.stop_details, 'category', None)})"
                    )
                if message.stop_reason == "max_tokens":
                    raise ValueError("max_tokens 도달로 응답이 잘렸습니다")

                text = next((b.text for b in message.content if b.type == "text"), None)
                if not text:
                    raise ValueError("응답에 텍스트 블록이 없습니다")

                narrative = json.loads(text)
                logger.info(f"  리포트 서술 생성 완료 ({model})")
                return {"narrative": narrative, "error": None, "model": model}

            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                last_error = e
                status = getattr(e, "status_code", 0)
                if status in (429, 500, 502, 503, 529):
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.warning(f"  리포트 LLM 과부하({status}) — {delay}초 후 재시도")
                    time.sleep(delay)
                    continue
                logger.warning(f"  리포트 LLM API 에러({model}, {status}): {e}")
                break
            except anthropic.APIConnectionError as e:
                last_error = e
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(f"  리포트 LLM 네트워크 오류 — {delay}초 후 재시도: {e}")
                time.sleep(delay)
                continue
            except Exception as e:
                last_error = e
                logger.warning(f"  리포트 서술 생성 실패({model}): {e}")
                break

    logger.error(f"  리포트 서술 생성 전체 실패: {last_error}")
    return {"narrative": None, "error": str(last_error), "model": None}


# ══════════════════════════════════════════════════════════════════
# 오케스트레이션
# ══════════════════════════════════════════════════════════════════

def _mode_tag() -> str:
    if settings.KR_DRY_RUN:
        return "국내·드라이런"
    return "국내·모의" if settings.KIS_USE_MOCK else "국내·실전"


def _report_dir() -> str:
    path = settings.KR_REPORT_DIR
    if not os.path.isabs(path):
        # app/services/kr/ → 프로젝트 루트
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        path = os.path.join(root, path)
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_old_reports(directory: str):
    """오래된 리포트 PDF 정리 (디스크 무한 증가 방지)."""
    keep_days = settings.KR_REPORT_KEEP_DAYS
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    for path in glob.glob(os.path.join(directory, "kr_report_*.pdf")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError as e:
            logger.debug(f"  낡은 리포트 삭제 실패({path}): {e}")


def _slack_comment(ctx: dict) -> str:
    quote = ctx.get("quote") or {}
    narrative = ctx.get("narrative") or {}
    approved = ctx.get("approved") or []
    sell = [d for d in (ctx.get("sell_decisions") or []) if d.get("decision") != "HOLD"]

    lines = [f"*[{ctx.get('mode')}] {ctx.get('date')} 국내주식 분석 리포트*"]
    if narrative.get("headline"):
        lines.append(f"_{narrative['headline']}_")
    if approved:
        picks = ", ".join(
            f"{r['stock_name']}({r['quantity']}주)" if r.get("quantity") is not None
            else r["stock_name"]
            for r in quote.get("rows", [])
        )
        lines.append(
            f"• 매수 예약 {len(approved)}종목 — {picks}\n"
            f"• 예상 투입 {quote.get('total_amount', 0):,.0f}원 "
            f"(총자산 대비 {quote.get('total_ratio_pct', 0):.1f}%) "
            f"→ 다음 영업일 {settings.KR_EXECUTION_TIME} 집행"
        )
    else:
        lines.append("• 매수 예약 없음")
    if sell:
        lines.append(f"• 매도 판정 {len(sell)}건 — " + ", ".join(
            f"{d.get('stock_name')}({d.get('decision')})" for d in sell
        ))
    if ctx.get("narrative_error"):
        lines.append("⚠️ LLM 서술 생성 실패 — 기계적 데이터만 수록")
    return "\n".join(lines)


def _pending_buy_queue_as_candidates() -> List[dict]:
    """
    kr_buy_queue 의 pending 행을 build_buy_quote() 가 기대하는 후보 형태로 변환.

    build_and_send_report() 가 원자료(artifacts) 없이 단독 호출됐을 때(메뉴 '분석
    리포트 전송')를 위한 보정이다. 원래 이 함수는 스케줄러가 방금 만든 결과만 그리도록
    설계돼 있어서, 메뉴에서 단독으로 부르면 실제로는 대기열에 종목이 있어도 리포트엔
    0종목으로 나온다 — kr_screening_service(신규 종목 추천, 메뉴 4~7)로 승인한 매수는
    이 프로세스의 스케줄러 인스턴스(kr_scheduler._artifacts)를 전혀 거치지 않고 바로
    kr_buy_queue 에 적재되기 때문이다. DB 의 pending 상태가 곧 "지금 큐에 있는 것"의
    유일한 진실이므로, 여기서 직접 읽어 채운다.
    """
    from app.db.supabase import supabase

    try:
        resp = (
            supabase.table("kr_buy_queue")
            .select("*")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        logger.warning(f"  대기 중인 매수 큐 조회 실패(빈 목록으로 진행): {e}")
        return []

    rows = resp.data or []
    return [
        {
            "code": r.get("code"),
            "stock_name": r.get("stock_name") or universe.CODE_TO_NAME.get(r.get("code"), r.get("code")),
            "sector": universe.CODE_TO_SECTOR.get(r.get("code"), ""),
            "composite_score": r.get("composite_score"),
            "rise_probability": r.get("rise_probability"),
            "llm_reason": r.get("llm_reason"),
        }
        for r in rows
    ]


def build_and_send_report(artifacts: Optional[dict] = None) -> dict:
    """
    분석 파이프라인 결과 → 리포트 PDF → Slack 업로드.

    Args:
        artifacts: 파이프라인이 수집한 원자료.
                   {market, all_candidates, approved, held, llm_reasoning,
                    sell_decisions, sell_market_analysis, steps}
                   None 이면(= 메뉴에서 단독 호출) 원자료 대신 kr_buy_queue 의 pending
                   상태를 읽어 approved 를 채운다 — all_candidates/llm_reasoning/
                   sell_decisions 처럼 LLM 판정 사유 자체는 DB 로 복원할 수 없어 비운다.
    """
    if not settings.KR_REPORT_ENABLED:
        logger.info("KR_REPORT_ENABLED=false — 분석 리포트 생성 스킵")
        return {"success": True, "skipped": "disabled", "pdf_path": None, "uploaded": False}

    standalone = artifacts is None
    artifacts = artifacts or {}
    if standalone:
        try:
            artifacts["approved"] = _pending_buy_queue_as_candidates()
        except Exception as e:
            logger.warning(f"  단독 실행 — 매수 큐 보정 실패(0종목으로 진행): {e}")
        artifacts.setdefault(
            "llm_reasoning",
            "이 리포트는 파이프라인을 새로 돌린 게 아니라 현재 kr_buy_queue 의 "
            "대기(pending) 항목을 그대로 보여줍니다 — 판정 사유는 최초 분석 시점의 "
            "것으로 복원되지 않습니다.",
        )
    now = datetime.now(KST)

    # 매수 스위치 OFF 등으로 후보 산출 자체를 건너뛴 회차는 시장환경이 비어 있다.
    # 리포트의 시장 진단은 매수 여부와 무관하게 있어야 하므로 여기서 한 번 더 조회한다.
    if not artifacts.get("market"):
        try:
            from app.services.kr import kr_market_data_service

            artifacts["market"] = kr_market_data_service.get_market_context()
        except Exception as e:
            logger.warning(f"리포트용 시장환경 조회 실패: {e}")

    ctx = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now,
        "mode": _mode_tag(),
        "execution_time": settings.KR_EXECUTION_TIME,
        "market": artifacts.get("market") or {},
        "all_candidates": artifacts.get("all_candidates") or [],
        "approved": artifacts.get("approved") or [],
        "held": artifacts.get("held") or [],
        "llm_reasoning": artifacts.get("llm_reasoning"),
        "sell_decisions": artifacts.get("sell_decisions") or [],
        "sell_market_analysis": artifacts.get("sell_market_analysis"),
        "steps": artifacts.get("steps") or {},
    }

    try:
        ctx["quote"] = build_buy_quote(ctx["approved"])
    except Exception as e:
        logger.error(f"매수 견적서 산출 실패: {e}", exc_info=True)
        ctx["quote"] = {"rows": [], "note": f"견적 산출 실패: {e}"}

    result = generate_narrative(ctx)
    ctx["narrative"] = result.get("narrative")
    ctx["narrative_error"] = result.get("error")

    directory = _report_dir()
    filename = f"kr_report_{now:%Y%m%d_%H%M}.pdf"
    pdf_path = os.path.join(directory, filename)

    try:
        kr_pdf_service.build_report_pdf(ctx, pdf_path)
    except Exception as e:
        logger.error(f"리포트 PDF 생성 실패: {e}", exc_info=True)
        try:
            _send(
                title=f"⚠️ [{ctx['mode']}] 분석 리포트 PDF 생성 실패",
                message=f"파이프라인 결과에는 영향이 없습니다.\n```{str(e)[:500]}```",
                color="#f59e0b",
            )
        except Exception:
            pass
        return {"success": False, "pdf_path": None, "uploaded": False, "error": str(e)}

    _cleanup_old_reports(directory)

    comment = _slack_comment(ctx)
    uploaded = slack_file_service.upload_file(
        pdf_path,
        title=f"[{ctx['mode']}] {ctx['date']} 국내주식 분석 리포트",
        initial_comment=comment,
    )
    if not uploaded:
        # Bot Token 미설정/업로드 실패 — 최소한 Webhook 으로 요약과 경로는 알린다
        try:
            _send(
                title=f"📄 [{ctx['mode']}] {ctx['date']} 국내주식 분석 리포트",
                message=(
                    f"{comment}\n\n"
                    f"_PDF 업로드가 되지 않아 서버에만 저장했습니다: `{pdf_path}`_\n"
                    f"_(Slack 첨부를 원하면 SLACK_BOT_TOKEN / SLACK_REPORT_CHANNEL 설정)_"
                ),
                color="#2563eb",
            )
        except Exception as e:
            logger.warning(f"리포트 Webhook 알림 실패: {e}")

    return {
        "success": True,
        "pdf_path": pdf_path,
        "uploaded": uploaded,
        "narrative_model": result.get("model"),
        "error": ctx.get("narrative_error"),
    }
