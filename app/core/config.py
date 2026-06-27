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

    ALPHA_VANTAGE_API_KEY: str = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    # 실적 캘린더(EARNINGS_CALENDAR) 전용 키 — 감성분석 키와 분리하여 일일 호출 한도 충돌 방지
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

    # Cross-sectional z-score 점수 시스템 v2 활성화
    # false: v1 (raw weighted sum) 으로 매수 결정, v2 점수는 로깅만
    # true:  v2 (z-score) 로 매수 결정
    # 참조: documents/10_멀티팩터_변별력_개선_기획.md
    USE_SCORING_V2: bool = os.getenv("USE_SCORING_V2", "false").lower() == "true"

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

# 싱글톤 설정 객체 생성
settings = Settings()