"""
국내 경제지표 + KOSPI100 주가 수집 → Supabase `kr_economic_and_stock_data`.

미국 트랙(stock.py + economic_service.py)의 국내판이다. 역할 분담은 이렇게 나눴다.

  Yahoo Finance  : KOSPI/KOSPI200/KOSDAQ 지수, 환율, 글로벌 지표, 30종목 일별 종가
                   → 2006년부터의 히스토리 백필이 필요해 무료·무제한 소스를 쓴다.
  한국은행 ECOS  : 한국 전용 거시지표 (기준금리, 국고채, CPI, M2).
                   → FRED 의 한국판. 키가 없거나 코드가 안 맞으면 해당 컬럼만 비고 진행.
  KIS API        : 당일 시세·거래량·ADX/ATR (kr_recommendation_service 에서 사용).
                   → 여기서는 쓰지 않는다. 히스토리 조회는 100건 제한이 있어 백필에 부적합.

ECOS 통계항목코드는 한국은행이 개편하면 바뀔 수 있다. 그래서 모든 ECOS 시리즈는
best-effort 이고, 실패하면 경고만 남긴다. 어떤 시리즈가 살아있는지는
GET /kr/economic/ecos-check 로 확인할 수 있다.
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pytz
import requests

from app.core.config import settings
from app.db.supabase import supabase
from app.services.kr import universe

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

# ══════════════════════════════════════════════════════════════════
# Yahoo Chart API 다운로더
#
#   stock.py 의 download_yahoo_chart 는 기간을 range 문자열(max/5y/1y…)로 지정하는데,
#   range=max 응답이 축약돼서 장기 백필에 쓸 수 없다. 실측(005930.KS):
#       range=max -> 320건 (2000~2026)   range=10y -> 2,448건
#       range=1y  -> 244건                period1/period2 -> 5,096건 (2006-01-02~)
#   ^KS200 은 range=max 로 1건만 돌아왔다.
#   그래서 국내 트랙은 period1/period2(unix epoch)로 구간을 명시한다.
# ══════════════════════════════════════════════════════════════════

_YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_YF_SESSION = requests.Session()
_YF_SESSION.headers.update(
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
)


def download_yahoo_daily(
    symbol: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    """
    Yahoo Chart API 로 일별 종가를 받아온다.

    Args:
        symbol: Yahoo 티커 ("^KS11", "005930.KS", "KRW=X" …)
        start_date / end_date: "YYYY-MM-DD"

    Returns:
        "Close" 단일 컬럼 + tz-naive DatetimeIndex DataFrame. 실패 시 None.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    # 종료일 당일을 포함시키기 위해 하루 더한다 (period2 는 exclusive 처럼 동작)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

    resp = _YF_SESSION.get(
        _YF_URL.format(symbol=symbol),
        params={
            "period1": int(start_dt.timestamp()),
            "period2": int(end_dt.timestamp()),
            "interval": "1d",
            "includePrePost": "false",
            "events": "div|split",
        },
        timeout=30,
    )
    resp.raise_for_status()

    result = (resp.json().get("chart") or {}).get("result") or [None]
    result = result[0]
    if not result or "timestamp" not in result:
        return None

    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]

    # 거래일 기준 날짜만 남긴다 (장중 시각은 버림)
    dates = [pd.Timestamp.fromtimestamp(ts).date() for ts in timestamps]
    df = pd.DataFrame({"Close": closes}, index=pd.DatetimeIndex(dates))

    df = df[df["Close"].notna()]
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep="last")]

    df = df[
        (df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))
    ]
    return df if not df.empty else None


TABLE = "kr_economic_and_stock_data"


def default_start_date() -> str:
    """
    백필 시작일 = 오늘 - KR_HISTORY_YEARS 년.

    고정 날짜(2006-01-01)를 쓰지 않는 이유: 종목이 100개라 기간이 길수록 수집 시간과
    ML 학습 시간이 선형으로 늘어나는데, 오래된 구간은 유니버스 종목 상당수가
    상장 전이라 어차피 학습에 쓰이지 못한다.
    """
    today = datetime.now(KST).date()
    try:
        start = today.replace(year=today.year - settings.KR_HISTORY_YEARS)
    except ValueError:  # 2/29 → 평년
        start = today.replace(year=today.year - settings.KR_HISTORY_YEARS, day=28)
    return start.strftime("%Y-%m-%d")

