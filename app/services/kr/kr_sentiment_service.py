"""
국내주식 뉴스 감성 분석.

네이버 검색 API 는 감성 점수를 주지 않고 기사 제목/요약 텍스트만 준다.
그래서 점수화를 여기서 직접 한다.

흐름:
  1. 종목별 최근 N일 뉴스 수집 (naver_service)
  2. 여러 종목을 한 프롬프트에 묶어 Claude 로 배치 스코어링 (-1 ~ +1)
  3. kr_ticker_sentiment_analysis 저장 (기존 데이터 전체 교체)
  4. 근거 추적용으로 기사 원문 헤드라인은 kr_news_articles 에 함께 저장

설계 원칙:
  - 감성 분석 실패가 파이프라인을 죽이면 안 된다. 실패 종목은 score=None(중립)으로
    남기고 진행한다. 매수 판단에서 None 은 z-score 0(평균)으로 처리된다.
  - LLM 최종 검토(kr_llm_review_service)와는 완전히 별개 모듈이다.
    여기는 '기사 → 숫자', 저기는 '후보 종목 → BUY/HOLD'.
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
from app.db.supabase import supabase
from app.services.kr import naver_service, universe

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

# 한 번의 Claude 호출에 묶을 종목 수 / 종목당 기사 수
TICKERS_PER_CALL = 6
ARTICLES_PER_TICKER = 10
DESCRIPTION_MAX_CHARS = 160

# 폴백 체인 (앞이 실패하면 다음 모델)
_FALLBACK_MODEL = "claude-sonnet-5"

# sampling 파라미터(temperature 등)를 받지 않는 모델
MODELS_WITHOUT_TEMPERATURE = {
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-fable-5",
}

SYSTEM_PROMPT = """당신은 한국 주식시장 뉴스를 읽고 종목별 투자 심리를 계량화하는 애널리스트입니다.

## 점수 기준 (-1.0 ~ +1.0)
| 점수대 | 의미 |
|--------|------|
| +0.6 ~ +1.0 | 강한 호재: 어닝 서프라이즈, 대형 수주, 신규 대형 고객, 정책 수혜 확정 |
| +0.2 ~ +0.6 | 완만한 호재: 목표주가 상향, 업황 개선 전망, 자사주 매입 |
| -0.2 ~ +0.2 | 중립: 단순 시황/수급 기사, 사실 보도, 방향성 없는 전망 |
| -0.6 ~ -0.2 | 완만한 악재: 목표주가 하향, 실적 컨센서스 하회, 경쟁 심화 |
| -1.0 ~ -0.6 | 강한 악재: 어닝 쇼크, 대규모 리콜/소송/제재, 유상증자, 오너 리스크 |

## 한국 시장 특유의 판단 지침
- "주가 급등/급락" 같은 결과 보도는 그 자체로 호재/악재가 아니다. 원인이 기사에 있으면 그 원인으로 판단하고, 없으면 중립에 가깝게 둔다.
- 증권사 리포트 인용 기사는 목표주가 방향과 투자의견 변경 여부로 판단한다.
- 유상증자·전환사채 발행은 한국 시장에서 통상 강한 악재로 해석된다.
- 자사주 매입·소각, 배당 확대는 주주환원 기대로 뚜렷한 호재다.
- 지주회사(SK, 삼성물산 등)는 자회사 뉴스가 섞여 들어온다. 해당 종목 자체의 가치에 영향이 큰 경우만 반영한다.
- 광고성 기사, 단순 인사/사회공헌 기사는 중립(0.0) 처리한다.
- 제목이 해당 종목과 무관하면 그 기사는 무시하고, 남은 기사로만 판단한다.

## 응답 형식
반드시 아래 JSON 만 출력하세요. 설명 문장이나 코드펜스를 덧붙이지 마세요.
{
  "results": [
    {
      "code": "종목코드 6자리",
      "score": -1.0 ~ 1.0 사이 실수 (소수 둘째 자리),
      "relevant_count": 해당 종목과 실제로 관련 있다고 판단한 기사 수 (정수),
      "summary": "판단 근거 한 문장 (한국어, 60자 이내)"
    }
  ]
}
관련 기사가 하나도 없으면 score 0.0, relevant_count 0 으로 반환하세요."""


def _build_user_prompt(batch: List[dict]) -> str:
    """batch: [{"code", "name", "articles": [...]}] → 프롬프트 텍스트."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    parts = [f"오늘 날짜: {today}\n"]

    for item in batch:
        parts.append(f"\n### {item['name']} ({item['code']})")
        if not item["articles"]:
            parts.append("(수집된 기사 없음)")
            continue
        for i, a in enumerate(item["articles"][:ARTICLES_PER_TICKER], 1):
            pub = a.get("pub_date")
            when = pub.strftime("%m/%d") if pub else "?"
            desc = (a.get("description") or "")[:DESCRIPTION_MAX_CHARS]
            parts.append(f"{i}. [{when}] {a.get('title', '')}")
            if desc:
                parts.append(f"   {desc}")

    parts.append(
        f"\n\n위 {len(batch)}개 종목 각각에 대해 감성 점수를 산출하세요. "
        "results 배열에 정확히 이 종목들만, 종목코드와 함께 반환하세요."
    )
    return "\n".join(parts)


