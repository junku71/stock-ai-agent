"""
1단계 퀀트 스크리닝 데이터 — ai-stock.co.kr 의 KRX 전종목 퀀트 엑셀.

종목별로 DART API 를 호출하던 기존 방식과 달리, 매주 갱신되는 엑셀 파일 하나를 통째로
받아 재무·시가총액·업종 데이터를 한 번에 확보한다. 100종목(고정 유니버스, universe.py)
전부의 데이터가 API 호출 없이 로컬 파일 룩업 하나로 끝난다.

## 왜 새 데이터 소스가 필요한가

DART 는 ROIC·FCF 계산에 필요한 재무제표 원장을 종목당 개별 호출로만 준다. 이 파일은
같은 성격의 지표(영업이익률/ROE/PER/부채비율/영업현금흐름/CAPEX/FCF)를 전종목 한 시트에
미리 계산해 두므로, 200종목 동적 유니버스를 만드느라 KIS 를 8~13분씩 두드리던
kr_universe_service 경로 없이도 시가총액·업종 데이터까지 한 번에 얻는다.

## 컬럼 단위 주의

`NA_분기_매출액(억원)` 등 재무 금액 컬럼은 **억원 단위**다. 기존
`kr_fundamental_review_service.review()` 가 기대하는 딕셔너리 shape(DART 기준, **원**
단위)에 맞추려면 ×1억 정규화가 필요하다 — `get_fundamentals_bulk()` 안에서 처리한다.

파일에 ROIC·유동비율(current_ratio) 컬럼이 없다. `kr_fundamental_review_service._hard_gate`
는 영업이익률/ROE/ROIC 중 **2개 이상 결측**일 때만 탈락시키므로, 영업이익률·ROE 가
확보되는 한 ROIC=None 이어도 하드게이트에는 걸리지 않는다(LLM 프롬프트에는 결측으로
노출될 뿐).

## 실패 처리

다운로드·파싱 실패 시 예외를 그대로 올린다(Fail-Close). DART 등 다른 소스로 조용히
폴백하지 않는다 — 회차마다 재무 점수 산출 기준이 섞이는 걸 막기 위해서다. 호출부
(kr_screening_service.run_stage1)가 예외를 잡아 1단계 자체를 중단시킨다.
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import pytz
import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

_SHEET = "Tickerboard"
_XLSX_NAME = "quant_data.xlsx"
_META_NAME = "quant_data.meta.json"
# 브라우저 UA 없이 요청하면 403 — 기본 requests UA 는 이 사이트에서 차단된다.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
# data/<파일명>_YYMMDD.xlsx 형태의 다운로드 링크를 찾는다
_LINK_RE = re.compile(r'href="(data/[^"]+?_(\d{6})\.xlsx)"')
# 재무 금액 컬럼(NA_분기_*)과 Marcap 은 각각 억원/원 단위 — 기존 관례(억원)로 맞출 때 쓴다
_OEOK = 100_000_000

_df_cache: Optional[pd.DataFrame] = None
_df_cache_path: Optional[str] = None


def _data_dir() -> Path:
    p = Path(settings.KR_QUANT_DATA_DIR)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[3] / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _xlsx_path() -> Path:
    return _data_dir() / _XLSX_NAME


def _meta_path() -> Path:
    return _data_dir() / _META_NAME


def _load_meta() -> dict:
    path = _meta_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"  퀀트데이터 메타 읽기 실패: {e}")
        return {}


def _save_meta(meta: dict) -> None:
    try:
        _meta_path().write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"  퀀트데이터 메타 저장 실패: {e}")


def cache_status() -> Optional[dict]:
    """
    현재 캐시된 퀀트데이터 상태(다운로드/파싱 없이 조회만) — 운영 메뉴 상태 화면용.

    Returns: {"source_date"(YYMMDD), "age_days", "path"} — 캐시가 없으면 None.
    """
    meta = _load_meta()
    if not meta or not _xlsx_path().exists():
        return None
    age_days = None
    downloaded_at = meta.get("downloaded_at")
    if downloaded_at:
        try:
            age_days = (
                datetime.now(KST) - datetime.fromisoformat(downloaded_at)
            ).total_seconds() / 86400.0
        except ValueError:
            age_days = None
    return {
        "source_date": meta.get("source_date"),
        "age_days": age_days,
        "path": str(_xlsx_path()),
    }


def _find_latest_file() -> tuple:
    """목록 페이지에서 가장 최근 날짜(YYMMDD)의 다운로드 링크를 찾는다.

    Returns: (절대 URL, "YYMMDD" 문자열)
    Raises: RuntimeError — 페이지 조회 실패 / 링크 없음
    """
    url = settings.KR_QUANT_DATA_SOURCE_URL
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"퀀트데이터 목록 페이지 조회 실패({url}): {e}") from e

    # 서버가 Content-Type 에 charset 을 안 보내 requests 가 ISO-8859-1 로 오판한다 —
    # 실제 본문은 UTF-8 이라 링크 안의 한글 파일명이 깨져 잘못된 다운로드 URL 이 된다.
    resp.encoding = "utf-8"
    matches = _LINK_RE.findall(resp.text)
    if not matches:
        raise RuntimeError(f"퀀트데이터 다운로드 링크를 찾지 못했습니다({url})")

    rel_path, date_str = max(matches, key=lambda m: m[1])
    from urllib.parse import urljoin

    return urljoin(url, rel_path), date_str


def ensure_fresh(force_refresh: bool = False) -> Path:
    """캐시가 오래됐거나(KR_QUANT_CACHE_DAYS 초과) 새 파일이 나왔으면 재다운로드.

    Raises: RuntimeError — 목록 조회/다운로드/저장 어느 단계든 실패하면 그대로 올린다
            (Fail-Close, 호출부가 잡아서 1단계를 중단시킨다).
    """
    meta = _load_meta()
    cached_date = meta.get("source_date")
    downloaded_at = meta.get("downloaded_at")
    xlsx_path = _xlsx_path()

    age_days = 1e9
    if downloaded_at:
        try:
            age_days = (
                datetime.now(KST) - datetime.fromisoformat(downloaded_at)
            ).total_seconds() / 86400.0
        except ValueError:
            age_days = 1e9

    if (
        not force_refresh
        and xlsx_path.exists()
        and cached_date
        and age_days <= settings.KR_QUANT_CACHE_DAYS
    ):
        logger.debug(f"  퀀트데이터 캐시 사용 ({cached_date}, {age_days:.1f}일 전 확인)")
        return xlsx_path

    url, latest_date = _find_latest_file()

    if not force_refresh and xlsx_path.exists() and cached_date == latest_date:
        # 최신 파일과 날짜가 같으면(파일은 여전히 최신) 메타의 확인 시각만 갱신
        meta["downloaded_at"] = datetime.now(KST).isoformat()
        _save_meta(meta)
        logger.debug(f"  퀀트데이터 최신 확인 — 재다운로드 불필요 ({latest_date})")
        return xlsx_path

    logger.info(f"  퀀트데이터 다운로드: {url}")
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=60)
        resp.raise_for_status()
        xlsx_path.write_bytes(resp.content)
    except Exception as e:
        raise RuntimeError(f"퀀트데이터 다운로드 실패({url}): {e}") from e

    _save_meta(
        {
            "source_date": latest_date,
            "source_url": url,
            "downloaded_at": datetime.now(KST).isoformat(),
        }
    )
    global _df_cache
    _df_cache = None  # 새로 받았으니 데이터프레임 캐시 무효화
    logger.info(f"  퀀트데이터 갱신 완료 ({latest_date}, {len(resp.content):,} bytes)")
    return xlsx_path


def load_dataframe(force_refresh: bool = False) -> pd.DataFrame:
    """캐시된(또는 방금 받은) xlsx 를 DataFrame 으로. Code 를 6자리 0패딩 인덱스로 씀."""
    global _df_cache, _df_cache_path
    path = ensure_fresh(force_refresh=force_refresh)

    if _df_cache is not None and _df_cache_path == str(path):
        return _df_cache

    try:
        df = pd.read_excel(path, sheet_name=_SHEET)
    except Exception as e:
        raise RuntimeError(f"퀀트데이터 파싱 실패({path}): {e}") from e

    df["Code"] = df["Code"].astype(str).str.zfill(6)
    df = df.set_index("Code", drop=False)
    _df_cache = df
    _df_cache_path = str(path)
    return df


def _num(row, col) -> Optional[float]:
    v = row.get(col)
    if v is None or pd.isna(v):
        return None
    return float(v)


def _pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return round(num / den * 100, 2)


def get_fundamentals_bulk(codes: List[str], progress: bool = False) -> Dict[str, dict]:
    """dart_service.get_fundamentals_bulk() 와 동일한 shape 을 만든다.

    Returns: {code: {operating_margin, roe, roic, per, debt_ratio, current_ratio,
                      cf_operating, cf_investing, cf_financing, capex, fcf,
                      revenue, operating_income, net_income, equity, assets,
                      fiscal_year}}
    파일에 없는 종목(상장폐지 등)은 결과에서 생략한다 — DART 쪽과 동일한 동작이며,
    kr_fundamental_review_service._hard_gate 가 "재무제표 조회 실패"로 처리한다.
    """
    df = load_dataframe()
    meta = _load_meta()
    fiscal_label = f"quant_{meta.get('source_date', '?')}"

    result: Dict[str, dict] = {}
    for code in codes:
        if code not in df.index:
            continue
        row = df.loc[code]

        revenue = _num(row, "NA_분기_매출액(억원)")
        op_income = _num(row, "NA_분기_영업이익(억원)")
        net_income = _num(row, "NA_분기_당기순이익_지배(억원)")
        equity = _num(row, "NA_분기_자본총계")
        cf_operating = _num(row, "NA_분기_영업현금흐름")
        capex = _num(row, "NA_분기_CAPEX")
        fcf = _num(row, "NA_분기_FCF")

        result[code] = {
            "operating_margin": _pct(op_income, revenue),
            "roe": _num(row, "NA_분기_ROE(%)"),
            "roic": None,  # 파일에 없음 — 하드게이트는 수익성 3종 중 2개 결측일 때만 탈락
            "per": _num(row, "NA_분기_PER"),
            "debt_ratio": _num(row, "NA_분기_부채비율(%)"),
            "current_ratio": None,  # 파일에 없음
            "cf_operating": cf_operating * _OEOK if cf_operating is not None else None,
            "cf_investing": None,
            "cf_financing": None,
            "capex": capex * _OEOK if capex is not None else None,
            "fcf": fcf * _OEOK if fcf is not None else None,
            "revenue": revenue * _OEOK if revenue is not None else None,
            "operating_income": op_income * _OEOK if op_income is not None else None,
            "net_income": net_income * _OEOK if net_income is not None else None,
            "equity": equity * _OEOK if equity is not None else None,
            "assets": None,  # 파일에 없음 — LLM 프롬프트 참고용 원자료라 결측 영향 없음
            "fiscal_year": fiscal_label,
        }

    if progress:
        logger.info(f"  퀀트데이터 재무 매핑: {len(result)}/{len(codes)}종목")
    return result


def get_market_meta_bulk(codes: List[str]) -> Dict[str, dict]:
    """{code: {"market_cap"(억원), "sector_krx", "rank"}} — 시총 내림차순 순위 포함."""
    df = load_dataframe()

    rows = []
    for code in codes:
        if code not in df.index:
            continue
        row = df.loc[code]
        marcap_won = _num(row, "Marcap")
        rows.append(
            {
                "code": code,
                "market_cap": (marcap_won / _OEOK) if marcap_won is not None else None,
                "sector_krx": row.get("KRX업종(대분류)") or None,
                "_marcap_won": marcap_won or 0.0,
            }
        )

    rows.sort(key=lambda r: r["_marcap_won"], reverse=True)
    result: Dict[str, dict] = {}
    for i, r in enumerate(rows, 1):
        result[r["code"]] = {
            "market_cap": r["market_cap"],
            "sector_krx": r["sector_krx"],
            "rank": i,
        }
    return result


# 0단계 "최소필터"의 유동성 축 — 파일이 이미 계산해 둔 스크리닝 필터 컬럼 중, 거래 자체가
# 불가능하거나 사실상 투자 부적격인 상태만 골랐다. 재무건전성 계열 필터(적자기업_isFilter,
# 부채비율_isFilter 등)는 kr_fundamental_review_service 의 하드게이트/LLM 이 이미 더
# 세밀하게(업종 감안) 판단하므로 여기서 중복 적용하지 않는다.
_LIQUIDITY_FILTER_COLUMNS = {
    "동전주_isFilter": "동전주(초저가주)",
    "거래정지종목_isFilter": "거래정지",
    "관리종목_isFilter": "관리종목 지정",
}


def get_liquidity_flags(codes: List[str]) -> Dict[str, dict]:
    """{code: {"excluded": bool, "reason": str}} — 동전주/거래정지/관리종목만 배제 대상."""
    df = load_dataframe()
    result: Dict[str, dict] = {}
    for code in codes:
        if code not in df.index:
            continue
        row = df.loc[code]
        hit = []
        for col, label in _LIQUIDITY_FILTER_COLUMNS.items():
            if col in row.index:
                v = row.get(col)
                if v is not None and not pd.isna(v) and bool(v):
                    hit.append(label)
        result[code] = {"excluded": bool(hit), "reason": ", ".join(hit)}
    return result
