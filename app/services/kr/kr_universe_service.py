"""
KOSPI 시가총액 상위 N 종목 유니버스 — 동적 산출.

기존 universe.py 는 분기마다 사람이 손으로 갱신하는 고정 리스트다. 이 모듈은 그것을
대체하지 않고 **분석 후보군**만 매번 새로 계산한다. 둘의 역할이 다르기 때문이다.

  · universe.py (고정)   — ML 학습 대상. kr_economic_and_stock_data 의 종가 컬럼과
                           predict_kr.py 의 TARGET_COLUMNS 가 이 목록에 1:1로 묶여 있어서
                           마음대로 바꾸면 학습 데이터가 깨진다.
  · 이 모듈 (동적)        — 1·2단계 필터링 후보군. 매매 판단만 하므로 매번 최신 시총
                           순위를 그대로 쓰는 편이 낫다.

두 목록을 일치시키고 싶으면 sync_static_universe() 가 만들어 주는 산출물로
세 파일(universe.py / setup_kr.sql / predict_kr.py)을 함께 갱신하면 된다.

## 시총 순위를 구하는 방법

KRX Open API(openapi.krx.co.kr)가 정석이지만 API 별로 이용신청이 따로 필요해서
승인 전에는 전부 401 을 돌려준다. 그래서 KIS 만으로 해결한다.

  1) KIS 종목마스터(kospi_code.mst.zip)를 받아 KOSPI 전 종목을 얻는다.
     - 고정폭 레코드다. 뒤에서 **227 바이트**가 고정 필드부이고 그 앞이 코드+표준코드+한글명.
       한글이 cp949 2바이트라 문자열로 자르면 어긋난다 — 반드시 bytes 로 잘라야 한다.
     - 그룹코드 ST(주권)만 남기고 EF(ETF)/EN(ETN)/RT(리츠)/BC/SW 는 버린다.
     - 우선주는 종목코드 끝자리가 0 이 아니다(삼성전자 005930 / 삼성전자우 005935).
     - 스팩은 이름으로 거른다(그룹코드가 ST 라 코드로는 구분되지 않는다).
  2) 남은 종목마다 현재가 API(inquire-price)의 hts_avls(시가총액, 억원)를 읽어 정렬한다.

2)가 800회 남짓 호출이라 모의계좌 기준(0.6초 간격) 8분쯤 걸린다. 그래서 결과를 하루
단위로 캐시하고, 파이프라인이 돌 때마다 다시 재지는 않는다.

시가총액 상위 30 은 ranking/market-cap(FHPST01740000)으로 한 번에 받을 수 있지만
모의계좌에서는 연속조회가 막혀 30건이 상한이라 200 을 채울 수 없다. 그래서 쓰지 않는다.
"""
import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytz
import requests

from app.core.config import settings
from app.services.kr import kis_domestic_service as kis
from app.services.kr import universe as static_universe

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

MASTER_URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
MASTER_MEMBER = "kospi_code.mst"

# 고정 필드부 길이(바이트). KIS 마스터 포맷 상수다.
_TRAILER_BYTES = 227

# 개별 기업 분석 대상이 아닌 것들 — 이름으로 거른다
_EXCLUDE_NAME_PATTERNS = re.compile(r"스팩|기업인수목적|리츠|리 츠")

_CACHE_FILENAME = "kr_universe_cache.json"


# ══════════════════════════════════════════════════════════════════
# 1) 종목마스터
# ══════════════════════════════════════════════════════════════════

def _cache_path() -> Path:
    p = Path(settings.KR_UNIVERSE_CACHE_DIR)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[3] / p
    p.mkdir(parents=True, exist_ok=True)
    return p / _CACHE_FILENAME


def fetch_kospi_master() -> List[dict]:
    """
    KIS 종목마스터에서 KOSPI 보통주 목록을 뽑는다.

    Returns: [{"code","name","cap_scale"}] — 시총 정보는 아직 없다(순서 = 마스터 순서).
    """
    resp = requests.get(MASTER_URL, timeout=90)
    resp.raise_for_status()
    raw = zipfile.ZipFile(io.BytesIO(resp.content)).read(MASTER_MEMBER)

    stocks: List[dict] = []
    for row in raw.split(b"\n"):
        row = row.rstrip(b"\r")
        if len(row) <= _TRAILER_BYTES + 21:
            continue

        head = row[: len(row) - _TRAILER_BYTES]
        try:
            code = head[0:9].decode("cp949").strip()
            name = head[21:].decode("cp949", "replace").strip()
            tail = row[-_TRAILER_BYTES:].decode("cp949", "replace")
        except Exception:
            continue

        if tail[0:2] != "ST":            # 주권만 (ETF/ETN/리츠/신주인수권 제외)
            continue
        if not (code.isdigit() and len(code) == 6):
            continue
        if not code.endswith("0"):       # 우선주 제외
            continue
        if _EXCLUDE_NAME_PATTERNS.search(name):
            continue

        stocks.append({"code": code, "name": name, "cap_scale": tail[2:3]})

    logger.info(f"  KOSPI 종목마스터: 보통주 {len(stocks)}종목")
    return stocks


