"""
OpenDART 재무제표 → 기본적 분석 지표.

1단계 필터링의 '기본적 분석'에 쓸 수치를 만든다. KIS 의 finance/financial-ratio 를 쓰지
않는 이유가 둘이다.

  1. **모의투자에서 동작하지 않는다** (실전 계좌 전용 TR). 드라이런·모의 단계에서
     기본적 분석이 통째로 비면 파이프라인을 검증할 수 없다.
  2. ROIC 와 FCF 를 주지 않는다. 둘 다 손익계산서·재무상태표·현금흐름표를 조합해야
     나오는 값이라 원장 자체가 필요하다.

DART 는 세 재무제표를 한 번에 주므로(fnlttSinglAcntAll) 다섯 지표를 모두 직접 계산한다.

## 산출 지표

| 구분 | 지표 | 계산 |
|------|------|------|
| 수익성 | 영업이익률 | 영업이익 / 매출액 |
| 수익성 | ROE | 당기순이익 / 자본총계 |
| 수익성 | ROIC | 세후영업이익(NOPAT) / 투하자본(자본총계 + 총차입금 − 현금성자산) |
| 성장성 | PER | 시가총액 / 당기순이익 |
| 안정성 | 부채비율 / 유동비율 | 부채총계/자본총계, 유동자산/유동부채 |
| FCF | FCF·영업·투자·재무 현금흐름 | 영업활동현금흐름 − CAPEX(유형+무형자산 취득) |

## 조회 비용과 캐시

corp_code 매핑(corpCode.xml, 12만 법인)은 하루 1회면 충분하고, 종목별 재무제표는 분기
보고서가 나올 때만 바뀐다. 그래서 둘 다 파일로 캐시하고 KR_DART_CACHE_DAYS 안에는
재조회하지 않는다. OpenDART 는 일 20,000회 제한이 있어 200종목을 매번 새로 받으면
금방 소진된다.

## 사업연도 선택

분기보고서(11013/11012/11014)는 누적이 아닌 분기 단독 수치를 섞어 주는 경우가 있어
비교가 까다롭다. 그래서 **사업보고서(11011, 연간)** 를 기본으로 쓰고, 아직 안 나온
연도면 직전 연도로 자동 후퇴한다. 연간 기준이라 지표가 최대 1년 낡을 수 있는데,
1단계는 '재무 체력이 되는 회사인가'를 보는 거친 필터라 그 정도 지연은 허용된다.
"""
import io
import json
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytz
import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

_BASE = "https://opendart.fss.or.kr/api"
_REPORT_ANNUAL = "11011"  # 사업보고서

_CORP_CACHE = "dart_corp_codes.json"
_FIN_CACHE = "dart_financials.json"


def is_configured() -> bool:
    return bool(settings.DART_API_KEY)


def _cache_dir() -> Path:
    p = Path(settings.KR_UNIVERSE_CACHE_DIR)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[3] / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_json(name: str) -> Optional[dict]:
    path = _cache_dir() / name
    if not path.exists():
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"  DART 캐시 읽기 실패({name}): {e}")
        return None


def _save_json(name: str, payload: dict) -> None:
    try:
        with io.open(_cache_dir() / name, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"  DART 캐시 저장 실패({name}): {e}")


def _age_days(iso: Optional[str]) -> float:
    if not iso:
        return 1e9
    try:
        return (datetime.now(KST) - datetime.fromisoformat(iso)).total_seconds() / 86400.0
    except ValueError:
        return 1e9


# ══════════════════════════════════════════════════════════════════
# 1) corp_code 매핑
# ══════════════════════════════════════════════════════════════════

def get_corp_code_map(force_refresh: bool = False) -> Dict[str, str]:
    """종목코드(6자리) → DART corp_code(8자리)."""
    cache = _load_json(_CORP_CACHE)
    if cache and not force_refresh and _age_days(cache.get("built_at")) <= 30:
        return cache.get("map", {})

    if not is_configured():
        logger.warning("  DART_API_KEY 미설정 — corp_code 매핑을 건너뜁니다")
        return (cache or {}).get("map", {})

    try:
        resp = requests.get(
            f"{_BASE}/corpCode.xml", params={"crtfc_key": settings.DART_API_KEY}, timeout=120
        )
        resp.raise_for_status()
        if resp.content[:2] != b"PK":
            # 에러는 JSON 으로 온다
            raise RuntimeError(resp.text[:200])
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        root = ET.fromstring(z.read(z.namelist()[0]).decode("utf-8"))
        mapping = {}
        for e in root.findall("list"):
            stock = (e.findtext("stock_code") or "").strip()
            corp = (e.findtext("corp_code") or "").strip()
            if stock and corp:
                mapping[stock] = corp
        _save_json(_CORP_CACHE, {"built_at": datetime.now(KST).isoformat(), "map": mapping})
        logger.info(f"  DART corp_code 매핑 갱신: 상장 {len(mapping)}종목")
        return mapping
    except Exception as e:
        logger.error(f"  DART corp_code 조회 실패: {e}")
        return (cache or {}).get("map", {})


