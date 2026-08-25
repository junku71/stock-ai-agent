"""
KOSPI 100 종목 유니버스.

선정 기준: 코스피 시가총액 상위 100개 보통주
  - 우선주 제외 (보통주와 신호가 중복되고 유동성이 낮음)
  - ETF / 리츠 / 스팩 제외 (개별 기업 분석 대상이 아님)
  - 기준일: 2026-08-22 (네이버 금융 시가총액 순위), 업종도 같은 출처

리밸런싱:
  분기 1회 정도 수동 갱신을 전제로 한 고정 리스트다.
  종목이 바뀌면 ML 학습 컬럼(kr_economic_and_stock_data)도 함께 바뀌므로
  자동 리밸런싱은 의도적으로 넣지 않았다.

  ★ 유니버스를 바꿀 때 반드시 함께 고쳐야 하는 3곳:
      1. 이 파일의 UNIVERSE
      2. sql/kr/setup_kr.sql 의 종목 종가 컬럼
      3. kaggle_notebook_kr/predict_kr.py 의 TARGET_COLUMNS

sector 는 LLM 검토 단계의 '동일 섹터 집중' 판단에만 쓰인다 (매매 로직 무관).
"""
from typing import Dict, List, Optional

# (종목코드, 종목명, 업종) — 시가총액 내림차순
UNIVERSE: List[Dict[str, str]] = [
    {"code": "005930", "name": "삼성전자", "sector": "반도체와반도체장비"},
    {"code": "000660", "name": "SK하이닉스", "sector": "반도체와반도체장비"},
    {"code": "402340", "name": "SK스퀘어", "sector": "반도체와반도체장비"},
    {"code": "009150", "name": "삼성전기", "sector": "전자장비와기기"},
    {"code": "005380", "name": "현대차", "sector": "자동차"},
    {"code": "373220", "name": "LG에너지솔루션", "sector": "전기제품"},
    {"code": "207940", "name": "삼성바이오로직스", "sector": "제약"},
    {"code": "032830", "name": "삼성생명", "sector": "생명보험"},
    {"code": "028260", "name": "삼성물산", "sector": "복합기업"},
    {"code": "105560", "name": "KB금융", "sector": "은행"},
    {"code": "012450", "name": "한화에어로스페이스", "sector": "우주항공과국방"},
    {"code": "000270", "name": "기아", "sector": "자동차"},
    {"code": "055550", "name": "신한지주", "sector": "은행"},
    {"code": "329180", "name": "HD현대중공업", "sector": "조선"},
    {"code": "034020", "name": "두산에너빌리티", "sector": "기계"},
    {"code": "012330", "name": "현대모비스", "sector": "자동차부품"},
    {"code": "068270", "name": "셀트리온", "sector": "제약"},
    {"code": "034730", "name": "SK", "sector": "석유와가스"},
    {"code": "006400", "name": "삼성SDI", "sector": "전기제품"},
    {"code": "086790", "name": "하나금융지주", "sector": "은행"},
    {"code": "035420", "name": "NAVER", "sector": "양방향미디어와서비스"},
    {"code": "066570", "name": "LG전자", "sector": "전자제품"},
    {"code": "000810", "name": "삼성화재", "sector": "손해보험"},
    {"code": "010120", "name": "LS ELECTRIC", "sector": "전기장비"},
    {"code": "267260", "name": "HD현대일렉트릭", "sector": "전기장비"},
    {"code": "010130", "name": "고려아연", "sector": "비철금속"},
    {"code": "042660", "name": "한화오션", "sector": "조선"},
    {"code": "298040", "name": "효성중공업", "sector": "전기장비"},
    {"code": "009540", "name": "HD한국조선해양", "sector": "조선"},
    {"code": "005490", "name": "POSCO홀딩스", "sector": "철강"},
    {"code": "316140", "name": "우리금융지주", "sector": "은행"},
    {"code": "017670", "name": "SK텔레콤", "sector": "무선통신서비스"},
    {"code": "096770", "name": "SK이노베이션", "sector": "석유와가스"},
    {"code": "015760", "name": "한국전력", "sector": "전기유틸리티"},
    {"code": "011200", "name": "HMM", "sector": "해운사"},
    {"code": "042700", "name": "한미반도체", "sector": "반도체와반도체장비"},
    {"code": "138040", "name": "메리츠금융지주", "sector": "증권"},
    {"code": "006800", "name": "미래에셋증권", "sector": "증권"},
    {"code": "033780", "name": "KT&G", "sector": "담배"},
    {"code": "051910", "name": "LG화학", "sector": "화학"},
    {"code": "010140", "name": "삼성중공업", "sector": "조선"},
    {"code": "018260", "name": "삼성에스디에스", "sector": "IT서비스"},
    {"code": "000150", "name": "두산", "sector": "복합기업"},
    {"code": "267250", "name": "HD현대", "sector": "조선"},
    {"code": "003550", "name": "LG", "sector": "복합기업"},
    {"code": "024110", "name": "기업은행", "sector": "은행"},
    {"code": "010950", "name": "S-Oil", "sector": "석유와가스"},
    {"code": "035720", "name": "카카오", "sector": "양방향미디어와서비스"},
    {"code": "079550", "name": "LIG디펜스앤에어로스페이스", "sector": "우주항공과국방"},
    {"code": "086280", "name": "현대글로비스", "sector": "항공화물운송과물류"},
    {"code": "278470", "name": "에이피알", "sector": "화장품"},
    {"code": "064350", "name": "현대로템", "sector": "우주항공과국방"},
    {"code": "003670", "name": "포스코퓨처엠", "sector": "화학"},
    {"code": "272210", "name": "한화시스템", "sector": "우주항공과국방"},
    {"code": "030200", "name": "KT", "sector": "다각화된통신서비스"},
    {"code": "011070", "name": "LG이노텍", "sector": "전자장비와기기"},
    {"code": "047810", "name": "한국항공우주", "sector": "우주항공과국방"},
    {"code": "307950", "name": "현대오토에버", "sector": "IT서비스"},
    {"code": "000720", "name": "현대건설", "sector": "건설"},
    {"code": "005830", "name": "DB손해보험", "sector": "손해보험"},
    {"code": "078930", "name": "GS", "sector": "석유와가스"},
    {"code": "003230", "name": "삼양식품", "sector": "식품"},
    {"code": "071050", "name": "한국금융지주", "sector": "증권"},
    {"code": "259960", "name": "크래프톤", "sector": "게임엔터테인먼트"},
    {"code": "323410", "name": "카카오뱅크", "sector": "은행"},
    {"code": "005940", "name": "NH투자증권", "sector": "증권"},
    {"code": "003490", "name": "대한항공", "sector": "항공사"},
    {"code": "006260", "name": "LS", "sector": "전기장비"},
    {"code": "047050", "name": "포스코인터내셔널", "sector": "무역회사와판매업체"},
    {"code": "443060", "name": "HD현대마린솔루션", "sector": "조선"},
    {"code": "028050", "name": "삼성E&A", "sector": "건설"},
    {"code": "016360", "name": "삼성증권", "sector": "증권"},
    {"code": "161390", "name": "한국타이어앤테크놀로지", "sector": "자동차부품"},
    {"code": "090430", "name": "아모레퍼시픽", "sector": "화장품"},
    {"code": "180640", "name": "한진칼", "sector": "항공사"},
    {"code": "007660", "name": "이수페타시스", "sector": "반도체와반도체장비"},
    {"code": "352820", "name": "하이브", "sector": "방송과엔터테인먼트"},
    {"code": "009830", "name": "한화솔루션", "sector": "에너지장비및서비스"},
    {"code": "039490", "name": "키움증권", "sector": "증권"},
    {"code": "064400", "name": "LG씨엔에스", "sector": "IT서비스"},
    {"code": "021240", "name": "코웨이", "sector": "가정용기기와용품"},
    {"code": "000100", "name": "유한양행", "sector": "제약"},
    {"code": "326030", "name": "SK바이오팜", "sector": "제약"},
    {"code": "047040", "name": "대우건설", "sector": "건설"},
    {"code": "032640", "name": "LG유플러스", "sector": "무선통신서비스"},
    {"code": "000880", "name": "한화", "sector": "복합기업"},
    {"code": "377300", "name": "카카오페이", "sector": "IT서비스"},
    {"code": "241560", "name": "두산밥캣", "sector": "기계"},
    {"code": "267270", "name": "HD건설기계", "sector": "기계"},
    {"code": "029780", "name": "삼성카드", "sector": "카드"},
    {"code": "128940", "name": "한미약품", "sector": "제약"},
    {"code": "001440", "name": "대한전선", "sector": "전기장비"},
    {"code": "353200", "name": "대덕전자", "sector": "전자장비와기기"},
    {"code": "062040", "name": "산일전기", "sector": "전기장비"},
    {"code": "175330", "name": "JB금융지주", "sector": "은행"},
    {"code": "271560", "name": "오리온", "sector": "식품"},
    {"code": "010060", "name": "OCI홀딩스", "sector": "화학"},
    {"code": "036570", "name": "NC", "sector": "게임엔터테인먼트"},
    {"code": "088350", "name": "한화생명", "sector": "생명보험"},
    {"code": "034220", "name": "LG디스플레이", "sector": "디스플레이패널"},
]

