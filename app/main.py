import os
import sys

if __name__ == "__main__":
    # app/main.py를 app/ 디렉토리에서 직접 실행할 때 `app` 패키지를 찾을 수 있도록
    # 프로젝트 루트를 sys.path에 추가 (python run.py로 실행할 때는 불필요하지만 무해함)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from app.api.api import api_router
from app.core.config import settings
from app.utils.kr_scheduler import start_kr_scheduler, stop_kr_scheduler
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: runs once when app starts
    await startup()
    yield
    # Shutdown: 필요한 정리 작업
    if settings.KR_ENABLED:
        stop_kr_scheduler()  # 국내주식(KOSPI 100) 스케줄러 종료

app = FastAPI(title="국내주식 자동매매 API", lifespan=lifespan)

# CORS 미들웨어 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 모든 오리진 허용 (프로덕션에서는 특정 도메인으로 제한 권장)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 라우터 등록 (중앙 관리 방식)
app.include_router(api_router)

@app.get("/")
def read_root():
    return {"message": "국내주식(KOSPI 100) 자동매매 API에 오신 것을 환영합니다"}


async def startup():
    # 국내주식(KOSPI 100) 트랙 — KR_ENABLED=false 로 두면 API 만 뜨고 스케줄러는 멈춘다.
    # 점검·수동 백필처럼 자동매매가 돌면 곤란한 상황에서 쓰는 킬 스위치다.
    if settings.KR_ENABLED:
        print("국내주식(KOSPI 100) 스케줄러를 시작합니다...")
        start_kr_scheduler()
    else:
        print("KR_ENABLED=false — 국내주식 스케줄러는 기동하지 않습니다.")

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