# ══════════════════════════════════════════════════════════════════
# 2) 재무제표 원장
# ══════════════════════════════════════════════════════════════════

def _num(raw) -> Optional[float]:
    if raw in (None, "", "-"):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _norm(text: Optional[str]) -> str:
    """계정명 비교용 정규화 — 공백 제거."""
    return re.sub(r"\s+", "", text or "")


def _pick(
    rows: List[dict],
    sj: str,
    ids: Optional[List[str]] = None,
    names: Optional[List[str]] = None,
) -> Optional[float]:
    """
    재무제표(sj_div)에서 계정 하나의 당기 금액을 뽑는다.

    **IFRS 표준계정코드(account_id)를 먼저 본다.** 계정 '이름'은 회사마다 표기가 갈린다 —
    삼성전자는 "영업활동현금흐름", SK하이닉스는 "영업활동 현금흐름"(공백)으로 쓴다.
    이름만 보고 부분일치를 하면 이런 종목에서 조용히 결측이 나므로, 표준코드가 있으면
    그쪽을 신뢰하고 이름 매칭은 표준코드 미사용 항목을 위한 폴백으로만 쓴다.

    이름 폴백도 공백을 지운 뒤 **완전일치 → 부분일치** 순으로 본다. '자본총계'를 찾을 때
    '부채와자본총계'가 먼저 잡히는 사고를 막기 위해서다.
    """
    pool = [r for r in rows if r.get("sj_div") == sj]

    for account_id in ids or []:
        for r in pool:
            if (r.get("account_id") or "").strip() == account_id:
                v = _num(r.get("thstrm_amount"))
                if v is not None:
                    return v

    wanted = [_norm(n) for n in (names or [])]
    for w in wanted:
        for r in pool:
            if _norm(r.get("account_nm")) == w:
                v = _num(r.get("thstrm_amount"))
                if v is not None:
                    return v
    for w in wanted:
        for r in pool:
            if w in _norm(r.get("account_nm")):
                v = _num(r.get("thstrm_amount"))
                if v is not None:
                    return v
    return None


def _pick_any(rows: List[dict], sjs: List[str], ids=None, names=None) -> Optional[float]:
    """여러 재무제표를 순서대로 뒤진다 (손익계산서는 IS/CIS 로 갈린다)."""
    for sj in sjs:
        v = _pick(rows, sj, ids, names)
        if v is not None:
            return v
    return None