# 코스피 20일 실현변동성(연율 %) — 매수 게이트로 쓰는 '한국판 공포지수'.
# 파생값이지만 백테스팅/구간별 성과 분석을 위해 컬럼으로도 적재한다.
VOLATILITY_COLUMN = "코스피 변동성 20일"
VOLATILITY_WINDOW = 20

# ══════════════════════════════════════════════════════════════════
# Yahoo Finance 지표
# ══════════════════════════════════════════════════════════════════

# 한국 시장 지수 / 환율
KR_MARKET_TICKERS: Dict[str, str] = {
    "코스피": "^KS11",
    # ^KS200 은 Yahoo 쪽 시계열이 깨져 있다 (실측: 코스피와의 일간 수익률 상관 0.86,
    # KODEX200 은 0.99 / 2026-07-16 이후 갱신 중단). 실제 거래되는 추종 ETF 로 대체한다.
    "코스피200": "069500.KS",
    "코스닥": "^KQ11",
    "원달러환율": "KRW=X",
    "엔원환율": "JPYKRW=X",
}

# 글로벌 지표 — 한국 증시는 미국장·반도체 업황·환율에 강하게 연동되므로 함께 학습시킨다
GLOBAL_TICKERS: Dict[str, str] = {
    "S&P 500 지수": "^GSPC",
    "나스닥 종합지수": "^IXIC",
    "VIX 지수": "^VIX",
    "필라델피아 반도체 지수": "^SOX",
    "달러 인덱스": "DX-Y.NYB",
    "미국 10년 국채금리": "^TNX",
    "금 가격": "GC=F",
    "WTI 유가": "CL=F",
    "닛케이 225": "^N225",
    "상해종합": "000001.SS",
    "항셍": "^HSI",
}

def compute_realized_volatility(
    close: pd.Series, window: int = VOLATILITY_WINDOW
) -> pd.Series:
    """
    거래일 기준 N일 실현변동성(연율 %).

    입력은 '거래일만 있는' 종가 시계열이어야 한다. 달력일로 ffill 된 시계열을 넣으면
    휴장일의 수익률 0 이 표준편차를 끌어내려 변동성이 과소평가된다.
    (그래서 _download_all 에서 병합·ffill 이전의 원본 프레임으로 계산한다)
    """
    returns = close.pct_change().dropna()
    vol = returns.rolling(window).std() * (252 ** 0.5) * 100
    return vol.round(2)


# ══════════════════════════════════════════════════════════════════
# 한국은행 ECOS 시리즈
#   (컬럼명, 통계표코드, 주기, 통계항목코드)
#   주기: D(일) M(월) Q(분기) A(년)
#   ※ 항목코드는 ECOS 개편 시 바뀔 수 있다. 실패해도 파이프라인은 진행된다.
# ══════════════════════════════════════════════════════════════════
ECOS_SERIES: List[dict] = [
    {"column": "한국 기준금리", "stat": "722Y001", "cycle": "D", "item": "0101000"},
    {"column": "국고채 3년", "stat": "817Y002", "cycle": "D", "item": "010200000"},
    {"column": "국고채 10년", "stat": "817Y002", "cycle": "D", "item": "010210000"},
    {"column": "CD 91일", "stat": "817Y002", "cycle": "D", "item": "010502000"},
    {"column": "한국 소비자물가지수", "stat": "901Y009", "cycle": "M", "item": "0"},
    # 161Y005 = M2 상품별 구성내역(평잔, 계절조정계열). 2003-10~ 현재까지 갱신된다.
    # 구 101Y004/101Y003 계열은 2004-09 에 종료돼 조회하면 0건이 나온다.
    {"column": "한국 통화량 M2", "stat": "161Y005", "cycle": "M", "item": "BBHS00"},
]

ECOS_BASE = "https://ecos.bok.or.kr/api/StatisticSearch"


