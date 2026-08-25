"""
한국투자증권 국내주식 API 래퍼.

국내주식 전용 TR_ID·파라미터 체계를 다룬다. 토큰 발급/캐싱은
kis_auth_service.get_access_token 을 재사용
(동일 앱키로 국내/해외 모두 호출 가능).

TR_ID 출처: github.com/koreainvestment/open-trading-api
            examples_user/domestic_stock/domestic_stock_functions.py

| 기능             | 엔드포인트                                       | 실전        | 모의        |
|------------------|--------------------------------------------------|-------------|-------------|
| 주문(현금) 매수  | /trading/order-cash                              | TTTC0012U   | VTTC0012U   |
| 주문(현금) 매도  | /trading/order-cash                              | TTTC0011U   | VTTC0011U   |
| 잔고             | /trading/inquire-balance                         | TTTC8434R   | VTTC8434R   |
| 매수가능금액     | /trading/inquire-psbl-order                      | TTTC8908R   | VTTC8908R   |
| 현재가           | /quotations/inquire-price                        | FHKST01010100 (공통)      |
| 기간별시세(일봉) | /quotations/inquire-daily-itemchartprice         | FHKST03010100 (공통)      |
| 업종 기간별시세  | /quotations/inquire-daily-indexchartprice        | FHKUP03500100 (공통)      |
| 업종 현재지수    | /quotations/inquire-index-price                  | FHPUP02100000 (실전만)    |
| 투자자매매동향   | /quotations/inquire-investor-daily-by-market     | FHPTJ04040000 (실전만)    |
| 재무비율         | /finance/financial-ratio                         | FHKST66430300 (실전만)    |
| 휴장일조회       | /quotations/chk-holiday                          | CTCA0903R (1일 1회 권장)  |
"""
import logging
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional

import pytz
import requests

from app.core.config import settings
from app.services.kis_auth_service import get_access_token, current_account_type  # noqa: F401

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

# KIS 는 초당 호출 건수를 제한한다 (실전 20건/초, 모의 2건/초).
# 모의투자 기준으로 안전하게 0.6초 간격을 강제한다.
_MIN_INTERVAL_SEC = 0.6
_last_call_at = 0.0
_call_lock = Lock()

# 업종 코드 (FID_INPUT_ISCD, FID_COND_MRKT_DIV_CODE="U")
INDEX_KOSPI = "0001"
INDEX_KOSPI200 = "2001"
INDEX_KOSDAQ = "1001"


def _throttle():
    """KIS 초당 호출 제한 회피 — 전역 최소 간격 보장."""
    global _last_call_at
    with _call_lock:
        gap = time.time() - _last_call_at
        if gap < _MIN_INTERVAL_SEC:
            time.sleep(_MIN_INTERVAL_SEC - gap)
        _last_call_at = time.time()


def _is_mock() -> bool:
    return settings.KIS_USE_MOCK


def _tr(real: str, mock: str) -> str:
    return mock if _is_mock() else real