def _fetch_statements(corp_code: str, year: int) -> List[dict]:
    """연결(CFS) 우선, 없으면 개별(OFS)."""
    for fs_div in ("CFS", "OFS"):
        try:
            resp = requests.get(
                f"{_BASE}/fnlttSinglAcntAll.json",
                params={
                    "crtfc_key": settings.DART_API_KEY,
                    "corp_code": corp_code,
                    "bsns_year": str(year),
                    "reprt_code": _REPORT_ANNUAL,
                    "fs_div": fs_div,
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("status") == "000" and data.get("list"):
                return data["list"]
        except Exception as e:
            logger.debug(f"  DART {corp_code} {year} {fs_div} 조회 실패: {e}")
    return []


# ══════════════════════════════════════════════════════════════════
# 3) 지표 산출
# ══════════════════════════════════════════════════════════════════

def _compute(rows: List[dict], market_cap_won: Optional[float]) -> dict:
    """재무제표 원장 → 기본적 분석 지표 dict."""
    IS_ANY = ["IS", "CIS"]

    revenue = _pick_any(rows, IS_ANY, ["ifrs-full_Revenue"],
                        ["매출액", "수익(매출액)", "영업수익"])
    op_income = _pick_any(rows, IS_ANY, ["dart_OperatingIncomeLoss"],
                          ["영업이익", "영업이익(손실)"])
    net_income = _pick_any(rows, IS_ANY, ["ifrs-full_ProfitLoss"],
                           ["당기순이익", "당기순이익(손실)", "연결당기순이익"])

    equity = _pick(rows, "BS", ["ifrs-full_Equity"], ["자본총계"])
    assets = _pick(rows, "BS", ["ifrs-full_Assets"], ["자산총계"])
    liabilities = _pick(rows, "BS", ["ifrs-full_Liabilities"], ["부채총계"])
    cur_assets = _pick(rows, "BS", ["ifrs-full_CurrentAssets"], ["유동자산"])
    cur_liabilities = _pick(rows, "BS", ["ifrs-full_CurrentLiabilities"], ["유동부채"])
    cash = _pick(rows, "BS", ["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"])
    st_debt = _pick(rows, "BS", ["ifrs-full_ShorttermBorrowings"], ["단기차입금"]) or 0.0
    lt_debt = _pick(rows, "BS", ["ifrs-full_LongtermBorrowings"], ["장기차입금"]) or 0.0
    bonds = _pick(rows, "BS", None, ["사채"]) or 0.0

    cf_op = _pick(rows, "CF", ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
                  ["영업활동현금흐름", "영업활동으로인한현금흐름"])
    cf_inv = _pick(rows, "CF", ["ifrs-full_CashFlowsFromUsedInInvestingActivities"],
                   ["투자활동현금흐름", "투자활동으로인한현금흐름"])
    cf_fin = _pick(rows, "CF", ["ifrs-full_CashFlowsFromUsedInFinancingActivities"],
                   ["재무활동현금흐름", "재무활동으로인한현금흐름"])
    capex_tangible = _pick(rows, "CF", ["dart_PurchaseOfPropertyPlantAndEquipment"],
                           ["유형자산의취득"]) or 0.0
    capex_intangible = _pick(rows, "CF", ["dart_PurchaseOfIntangibleAssets"],
                             ["무형자산의취득"]) or 0.0

    def pct(num, den):
        if num is None or not den:
            return None
        return round(num / den * 100, 2)

    # ROIC = NOPAT / 투하자본. 법인세율은 한국 실효세율 근사치 22% 를 쓴다
    # (종목별 실효세율을 정확히 뽑으려면 법인세비용/세전이익이 필요한데 결측이 잦다).
    nopat = op_income * (1 - 0.22) if op_income is not None else None
    invested = None
    if equity is not None:
        invested = equity + st_debt + lt_debt + bonds - (cash or 0.0)
        if invested <= 0:
            invested = None

    capex = capex_tangible + capex_intangible
    fcf = (cf_op - capex) if cf_op is not None else None

    return {
        # 수익성
        "operating_margin": pct(op_income, revenue),
        "roe": pct(net_income, equity),
        "roic": pct(nopat, invested),
        # 성장성
        "per": (
            round(market_cap_won / net_income, 2)
            if market_cap_won and net_income and net_income > 0
            else None
        ),
        # 안정성
        "debt_ratio": pct(liabilities, equity),
        "current_ratio": pct(cur_assets, cur_liabilities),
        # 현금흐름
        "cf_operating": cf_op,
        "cf_investing": cf_inv,
        "cf_financing": cf_fin,
        "capex": capex or None,
        "fcf": fcf,
        # 원자료 (LLM 프롬프트 참고용)
        "revenue": revenue,
        "operating_income": op_income,
        "net_income": net_income,
        "equity": equity,
        "assets": assets,
    }


def get_fundamentals(
    code: str,
    market_cap_won: Optional[float] = None,
    corp_map: Optional[Dict[str, str]] = None,
) -> Optional[dict]:
    """
    종목 하나의 기본적 분석 지표. 조회 실패 시 None.

    market_cap_won 을 주면 PER 을 함께 계산한다(시가총액 ÷ 당기순이익).
    """
    if not is_configured():
        return None

    corp_map = corp_map if corp_map is not None else get_corp_code_map()
    corp_code = corp_map.get(code)
    if not corp_code:
        return None

    year = datetime.now(KST).year
    # 올해 사업보고서는 보통 3월 이후에 나온다. 없으면 한 해씩 뒤로 물러난다.
    for candidate in (year - 1, year - 2, year):
        rows = _fetch_statements(corp_code, candidate)
        if rows:
            result = _compute(rows, market_cap_won)
            result["fiscal_year"] = candidate
            result["corp_code"] = corp_code
            return result
    return None


def get_fundamentals_bulk(
    universe: List[dict], force_refresh: bool = False, progress: bool = False
) -> Dict[str, dict]:
    """
    후보군 전체의 기본적 분석 지표. 종목당 1~2회 호출이라 캐시가 사실상 필수다.

    universe: [{"code","name","market_cap"(억원, 선택)}]
    Returns: {code: 지표 dict}
    """
    if not is_configured():
        logger.warning("  DART_API_KEY 미설정 — 기본적 분석을 건너뜁니다")
        return {}

    cache = _load_json(_FIN_CACHE) or {}
    fresh = _age_days(cache.get("built_at")) <= settings.KR_DART_CACHE_DAYS
    stored: Dict[str, dict] = cache.get("data", {}) if fresh else {}

    corp_map = get_corp_code_map()
    result: Dict[str, dict] = {}
    todo = []
    for u in universe:
        if not force_refresh and u["code"] in stored:
            result[u["code"]] = stored[u["code"]]
        else:
            todo.append(u)

    if todo:
        logger.info(f"  DART 재무제표 조회 {len(todo)}종목 (캐시 적중 {len(result)}종목)")
    for i, u in enumerate(todo, 1):
        cap_won = (u.get("market_cap") or 0) * 100_000_000 or None  # 억원 → 원
        try:
            fund = get_fundamentals(u["code"], cap_won, corp_map)
        except Exception as e:
            logger.debug(f"  {u['code']} DART 조회 예외: {e}")
            fund = None
        if fund:
            result[u["code"]] = fund
        if progress and (i % 25 == 0 or i == len(todo)):
            logger.info(f"  DART 진행 {i}/{len(todo)} (성공 {len(result)}종목)")

    _save_json(
        _FIN_CACHE,
        {"built_at": datetime.now(KST).isoformat(), "data": {**stored, **result}},
    )
    return result
