from fastapi import APIRouter
from app.api.routes.kr import router as kr_router
from app.api.routes.buy_switch import router as buy_switch_router

api_router = APIRouter()
api_router.include_router(kr_router, prefix="/kr", tags=["국내주식 (KOSPI 100)"])
api_router.include_router(buy_switch_router, prefix="/buy-switch", tags=["신규 매수 on/off"])