# ══════════════════════════════════════════════════════════════════
# 2) 시가총액 정렬
# ══════════════════════════════════════════════════════════════════

def _quote(code: str) -> Optional[dict]:
    """
    현재가 API 한 번으로 시가총액과 업종명을 함께 가져온다.

    업종명(bstp_kor_isnm)은 KRX 업종 분류라 **모든 상장 종목에 빠짐없이 들어 있다.**
    고정 유니버스(universe.py)의 섹터 라벨은 100종목분뿐이라 나머지 100종목이 '미분류'로
    한 덩어리가 되는데, 그러면 섹터 분산 규칙(KR_MAX_PER_SECTOR)이 은행과 화학을 같은
    섹터로 세는 사고가 난다. 그래서 어차피 하는 호출에서 업종명을 같이 챙긴다.

    Returns: {"market_cap"(억원), "sector_krx"} — 조회 실패 시 None
    """
    try:
        result = kis.get_price(code)
        if result.get("rt_cd") != "0":
            return None
        output = result.get("output") or {}
        raw_cap = output.get("hts_avls")
        if raw_cap in (None, ""):
            return None
        return {
            "market_cap": float(raw_cap),
            "sector_krx": (output.get("bstp_kor_isnm") or "").strip() or None,
        }
    except Exception:
        return None


def build_universe(top_n: Optional[int] = None, progress: bool = False) -> List[dict]:
    """
    KOSPI 보통주 전체의 시가총액을 조회해 상위 top_n 을 돌려준다.

    종목당 1회 호출이라 모의계좌 기준 8분 안팎이 걸린다. 파이프라인에서 직접 부르지 말고
    get_universe() 를 통해 캐시를 쓰라.

    progress=True 면 진행 상황을 로그로 남긴다 (메뉴에서 손으로 돌릴 때).
    """
    top_n = top_n or settings.KR_UNIVERSE_SIZE
    stocks = fetch_kospi_master()
    total = len(stocks)
    started = time.time()

    priced: List[dict] = []
    for i, item in enumerate(stocks, 1):
        quote = _quote(item["code"])
        if quote is not None:
            priced.append({**item, **quote})
        if progress and (i % 50 == 0 or i == total):
            elapsed = time.time() - started
            logger.info(
                f"  시가총액 조회 {i}/{total} ({elapsed:.0f}초 경과, "
                f"수집 {len(priced)}종목)"
            )

    priced.sort(key=lambda x: x["market_cap"], reverse=True)
    top = priced[:top_n]

    # 섹터 라벨은 둘을 함께 들고 간다.
    #   sector      — 고정 유니버스의 세분류(예: '반도체와반도체장비'). 표시·LLM 프롬프트용.
    #                 없으면 KRX 업종명으로 대체한다.
    #   sector_krx  — KRX 업종명(예: '전기·전자'). 전 종목에 있으므로 분산 규칙의 기준.
    for rank, entry in enumerate(top, 1):
        entry["sector"] = static_universe.sector(entry["code"]) or entry.get("sector_krx")
        entry["rank"] = rank

    logger.info(
        f"  시가총액 상위 {len(top)}종목 확정 "
        f"({time.time() - started:.0f}초, 조회 성공 {len(priced)}/{total})"
    )
    return top


# ══════════════════════════════════════════════════════════════════
# 3) 캐시
# ══════════════════════════════════════════════════════════════════

def _read_cache() -> Optional[dict]:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"  유니버스 캐시 읽기 실패: {e}")
        return None


def _write_cache(items: List[dict]) -> None:
    payload = {
        "built_at": datetime.now(KST).isoformat(),
        "size": len(items),
        "items": items,
    }
    try:
        with io.open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning(f"  유니버스 캐시 저장 실패: {e}")


def cache_age_days() -> Optional[float]:
    """캐시가 만들어진 뒤 지난 일수. 캐시가 없으면 None."""
    cache = _read_cache()
    if not cache:
        return None
    try:
        built = datetime.fromisoformat(cache["built_at"])
    except (KeyError, ValueError):
        return None
    return (datetime.now(KST) - built).total_seconds() / 86400.0


def get_universe(force_refresh: bool = False, progress: bool = False) -> List[dict]:
    """
    분석 후보군 (시총 상위 KR_UNIVERSE_SIZE 종목).

    캐시가 KR_UNIVERSE_REFRESH_DAYS 보다 오래됐으면 다시 만든다. 시총 순위는 하루아침에
    뒤집히지 않으므로 기본 7일이면 충분하다.

    조회 자체가 실패하면(네트워크/KIS 장애) 낡은 캐시라도 그대로 쓴다 — 후보군이 하루
    낡은 것보다 파이프라인이 통째로 멈추는 쪽이 나쁘다. 캐시도 없으면 고정 유니버스로
    폴백한다.
    """
    cache = _read_cache()
    age = cache_age_days()
    fresh = (
        cache is not None
        and age is not None
        and age <= settings.KR_UNIVERSE_REFRESH_DAYS
        and cache.get("size", 0) > 0
    )

    if fresh and not force_refresh:
        return cache["items"]

    try:
        items = build_universe(progress=progress)
        if items:
            _write_cache(items)
            return items
        raise RuntimeError("시가총액 조회 결과가 비었습니다")
    except Exception as e:
        logger.error(f"  유니버스 갱신 실패: {e}")
        if cache and cache.get("items"):
            logger.warning(
                f"  낡은 캐시로 폴백합니다 (생성 {cache.get('built_at')}, "
                f"{len(cache['items'])}종목)"
            )
            return cache["items"]
        logger.warning("  캐시도 없어 고정 유니버스(universe.py)로 폴백합니다")
        return [
            {**u, "market_cap": None, "rank": i + 1}
            for i, u in enumerate(static_universe.UNIVERSE)
        ]


