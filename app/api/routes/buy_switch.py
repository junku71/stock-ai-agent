"""신규 매수 원격 on/off 라우트."""
from fastapi import APIRouter, HTTPException, Query

from app.services import buy_switch_service

router = APIRouter()


@router.get("", summary="매수 스위치 상태 조회")
def status():
    """신규 매수 허용 여부와 마지막 변경 사유를 반환합니다."""
    return buy_switch_service.get_status()


@router.post("/enable", summary="신규 매수 재개")
def enable():
    """매도 감시/정합성 확인은 이 스위치와 무관하게 항상 동작합니다."""
    return buy_switch_service.set_buy_enabled(True)


@router.post("/disable", summary="신규 매수 중단")
def disable(
    reason: str = Query(..., description="중단 사유 (감사 로그에 남습니다)"),
):
    """
    **켜기 전까지 계속 꺼진 채 유지됩니다** (자정 자동복귀 없음).
    신규 매수만 막히며, 이미 보유 중인 포지션의 손절/익절/트레일링과 매도 감시,
    KIS 원장 정합성 확인은 그대로 계속 동작합니다.
    """
    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="reason 은 비워둘 수 없습니다")
    return buy_switch_service.set_buy_enabled(False, reason=reason.strip())