def _ecos_fetch(series: dict, start: str, end: str) -> Optional[pd.DataFrame]:
    """
    ECOS 단일 시리즈 조회 → DatetimeIndex + 단일 컬럼 DataFrame.
    실패 시 None (호출부에서 스킵).
    """
    if not settings.ECOS_API_KEY:
        return None

    cycle = series["cycle"]
    # 주기별 날짜 포맷: D=YYYYMMDD, M=YYYYMM, Q=YYYYQn, A=YYYY
    if cycle == "D":
        s, e = start.replace("-", ""), end.replace("-", "")
    elif cycle == "M":
        s, e = start[:7].replace("-", ""), end[:7].replace("-", "")
    else:
        s, e = start[:4], end[:4]

    url = (
        f"{ECOS_BASE}/{settings.ECOS_API_KEY}/json/kr/1/100000/"
        f"{series['stat']}/{cycle}/{s}/{e}/{series['item']}"
    )

    try:
        resp = requests.get(url, timeout=20)
        payload = resp.json()
    except Exception as e:
        logger.warning(f"  ECOS {series['column']} 호출 실패: {e}")
        return None

    if "StatisticSearch" not in payload:
        reason = payload.get("RESULT", {}).get("MESSAGE", str(payload)[:150])
        logger.warning(f"  ECOS {series['column']} 응답 이상: {reason}")
        return None

    rows = payload["StatisticSearch"].get("row", [])
    if not rows:
        logger.warning(f"  ECOS {series['column']} 데이터 0건")
        return None

    records = []
    for r in rows:
        raw_time = r.get("TIME", "")
        value = r.get("DATA_VALUE")
        if value in (None, "", "-"):
            continue
        try:
            if cycle == "D":
                dt = datetime.strptime(raw_time, "%Y%m%d")
            elif cycle == "M":
                dt = datetime.strptime(raw_time + "01", "%Y%m%d")
            else:
                dt = datetime.strptime(raw_time[:4] + "0101", "%Y%m%d")
            records.append((dt, float(value)))
        except (ValueError, TypeError):
            continue

    if not records:
        return None

    df = pd.DataFrame(records, columns=["date", series["column"]]).set_index("date")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # 월/분기 시리즈는 일별로 확장 (전진 채움)
    if cycle != "D":
        df = df.resample("D").ffill()
    logger.info(f"  ECOS {series['column']} 수집 완료 ({len(df)}건)")
    return df


def check_ecos_series() -> List[dict]:
    """
    ECOS 시리즈 코드가 현재도 유효한지 진단한다 (최근 90일 조회).
    통계항목코드가 개편으로 바뀌었을 때 어디를 고쳐야 하는지 알려주는 용도.
    """
    end = datetime.now(KST).strftime("%Y-%m-%d")
    start = (datetime.now(KST) - timedelta(days=90)).strftime("%Y-%m-%d")

    results = []
    for series in ECOS_SERIES:
        df = _ecos_fetch(series, start, end)
        results.append(
            {
                "column": series["column"],
                "stat_code": series["stat"],
                "item_code": series["item"],
                "cycle": series["cycle"],
                "ok": df is not None and not df.empty,
                "rows": 0 if df is None else len(df),
                "latest_value": None if df is None or df.empty else float(df.iloc[-1, 0]),
            }
        )
        time.sleep(0.3)
    return results


# ══════════════════════════════════════════════════════════════════
# 수집 파이프라인
# ══════════════════════════════════════════════════════════════════