# ══════════════════════════════════════════════════════════════════
# 4) 조회 헬퍼
# ══════════════════════════════════════════════════════════════════

def codes(force_refresh: bool = False) -> List[str]:
    return [u["code"] for u in get_universe(force_refresh=force_refresh)]


def as_map(force_refresh: bool = False) -> Dict[str, dict]:
    return {u["code"]: u for u in get_universe(force_refresh=force_refresh)}


def _cached_map() -> Dict[str, dict]:
    """
    캐시에 있는 것만 본다. **없으면 빈 dict 를 돌려주고 절대 새로 만들지 않는다.**

    display()/sector() 같은 표시용 헬퍼가 get_universe() 를 부르면, 캐시가 없을 때
    801종목 시가총액 조회(8분)가 조용히 시작된다. 잔고 화면 한 번 열었다가 8분을
    기다리게 되는 셈이라, 조회용 경로는 캐시만 읽도록 분리했다.
    """
    cache = _read_cache()
    if not cache or not cache.get("items"):
        return {}
    return {u["code"]: u for u in cache["items"]}


def display(code: str) -> str:
    """'삼성전자(005930)' 형태. 캐시에 없으면 고정 유니버스를 본다."""
    item = _cached_map().get(code)
    if item:
        return f"{item['name']}({code})"
    return static_universe.display(code)


def sector_key(code: str) -> str:
    """
    섹터 분산 규칙(KR_MAX_PER_SECTOR)이 쓰는 버킷 키.

    KRX 업종명을 우선한다 — 전 종목에 빠짐없이 있어서 어떤 종목이든 제 버킷으로 간다.
    캐시에 없는 종목(유니버스 밖 보유분 등)은 고정 유니버스의 세분류로, 그것도 없으면
    '미분류'로 떨어진다.
    """
    item = _cached_map().get(code) or {}
    return item.get("sector_krx") or item.get("sector") or static_universe.sector(code) or "미분류"


def sector(code: str) -> Optional[str]:
    """종목 업종. 캐시 → 고정 유니버스 순으로 찾고, 둘 다 없으면 None."""
    item = _cached_map().get(code)
    if item and item.get("sector"):
        return item["sector"]
    return static_universe.sector(code)


# ══════════════════════════════════════════════════════════════════
# 5) 고정 유니버스 동기화 산출물
# ══════════════════════════════════════════════════════════════════

def sync_static_universe(out_dir: Optional[str] = None) -> dict:
    """
    현재 시총 상위 목록으로 '세 파일을 갱신하기 위한 조각'을 만들어 파일로 떨군다.

    universe.py / setup_kr.sql / predict_kr.py 를 직접 덮어쓰지는 않는다. ML 학습 대상이
    바뀌면 과거 학습 데이터의 컬럼과 어긋나므로, 사람이 내용을 보고 반영할지 정해야 한다.

    Returns: {"out_dir", "files": [...], "size"}
    """
    items = get_universe()
    out = Path(out_dir) if out_dir else _cache_path().parent / "universe_sync"
    out.mkdir(parents=True, exist_ok=True)

    # 1) universe.py 의 UNIVERSE 리스트
    lines = ["UNIVERSE: List[Dict[str, str]] = ["]
    for it in items:
        sec = it.get("sector") or "미분류"
        lines.append(
            f'    {{"code": "{it["code"]}", "name": "{it["name"]}", "sector": "{sec}"}},'
        )
    lines.append("]")
    (out / "universe_list.py.txt").write_text("\n".join(lines), encoding="utf-8")

    # 2) setup_kr.sql 의 종가 컬럼
    sql = [
        f'ALTER TABLE kr_economic_and_stock_data ADD COLUMN IF NOT EXISTS "{it["name"]}" NUMERIC;'
        for it in items
    ]
    (out / "add_price_columns.sql").write_text("\n".join(sql), encoding="utf-8")

    # 3) predict_kr.py 의 TARGET_COLUMNS
    tgt = ["TARGET_COLUMNS = ["]
    tgt += [f'    "{it["name"]}",' for it in items]
    tgt.append("]")
    (out / "target_columns.py.txt").write_text("\n".join(tgt), encoding="utf-8")

    logger.info(f"  유니버스 동기화 산출물 생성: {out}")
    return {
        "out_dir": str(out),
        "files": ["universe_list.py.txt", "add_price_columns.sql", "target_columns.py.txt"],
        "size": len(items),
    }