def _request(
    path: str,
    tr_id: str,
    params: dict,
    post: bool = False,
    max_retries: int = 2,
) -> dict:
    """
    KIS 국내주식 API 공통 호출.

    - rt_cd != "0" 이면 1회 재시도 (초당 제한이면 대기, 그 외엔 토큰 강제 갱신)
    - 예외는 삼키지 않고 rt_cd="1" 형태의 dict 로 정규화해 반환 (호출부 분기 단순화)
    """
    url = f"{settings.kis_base_url}{path}"

    for attempt in range(max_retries):
        try:
            access_token = get_access_token()
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "authorization": f"Bearer {access_token}",
                "appkey": settings.KIS_APPKEY,
                "appsecret": settings.KIS_APPSECRET,
                "tr_id": tr_id,
                "custtype": "P",  # 개인
            }

            _throttle()
            if post:
                resp = requests.post(url, headers=headers, json=params, timeout=15)
            else:
                resp = requests.get(url, headers=headers, params=params, timeout=15)

            if resp.status_code != 200:
                logger.warning(f"KIS {tr_id} HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries - 1:
                    time.sleep(1.5)
                    continue
                return {"rt_cd": "1", "msg_cd": f"HTTP_{resp.status_code}", "msg1": resp.text[:200]}

            result = resp.json()

            if result.get("rt_cd") != "0" and attempt < max_retries - 1:
                msg1 = result.get("msg1", "")
                logger.warning(f"KIS {tr_id} 오류: {result.get('msg_cd')} - {msg1}. 재시도")
                if "초당" in msg1:
                    time.sleep(2)
                else:
                    # 토큰 만료/무효 가능성 → 강제 갱신 후 재시도
                    get_access_token(force_refresh=True)
                    time.sleep(1)
                continue

            return result

        except Exception as e:
            logger.error(f"KIS {tr_id} 호출 예외: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return {"rt_cd": "1", "msg_cd": "EXCEPTION", "msg1": str(e)}

    return {"rt_cd": "1", "msg_cd": "RETRY_EXHAUSTED", "msg1": "재시도 소진"}


# ══════════════════════════════════════════════════════════════════
# 호가가격단위 (유가증권시장, 2023-01-25 개정)
# ══════════════════════════════════════════════════════════════════

# (상한 미만, 호가단위) — 오름차순
_TICK_TABLE = [
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
]
_TICK_ABOVE = 1_000  # 500,000원 이상


def tick_size(price: float) -> int:
    """주가에 해당하는 호가가격단위(원)."""
    for upper, tick in _TICK_TABLE:
        if price < upper:
            return tick
    return _TICK_ABOVE


def round_to_tick(price: float, mode: str = "down") -> int:
    """
    지정가 주문가를 호가단위에 맞춰 정규화한다.
    호가단위에 안 맞는 가격은 KIS 가 주문을 거부하므로 반드시 통과시켜야 한다.

    Args:
        price: 원 단위 가격
        mode:  "down" 매수(불리하지 않게 내림) / "up" 매도(올림) / "nearest"
    """
    if price <= 0:
        return 0
    t = tick_size(price)
    if mode == "up":
        return int(-(-price // t) * t)
    if mode == "nearest":
        return int(round(price / t) * t)
    return int(price // t * t)


# ══════════════════════════════════════════════════════════════════
# 장 운영 시간 / 휴장일
# ══════════════════════════════════════════════════════════════════

_holiday_cache: Dict[str, bool] = {}  # {"YYYYMMDD": 개장여부}
_holiday_cache_date: Optional[str] = None


def is_business_day(date: Optional[datetime] = None) -> bool:
    """
    KRX 개장일 여부. chk-holiday(CTCA0903R) 는 1일 1회 호출 권장이라 결과를 캐시한다.
    API 실패 시 주말 여부로 폴백한다(임시공휴일은 놓칠 수 있으나 주문은 거래소가 거부).
    """
    global _holiday_cache, _holiday_cache_date

    now = date or datetime.now(KST)
    ymd = now.strftime("%Y%m%d")

    if ymd in _holiday_cache:
        return _holiday_cache[ymd]

    today_ymd = datetime.now(KST).strftime("%Y%m%d")

    # CTCA0903R 은 실전 전용 TR 이다. 모의투자 계좌로 호출하면 매번
    # "모의투자 TR 이 아닙니다"(EGW02006) 로 실패하는데, 매도 스케줄러가 1분마다
    # 돌기 때문에 그대로 두면 하루 1,400회 헛호출 + 로그 오염이 된다.
    # 모의 모드에서는 아예 호출하지 않고 주말 판정으로만 간다.
    # (임시공휴일은 못 잡지만, 그날 주문은 거래소가 거부하므로 실질 피해는 없다)
    if _is_mock():
        if _holiday_cache_date != today_ymd:
            _holiday_cache_date = today_ymd
            logger.info("모의투자 모드 — 휴장일 조회(CTCA0903R) 생략, 주말 판정으로 대체")
        return now.weekday() < 5

    # 하루 한 번만 조회 — 오늘 기준으로 향후 일정까지 한 번에 받아 캐시
    if _holiday_cache_date != today_ymd:
        result = _request(
            "/uapi/domestic-stock/v1/quotations/chk-holiday",
            "CTCA0903R",
            {"BASS_DT": today_ymd, "CTX_AREA_FK": "", "CTX_AREA_NK": ""},
        )
        # 성공/실패 무관하게 오늘 시도했음을 기록한다 (실패 시 1분마다 재시도 방지)
        _holiday_cache_date = today_ymd
        if result.get("rt_cd") == "0":
            rows = result.get("output", [])
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                d = row.get("bass_dt")
                if d:
                    _holiday_cache[d] = row.get("opnd_yn") == "Y"
            logger.info(f"KRX 휴장일 캘린더 {len(rows)}일치 캐시 완료")
        else:
            logger.warning(f"휴장일 조회 실패 → 주말 판정으로 폴백: {result.get('msg1')}")

    if ymd in _holiday_cache:
        return _holiday_cache[ymd]
    return now.weekday() < 5  # 폴백: 월~금


def is_market_open(now: Optional[datetime] = None) -> bool:
    """KRX 정규장(09:00~15:30 KST) 개장 중 여부."""
    now = now or datetime.now(KST)
    if not is_business_day(now):
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 30


def is_sell_window(now: Optional[datetime] = None) -> bool:
    """
    자동 매도 판단 허용 구간 (09:00~15:20 KST).
    15:20~15:30 은 종가 단일가(동시호가)라 지정가 주문의 체결 성격이 달라지므로 제외한다.
    """
    now = now or datetime.now(KST)
    if not is_business_day(now):
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 20


# ══════════════════════════════════════════════════════════════════
# 시세
# ══════════════════════════════════════════════════════════════════

def get_price(code: str) -> dict:
    """
    주식현재가 시세 (FHKST01010100).

    output 주요 필드:
      stck_prpr(현재가), stck_oprc(시가), stck_hgpr(고가), stck_lwpr(저가),
      prdy_vrss(전일대비), prdy_ctrt(전일대비율), acml_vol(누적거래량),
      acml_tr_pbmn(누적거래대금), per, pbr, eps, bps, hts_avls(시가총액),
      w52_hgpr / w52_lwpr(52주 최고/최저), stck_mxpr(상한가), stck_llam(하한가)
    """
    return _request(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )


def get_current_price_value(code: str) -> Optional[float]:
    """현재가만 숫자로. 조회 실패 시 None."""
    result = get_price(code)
    if result.get("rt_cd") != "0":
        logger.error(f"{code} 현재가 조회 실패: {result.get('msg1')}")
        return None
    try:
        price = float(result.get("output", {}).get("stck_prpr", 0) or 0)
        return price if price > 0 else None
    except (ValueError, TypeError):
        return None


def get_daily_chart(
    code: str,
    start_date: str,
    end_date: str,
    period: str = "D",
    adjusted: bool = True,
) -> List[dict]:
    """
    국내주식 기간별시세 (FHKST03010100). 1회 최대 100건.

    Args:
        start_date/end_date: "YYYYMMDD"
        period: D(일) W(주) M(월) Y(년)
        adjusted: True 면 수정주가(FID_ORG_ADJ_PRC="0")

    Returns:
        output2 리스트를 '최신일이 index 0' 순서로 반환.
        각 항목: stck_bsop_date(일자), stck_clpr(종가), stck_oprc(시가),
                 stck_hgpr(고가), stck_lwpr(저가), acml_vol(거래량), acml_tr_pbmn(거래대금)
    """
    result = _request(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "FHKST03010100",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
        },
    )
    if result.get("rt_cd") != "0":
        logger.error(f"{code} 일봉 조회 실패: {result.get('msg1')}")
        return []

    rows = [r for r in (result.get("output2") or []) if r.get("stck_bsop_date")]
    rows.sort(key=lambda r: r["stck_bsop_date"], reverse=True)  # 최신일 우선
    return rows


def get_recent_daily_chart(code: str, days: int = 100) -> List[dict]:
    """최근 N영업일 일봉 (ATR/ADX/거래량비율 계산용). 100건 상한이라 days<=100 권장."""
    end = datetime.now(KST)
    # 주말/공휴일을 감안해 넉넉히 역산 (영업일 ≈ 달력일 × 0.69)
    start = end - timedelta(days=int(days * 1.6) + 10)
    return get_daily_chart(code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))


def get_index_daily(index_code: str, start_date: str, end_date: str) -> List[dict]:
    """
    국내업종 기간별시세 (FHKUP03500100). KOSPI=0001, KOSPI200=2001, KOSDAQ=1001.

    Returns: output2 (최신일 우선). bstp_nmix_prpr(지수), acml_vol, acml_tr_pbmn 등.
    """
    result = _request(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
        "FHKUP03500100",
        {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
        },
    )
    if result.get("rt_cd") != "0":
        logger.warning(f"지수({index_code}) 기간별시세 조회 실패: {result.get('msg1')}")
        return []
    rows = [r for r in (result.get("output2") or []) if r.get("stck_bsop_date")]
    rows.sort(key=lambda r: r["stck_bsop_date"], reverse=True)
    return rows


def get_index_price(index_code: str = INDEX_KOSPI) -> dict:
    """국내업종 현재지수 (FHPUP02100000). ※ 모의투자 미지원."""
    return _request(
        "/uapi/domestic-stock/v1/quotations/inquire-index-price",
        "FHPUP02100000",
        {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": index_code},
    )


def get_investor_trend_by_market(date_ymd: str, market: str = "KSP") -> List[dict]:
    """
    시장별 투자자매매동향(일별) (FHPTJ04040000). ※ 모의투자 미지원.

    Args:
        date_ymd: "YYYYMMDD"
        market:   KSP(코스피) / KSQ(코스닥)

    한국 시장에서 외국인·기관 순매수는 가장 검증된 수급 신호라 매수 판단에 반영한다.
    """
    result = _request(
        "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market",
        "FHPTJ04040000",
        {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": INDEX_KOSPI,
            "FID_INPUT_DATE_1": date_ymd,
            "FID_INPUT_ISCD_1": market,
            "FID_INPUT_DATE_2": date_ymd,
            "FID_INPUT_ISCD_2": INDEX_KOSPI,
        },
    )
    if result.get("rt_cd") != "0":
        logger.warning(f"투자자매매동향 조회 실패({date_ymd}): {result.get('msg1')}")
        return []
    rows = result.get("output") or []
    return rows if isinstance(rows, list) else [rows]


def get_stock_investor_trend(code: str) -> List[dict]:
    """
    종목별 투자자매매동향 (FHKST01010900, 주식현재가 투자자). 모의투자에서도 동작한다.

    최근 며칠치 일별 순매수를 최신일 우선으로 돌려준다.
    주요 필드:
      stck_bsop_date(일자), prsn_ntby_qty(개인 순매수량),
      frgn_ntby_qty(외국인 순매수량), orgn_ntby_qty(기관 순매수량),
      prsn_ntby_tr_pbmn / frgn_ntby_tr_pbmn / orgn_ntby_tr_pbmn(순매수 대금, 백만원)

    ※ 당일 데이터는 장 종료 후에만 제공된다.
    ※ 외국인 = 외국인투자등록번호 보유분 + 기타 외국인.
    """
    result = _request(
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        "FHKST01010900",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )
    if result.get("rt_cd") != "0":
        logger.warning(f"{code} 투자자매매동향 조회 실패: {result.get('msg1')}")
        return []
    rows = result.get("output") or []
    if isinstance(rows, dict):
        rows = [rows]
    rows = [r for r in rows if r.get("stck_bsop_date")]
    rows.sort(key=lambda r: r["stck_bsop_date"], reverse=True)
    return rows


def get_financial_ratio(code: str, quarterly: bool = True) -> List[dict]:
    """
    국내주식 재무비율 (FHKST66430300). ※ 모의투자 미지원.

    output: ROE(roe_val), 매출증가율(grs), 영업이익증가율(bsop_prfi_inrt),
            EPS, BPS, 부채비율(lblt_rate), 유보비율 등 (최근 결산 순)
    """
    result = _request(
        "/uapi/domestic-stock/v1/finance/financial-ratio",
        "FHKST66430300",
        {
            "FID_DIV_CLS_CODE": "1" if quarterly else "0",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": code,
        },
    )
    if result.get("rt_cd") != "0":
        return []
    rows = result.get("output") or []
    return rows if isinstance(rows, list) else [rows]


# ══════════════════════════════════════════════════════════════════
# 계좌 / 주문
# ══════════════════════════════════════════════════════════════════

def get_balance() -> dict:
    """
    주식잔고조회 (TTTC8434R / VTTC8434R).

    Returns:
        {"rt_cd", "output1": [보유종목...], "output2": [계좌요약]}
        output1 항목: pdno(종목코드), prdt_name(종목명), hldg_qty(보유수량),
                      ord_psbl_qty(주문가능수량), pchs_avg_pric(매입평균가),
                      prpr(현재가), evlu_amt(평가금액), evlu_pfls_amt(평가손익),
                      evlu_pfls_rt(수익률), pchs_amt(매입금액)
        output2 항목: dnca_tot_amt(예수금), prvs_rcdl_excc_amt(D+2예수금),
                      tot_evlu_amt(총평가금액), nass_amt(순자산)
    """
    result = _request(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        _tr("TTTC8434R", "VTTC8434R"),
        {
            "CANO": settings.KIS_CANO,
            "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",          # 종목별
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",          # 전일매매포함
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    if result.get("rt_cd") != "0":
        return {"rt_cd": "1", "msg1": result.get("msg1", ""), "output1": [], "output2": []}

    # 보유수량 0 인 잔재 레코드 제거 (당일 전량매도 후 D+2 까지 남아있음)
    holdings = [h for h in (result.get("output1") or []) if int(h.get("hldg_qty", 0) or 0) > 0]
    summary = result.get("output2") or []
    return {"rt_cd": "0", "msg1": result.get("msg1", ""), "output1": holdings, "output2": summary}


def get_orderable_cash(code: str, price: float) -> Optional[float]:
    """
    매수가능금액 조회 (TTTC8908R / VTTC8908R) → 주문가능현금(원).

    ord_psbl_cash 를 우선 쓰고, 없으면 nrcvb_buy_amt(미수없는매수금액)로 폴백한다.
    """
    result = _request(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        _tr("TTTC8908R", "VTTC8908R"),
        {
            "CANO": settings.KIS_CANO,
            "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
            "PDNO": code,
            "ORD_UNPR": str(int(price)),
            "ORD_DVSN": "00",              # 지정가
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        },
    )
    if result.get("rt_cd") != "0":
        logger.error(f"{code} 매수가능금액 조회 실패: {result.get('msg1')}")
        return None

    out = result.get("output", {}) or {}
    for key in ("ord_psbl_cash", "nrcvb_buy_amt", "max_buy_amt"):
        try:
            v = float(out.get(key, 0) or 0)
            if v > 0:
                return v
        except (ValueError, TypeError):
            continue
    return 0.0


def order_stock(code: str, quantity: int, price: int, is_buy: bool) -> dict:
    """
    국내주식 현금 주문 (지정가).

    실전: 매수 TTTC0012U / 매도 TTTC0011U
    모의: 매수 VTTC0012U / 매도 VTTC0011U
    ※ EXCG_ID_DVSN_CD="KRX" 는 필수 파라미터다 (누락 시 주문 거부).

    KR_DRY_RUN=true 면 실제 호출 없이 성공 응답을 흉내낸다.
    """
    label = "매수" if is_buy else "매도"

    if settings.KR_DRY_RUN:
        logger.warning(
            f"[DRY_RUN] {code} {label} 주문 스킵: {quantity}주 @ {price:,}원 "
            f"(총 {quantity * price:,}원)"
        )
        return {
            "rt_cd": "0",
            "msg_cd": "DRY_RUN",
            "msg1": "드라이런 — 실제 주문 미전송",
            "output": {"ODNO": "DRYRUN", "ORD_TMD": datetime.now(KST).strftime("%H%M%S")},
        }

    if is_buy:
        tr_id = _tr("TTTC0012U", "VTTC0012U")
    else:
        tr_id = _tr("TTTC0011U", "VTTC0011U")

    body = {
        "CANO": settings.KIS_CANO,
        "ACNT_PRDT_CD": settings.KIS_ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "00",               # 00: 지정가
        "ORD_QTY": str(int(quantity)),
        "ORD_UNPR": str(int(price)),
        "EXCG_ID_DVSN_CD": "KRX",       # 필수
        "SLL_TYPE": "" if is_buy else "01",  # 01: 일반매도
        "CNDT_PRIC": "",
    }

    logger.info(f"{code} {label} 주문: {quantity}주 @ {price:,}원 (tr_id={tr_id})")
    result = _request(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id,
        body,
        post=True,
        max_retries=1,  # 주문은 중복 체결 위험이 있어 자동 재시도하지 않는다
    )

    if result.get("rt_cd") == "0":
        odno = (result.get("output") or {}).get("ODNO", "")
        logger.info(f"{code} {label} 주문 접수 성공 (주문번호 {odno})")
    else:
        logger.error(f"{code} {label} 주문 실패: {result.get('msg_cd')} {result.get('msg1')}")
    return result


def get_account_summary() -> dict:
    """
    계좌 요약 (총평가·예수금·손익). 잔고 API output2 를 정규화해서 반환.
    조회 실패 시 빈 dict.
    """
    balance = get_balance()
    if balance.get("rt_cd") != "0":
        return {}
    rows = balance.get("output2") or []
    if not rows:
        return {}
    row = rows[0]

    def _f(key: str) -> float:
        try:
            return float(row.get(key, 0) or 0)
        except (ValueError, TypeError):
            return 0.0

    pchs = _f("pchs_amt_smtl_amt")
    evlu = _f("evlu_amt_smtl_amt")
    pnl = _f("evlu_pfls_smtl_amt")
    return {
        "deposit": _f("dnca_tot_amt"),              # 예수금
        "d2_deposit": _f("prvs_rcdl_excc_amt"),     # D+2 예수금 (실제 가용)
        "stock_purchase_amount": pchs,              # 매입금액 합계
        "stock_eval_amount": evlu,                  # 평가금액 합계
        "eval_profit_loss": pnl,                    # 평가손익 합계
        "eval_profit_loss_pct": (pnl / pchs * 100) if pchs > 0 else 0.0,
        "total_eval_amount": _f("tot_evlu_amt"),    # 총평가금액 (예수금 포함)
        "net_asset": _f("nass_amt"),                # 순자산
    }