def _extract_json(text: str) -> Optional[dict]:
    """LLM 응답에서 JSON 오브젝트를 추출. 코드펜스/앞뒤 설명이 섞여도 견디게."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 가장 바깥 중괄호 블록만 잘라 재시도
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _score_batch(client: anthropic.Anthropic, batch: List[dict]) -> Dict[str, dict]:
    """
    종목 묶음 하나를 Claude 로 채점. {code: {"score", "relevant_count", "summary"}}.
    두 모델 모두 실패하면 빈 dict 를 돌려주고, 호출부는 해당 종목을 중립 처리한다.
    """
    user_prompt = _build_user_prompt(batch)
    codes_in_batch = {b["code"] for b in batch}

    for model in (settings.KR_SENTIMENT_MODEL, _FALLBACK_MODEL):
        for attempt in range(2):
            try:
                kwargs = {
                    "model": model,
                    "max_tokens": 16000,
                    # 정형화된 분류 작업이라 깊은 추론이 필요 없다.
                    # effort 를 낮춰 thinking 토큰 소비를 줄인다.
                    "output_config": {"effort": "low"},
                    "system": [
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            # 채점 루브릭은 호출마다 동일 → 프리픽스 캐시 대상
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                if model not in MODELS_WITHOUT_TEMPERATURE:
                    kwargs["temperature"] = 0

                message = client.messages.create(**kwargs)

                if message.stop_reason == "refusal":
                    raise ValueError(
                        f"모델이 응답을 거부했습니다 "
                        f"(category={getattr(message.stop_details, 'category', None)})"
                    )
                if message.stop_reason == "max_tokens":
                    raise ValueError("max_tokens 도달로 응답이 잘렸습니다")

                # Opus 5 등은 content[0] 이 ThinkingBlock 이므로 text 블록을 찾아 쓴다
                text_block = next((b for b in message.content if b.type == "text"), None)
                if text_block is None:
                    raise ValueError("응답에 텍스트 블록이 없습니다 (thinking 만 반환)")

                data = _extract_json(text_block.text)
                if not data or "results" not in data:
                    raise ValueError(f"JSON 파싱 실패: {text_block.text[:300]}")

                scored: Dict[str, dict] = {}
                for row in data.get("results", []):
                    raw_code = str(row.get("code", "")).strip()
                    # 종목코드는 앞자리 0 이 잘려 오는 경우가 있고("5930"), 섹터는
                    # "SEC:" 접두사를 모델이 종종 떼고 돌려준다("SEC:화장품" → "화장품").
                    # 둘 다 원본 매칭 실패 시 정규화해서 한 번 더 찾아본다.
                    if raw_code in codes_in_batch:
                        code = raw_code
                    elif raw_code.zfill(6) in codes_in_batch:
                        code = raw_code.zfill(6)
                    elif f"SEC:{raw_code}" in codes_in_batch:
                        code = f"SEC:{raw_code}"
                    else:
                        continue  # 환각으로 끼어든 종목은 버린다
                    try:
                        score = float(row.get("score", 0) or 0)
                    except (ValueError, TypeError):
                        score = 0.0
                    scored[code] = {
                        "score": max(-1.0, min(1.0, round(score, 2))),
                        "relevant_count": int(row.get("relevant_count", 0) or 0),
                        "summary": (row.get("summary") or "")[:200],
                        "model": model,
                    }
                return scored

            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                status = getattr(e, "status_code", 0)
                logger.warning(f"감성 스코어링 API 오류 ({model}, {status}): {e}")
                if status in (429, 500, 502, 503, 529) and attempt == 0:
                    time.sleep(8)
                    continue
                break
            except anthropic.APIConnectionError as e:
                logger.warning(f"감성 스코어링 네트워크 오류 ({model}): {e}")
                if attempt == 0:
                    time.sleep(5)
                    continue
                break
            except Exception as e:
                logger.warning(f"감성 스코어링 실패 ({model}, 시도 {attempt + 1}): {e}")
                if attempt == 0:
                    time.sleep(3)
                    continue
                break

        if model != _FALLBACK_MODEL:
            logger.info(f"{model} 실패 → 폴백 모델 {_FALLBACK_MODEL} 로 전환")

    logger.error(f"감성 스코어링 전체 실패 — 중립 처리: {sorted(codes_in_batch)}")
    return {}


def _collect_articles(codes: List[str]) -> List[dict]:
    """종목별 최근 뉴스 수집. [{"code", "name", "articles"}]"""
    collected = []
    for code in codes:
        name = universe.CODE_TO_NAME.get(code, code)
        query = universe.news_query(code)
        articles = naver_service.search_recent_news(
            query,
            days=settings.KR_SENTIMENT_LOOKBACK_DAYS,
            display=50,
        )
        logger.info(f"  {universe.display(code)} 기사 {len(articles)}건 (검색어: {query})")
        collected.append({"code": code, "name": name, "articles": articles})
        time.sleep(0.2)  # 네이버 API 예의상 간격
    return collected


def _save_articles(collected: List[dict], run_date: str):
    """수집 기사 헤드라인 저장 (감성 점수 근거 추적용). 실패해도 무시."""
    rows = []
    for item in collected:
        for a in item["articles"][:ARTICLES_PER_TICKER]:
            pub = a.get("pub_date")
            rows.append(
                {
                    "collected_date": run_date,
                    "code": item["code"],
                    "stock_name": item["name"],
                    "title": a.get("title", "")[:500],
                    "description": (a.get("description") or "")[:1000],
                    "link": (a.get("link") or "")[:500],
                    "published_at": pub.isoformat() if pub else None,
                }
            )
    if not rows:
        return
    try:
        supabase.table("kr_news_articles").delete().eq("collected_date", run_date).execute()
        # 대량 insert 는 배치로 쪼갠다
        for i in range(0, len(rows), 200):
            supabase.table("kr_news_articles").insert(rows[i : i + 200]).execute()
        logger.info(f"  뉴스 원문 {len(rows)}건 저장")
    except Exception as e:
        logger.warning(f"  뉴스 원문 저장 실패(무시): {e}")


def fetch_and_store_sentiment(extra_codes: Optional[List[str]] = None) -> dict:
    """
    KOSPI100 + (보유 종목 등 추가 종목) 의 뉴스 감성을 산출해 저장한다.

    Args:
        extra_codes: 유니버스 밖이지만 보유 중이라 감성을 봐야 하는 종목코드

    Returns:
        {"message", "results": [{code, stock_name, score, article_count, summary}, ...]}
    """
    codes = list(universe.ALL_CODES)
    for c in extra_codes or []:
        if c not in codes:
            codes.append(c)

    if not naver_service.is_configured():
        msg = "NAVER API Hub 키 미설정 — 감성 분석 스킵 (모든 종목 중립 처리)"
        logger.warning(msg)
        return {"message": msg, "results": []}

    if not settings.ANTHROPIC_API_KEY:
        msg = "ANTHROPIC_API_KEY 미설정 — 감성 스코어링 스킵"
        logger.warning(msg)
        return {"message": msg, "results": []}

    run_date = datetime.now(KST).strftime("%Y-%m-%d")
    logger.info(f"국내 뉴스 감성 분석 시작: {len(codes)}종목")

    # 1) 기사 수집
    collected = _collect_articles(codes)
    _save_articles(collected, run_date)

    # 2) 배치 스코어링
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    scores: Dict[str, dict] = {}
    for i in range(0, len(collected), TICKERS_PER_CALL):
        batch = collected[i : i + TICKERS_PER_CALL]
        logger.info(
            f"  감성 스코어링 {i // TICKERS_PER_CALL + 1}/"
            f"{-(-len(collected) // TICKERS_PER_CALL)} 배치 ({len(batch)}종목)"
        )
        scores.update(_score_batch(client, batch))

    # 3) 저장 (기존 데이터 전량 교체)
    rows = []
    for item in collected:
        code = item["code"]
        s = scores.get(code)
        if s is None:
            continue  # 스코어링 실패 종목은 저장하지 않음 → 조회 시 None(중립)
        rows.append(
            {
                "code": code,
                "stock_name": item["name"],
                "sentiment_score": s["score"],
                "article_count": s["relevant_count"],
                "collected_article_count": len(item["articles"]),
                "summary": s["summary"],
                "model": s["model"],
                "calculation_date": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    try:
        supabase.table("kr_ticker_sentiment_analysis").delete().neq("code", "").execute()
        if rows:
            supabase.table("kr_ticker_sentiment_analysis").insert(rows).execute()
    except Exception as e:
        logger.error(f"감성 분석 결과 저장 실패: {e}")
        return {"message": f"저장 실패: {e}", "results": rows}

    scored_n = len(rows)
    positive = sum(1 for r in rows if r["sentiment_score"] >= 0.2)
    negative = sum(1 for r in rows if r["sentiment_score"] <= -0.2)
    msg = (
        f"{scored_n}/{len(codes)}종목 감성 산출 완료 "
        f"(긍정 {positive} / 부정 {negative} / 중립 {scored_n - positive - negative})"
    )
    logger.info(msg)
    return {"message": msg, "results": rows}


def get_sentiment_map() -> Dict[str, dict]:
    """저장된 감성 점수를 {code: row} 로 조회. 실패 시 빈 dict."""
    try:
        resp = supabase.table("kr_ticker_sentiment_analysis").select("*").execute()
        return {r["code"]: r for r in (resp.data or [])}
    except Exception as e:
        logger.warning(f"감성 점수 조회 실패: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════
# 신규종목 추천 스크리닝용 (kr_screening_service)
#   fetch_and_store_sentiment() 는 고정 유니버스(universe.CODE_TO_NAME)를 전제로 한다.
#   스크리닝도 지금은 같은 고정 100종목을 쓰지만, 종목명을 인자로 직접 받는 게
#   호출부(kr_screening_service) 입장에서 더 단순해 별도 진입점을 유지한다.
#   결과를 DB 에 저장하지 않는 것도 의도적이다 — kr_ticker_sentiment_analysis 는
#   ML/점수 파이프라인이 읽는 테이블이라 스크리닝 결과를 섞으면 안 된다.
# ══════════════════════════════════════════════════════════════════

def score_items(
    items: List[dict], with_blog: bool = True, progress: bool = True
) -> Dict[str, dict]:
    """
    임의의 종목 목록을 감성 채점한다. DB 에 쓰지 않는다.

    items: [{"code", "name"}]
    Returns: {code: {"score", "relevant_count", "summary", "article_count", "blog_buzz"}}
    """
    if not items:
        return {}
    if not naver_service.is_configured():
        logger.warning("  NAVER API 미설정 — 감성 분석을 건너뜁니다")
        return {}
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("  ANTHROPIC_API_KEY 미설정 — 감성 분석을 건너뜁니다")
        return {}

    collected = []
    for i, it in enumerate(items, 1):
        query = universe.news_query_for(it["code"], it.get("name"))
        try:
            articles = naver_service.search_recent_news(
                query, days=settings.KR_SENTIMENT_LOOKBACK_DAYS, display=50
            )
        except Exception as e:
            logger.debug(f"  {it['code']} 뉴스 조회 실패: {e}")
            articles = []
        buzz = 0
        if with_blog:
            try:
                buzz = naver_service.search_blog_buzz(
                    query, days=settings.KR_SENTIMENT_LOOKBACK_DAYS
                )
            except Exception:
                buzz = 0
        collected.append(
            {"code": it["code"], "name": it.get("name") or it["code"],
             "articles": articles, "blog_buzz": buzz}
        )
        if progress and (i % 10 == 0 or i == len(items)):
            logger.info(f"  감성 분석 뉴스 수집 {i}/{len(items)}")
        time.sleep(0.2)

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    scored: Dict[str, dict] = {}
    batches = -(-len(collected) // TICKERS_PER_CALL)
    for i in range(0, len(collected), TICKERS_PER_CALL):
        batch = collected[i : i + TICKERS_PER_CALL]
        if progress:
            logger.info(
                f"  감성 분석 LLM {i // TICKERS_PER_CALL + 1}/{batches} 배치 ({len(batch)}종목)"
            )
        try:
            scored.update(_score_batch(client, batch))
        except Exception as e:
            logger.warning(f"  감성 배치 채점 실패(중립 처리): {e}")

    for item in collected:
        entry = scored.get(item["code"]) or {}
        entry.setdefault("score", 0.0)
        entry["article_count"] = len(item["articles"])
        entry["blog_buzz"] = item["blog_buzz"]
        scored[item["code"]] = entry
    return scored


def score_sectors(sectors: List[str]) -> Dict[str, dict]:
    """
    섹터(업종) 단위 뉴스 감성.

    네이버 금융 '산업분석'은 공식 API 가 없고 크롤링은 ToS 위반 소지가 있어 쓰지 않는다.
    대신 **섹터명을 검색어로** 뉴스를 한 번 더 긁어 업황 감성을 만든다. 개별 종목 뉴스는
    회사 이벤트에 좌우되지만 섹터 뉴스는 업황을 반영하므로, 둘을 같이 보면 '회사는
    조용한데 업황이 무너지는' 경우를 잡아낼 수 있다.

    Returns: {sector: {"score", "summary", "article_count"}}
    """
    targets = [s for s in dict.fromkeys(sectors) if s]
    if not targets:
        return {}
    items = [{"code": f"SEC:{s}", "name": f"{s} 업황"} for s in targets]
    scored = score_items(items, with_blog=False)
    return {
        s: scored.get(f"SEC:{s}", {"score": 0.0})
        for s in targets
    }
