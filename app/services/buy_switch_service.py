"""
신규 매수 원격 on/off 스위치.

켜기 전까지 계속 꺼진 채 유지되는 영속 스위치이며, **신규 매수만** 막는다 —
매도 감시/주문 정합성 확인/데이터 수집은 이 스위치와 무관하게 항상 그대로 돈다.
자정 자동복귀 같은 것은 없다. 외출·출장처럼 사람이 개입할 수 없는 기간에 새 포지션이
쌓이는 것만 막고 싶을 때 쓴다.

fail-open 설계: 조회 실패(DB 일시 장애 등) 시 매수를 "허용"으로 간주한다. 이 스위치의
기본 상태는 '켜짐'이고 끄는 쪽이 예외적 개입이므로, 일시적 DB 장애로 전 종목 매수가
조용히 멈추는 것보다는 원래 하던 대로 계속 도는 쪽이 안전하다. (반대로 변동성
게이트(kr_override_service)는 기본이 '차단'인 게이트라 실패 시에도 차단 유지가
안전한 것과 대비된다 — 스위치의 평상시 기본값이 무엇이냐에 따라 fail 방향이 갈린다.)

저장소는 market_switches 테이블의 market='KR' 행 하나다. 이 시스템은 국내 시장만
다루므로 파이썬 API 에서는 시장 인자를 받지 않는다.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.supabase import supabase
from app.services.slack_service import _send

logger = logging.getLogger(__name__)

TABLE = "market_switches"
MARKET_KEY = "KR"  # 테이블 스키마 유지를 위한 행 키 (시장은 국내 하나뿐)


def is_buy_enabled() -> bool:
    """신규 매수 허용 여부. 행이 없거나 조회 실패 시 True(fail-open)."""
    try:
        resp = (
            supabase.table(TABLE)
            .select("buy_enabled")
            .eq("market", MARKET_KEY)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.warning(f"  매수 스위치 조회 실패(허용으로 간주): {e}")
        return True
    if not rows:
        return True
    return bool(rows[0].get("buy_enabled", True))


def get_status() -> dict:
    """현재 스위치 상태 (API 응답용)."""
    try:
        resp = (
            supabase.table(TABLE)
            .select("*")
            .eq("market", MARKET_KEY)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.warning(f"  매수 스위치 상태 조회 실패: {e}")
        rows = []

    row = rows[0] if rows else None
    return {
        "buy_enabled": bool(row.get("buy_enabled", True)) if row else True,
        "reason": row.get("reason") if row else None,
        "updated_at": row.get("updated_at") if row else None,
    }


def set_buy_enabled(enabled: bool, reason: Optional[str] = None) -> dict:
    """매수 스위치를 켜거나 끈다. 실제로 상태가 바뀔 때만 Slack 알림을 보낸다."""
    was_enabled = is_buy_enabled()

    record = {
        "market": MARKET_KEY,
        "buy_enabled": enabled,
        "reason": reason,
        # upsert 는 지정한 컬럼만 갱신한다 — updated_at 의 DEFAULT NOW() 는 INSERT 시에만
        # 적용되고 충돌로 인한 UPDATE 경로에선 적용되지 않으므로 명시적으로 채워야 한다.
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table(TABLE).upsert(record, on_conflict="market").execute()
    except Exception as e:
        logger.error(f"  매수 스위치 갱신 실패: {e}", exc_info=True)
        raise

    logger.warning(
        f"신규 매수 스위치 → {'켜짐' if enabled else '꺼짐'} (사유: {reason or '없음'})"
    )

    if was_enabled != enabled:
        try:
            _notify_switch_changed(enabled, reason)
        except Exception as e:
            logger.warning(f"  매수 스위치 변경 알림 발송 실패: {e}")

    return {"buy_enabled": enabled, "reason": reason}


def _notify_switch_changed(enabled: bool, reason: Optional[str]):
    if enabled:
        _send(
            title="✅ 신규 매수 재개",
            message=f"국내주식 신규 매수가 다시 허용됩니다.\n*사유:* {reason or '없음'}",
            color="#2eb886",
        )
    else:
        _send(
            title="⛔ 신규 매수 중단",
            message=(
                f"국내주식 신규 매수가 중단됩니다. 다시 켜기 전까지 계속 꺼진 채 "
                f"유지됩니다.\n*사유:* {reason or '없음'}\n\n"
                f"_매도 감시·정합성 확인·데이터 수집은 영향을 받지 않고 그대로 동작합니다._"
            ),
            color="#ff9800",
        )
