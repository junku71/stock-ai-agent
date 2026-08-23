from pydantic import Field
from pydantic_settings import BaseSettings
from typing import List, Optional, Union, Literal, get_type_hints
import os
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

class Settings(BaseSettings):
    PROJECT_NAME: str = "주식 분석 API"
    PROJECT_DESCRIPTION: str = "해외주식 잔고 조회 및 주식 예측 API"
    PROJECT_VERSION: str = "1.0.0"

    # DEBUG 설정 추가
    DEBUG: bool = Field(default=False, description="디버그 모드 활성화 여부")

    CORS_ORIGINS: List[str] = ["*"]

    SUPABASE_URL: str = os.getenv("SUPABASE_URL")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY")
    # service_role 키 (RLS 우회) — 서버 백엔드 전용, 절대 외부 노출 금지.
    # 설정돼 있으면 Supabase 클라이언트가 이 키를 우선 사용 (RLS ON 환경에서 서버 쓰기 보장)
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    # 한국투자증권 API 설정
    KIS_USE_MOCK: bool = Field(default=True, description="모의투자 사용 여부")

    KIS_BASE_URL: str = Field(
        default="https://openapivts.koreainvestment.com:29443",
        description="한국투자증권 API 기본 URL (모의투자용)"
    )
    KIS_REAL_URL: str = Field(
        default="https://openapi.koreainvestment.com:9443",
        description="한국투자증권 API 기본 URL (실제투자용)"
    )

    # 모의투자 계좌 정보
    KIS_MOCK_APPKEY: str = Field(default="", description="모의투자 앱키")
    KIS_MOCK_APPSECRET: str = Field(default="", description="모의투자 앱시크릿")
    KIS_MOCK_CANO: str = Field(default="50173046", description="모의투자 계좌번호")

    # 실제투자 계좌 정보
    KIS_REAL_APPKEY: str = Field(default="", description="실제투자 앱키")
    KIS_REAL_APPSECRET: str = Field(default="", description="실제투자 앱시크릿")
    KIS_REAL_CANO: str = Field(default="64856431", description="실제투자 계좌번호")

    # .env 호환용 (직접 사용하지 않고 property로 대체)
    KIS_APPKEY: str = Field(default="", description="한국투자증권 API 앱키")
    KIS_APPSECRET: str = Field(default="", description="한국투자증권 API 앱시크릿")
    KIS_CANO: str = Field(default="", description="계좌번호 앞 8자리")
    KIS_ACNT_PRDT_CD: str = Field(default="01", description="계좌번호 뒤 2자리")

    # 실적 캘린더(EARNINGS_CALENDAR) 전용 키 — 감성분석 키와 분리하여 일일 호출 한도 충돌 방지
    ALPHA_VANTAGE_API_KEY: str = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    ALPHA_VANTAGE_API_KEY_EARNINGS: str = os.getenv("ALPHA_VANTAGE_API_KEY_EARNINGS", "")


    # Finnhub — Alpha Vantage 캘린더에 없는 종목(MU/COST/AVGO 등)의 실적일 보강용 (yfinance 429 대체)
    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    TR_ID: str = os.getenv("TR_ID")

    # Kaggle API (ML 예측 노트북 트리거용)
    # 신형 Access Token (KGAT_*) 우선, 없으면 기존 KAGGLE_KEY (32자리 hex) 사용
    KAGGLE_USERNAME: str = os.getenv("KAGGLE_USERNAME", "")
    KAGGLE_API_TOKEN: str = os.getenv("KAGGLE_API_TOKEN", "")
    KAGGLE_KEY: str = os.getenv("KAGGLE_KEY", "")
    KAGGLE_KERNEL_SLUG: str = os.getenv("KAGGLE_KERNEL_SLUG", "stock-prediction")
    KAGGLE_NOTEBOOK_DIR: str = os.getenv("KAGGLE_NOTEBOOK_DIR", "kaggle_notebook")

    # Slack 알림 (비어있으면 알림 비활성)
    SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")
    SLACK_NOTIFY_LEVEL: str = os.getenv("SLACK_NOTIFY_LEVEL", "info")

    # Slack 파일 업로드 (PDF 리포트 첨부용).
    #   Incoming Webhook 은 텍스트 전용이라 파일을 붙일 수 없다. PDF 를 채널에 올리려면
    #   Bot Token(xoxb-…, scope: files:write / 채널명 해석 시 channels:read)이 따로 필요하다.
    #   둘 중 하나라도 비어 있으면 PDF 는 로컬에만 저장되고 업로드는 조용히 생략된다.
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_REPORT_CHANNEL: str = os.getenv("SLACK_REPORT_CHANNEL", "")

    # ══════════════════════════════════════════════════════════════
    # 미국 트랙 포지션 사이징 (app/services/position_sizing.py 공유)
    # ══════════════════════════════════════════════════════════════
    US_SLOT_RATIO: float = float(os.getenv("US_SLOT_RATIO", "0.10"))

    # 확신도 가중 배분. 0.0 이면 전 종목 균등(기존 동작), 0.5 면 1위 1.5배·꼴찌 0.5배
    US_SLOT_TILT: float = float(os.getenv("US_SLOT_TILT", "0.5"))
    US_SLOT_METHOD: str = os.getenv("US_SLOT_METHOD", "rank")  # rank | score
    US_MIN_SLOT_RATIO: float = float(os.getenv("US_MIN_SLOT_RATIO", "0.05"))
    US_MAX_SLOT_RATIO: float = float(os.getenv("US_MAX_SLOT_RATIO", "0.20"))
    US_MAX_TOTAL_EXPOSURE: float = float(os.getenv("US_MAX_TOTAL_EXPOSURE", "0.80"))

    # 매수 여력의 기준 (inquire-psamount 응답 중 어느 필드를 현금으로 볼지)
    #   "integrated" — frcr_ord_psbl_amt1 (앱의 "통합" 금액, 원화 자동환전 포함)
    #                  원화통합증거금 계좌에서 원화까지 끌어 쓰려면 이 값.
    #   "foreign"    — ovrs_ord_psbl_amt (앱의 "외화" 금액, 보유 외화만)
    #                  실제 보유 외화만 쓴다. 자동환전을 원치 않거나 모의계좌면 이 쪽.
    #   ※ 모의투자에서는 integrated 값이 실가용액의 7배 이상으로 나와 주문이 거부된다.
    #     KIS_USE_MOCK=true 면 코드가 자동으로 foreign 을 쓴다.
    US_CASH_BASIS: str = os.getenv("US_CASH_BASIS", "integrated")

    # Cross-sectional z-score 점수 시스템 v2 활성화
    # false: v1 (raw weighted sum) 으로 매수 결정, v2 점수는 로깅만
    # true:  v2 (z-score) 로 매수 결정
    # 참조: documents/10_멀티팩터_변별력_개선_기획.md
    USE_SCORING_V2: bool = os.getenv("USE_SCORING_V2", "false").lower() == "true"

    # ══════════════════════════════════════════════════════════════
    # 국내주식(KOSPI 100) 트랙 설정
    #   기존 미국 트랙과 병행 운영. KR_ENABLED=false 면 KR 스케줄러 미기동.
    #   참조: documents/20_국내주식_KOSPI30_설계.md
    # ══════════════════════════════════════════════════════════════
    KR_ENABLED: bool = os.getenv("KR_ENABLED", "false").lower() == "true"

    # true 면 KIS 주문 API 를 호출하지 않고 로그만 남김 (로직 검증용 드라이런)
    KR_DRY_RUN: bool = os.getenv("KR_DRY_RUN", "false").lower() == "true"

    # ML 예측 신뢰도 하한 (%). Transformer 방향성 정확도(100 - MAPE)가 이 값 미만인
    # 종목은 매수 후보에서 제외한다. 정확도 낮은 예측이 z-score 상위를 차지하는 것을 막는다.
    #   0 으로 두면 필터를 끈다.
    KR_MIN_ML_ACCURACY: float = float(os.getenv("KR_MIN_ML_ACCURACY", "80"))

    # 예측 상승률 하한 (%)
    KR_MIN_RISE_PROBABILITY: float = float(os.getenv("KR_MIN_RISE_PROBABILITY", "2"))

    # 종목당 기준 투자 비중 (총자산 대비) / 동시 보유 최대 종목 수
    KR_SLOT_RATIO: float = float(os.getenv("KR_SLOT_RATIO", "0.10"))
    KR_MAX_POSITIONS: int = int(os.getenv("KR_MAX_POSITIONS", "8"))

    # ── 확신도 가중 배분 (app/services/position_sizing.py) ──
    # 종합점수가 높은 종목에 더 많이 배분한다.
    #   0.0 → 전 종목 균등 (KR_SLOT_RATIO 그대로, 기존 동작)
    #   0.5 → 1위 1.5×, 중간 1.0×, 꼴찌 0.5× (권장)
    #   1.0 → 1위 2.0×, 꼴찌 0×
    KR_SLOT_TILT: float = float(os.getenv("KR_SLOT_TILT", "0.5"))

    # rank: 순위 등간격 (총 투입액이 종목 수로 고정, 이상치에 강함 — 권장)
    # score: 점수 차 크기 반영 (이상치 하나가 나머지를 바닥으로 눌러버릴 수 있음)
    KR_SLOT_METHOD: str = os.getenv("KR_SLOT_METHOD", "rank")

    # 개별 종목 비중 하한/상한, 총 투입 비율 상한
    KR_MIN_SLOT_RATIO: float = float(os.getenv("KR_MIN_SLOT_RATIO", "0.05"))
    KR_MAX_SLOT_RATIO: float = float(os.getenv("KR_MAX_SLOT_RATIO", "0.20"))
    KR_MAX_TOTAL_EXPOSURE: float = float(os.getenv("KR_MAX_TOTAL_EXPOSURE", "0.80"))

    # 시장 데이터 수집 기간 (년). 백필 시작일 = 오늘 - 이 값.
    #   종목이 100개라 기간이 길수록 수집·학습 시간이 선형으로 늘어난다.
    #   5년이면 코로나 이후 국면 + 금리 인상/인하 사이클을 포함한다.
    KR_HISTORY_YEARS: int = int(os.getenv("KR_HISTORY_YEARS", "5"))

    # ── 매도 전략: 부분익절 + 샹들리에 트레일링 + 교체매매 ──
    # 2.5×ATR 익절가 도달 시 전량매도 대신 일부만 팔고, 잔량은 진입 시점 ATR 로 고정한
    # 샹들리에(고점 대비 배수) 트레일링 스탑으로 넘긴다. 손절/기존 기술신호·공포장 규칙은 그대로 유지.
    KR_CHANDELIER_ATR_MULT: float = float(os.getenv("KR_CHANDELIER_ATR_MULT", "3.0"))
    KR_PARTIAL_SELL_RATIO: float = float(os.getenv("KR_PARTIAL_SELL_RATIO", "0.3"))
    # 부분익절 수량이 이 값 미만이면 부분매도 대신 전량매도로 대체 (소량 잔량 방지)
    KR_MIN_PARTIAL_SHARES: int = int(os.getenv("KR_MIN_PARTIAL_SHARES", "1"))
    # 교체매매: 대기 중인 미보유 후보 점수가 가장 약한 보유종목 점수보다 이 값 이상 높고
    # 보유종목수가 KR_MAX_POSITIONS 에 도달했을 때만 LLM 매도검토에 교체후보로 표시한다.
    KR_ROTATION_MIN_SCORE_GAP: float = float(os.getenv("KR_ROTATION_MIN_SCORE_GAP", "0.30"))

    # LLM 매도검토 프롬프트에 보여줄 점수/순위 추세 일수. "하루짜리 노이즈 vs 추세적 악화"를
    # 실제로 구분할 수 있게 kr_llm_sell_decision_logs 이력을 며칠치 보여줄지 결정한다.
    KR_SCORE_TREND_DAYS: int = int(os.getenv("KR_SCORE_TREND_DAYS", "5"))

    # LLM 매도판정 집행 재검증 임계값(%). 판단 시점(price_at_decision) 대비 현재가가 이 값
    # 이상 유리한 방향(상승)으로 이미 움직였으면, 판단이 낡았다고 보고 이번 사이클 집행을
    # 보류한다(취소가 아니라 다음 사이클에 다시 검사). 0 이하로 두면 재검증을 끈다.
    KR_SELL_REVALIDATE_PCT: float = float(os.getenv("KR_SELL_REVALIDATE_PCT", "3.0"))

    # 장중 추가 매도검토(조건부 주기체크): 공포지수가 이 값을 넘는 날은, 마지막 매도검토 이후
    # KR_INTRADAY_REVIEW_INTERVAL_HOURS 시간마다 LLM 매도검토를 한 번 더 돌린다. 공포지수는
    # 장 마감 후에만 갱신되므로 "장중에 막 넘어선 순간"은 관측 불가 — 그래서 이벤트 트리거가
    # 아니라 "오늘 이미 넘어선 상태가 지속되는 동안 주기적으로 재점검"하는 방식이다.
    # 0 이하로 두면 끈다.
    KR_INTRADAY_FEAR_REVIEW_THRESHOLD: float = float(
        os.getenv("KR_INTRADAY_FEAR_REVIEW_THRESHOLD", "40.0")
    )
    KR_INTRADAY_REVIEW_INTERVAL_HOURS: float = float(
        os.getenv("KR_INTRADAY_REVIEW_INTERVAL_HOURS", "2")
    )

    # ── 분석 리포트 PDF (Phase A 종료 후 LLM 작성 → Slack 업로드) ──
    #   파이프라인 결과(시장환경·후보·LLM 판단·매수 견적)를 Claude 가 리포트로 정리해
    #   PDF 로 만들어 Slack 채널에 올린다. 실패해도 파이프라인 결과에는 영향이 없다.
    KR_REPORT_ENABLED: bool = os.getenv("KR_REPORT_ENABLED", "true").lower() == "true"
    KR_REPORT_MODEL: str = os.getenv("KR_REPORT_MODEL", "claude-opus-5")
    # 리포트 PDF 저장 경로 (프로젝트 루트 기준 상대경로 허용)
    KR_REPORT_DIR: str = os.getenv("KR_REPORT_DIR", "reports/kr")
    # 이 일수보다 오래된 리포트 PDF 는 생성 시 자동 정리. 0 이면 정리하지 않음.
    KR_REPORT_KEEP_DAYS: int = int(os.getenv("KR_REPORT_KEEP_DAYS", "60"))

    # 분석 파이프라인(장 마감 후) / 매수 집행(장 시작 후) 시각 — 모두 KST
    KR_ANALYSIS_TIME: str = os.getenv("KR_ANALYSIS_TIME", "16:30")
    KR_EXECUTION_TIME: str = os.getenv("KR_EXECUTION_TIME", "09:05")

    # NAVER API Hub (구 openapi.naver.com 검색 API 의 이관처)
    #   콘솔: NAVER Cloud Platform → NAVER API HUB
    NAVER_API_KEY_ID: str = os.getenv("NAVER_API_KEY_ID", "")
    NAVER_API_KEY: str = os.getenv("NAVER_API_KEY", "")
    NAVER_API_HUB_BASE: str = os.getenv(
        "NAVER_API_HUB_BASE", "https://naverapihub.apigw.ntruss.com"
    )
    # 데이터랩(검색어 트렌드)은 검색 API 와 별개 상품이라 호스트가 다르다.
    #   검색   : naverapihub.apigw.ntruss.com   (NAVER API HUB)
    #   데이터랩: naveropenapi.apigw.ntruss.com  (AI·NAVER API)
    # 상품을 따로 신청해야 권한이 열린다 (미신청 시 401).
    NAVER_DATALAB_BASE: str = os.getenv(
        "NAVER_DATALAB_BASE", "https://naveropenapi.apigw.ntruss.com"
    )

    # 뉴스 감성 스코어링 모델 (기사 텍스트 → -1~+1 점수)
    #   네이버 검색 API 는 AlphaVantage 와 달리 감성 점수를 주지 않으므로 직접 산출한다.
    KR_SENTIMENT_MODEL: str = os.getenv("KR_SENTIMENT_MODEL", "claude-opus-5")
    KR_SENTIMENT_LOOKBACK_DAYS: int = int(os.getenv("KR_SENTIMENT_LOOKBACK_DAYS", "3"))

    # KRX Open API (openapi.krx.co.kr) — 시장 전체 시세/지수. 없으면 해당 수집만 스킵.
    KRX_AUTH_KEY: str = os.getenv("KRX_AUTH_KEY", "")

    # 한국은행 ECOS OpenAPI — 한국 거시지표 (FRED 의 한국판)
    ECOS_API_KEY: str = os.getenv("ECOS_API_KEY", "")

    # OpenDART — 공시/재무제표 (실적 리스크 판단 보강용, 선택)
    DART_API_KEY: str = os.getenv("DART_API_KEY", "")

    # KR ML 예측 전용 Kaggle 커널 (미국 커널과 분리)
    KAGGLE_KERNEL_SLUG_KR: str = os.getenv("KAGGLE_KERNEL_SLUG_KR", "stock-prediction-kr")
    KAGGLE_NOTEBOOK_DIR_KR: str = os.getenv("KAGGLE_NOTEBOOK_DIR_KR", "kaggle_notebook_kr")

    @property
    def kis_base_url(self) -> str:
        """사용할 한국투자증권 API URL 반환"""
        return self.KIS_BASE_URL if self.KIS_USE_MOCK else self.KIS_REAL_URL

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # KIS_USE_MOCK에 따라 활성 계좌 정보 자동 전환
        if self.KIS_USE_MOCK:
            if self.KIS_MOCK_APPKEY:
                self.KIS_APPKEY = self.KIS_MOCK_APPKEY
            if self.KIS_MOCK_APPSECRET:
                self.KIS_APPSECRET = self.KIS_MOCK_APPSECRET
            if self.KIS_MOCK_CANO:
                self.KIS_CANO = self.KIS_MOCK_CANO
        else:
            if self.KIS_REAL_APPKEY:
                self.KIS_APPKEY = self.KIS_REAL_APPKEY
            if self.KIS_REAL_APPSECRET:
                self.KIS_APPSECRET = self.KIS_REAL_APPSECRET
            if self.KIS_REAL_CANO:
                self.KIS_CANO = self.KIS_REAL_CANO

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        # .env에 정의되지 않은 변수(오타 포함)가 있어도 서버가 죽지 않도록 무시
        # (수강생이 SUPABASE_SEVICE_ROLE_KEY 같은 오타를 내면 extra_forbidden으로 기동 실패하는 사례 방지)
        extra = "ignore"

# 싱글톤 설정 객체 생성
settings = Settings()