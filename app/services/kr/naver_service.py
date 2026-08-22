"""
NAVER API Hub 클라이언트 (뉴스 검색 + 데이터랩 검색어 트렌드).

⚠️ 기존 `openapi.naver.com` (네이버 개발자센터) 검색 API 는 NAVER API HUB 로 이관됐다.
   호스트와 인증 헤더가 모두 바뀌었으므로 도메인만 갈아끼우면 동작하지 않는다.

   호스트 : https://naverapihub.apigw.ntruss.com
   인증   : X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
   뉴스   : GET /search/v1/news   (일 25,000회)

   ⚠️ API 는 Application 별로 하나씩 활성화해야 한다. 뉴스만 켜져 있고 블로그를 켜지
      않았다면 블로그 호출은 401 이 난다. 데이터랩(검색어 트렌드)은 아예 다른 상품이라
      호스트도 다르고(NAVER_DATALAB_BASE) 별도 신청이 필요하다.
      셋 다 없어도 파이프라인은 정상 동작한다 (뉴스 감성만 있으면 충분).

AlphaVantage NEWS_SENTIMENT 와의 결정적 차이:
   AlphaVantage 는 ticker_sentiment_score(-1~+1) 를 완제품으로 줬지만
   네이버는 기사 제목/요약 텍스트만 준다. 감성 점수 산출은
   app/services/kr/kr_sentiment_service.py 가 Claude 로 별도 수행한다.
"""
import html
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz
import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

# API Hub 경로. 검색 계열은 /search/v1/{서비스}, 데이터랩은 /datalab/v1/search.
PATH_NEWS = "/search/v1/news"
PATH_BLOG = "/search/v1/blog"
PATH_CAFE = "/search/v1/cafearticle"
PATH_DATALAB_SEARCH = "/datalab/v1/search"

_TAG_RE = re.compile(r"<[^>]+>")


def is_configured() -> bool:
    return bool(settings.NAVER_API_KEY_ID and settings.NAVER_API_KEY)


def _headers() -> dict:
    return {
        "X-NCP-APIGW-API-KEY-ID": settings.NAVER_API_KEY_ID,
        "X-NCP-APIGW-API-KEY": settings.NAVER_API_KEY,
        "Content-Type": "application/json",
    }