# 하위 호환 별칭 (기존 코드가 KOSPI30 을 참조하던 자리)
KOSPI100 = UNIVERSE

# 코드 ↔ 이름 양방향 매핑
CODE_TO_NAME: Dict[str, str] = {s["code"]: s["name"] for s in UNIVERSE}
NAME_TO_CODE: Dict[str, str] = {s["name"]: s["code"] for s in UNIVERSE}
CODE_TO_SECTOR: Dict[str, str] = {s["code"]: s["sector"] for s in UNIVERSE}

# yfinance 티커 (과거 종가 백필용 — .KS 접미사)
CODE_TO_YF: Dict[str, str] = {s["code"]: f"{s['code']}.KS" for s in UNIVERSE}

ALL_CODES: List[str] = [s["code"] for s in UNIVERSE]
ALL_NAMES: List[str] = [s["name"] for s in UNIVERSE]


# ══════════════════════════════════════════════════════════════════
# 뉴스 검색어
#   종목명을 그대로 넣으면 오탐이 많은 종목이 있다.
#     - "SK", "LG", "GS"  → 그룹사 전체 기사
#     - "기아"            → '기아(饑餓)', 스포츠 구단 기사
#     - "한화", "두산"    → 스포츠 구단 기사
#     - "NC"              → 게임사가 아닌 다른 맥락
#   짧은 이름은 규칙으로 보정하고, 규칙만으로 부족한 것만 명시 오버라이드한다.
# ══════════════════════════════════════════════════════════════════