def _get_last_collected_date() -> str:
    """DB 최종 수집일의 다음 날짜. 데이터가 없으면 설정된 수집 기간의 시작일."""
    try:
        resp = (
            supabase.table(TABLE)
            .select("날짜")
            .order("날짜", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            last = datetime.strptime(resp.data[0]["날짜"][:10], "%Y-%m-%d")
            return (last + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception as e:
        logger.warning(f"최종 수집일 조회 실패 → 전체 백필: {e}")
    return default_start_date()


def _download_all(start: str, end: str) -> Optional[pd.DataFrame]:
    """Yahoo(지수/환율/글로벌/30종목) + ECOS 를 날짜 기준으로 병합."""
    frames: List[pd.DataFrame] = []

    def _pull(label: str, ticker: str, column: str):
        try:
            df = download_yahoo_daily(ticker, start, end)
            if df is None or df.empty:
                logger.warning(f"  {label} {column}({ticker}) 데이터 없음")
                return
            df = df.rename(columns={"Close": column})
            df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            frames.append(df[[column]])
            logger.info(f"  {label} {column}({ticker}) {len(df)}건")
        except Exception as e:
            logger.warning(f"  {label} {column}({ticker}) 수집 실패: {e}")
        time.sleep(0.6)  # Yahoo rate limit 회피

    logger.info("[1/4] 한국 시장 지수/환율 수집")
    for column, ticker in KR_MARKET_TICKERS.items():
        _pull("KR", ticker=ticker, column=column)

    # 변동성은 병합·ffill 이전의 '거래일만 있는' 코스피 원본으로 계산해야 정확하다
    kospi_frame = next((f for f in frames if "코스피" in f.columns), None)
    if kospi_frame is not None and len(kospi_frame) > VOLATILITY_WINDOW:
        vol = compute_realized_volatility(kospi_frame["코스피"]).dropna()
        if not vol.empty:
            frames.append(vol.to_frame(VOLATILITY_COLUMN))
            logger.info(
                f"  DERIVED {VOLATILITY_COLUMN} {len(vol)}건 "
                f"(최근 {vol.iloc[-1]:.2f}%)"
            )
    else:
        logger.warning("  코스피 시계열이 없어 변동성 컬럼을 만들지 못했습니다")

    logger.info("[2/4] 글로벌 지표 수집")
    for column, ticker in GLOBAL_TICKERS.items():
        _pull("GLOBAL", ticker=ticker, column=column)

    logger.info(f"[3/4] KOSPI100 종목 종가 수집 ({len(universe.UNIVERSE)}종목)")
    for stock in universe.UNIVERSE:
        _pull("STOCK", ticker=universe.CODE_TO_YF[stock["code"]], column=stock["name"])

    logger.info("[4/4] 한국은행 ECOS 거시지표 수집")
    if settings.ECOS_API_KEY:
        for series in ECOS_SERIES:
            df = _ecos_fetch(series, start, end)
            if df is not None and not df.empty:
                frames.append(df)
            time.sleep(0.3)
    else:
        logger.warning("  ECOS_API_KEY 미설정 — 한국 거시지표 수집 스킵")

    if not frames:
        return None

    # 인덱스 중복 제거 후 외부 조인
    for i, df in enumerate(frames):
        if df.index.duplicated().any():
            frames[i] = df[~df.index.duplicated(keep="last")]

    merged = pd.concat(frames, axis=1, join="outer").sort_index()
    merged.index = pd.to_datetime(merged.index.date)
    merged = merged[~merged.index.duplicated(keep="last")]

    # 휴장일/공표 주기 차이로 생기는 결측은 전진 채움 (미국 트랙과 동일 전략)
    merged = merged.ffill()
    return merged


def _seed_previous_row(before_date: str) -> Dict[str, object]:
    """백필 시작일 직전 행 — 첫 며칠의 결측을 이전 값으로 채우기 위한 시드."""
    try:
        resp = (
            supabase.table(TABLE)
            .select("*")
            .lt("날짜", before_date)
            .order("날짜", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            row = dict(resp.data[0])
            row.pop("id", None)
            row.pop("날짜", None)
            return {k: v for k, v in row.items() if v is not None}
    except Exception:
        pass
    return {}


def collect_market_data(force_full: bool = False) -> dict:
    """
    증분 수집 → kr_economic_and_stock_data upsert.

    Args:
        force_full: True 면 KR_HISTORY_YEARS 년 전부터 전체 재수집

    Returns:
        {"success", "message", "start_date", "end_date", "saved_rows"}
    """
    now_kst = datetime.now(KST)
    start_date = default_start_date() if force_full else _get_last_collected_date()

    # 당일 데이터는 장 마감(15:30 KST) 후에만 확정으로 본다.
    # 마감 전이면 어제까지만 저장한다 (미완료 종가로 지표가 오염되는 것 방지).
    market_closed = (now_kst.hour > 15) or (now_kst.hour == 15 and now_kst.minute >= 30)
    storage_end = now_kst.date() if market_closed else (now_kst - timedelta(days=1)).date()
    storage_end_str = storage_end.strftime("%Y-%m-%d")

    if start_date > storage_end_str:
        msg = f"수집할 새 데이터 없음 (시작일 {start_date} > 저장 종료일 {storage_end_str})"
        logger.info(msg)
        return {
            "success": True,
            "message": msg,
            "start_date": start_date,
            "end_date": storage_end_str,
            "saved_rows": 0,
        }

    logger.info(f"국내 시장 데이터 수집: {start_date} ~ {storage_end_str}")
    collection_end = now_kst.strftime("%Y-%m-%d")
    merged = _download_all(start_date, collection_end)

    if merged is None or merged.empty:
        msg = "수집된 데이터가 없습니다"
        logger.warning(msg)
        return {
            "success": False,
            "message": msg,
            "start_date": start_date,
            "end_date": storage_end_str,
            "saved_rows": 0,
        }

    # 저장 범위로 자르기
    merged = merged.loc[
        (merged.index >= pd.Timestamp(start_date)) & (merged.index <= pd.Timestamp(storage_end))
    ]
    if merged.empty:
        msg = f"저장 범위({start_date}~{storage_end_str})에 해당하는 행이 없습니다"
        logger.info(msg)
        return {
            "success": True,
            "message": msg,
            "start_date": start_date,
            "end_date": storage_end_str,
            "saved_rows": 0,
        }

    # 직전 행으로 시드 → 주말·공휴일에도 컬럼이 비지 않게 전진 채움
    previous = _seed_previous_row(start_date)
    rows = []
    for idx, series in merged.iterrows():
        row: Dict[str, object] = {"날짜": idx.strftime("%Y-%m-%d")}
        for col, val in series.items():
            if pd.notna(val):
                row[col] = float(val)
        # 이번 행에 없는 컬럼은 직전 값으로 채움
        for col, val in previous.items():
            if col not in row and val is not None:
                row[col] = val
        rows.append(row)
        previous = {k: v for k, v in row.items() if k != "날짜"}

    # upsert (날짜 unique) — 미국 트랙의 행별 select+insert 대비 훨씬 빠르다
    #
    # 변동성 컬럼은 나중에 추가된 것이라, 스키마 설치(sql/kr/setup_kr.sql)를
    # 아직 실행하지 않은 DB 에서는 PostgREST 가 "column not found"(PGRST204)로 거절한다.
    # 그때 백필 전체를 실패시키는 대신 해당 컬럼만 빼고 한 번 더 시도한다.
    def _upsert(chunk: List[dict]):
        supabase.table(TABLE).upsert(chunk, on_conflict="날짜").execute()

    saved = 0
    drop_volatility = False
    try:
        for i in range(0, len(rows), 200):
            chunk = rows[i : i + 200]
            try:
                if drop_volatility:
                    chunk = [
                        {k: v for k, v in r.items() if k != VOLATILITY_COLUMN}
                        for r in chunk
                    ]
                _upsert(chunk)
            except Exception as e:
                if not drop_volatility and VOLATILITY_COLUMN in str(e):
                    logger.warning(
                        f"'{VOLATILITY_COLUMN}' 컬럼이 없어 제외하고 저장합니다. "
                        "sql/kr/setup_kr.sql 을 실행한 뒤 다시 수집하세요."
                    )
                    drop_volatility = True
                    chunk = [
                        {k: v for k, v in r.items() if k != VOLATILITY_COLUMN}
                        for r in chunk
                    ]
                    _upsert(chunk)
                else:
                    raise
            saved += len(chunk)
    except Exception as e:
        logger.error(f"국내 시장 데이터 저장 실패: {e}", exc_info=True)
        raise Exception(f"kr_economic_and_stock_data 저장 실패: {e}")

    msg = f"{saved}행 저장 완료 ({start_date} ~ {storage_end_str}, {len(merged.columns)}개 컬럼)"
    logger.info(msg)
    return {
        "success": True,
        "message": msg,
        "start_date": start_date,
        "end_date": storage_end_str,
        "saved_rows": saved,
    }


# ══════════════════════════════════════════════════════════════════
# 시장 환경 지표 (매수 게이트용)
# ══════════════════════════════════════════════════════════════════

def get_market_context() -> dict:
    """
    최신 시장 환경. 미국 트랙의 'VIX 조회'에 해당하는 국내판이다.

    한국에는 VIX 에 정확히 대응하는 무료 실시간 지수(VKOSPI)를 붙이기 번거로워,
    코스피 일별 수익률의 20일 실현변동성(연율%)을 공포 지표로 쓴다.

    2006~2026 실측 분포: 중위 15.0% / 75%ile 20.4% / 90%ile 29.7% / 95%ile 41.7%
    (리먼 2008-10 = 88.5%, 코로나 2020-03 = 69.9%)
    → kr_scoring 의 임계값 눈금(<20 평온 … >35 매수차단)이 이 분포와 정합한다.

    코스피200 이 아니라 코스피를 쓰는 이유: 히스토리가 가장 길고, 추종 ETF 교체 같은
    소스 변경에 영향을 받지 않는다. 글로벌 리스크 프록시로 미국 VIX 도 함께 반환한다.

    Returns:
        {"date", "kospi", "kospi200", "kospi_vol_20d", "usdkrw", "usdkrw_chg_20d", "vix"}
    """
    try:
        # 공백이 들어간 컬럼명("VIX 지수")을 select 문자열로 넘기면 PostgREST 인용 규칙에
        # 의존하게 된다. 60행이면 전체 select 비용이 무시할 수준이라 그냥 * 로 받는다.
        resp = (
            supabase.table(TABLE)
            .select("*")
            .order("날짜", desc=True)
            .limit(60)
            .execute()
        )
    except Exception as e:
        logger.warning(f"시장 환경 조회 실패: {e}")
        return {}

    data = resp.data or []
    if not data:
        return {}

    df = pd.DataFrame(data).sort_values("날짜")
    latest = df.iloc[-1]

    def _num(col: str) -> Optional[float]:
        try:
            v = latest.get(col)
            return None if v is None or pd.isna(v) else float(v)
        except (ValueError, TypeError):
            return None

    # 코스피 20일 실현변동성 (연율 %)
    #   1순위: 수집 시 적재해둔 컬럼 (거래일 기준으로 계산돼 가장 정확)
    #   2순위: 컬럼이 없거나 비었으면 여기서 즉석 계산 (마이그레이션 전 호환)
    vol_20d = _num(VOLATILITY_COLUMN)

    if vol_20d is None and "코스피" in df.columns:
        prices = pd.to_numeric(df["코스피"], errors="coerce").dropna()
        # 휴장일 ffill 로 생긴 변동 0 구간을 빼야 변동성이 과소평가되지 않는다
        returns = prices.pct_change().replace(0, pd.NA).dropna()
        if len(returns) >= 15:
            vol_20d = round(
                float(returns.tail(VOLATILITY_WINDOW).std() * (252 ** 0.5) * 100), 2
            )

    # 소스가 stale/오염되면 말도 안 되는 값이 나온다. 그때는 게이트를 끄는 편이
    # 낫다 (None → kr_scoring 이 기본 임계값을 쓰고 하드블록도 걸리지 않음).
    if vol_20d is not None and not (0 < vol_20d < 150):
        logger.warning(
            f"코스피 20일 실현변동성 {vol_20d}% — 비현실적 값이라 무시합니다. "
            "가격 시계열이 오염됐는지 확인하세요."
        )
        vol_20d = None

    # 원/달러 20일 변화율 — 원화 약세는 외국인 순매도 압력으로 이어진다
    usdkrw_chg = None
    if "원달러환율" in df.columns:
        fx = pd.to_numeric(df["원달러환율"], errors="coerce").dropna()
        if len(fx) >= 21:
            usdkrw_chg = round(float((fx.iloc[-1] / fx.iloc[-21] - 1) * 100), 2)

    return {
        "date": latest.get("날짜"),
        "kospi": _num("코스피"),
        "kospi200": _num("코스피200"),
        "kospi_vol_20d": vol_20d,
        "usdkrw": _num("원달러환율"),
        "usdkrw_chg_20d": usdkrw_chg,
        "vix": _num("VIX 지수"),
    }