def _clean(text: str) -> str:
    """네이버 응답의 <b> 하이라이트 태그와 HTML 엔티티(&quot; 등)를 제거."""
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _parse_pub_date(raw: str) -> Optional[datetime]:
    """pubDate('Mon, 18 Aug 2026 09:12:00 +0900') → tz-aware datetime."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════
# 뉴스 검색
# ══════════════════════════════════════════════════════════════════

def search_news(
    query: str,
    display: int = 50,
    sort: str = "date",
    start: int = 1,
) -> List[dict]:
    """
    뉴스 검색 (GET /search/v1/news).

    Args:
        query:   검색어
        display: 1~100 (한 번에 받을 기사 수)
        sort:    date(최신순) / sim(정확도순)
        start:   1~1000 (페이징 시작 위치)

    Returns:
        [{"title", "description", "link", "original_link", "pub_date"(datetime|None)}, ...]
        호출 실패 시 빈 리스트 (파이프라인을 막지 않는다).
    """
    if not is_configured():
        logger.warning("NAVER_API_KEY_ID / NAVER_API_KEY 미설정 — 뉴스 검색 스킵")
        return []

    url = f"{settings.NAVER_API_HUB_BASE}{PATH_NEWS}"
    params = {
        "query": query,
        "display": max(1, min(display, 100)),
        "start": max(1, min(start, 1000)),
        "sort": sort,
    }

    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=10)
    except Exception as e:
        logger.warning(f"네이버 뉴스 검색 예외 ({query}): {e}")
        return []

    if resp.status_code != 200:
        logger.warning(
            f"네이버 뉴스 검색 실패 ({query}): HTTP {resp.status_code} {resp.text[:200]}"
        )
        return []

    try:
        items = resp.json().get("items", [])
    except ValueError:
        logger.warning(f"네이버 뉴스 응답 파싱 실패 ({query}): {resp.text[:200]}")
        return []

    articles = []
    for it in items:
        articles.append(
            {
                "title": _clean(it.get("title", "")),
                "description": _clean(it.get("description", "")),
                "link": it.get("link", ""),
                "original_link": it.get("originallink", ""),
                "pub_date": _parse_pub_date(it.get("pubDate", "")),
            }
        )
    return articles


def search_recent_news(query: str, days: int = 3, display: int = 50) -> List[dict]:
    """
    최근 N일 기사만 필터링해서 반환.

    네이버 검색 API 에는 기간 파라미터가 없으므로 sort=date 로 최신순을 받아
    pubDate 로 잘라낸다. 같은 기사가 여러 매체에 전재되는 경우가 많아 제목 기준 중복도 제거한다.
    """
    articles = search_news(query, display=display, sort="date")
    if not articles:
        return []

    cutoff = datetime.now(KST) - timedelta(days=days)
    seen_titles = set()
    recent = []

    for a in articles:
        pub = a.get("pub_date")
        if pub is not None and pub < cutoff:
            continue  # sort=date 라 이후는 더 오래된 기사지만, 안전하게 계속 순회

        # 제목 정규화 후 중복 제거 (전재 기사 제거)
        key = re.sub(r"[^\w가-힣]", "", a["title"])[:40]
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        recent.append(a)

    return recent


def search_blog_buzz(query: str, days: int = 3, display: int = 30) -> int:
    """
    블로그 언급량 (개인투자자 관심도 보조 신호).
    실패하거나 미설정이면 0 을 반환한다 — 이 신호는 없어도 파이프라인이 동작해야 한다.
    """
    if not is_configured():
        return 0
    url = f"{settings.NAVER_API_HUB_BASE}{PATH_BLOG}"
    try:
        resp = requests.get(
            url,
            headers=_headers(),
            params={"query": query, "display": min(display, 100), "sort": "date"},
            timeout=10,
        )
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                logger.info(
                    "블로그 검색 미활성 — NAVER API HUB 콘솔에서 blog 검색 API 를 "
                    "Application 에 추가해야 합니다 (뉴스와 별도 활성화)."
                )
            return 0
        items = resp.json().get("items", [])
    except Exception:
        return 0

    cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y%m%d")
    return sum(1 for it in items if (it.get("postdate") or "99999999") >= cutoff)


# ══════════════════════════════════════════════════════════════════
# 데이터랩 통합검색어 트렌드
#   미국판에 없던 신규 팩터: 종목명 검색량 급증 = 개인 관심 유입.
#   API Hub 경로가 변동될 수 있어 실패해도 None 만 반환하고 파이프라인은 계속 간다.
# ══════════════════════════════════════════════════════════════════

def search_trend(
    keyword_groups: List[Dict[str, List[str]]],
    start_date: str,
    end_date: str,
    time_unit: str = "date",
) -> Optional[List[dict]]:
    """
    통합검색어 트렌드 (POST /datalab/v1/search). 한 번에 최대 5개 키워드 그룹.

    Args:
        keyword_groups: [{"groupName": "삼성전자", "keywords": ["삼성전자", "삼성전자 주가"]}, ...]
        start_date/end_date: "YYYY-MM-DD"
        time_unit: date / week / month

    Returns:
        [{"title": 그룹명, "data": [{"period", "ratio"}, ...]}, ...] 또는 None(실패).
    """
    if not is_configured():
        return None

    url = f"{settings.NAVER_DATALAB_BASE}{PATH_DATALAB_SEARCH}"
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": time_unit,
        "keywordGroups": keyword_groups[:5],
    }

    try:
        resp = requests.post(url, headers=_headers(), json=body, timeout=10)
    except Exception as e:
        logger.info(f"데이터랩 트렌드 조회 예외(무시): {e}")
        return None

    if resp.status_code != 200:
        # 원인을 구분해서 알려준다 — "무시"만 찍히면 왜 안 되는지 알 수 없다.
        if resp.status_code in (401, 403):
            hint = (
                "NCP 콘솔에서 'Search Trend'(데이터랩) 상품을 신청/활성화해야 합니다. "
                "검색 API 와 별개 상품이라 키가 같아도 권한이 따로 필요합니다."
            )
        elif resp.status_code == 404:
            hint = f"경로 확인 필요 ({settings.NAVER_DATALAB_BASE}{PATH_DATALAB_SEARCH})"
        else:
            hint = resp.text[:120]
        logger.info(f"데이터랩 트렌드 미사용 (HTTP {resp.status_code}): {hint}")
        return None

    try:
        return resp.json().get("results", [])
    except ValueError:
        return None


def get_search_interest_ratios(
    names: List[str], lookback_days: int = 30
) -> Dict[str, float]:
    """
    종목별 '검색 관심도 배율' = 최근 3일 평균 검색량 / 직전 27일 평균.

    1.0 이면 평소 수준, 2.0 이면 관심도 2배. 거래량비율(volume_ratio)의 검색 버전이다.
    데이터랩이 5개 그룹씩만 받으므로 6회에 나눠 호출한다 (30종목 기준).

    실패한 종목은 결과 dict 에서 빠지며, 호출부는 None 처리(중립)해야 한다.
    """
    if not is_configured():
        return {}

    end = datetime.now(KST).date()
    start = end - timedelta(days=lookback_days)
    ratios: Dict[str, float] = {}

    for i in range(0, len(names), 5):
        chunk = names[i : i + 5]
        groups = [{"groupName": n, "keywords": [n]} for n in chunk]
        results = search_trend(
            groups, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        )
        if not results:
            continue

        for r in results:
            series = [float(d.get("ratio", 0) or 0) for d in r.get("data", [])]
            if len(series) < 7:
                continue
            recent = series[-3:]
            base = series[:-3]
            base_avg = sum(base) / len(base) if base else 0
            if base_avg <= 0:
                continue
            ratios[r.get("title", "")] = round((sum(recent) / len(recent)) / base_avg, 2)

        time.sleep(0.3)  # 데이터랩 호출 간 예의상 간격

    if ratios:
        logger.info(f"데이터랩 검색 관심도 수집: {len(ratios)}/{len(names)}종목")
    return ratios