# 규칙으로 잡히지 않는 종목만 개별 지정
NEWS_QUERY_OVERRIDE: Dict[str, str] = {
    "035420": "네이버 주가",          # NAVER — 서비스 장애 기사가 대부분
    "010120": "LS일렉트릭",           # LS ELECTRIC — 영문 표기로는 기사가 안 잡힘
    "028260": "삼성물산 주가",
    "402340": "SK스퀘어 주가",
    "000270": "기아 자동차 실적",
    "034020": "두산에너빌리티 주가",
    "241560": "두산밥캣 주가",
    "180640": "한진칼 주가",
    "036570": "엔씨소프트 주가",       # NC
}

# 이 길이 이하의 종목명은 단독 검색 시 오탐이 많아 " 주가" 를 붙인다
_SHORT_NAME_LEN = 3


def news_query(code: str) -> str:
    """종목코드 → 네이버 뉴스 검색어."""
    if code in NEWS_QUERY_OVERRIDE:
        return NEWS_QUERY_OVERRIDE[code]
    name = CODE_TO_NAME.get(code)
    if not name:
        return code
    if len(name) <= _SHORT_NAME_LEN:
        return f"{name} 주가"
    return name


def news_query_for(code: str, name: Optional[str] = None) -> str:
    """
    news_query() 의 동적 유니버스판 — 고정 유니버스에 없는 종목도 처리한다.

    동적 후보군(kr_universe_service)은 200종목이라 고정 리스트(100종목) 밖 종목이 절반쯤
    된다. 그 종목들은 CODE_TO_NAME 에 없으므로 이름을 인자로 받아 같은 규칙을 적용한다.
    """
    if code in NEWS_QUERY_OVERRIDE:
        return NEWS_QUERY_OVERRIDE[code]
    resolved = name or CODE_TO_NAME.get(code)
    if not resolved:
        return code
    if len(resolved) <= _SHORT_NAME_LEN:
        return f"{resolved} 주가"
    return resolved


def sector(code: str) -> Optional[str]:
    """종목코드 → 업종. 고정 유니버스 밖이면 None."""
    return CODE_TO_SECTOR.get(code)


def resolve(code_or_name: str) -> Optional[str]:
    """종목코드 또는 종목명을 받아 종목코드로 정규화. 유니버스 밖이면 None."""
    if code_or_name in CODE_TO_NAME:
        return code_or_name
    return NAME_TO_CODE.get(code_or_name)


def display(code: str) -> str:
    """로그/알림용 '삼성전자(005930)' 표기."""
    return f"{CODE_TO_NAME.get(code, code)}({code})"
