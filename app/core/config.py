from pydantic import Field
from pydantic_settings import BaseSettings
from typing import List, Optional, Union, Literal, get_type_hints
import os
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

class Settings(BaseSettings):
    PROJECT_NAME: str = "국내주식 자동매매 API"
    PROJECT_DESCRIPTION: str = "KOSPI 100 종목 분석·추천·자동매매 API"
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

    # Claude (뉴스 감성 스코어링 / LLM 매수·매도 검토 / 분석 리포트 작성)
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Kaggle API (ML 예측 노트북 트리거용)
    # 신형 Access Token (KGAT_*) 우선, 없으면 기존 KAGGLE_KEY (32자리 hex) 사용
    KAGGLE_USERNAME: str = os.getenv("KAGGLE_USERNAME", "")
    KAGGLE_API_TOKEN: str = os.getenv("KAGGLE_API_TOKEN", "")
    KAGGLE_KEY: str = os.getenv("KAGGLE_KEY", "")

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
    # 국내주식(KOSPI 100) 트랙 설정
    #   KR_ENABLED=false 면 API 만 뜨고 스케줄러는 기동하지 않는다(점검용 킬 스위치).
    #   참조: documents/20_국내주식_KOSPI30_설계.md
    # ══════════════════════════════════════════════════════════════
    KR_ENABLED: bool = os.getenv("KR_ENABLED", "true").lower() == "true"

    # true 면 KIS 주문 API 를 호출하지 않고 로그만 남김 (로직 검증용 드라이런)
    KR_DRY_RUN: bool = os.getenv("KR_DRY_RUN", "false").lower() == "true"

    # ML 예측 신뢰도 하한 (%). Transformer 방향성 정확도(100 - MAPE)가 이 값 미만인
    # 종목은 매수 후보에서 제외한다. 정확도 낮은 예측이 z-score 상위를 차지하는 것을 막는다.
    #   0 으로 두면 필터를 끈다.
    KR_MIN_ML_ACCURACY: float = float(os.getenv("KR_MIN_ML_ACCURACY", "80"))

    # 예측 상승률 추가 하한 (%). 기본 규칙은 '양수(>0)' 이고, 이 값을 올리면 더 조인다.
    KR_MIN_RISE_PROBABILITY: float = float(os.getenv("KR_MIN_RISE_PROBABILITY", "0"))

    # 종목당 기준 투자 비중 (총자산 대비) / 동시 보유 최대 종목 수
    KR_SLOT_RATIO: float = float(os.getenv("KR_SLOT_RATIO", "0.10"))
    KR_MAX_POSITIONS: int = int(os.getenv("KR_MAX_POSITIONS", "10"))

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

    # ── 분석 후보군 (동적 유니버스) ──
    #   KIS 종목마스터 + 현재가 시가총액으로 매번 상위 N 종목을 새로 뽑는다.
    #   universe.py(고정 리스트)는 ML 학습 대상 전용으로 남고, 이 값은 1·2단계 후보군만 정한다.
    KR_UNIVERSE_SIZE: int = int(os.getenv("KR_UNIVERSE_SIZE", "200"))
    # 캐시 유효기간(일). 시총 순위는 하루아침에 뒤집히지 않으므로 매번 재조회하지 않는다.
    #   갱신 1회에 KOSPI 보통주 800여 종목을 조회하므로 모의계좌 기준 8분쯤 걸린다.
    KR_UNIVERSE_REFRESH_DAYS: float = float(os.getenv("KR_UNIVERSE_REFRESH_DAYS", "7"))
    KR_UNIVERSE_CACHE_DIR: str = os.getenv("KR_UNIVERSE_CACHE_DIR", "cache/kr")
    # DART 재무제표 캐시 유효기간(일). 분기보고서가 나올 때만 값이 바뀌므로 길게 잡는다.
    #   OpenDART 는 일 20,000회 제한이 있어 200종목을 매번 새로 받으면 금방 소진된다.
    KR_DART_CACHE_DAYS: float = float(os.getenv("KR_DART_CACHE_DAYS", "14"))

    # ── 1단계 퀀트 스크리닝 데이터 (kr_quant_data_service) ──
    #   ai-stock.co.kr 가 매주 갱신하는 KRX 전종목 퀀트 엑셀 1개를 통째로 받아 재무·
    #   시총·업종 데이터를 종목별 API 호출 없이 확보한다. 1단계 후보군(고정 100종목,
    #   universe.py)이 여기서 나오므로 KR_UNIVERSE_SIZE(동적 200)는 1단계에 더 안 쓰인다.
    KR_QUANT_DATA_SOURCE_URL: str = os.getenv(
        "KR_QUANT_DATA_SOURCE_URL", "https://www.ai-stock.co.kr/krx-finstate.html"
    )
    KR_QUANT_DATA_DIR: str = os.getenv("KR_QUANT_DATA_DIR", "data")
    # 원본이 주간 갱신이므로 7일 — 그보다 자주 받아봐야 새 파일이 없다.
    KR_QUANT_CACHE_DAYS: float = float(os.getenv("KR_QUANT_CACHE_DAYS", "7"))

    # ── 신규 종목 추천 스크리닝 (kr_screening_service) ──
    # 1단계 Feature 통합(kr_scoring.compute_scores, ML 제외 — 재무+기술+수급+감성만) 이후
    # composite_score 상위 몇 종목까지 2단계 ML Filter 로 넘길지.
    KR_FEATURE_TOP_N: int = int(os.getenv("KR_FEATURE_TOP_N", "30"))
    # 2단계 ML Filter(상승확률·정확도) 통과 후 3단계 LLM 에게 넘길 최대 후보 수.
    KR_ML_FILTER_TOP_N: int = int(os.getenv("KR_ML_FILTER_TOP_N", "10"))
    # 포트폴리오 규칙(_apply_portfolio_rules, 슬롯·섹터 필터) 통과 후 3단계로 넘기는 상한.
    #   ML Filter 결과(KR_ML_FILTER_TOP_N)를 LLM 이 그대로 다 보게 하려고 같은 값으로 맞춘다
    #   — 여기서 미리 잘라버리면 LLM 이 "Risk/Market 감안해 3종목 이하로 종합판단"할 재료가
    #   줄어든다.
    KR_STAGE2_TOP_N: int = int(os.getenv("KR_STAGE2_TOP_N", "10"))
    # 3단계 LLM 이 그 후보들 중 실제로 매수 추천할 수 있는 최대 종목 수.
    KR_LLM_MAX_PICKS: int = int(os.getenv("KR_LLM_MAX_PICKS", "3"))
    # 리밸런싱 제안이 지켜야 할 보유 종목수 상한.
    #   KR_MAX_POSITIONS(하드 상한)보다 낮게 잡는다 — 여유 슬롯을 남겨두면 다음 회차에
    #   더 좋은 후보가 나왔을 때 기존 종목을 억지로 팔지 않고도 담을 수 있다.
    KR_REBALANCE_MAX_POSITIONS: int = int(os.getenv("KR_REBALANCE_MAX_POSITIONS", "8"))
    # 같은 섹터에 보유할 수 있는 최대 종목 수 (보유분 포함). '최대한 다양한 섹터' 규칙.
    KR_MAX_PER_SECTOR: int = int(os.getenv("KR_MAX_PER_SECTOR", "2"))
    # 3단계 리밸런싱 판단 모델
    KR_REBALANCE_MODEL: str = os.getenv("KR_REBALANCE_MODEL", "claude-sonnet-5")

    # 1단계 기본적 분석 판정 모델
    KR_FUNDAMENTAL_MODEL: str = os.getenv("KR_FUNDAMENTAL_MODEL", "claude-sonnet-5")
    # 1단계 감성 하한. 이 값 이하면 탈락 (-1 ~ +1 척도)
    KR_SENTIMENT_MIN_SCORE: float = float(os.getenv("KR_SENTIMENT_MIN_SCORE", "-0.1"))

    # 2단계 기술적 분석 — 매수 신호 최소 개수
    KR_MIN_BUY_SIGNALS: int = int(os.getenv("KR_MIN_BUY_SIGNALS", "2"))
    # 골든/데드크로스를 '최근'으로 인정할 기간(일)
    KR_CROSS_LOOKBACK_DAYS: int = int(os.getenv("KR_CROSS_LOOKBACK_DAYS", "10"))
    # 거래량 급증 판정 배수 (5일 평균 대비)
    KR_VOLUME_SURGE_RATIO: float = float(os.getenv("KR_VOLUME_SURGE_RATIO", "1.5"))

    # 2단계 수급 — 최근 N 거래일 안에서 M 일 연속 순매수(외국인·기관 각각)
    KR_FLOW_WINDOW_DAYS: int = int(os.getenv("KR_FLOW_WINDOW_DAYS", "5"))
    KR_FLOW_STREAK_DAYS: int = int(os.getenv("KR_FLOW_STREAK_DAYS", "3"))

    # 시장 데이터 수집 기간 (년). 백필 시작일 = 오늘 - 이 값.
    #   종목이 100개라 기간이 길수록 수집·학습 시간이 선형으로 늘어난다.
    #   5년이면 코로나 이후 국면 + 금리 인상/인하 사이클을 포함한다.
    KR_HISTORY_YEARS: int = int(os.getenv("KR_HISTORY_YEARS", "5"))

    # ── 매도 전략 ──
    # 매도는 언제나 **전량**이다 — 2.5×ATR 익절가 도달 시 전량 익절, 1.5×ATR 손절가 도달 시
    # 전량 손절. 부분매도/트레일링 잔량 보유는 하지 않는다.
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

    # 공포장 강제청산(매도 조건 3) 발동 최소 손실률(%). 양수로 적으며 "매입가 대비 이 %
    # 이상 손실일 때만 발동"을 뜻한다(기본 3.0 → -3.00% 이하). 조건 3 의 취지는 패닉
    # 국면에서 위험을 줄이는 것이지 본전/수익 포지션을 국면만 보고 털어내는 게 아니다.
    # 0 으로 두면 손실이기만 하면(-0.01% 도) 발동하고, 음수로 두면 수익 구간까지 발동한다.
    KR_FEAR_SELL_LOSS_PCT: float = float(os.getenv("KR_FEAR_SELL_LOSS_PCT", "3.0"))

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
    KR_REPORT_MODEL: str = os.getenv("KR_REPORT_MODEL", "claude-sonnet-5")
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
    KR_SENTIMENT_MODEL: str = os.getenv("KR_SENTIMENT_MODEL", "claude-sonnet-5")
    KR_SENTIMENT_LOOKBACK_DAYS: int = int(os.getenv("KR_SENTIMENT_LOOKBACK_DAYS", "3"))

    # KRX Open API (openapi.krx.co.kr) — 시장 전체 시세/지수. 없으면 해당 수집만 스킵.
    KRX_AUTH_KEY: str = os.getenv("KRX_AUTH_KEY", "")

    # 한국은행 ECOS OpenAPI — 한국 거시지표 (FRED 의 한국판)
    ECOS_API_KEY: str = os.getenv("ECOS_API_KEY", "")

    # OpenDART — 공시/재무제표 (실적 리스크 판단 보강용, 선택)
    DART_API_KEY: str = os.getenv("DART_API_KEY", "")

    # ML 예측 Kaggle 커널
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