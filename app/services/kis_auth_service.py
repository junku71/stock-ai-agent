"""
한국투자증권(KIS) OpenAPI 접근 토큰 관리.

국내주식 주문/시세 API(app/services/kr/kis_domestic_service.py)가 쓰는 인증 계층이다.
KIS 는 토큰 발급에 **1분당 1회** 제한을 두므로, 발급받은 토큰을 메모리와 Supabase
(access_tokens 테이블) 양쪽에 캐싱하고 스레드 락으로 동시 갱신을 막는다.

  - 메모리 캐시: 프로세스 수명 동안 유효. 가장 빠른 경로.
  - DB 캐시:     재기동 후에도 살아남아 불필요한 재발급을 막는다.
  - 1분 스로틀:  마지막 발급 후 60초가 안 지났으면 남은 시간만큼 대기한다.

KIS_USE_MOCK 값에 따라 모의(kis_mock)/실전(kis_real) 토큰을 별도 슬롯에 캐싱하므로
모드를 바꿔도 이전 모드의 토큰이 섞이지 않는다.
"""
import logging
import re
import requests
import time
from datetime import datetime, timedelta
import pytz
from app.core.config import settings
from app.db.supabase import supabase
from threading import Lock

logger = logging.getLogger(__name__)
# 토큰 캐시 히트는 KIS 호출마다 찍혀 화면을 덮으므로 DEBUG — 루트는 INFO 라 화면(콘솔
# 핸들러)엔 안 뜨지만, 이 로거만 따로 DEBUG 로 열어둬서 파일 로그에는 그대로 남긴다.
logger.setLevel(logging.DEBUG)


def parse_expiration_date(date_str):
    """DB 에 저장된 만료시각 문자열 -> tz-aware datetime.

    Supabase 가 마이크로초를 5자리로 돌려주는 경우가 있어(%f 는 6자리를 기대) 먼저
    6자리로 패딩한 뒤 파싱한다. 어떤 형식으로도 못 읽으면 '이미 만료됨'으로 간주해
    재발급을 유도한다 (읽지 못한 토큰을 유효한 것으로 믿는 쪽이 더 위험하다).
    """
    try:
        if isinstance(date_str, str) and re.search(r"\.\d{5}\+", date_str):
            date_str = re.sub(r"\.(\d{5})\+", r".\g<1>0+", date_str)

        if isinstance(date_str, str):
            try:
                return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%f%z")
            except ValueError:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    return dt.replace(tzinfo=pytz.UTC)
                except ValueError:
                    pass
        # 이미 datetime 객체인 경우
        return date_str
    except Exception as e:
        logger.warning(f"날짜 파싱 오류: {e}")
        return datetime.now(pytz.UTC) - timedelta(seconds=1)


# 메모리 토큰 캐시 — mock/real 별도 슬롯
# (KIS_USE_MOCK 변경 시 캐시 충돌 방지)
_token_cache = {
    "kis_mock": {"access_token": None, "expires_at": None},
    "kis_real": {"access_token": None, "expires_at": None},
}
_last_refresh_time = {"kis_mock": 0, "kis_real": 0}
_refresh_lock = Lock()  # 동시성 방지 락


def _current_token_type() -> str:
    """현재 활성 모드의 토큰 타입 반환 (kis_mock 또는 kis_real)"""
    return "kis_mock" if settings.KIS_USE_MOCK else "kis_real"


def current_account_type() -> str:
    """kr_trade_records.account_type 컬럼용 — 'mock' or 'real'.
    KIS_USE_MOCK 에 따라 모의/실전 거래 기록 구분."""
    return "mock" if settings.KIS_USE_MOCK else "real"


