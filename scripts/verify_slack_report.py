"""
Slack 리포트 PDF 업로드 검증 스크립트.

`slack_file_service` 가 실제 워크스페이스에 파일을 올릴 수 있는지 단계별로 진단한다.
업로드 3단계는 실패해도 에러 코드만 주고 어디가 문제인지 알려주지 않기 때문에,
그 앞에 사전 점검(토큰 유효성 → scope → 채널 존재 → 봇 채널 참여)을 따로 둔다.

실행:
    python scripts/verify_slack_report.py                 # 테스트 PDF 1장을 만들어 업로드
    python scripts/verify_slack_report.py --dry           # 실제 업로드 없이 사전 점검만
    python scripts/verify_slack_report.py --file <경로>   # 기존 PDF 로 업로드

필요 설정(.env):
    SLACK_BOT_TOKEN=xoxb-...      Bot Token Scopes: files:write
                                  (#채널명으로 지정할 경우 channels:read 추가)
    SLACK_REPORT_CHANNEL=C...     채널 ID 권장. 봇을 해당 채널에 초대해야 한다.
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.kr import slack_file_service as sf  # noqa: E402

OK, FAIL, WARN, INFO = "[ OK ]", "[FAIL]", "[WARN]", "[    ]"

# Slack 에러 코드 → 사람이 바로 조치할 수 있는 문장
HINTS = {
    "invalid_auth": "토큰이 유효하지 않다. xoxb- 로 시작하는 Bot User OAuth Token 인지 확인.",
    "not_authed": "Authorization 헤더가 비었다. SLACK_BOT_TOKEN 값을 확인.",
    "account_inactive": "토큰이 속한 앱이 비활성/삭제됐다. 워크스페이스에 재설치 필요.",
    "missing_scope": "Bot Token Scopes 에 필요한 권한이 없다. 추가 후 앱을 재설치해야 반영된다.",
    "not_in_channel": "봇이 채널에 없다. 채널에서 `/invite @봇이름` 으로 초대.",
    "channel_not_found": "채널 ID 가 잘못됐거나 봇이 볼 수 없는 채널이다.",
    "is_archived": "보관된 채널이라 업로드할 수 없다.",
}


def _hint(error: str) -> str:
    return HINTS.get(error, "")


def _line(mark: str, text: str, detail: str = ""):
    print(f"{mark} {text}")
    if detail:
        for chunk in str(detail).split("\n"):
            print(f"       {chunk}")


def check_config() -> bool:
    print("\n━━ 1. 설정 ━━")
    token = settings.SLACK_BOT_TOKEN
    channel = settings.SLACK_REPORT_CHANNEL
    ok = True

    if not token:
        _line(FAIL, "SLACK_BOT_TOKEN 미설정", ".env 에 xoxb- 로 시작하는 Bot Token 을 넣어야 한다")
        ok = False
    elif not token.startswith("xoxb-"):
        _line(
            WARN,
            f"SLACK_BOT_TOKEN 형식이 예상과 다름 (앞 5자: {token[:5]})",
            "User Token(xoxp-)이 아니라 Bot User OAuth Token(xoxb-)이어야 한다",
        )
    else:
        _line(OK, f"SLACK_BOT_TOKEN 설정됨 (…{token[-6:]})")

    if not channel:
        _line(FAIL, "SLACK_REPORT_CHANNEL 미설정", "채널 ID(C…) 권장. #채널명도 가능")
        ok = False
    else:
        kind = "채널 ID" if not channel.startswith("#") else "채널명(조회 필요)"
        _line(OK, f"SLACK_REPORT_CHANNEL = {channel} ({kind})")

    _line(INFO, f"is_configured() = {sf.is_configured()}")
    return ok


def check_auth() -> bool:
    """auth.test — 토큰 유효성 + 어느 워크스페이스/봇인지. 응답 헤더로 부여된 scope 도 본다."""
    print("\n━━ 2. 토큰 유효성 (auth.test) ━━")
    try:
        resp = requests.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"},
            timeout=20,
        )
        data = resp.json()
    except Exception as e:
        _line(FAIL, f"요청 실패: {e}")
        return False

    if not data.get("ok"):
        _line(FAIL, f"인증 실패: {data.get('error')}", _hint(data.get("error", "")))
        return False

    _line(OK, f"워크스페이스 '{data.get('team')}' / 봇 '{data.get('user')}' ({data.get('user_id')})")

    scopes = resp.headers.get("x-oauth-scopes", "")
    if scopes:
        granted = [s.strip() for s in scopes.split(",")]
        _line(INFO, f"부여된 scope: {', '.join(granted)}")
        if "files:write" in granted:
            _line(OK, "files:write 있음 (파일 업로드 가능)")
        else:
            _line(FAIL, "files:write 없음", "OAuth & Permissions 에서 추가 후 앱 재설치 필요")
            return False
        if settings.SLACK_REPORT_CHANNEL.startswith("#") and "channels:read" not in granted:
            _line(
                WARN,
                "channels:read 없음 — #채널명 해석 불가",
                "채널 ID(C…)를 직접 넣거나 channels:read 를 추가한다",
            )
    else:
        _line(WARN, "응답에 scope 헤더가 없어 권한 확인 생략 (업로드 단계에서 판별된다)")
    return True


def check_channel() -> str:
    """채널 해석 + 봇이 그 채널의 멤버인지 확인."""
    print("\n━━ 3. 채널 ━━")
    channel_id = sf._resolve_channel(settings.SLACK_REPORT_CHANNEL)
    if not channel_id:
        _line(FAIL, "채널 ID 를 해석하지 못했다", "채널명 대신 ID(C…)를 쓰거나 channels:read 추가")
        return ""
    _line(OK, f"채널 ID = {channel_id}")

    try:
        data = requests.get(
            "https://slack.com/api/conversations.info",
            headers={"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"},
            params={"channel": channel_id},
            timeout=20,
        ).json()
    except Exception as e:
        _line(WARN, f"채널 정보 조회 실패(업로드는 시도 가능): {e}")
        return channel_id

    if not data.get("ok"):
        error = data.get("error", "")
        # conversations.info 자체가 scope 부족으로 막힐 수 있다 — 업로드 불가와는 별개다
        mark = WARN if error == "missing_scope" else FAIL
        _line(mark, f"채널 정보 조회 실패: {error}", _hint(error))
        return channel_id

    ch = data.get("channel", {})
    _line(OK, f"채널명 #{ch.get('name')} (private={ch.get('is_private')})")
    if ch.get("is_archived"):
        _line(FAIL, "보관된 채널이라 업로드할 수 없다")
        return ""
    if ch.get("is_member"):
        _line(OK, "봇이 채널 멤버다")
    else:
        _line(FAIL, "봇이 채널에 없다", "채널에서 `/invite @봇이름` 실행 후 다시 검증")
    return channel_id


def make_test_pdf() -> str:
    """검증 전용 PDF 1장 생성 — 실제 리포트 렌더러를 그대로 태워 폰트까지 함께 확인한다."""
    from app.services.kr import kr_pdf_service

    now = datetime.now()
    ctx = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now,
        "mode": "업로드 검증",
        "execution_time": settings.KR_EXECUTION_TIME,
        "market": {"date": now.strftime("%Y-%m-%d")},
        "narrative": {
            "headline": "Slack 업로드 검증용 테스트 문서입니다 — 매매 판단과 무관합니다.",
            "market_view": "이 PDF 는 scripts/verify_slack_report.py 가 만든 검증용 문서다.\n"
                           "한글이 깨지지 않고 보이면 폰트 설정도 정상이다.",
        },
        "quote": {"rows": [], "note": "검증용 문서라 매수 견적 내용이 없습니다."},
    }
    out = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"slack_upload_test_{now:%Y%m%d_%H%M%S}.pdf",
    )
    kr_pdf_service.build_report_pdf(ctx, out)
    return out


def check_upload(path: str) -> bool:
    print("\n━━ 4. 실제 업로드 ━━")
    _line(INFO, f"파일: {path} ({os.path.getsize(path):,} bytes)")
    ok = sf.upload_file(
        path,
        title=f"[업로드 검증] {datetime.now():%Y-%m-%d %H:%M}",
        initial_comment=(
            "🧪 *Slack PDF 업로드 검증*\n"
            "`scripts/verify_slack_report.py` 가 보낸 테스트 문서입니다. "
            "이 메시지가 보이면 16:30 분석 리포트도 같은 경로로 전송됩니다."
        ),
    )
    if ok:
        _line(OK, "업로드 성공 — Slack 채널을 확인하세요")
    else:
        _line(FAIL, "업로드 실패 — 위 로그의 Slack 에러 코드를 확인하세요")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Slack 리포트 PDF 업로드 검증")
    parser.add_argument("--dry", action="store_true", help="실제 업로드 없이 사전 점검만")
    parser.add_argument("--file", help="업로드할 PDF 경로 (없으면 테스트 PDF 생성)")
    args = parser.parse_args()

    print("═══ Slack 리포트 PDF 업로드 검증 ═══")
    if not check_config():
        print("\n결과: 설정 미비로 중단")
        return 1
    if not check_auth():
        print("\n결과: 토큰/권한 문제로 중단")
        return 1
    if not check_channel():
        print("\n결과: 채널 문제로 중단")
        return 1

    if args.dry:
        print("\n결과: 사전 점검 통과 (--dry 라 업로드는 생략)")
        return 0

    path = args.file or make_test_pdf()
    success = check_upload(path)
    if not args.file:
        try:
            os.remove(path)
        except OSError:
            pass

    print(f"\n결과: {'검증 성공' if success else '검증 실패'}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
