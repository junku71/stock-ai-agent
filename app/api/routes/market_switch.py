"""시장별(미국/국내) 신규 매수 원격 on/off 라우트."""
from fastapi import APIRouter, HTTPException, Query

from app.services import market_switch_service

router = APIRouter()


@router.get("", summary="시장별 매수 스위치 상태 조회")
def status():
    """미국/국내 두 시장의 신규 매수 허용 여부를 한 번에 반환합니다."""
    return market_switch_service.get_status()


@router.post("/{market}/enable", summary="시장 신규 매수 재개")
def enable(market: str):
    """market: US 또는 KR. 매도 감시/정합성 확인은 이 스위치와 무관하게 항상 동작합니다."""
    market = market.upper()
    if market not in market_switch_service.MARKETS:
        raise HTTPException(status_code=400, detail="market 은 US 또는 KR 이어야 합니다")
    return market_switch_service.set_buy_enabled(market, True)


@router.post("/{market}/disable", summary="시장 신규 매수 중단")
def disable(
    market: str,
    reason: str = Query(..., description="중단 사유 (감사 로그에 남습니다)"),
):
    """
    market: US 또는 KR. **켜기 전까지 계속 꺼진 채 유지됩니다** (자정 자동복귀 없음).
    신규 매수만 막히며, 이미 보유 중인 포지션의 손절/익절/트레일링과 매도 감시,
    KIS 원장 정합성 확인은 그대로 계속 동작합니다.
    """
    market = market.upper()
    if market not in market_switch_service.MARKETS:
        raise HTTPException(status_code=400, detail="market 은 US 또는 KR 이어야 합니다")
    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="reason 은 비워둘 수 없습니다")
    return market_switch_service.set_buy_enabled(market, False, reason=reason.strip())
