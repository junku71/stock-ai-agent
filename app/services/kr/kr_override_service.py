"""
변동성 게이트 수동 오버라이드.

기본적으로 코스피 20일 실현변동성이 `kr_scoring.FEAR_HARD_BLOCK`(90%)을 넘으면
매수 후보 산출 자체가 차단된다. 이 모듈은 **운영자가 명시적으로 개입할 때만**
그 차단을 한시적으로 해제한다.

안전 설계 — 실수로 켜지거나, 켜둔 걸 잊는 상황을 막는 게 목적이다:
  1. 확인 문구 필수   — confirm="OVERRIDE" 를 정확히 입력해야 발급된다
  2. 사유 필수        — 감사 로그에 남는다 (누가 왜 열었는지)
  3. 자동 만료        — 기본 120분, 최대 24시간. 지나면 게이트가 저절로 복구된다
  4. 단일 활성        — 새로 발급하면 기존 오버라이드는 즉시 만료 처리된다
  5. Slack 알림       — 발급/해제 시 즉시 통지 (조용히 열려 있는 상태를 방지)
  6. DB 영속          — 서버가 재시작돼도 유지되고, 이력이 남는다

기본 동작은 '하드블록만' 해제다. 변동성에 따른 적응형 임계값(선별도)은 그대로
유지되므로, 폭락장에서는 여전히 상위 종목만 통과한다.
relax_threshold=True 를 주면 임계값까지 평온장 기준으로 낮춘다 — 파이프라인 전
구간을 관통시켜 보고 싶을 때(테스트) 쓰는 옵션이며, 실매매에서는 권장하지 않는다.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz

from app.db.supabase import supabase

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

TABLE = "kr_fear_gate_overrides"

CONFIRM_PHRASE = "OVERRIDE"
DEFAULT_MINUTES = 120
MAX_MINUTES = 24 * 60


class OverrideError(Exception):
    """오버라이드 발급 거부 (확인 문구 불일치, 사유 누락 등)."""


def get_active_override() -> Optional[dict]:
    """
    현재 유효한 오버라이드. 없으면 None.

    만료 판정은 DB 시각이 아니라 조회 시점(KST)으로 한다 — 서버 시계 기준으로
    일관되게 끊기 위해서다. 조회 실패 시에도 None 을 반환해 '게이트 정상 작동'
    쪽으로 안전하게 기운다(fail-safe).
    """
    try:
        resp = (
            supabase.table(TABLE)
            .select("*")
            .eq("status", "active")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning(f"오버라이드 조회 실패 → 게이트 정상 적용: {e}")
        return None

    rows = resp.data or []
    if not rows:
        return None

    row = rows[0]
    expires_at = row.get("expires_at")
    if not expires_at:
        return None

    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = KST.localize(expiry)
    except (ValueError, TypeError):
        logger.warning(f"오버라이드 만료시각 파싱 실패 → 무시: {expires_at}")
        return None

    if datetime.now(KST) >= expiry:
        # 지난 것은 정리해 두고 None
        _mark_expired(row["id"], "시간 만료")
        return None

    return row


def _mark_expired(override_id, note: str):
    try:
        supabase.table(TABLE).update(
            {
                "status": "expired",
                "closed_at": datetime.now(KST).isoformat(),
                "close_note": note,
            }
        ).eq("id", override_id).execute()
    except Exception as e:
        logger.warning(f"오버라이드 만료 처리 실패(id={override_id}): {e}")


def create_override(
    confirm: str,
    reason: str,
    minutes: int = DEFAULT_MINUTES,
    relax_threshold: bool = False,
) -> dict:
    """
    변동성 게이트를 한시적으로 해제한다.

    Args:
        confirm:         정확히 "OVERRIDE" 여야 한다 (오타 방지용 이중 확인)
        reason:          개입 사유 (감사 로그). 5자 이상.
        minutes:         유효 시간. 1 ~ 1440분.
        relax_threshold: True 면 적응형 임계값도 평온장 기준(0.40)으로 낮춘다.

    Raises:
        OverrideError: 확인 문구 불일치 / 사유 부족 / 시간 범위 초과
    """
    if confirm != CONFIRM_PHRASE:
        raise OverrideError(
            f'확인 문구가 일치하지 않습니다. confirm="{CONFIRM_PHRASE}" 를 정확히 입력하세요.'
        )
    if not reason or len(reason.strip()) < 5:
        raise OverrideError("개입 사유(reason)를 5자 이상 입력하세요. 감사 로그에 남습니다.")
    if not (1 <= minutes <= MAX_MINUTES):
        raise OverrideError(f"유효 시간은 1~{MAX_MINUTES}분 사이여야 합니다.")

    now = datetime.now(KST)
    expires_at = now + timedelta(minutes=minutes)

    # 기존 활성 오버라이드는 즉시 만료 (동시에 두 개가 살아있지 않게)
    try:
        prev = (
            supabase.table(TABLE).select("id").eq("status", "active").execute()
        ).data or []
        for row in prev:
            _mark_expired(row["id"], "새 오버라이드 발급으로 대체")
    except Exception as e:
        logger.warning(f"기존 오버라이드 정리 실패: {e}")

    # 발급 시점의 시장 상태를 함께 박아둔다 (사후에 "그때 얼마였나" 추적용)
    fear_at_creation = None
    try:
        from app.services.kr import kr_market_data_service

        fear_at_creation = kr_market_data_service.get_market_context().get("kospi_vol_20d")
    except Exception as e:
        logger.warning(f"오버라이드 발급 시 시장 상태 조회 실패: {e}")

    record = {
        "reason": reason.strip(),
        "relax_threshold": relax_threshold,
        "minutes": minutes,
        "fear_index_at_creation": fear_at_creation,
        "status": "active",
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    try:
        resp = supabase.table(TABLE).insert(record).execute()
        record["id"] = (resp.data or [{}])[0].get("id")
    except Exception as e:
        logger.error(f"오버라이드 저장 실패: {e}", exc_info=True)
        raise OverrideError(f"오버라이드 저장 실패: {e}")

    logger.warning(
        f"⚠️ 변동성 게이트 오버라이드 발급 — {minutes}분간 유효 "
        f"(만료 {expires_at:%Y-%m-%d %H:%M} KST, 발급 시 변동성 {fear_at_creation}%, "
        f"임계값 완화 {relax_threshold}) 사유: {reason.strip()}"
    )

    try:
        from app.services.kr.kr_notification_service import notify_override_created

        notify_override_created(record)
    except Exception as e:
        logger.warning(f"오버라이드 알림 발송 실패: {e}")

    return record


def revoke_override(note: str = "수동 해제") -> dict:
    """활성 오버라이드를 즉시 해제한다."""
    active = get_active_override()
    if not active:
        return {"revoked": False, "message": "활성 오버라이드가 없습니다"}

    _mark_expired(active["id"], note)
    logger.warning(f"변동성 게이트 오버라이드 해제됨 (id={active['id']}, {note})")

    try:
        from app.services.kr.kr_notification_service import notify_override_revoked

        notify_override_revoked(active, note)
    except Exception as e:
        logger.warning(f"오버라이드 해제 알림 발송 실패: {e}")

    return {"revoked": True, "override": active, "note": note}


def describe() -> dict:
    """현재 오버라이드 상태 요약 (API/알림용)."""
    active = get_active_override()
    if not active:
        return {"active": False, "message": "변동성 게이트가 정상 적용 중입니다"}

    remaining = None
    try:
        expiry = datetime.fromisoformat(str(active["expires_at"]).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = KST.localize(expiry)
        remaining = int((expiry - datetime.now(KST)).total_seconds() // 60)
    except (ValueError, TypeError, KeyError):
        pass

    return {
        "active": True,
        "reason": active.get("reason"),
        "relax_threshold": bool(active.get("relax_threshold")),
        "expires_at": active.get("expires_at"),
        "remaining_minutes": remaining,
        "fear_index_at_creation": active.get("fear_index_at_creation"),
        "message": (
            f"⚠️ 변동성 게이트가 수동 해제된 상태입니다 (잔여 {remaining}분). "
            f"사유: {active.get('reason')}"
        ),
    }