def get_access_token(force_refresh: bool = False):
    """한국투자증권 API 접근 토큰 발급 또는 캐시된 토큰 반환.
    KIS_USE_MOCK 에 따라 mock/real 토큰을 분리해서 캐시/저장.

    force_refresh: True 면 메모리/DB 캐시된 만료시각을 무시하고 새 토큰을 발급.
    KIS가 "기간이 만료된 token"(EGW00123) 등으로 거부했는데 로컬 expires_at은
    아직 안 지난 경우(다른 프로세스가 먼저 재발급해 구 토큰이 서버측에서
    선(先) 무효화된 경우 등) 캐시만 믿으면 같은 무효 토큰을 계속 재사용하게 되므로 필요.
    """
    global _token_cache, _last_refresh_time

    token_type = _current_token_type()
    cache = _token_cache[token_type]
    now = datetime.now(pytz.UTC)

    if force_refresh:
        with _refresh_lock:
            record_id = None
            try:
                response = supabase.table("access_tokens") \
                    .select("id") \
                    .eq("token_type", token_type) \
                    .order("updated_at", desc=True) \
                    .limit(1).execute()
                if response.data:
                    record_id = response.data[0]["id"]
            except Exception as e:
                logger.warning(f"강제 갱신 전 기존 토큰 조회 오류 ({token_type}): {str(e)}")

            token = refresh_token_with_retry(token_type=token_type, record_id=record_id)
            cache["access_token"] = token
            cache["expires_at"] = now + timedelta(days=1)
            _last_refresh_time[token_type] = time.time()
            return token

    # 메모리 캐시 (해당 모드 슬롯) 가 유효하면 사용
    if cache["access_token"] and cache["expires_at"] and now < cache["expires_at"]:
        logger.debug(f"메모리 캐시 토큰 사용 ({token_type})")
        return cache["access_token"]

    # 1분 제한 체크 (모드 별 별도 카운트)
    current_time = time.time()
    last_refresh = _last_refresh_time[token_type]
    if current_time - last_refresh < 60:
        time_to_wait = 60 - (current_time - last_refresh)
        logger.info(f"1분 제한으로 {time_to_wait:.1f}초 대기 ({token_type})")
        time.sleep(time_to_wait)

    with _refresh_lock:
        # 락 획득 후 다시 캐시 확인
        if cache["access_token"] and cache["expires_at"] and now < cache["expires_at"]:
            logger.debug(f"락 내에서 캐시 토큰 사용 ({token_type})")
            return cache["access_token"]

        try:
            # DB 에서 해당 모드 토큰만 조회
            response = supabase.table("access_tokens") \
                .select("*") \
                .eq("token_type", token_type) \
                .order("updated_at", desc=True) \
                .limit(1).execute()

            if response.data:
                token_data = response.data[0]
                expiration_time = parse_expiration_date(token_data["expires_at"])

                if now < expiration_time:
                    logger.debug(f"DB 기존 토큰 사용 ({token_type}) - 만료까지: {(expiration_time - now)}")
                    cache["access_token"] = token_data["access_token"]
                    cache["expires_at"] = expiration_time
                    _last_refresh_time[token_type] = current_time
                    return token_data["access_token"]

                logger.info(f"토큰 만료됨, 갱신 필요 ({token_type})")
                token = refresh_token_with_retry(token_type=token_type, record_id=token_data["id"])
            else:
                logger.info(f"토큰 레코드 없음 ({token_type}), 새로 생성")
                token = refresh_token_with_retry(token_type=token_type)

            cache["access_token"] = token
            cache["expires_at"] = now + timedelta(days=1)
            _last_refresh_time[token_type] = current_time
            return token

        except Exception as e:
            logger.warning(f"토큰 조회 오류 ({token_type}): {str(e)}")
            if cache["access_token"]:
                logger.warning(f"DB 조회 오류 - 메모리 캐시 토큰 사용 ({token_type})")
                return cache["access_token"]
            raise Exception(f"토큰 발급 실패 ({token_type}): {str(e)}")


def refresh_token_with_retry(token_type: str = None, record_id=None, max_retries=3):
    """토큰 갱신을 재시도하며 처리.
    token_type: 'kis_mock' 또는 'kis_real' (생략 시 현재 활성 모드)
    """
    if token_type is None:
        token_type = _current_token_type()

    for attempt in range(max_retries):
        try:
            url = f"{settings.kis_base_url}/oauth2/tokenP"
            data = {
                "grant_type": "client_credentials",
                "appkey": settings.KIS_APPKEY,
                "appsecret": settings.KIS_APPSECRET
            }

            response = requests.post(url, json=data)
            response_data = response.json()

            if 'access_token' not in response_data:
                raise Exception(f"토큰 발급 실패: {response_data}")

            access_token = response_data["access_token"]
            expires_in = response_data.get("expires_in", 86400)
            now = datetime.now(pytz.UTC)
            expiration_time = now + timedelta(seconds=expires_in)

            token_data = {
                "token_type": token_type,
                "access_token": access_token,
                "expires_at": expiration_time.isoformat(),
            }

            if record_id:
                supabase.table("access_tokens").update(token_data).eq("id", record_id).execute()
                logger.info(f"토큰 업데이트 완료 ({token_type})")
            else:
                supabase.table("access_tokens").insert(token_data).execute()
                logger.info(f"새 토큰 레코드 생성 완료 ({token_type})")

            return access_token

        except Exception as e:
            logger.warning(f"토큰 갱신 오류 ({token_type}, 시도 {attempt+1}/{max_retries}): {str(e)}")
            if "EGW00133" in str(e) and attempt < max_retries - 1:
                logger.warning("1분 제한 에러, 61초 대기 후 재시도")
                time.sleep(61)
            else:
                raise

