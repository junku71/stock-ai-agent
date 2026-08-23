"""
Slack 파일 업로드 (분석 리포트 PDF 전송용).

Incoming Webhook(`SLACK_WEBHOOK_URL`)은 텍스트 전용이라 파일을 첨부할 수 없다.
그래서 PDF 는 Bot Token 기반 Web API 3단계로 올린다
(구 `files.upload` 는 2025-03 폐지되어 신규 앱에서 동작하지 않는다):

  1) files.getUploadURLExternal   — 업로드 URL + file_id 발급
  2) POST <upload_url>            — 파일 바이트 전송
  3) files.completeUploadExternal — 채널에 공유 (제목/코멘트 첨부)

필요 설정:
  SLACK_BOT_TOKEN      xoxb-…  (OAuth scope: `files:write`,
                                #채널명으로 지정할 경우 `channels:read` 추가)
  SLACK_REPORT_CHANNEL 채널 ID(`C…`) 권장. `#채널명` 도 허용하며 이 경우 1회 조회 후 캐시한다.

둘 중 하나라도 비어 있으면 모든 함수가 조용히 no-op 한다 — 기존 Webhook 알림은 그대로 동작한다.
"""
import logging
import os
from typing import Optional

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
TIMEOUT = 30

# #채널명 → 채널ID 해석 결과 캐시 (봇 재기동 전까지 유지)
_channel_id_cache: dict = {}


def is_configured() -> bool:
    """Bot Token + 채널이 모두 설정돼 있어야 업로드가 가능하다."""
    return bool(settings.SLACK_BOT_TOKEN and settings.SLACK_REPORT_CHANNEL)


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"}


def _resolve_channel(value: str) -> Optional[str]:
    """
    `#채널명` → 채널 ID. 이미 ID 형태(C/G/D 로 시작)면 그대로 반환한다.

    completeUploadExternal 은 채널명을 받지 않고 ID 만 받기 때문에 필요한 변환이다.
    conversations.list 는 `channels:read`(비공개는 `groups:read`) scope 를 요구하므로,
    권한이 없으면 None 을 반환하고 호출부가 업로드를 건너뛴다.
    """
    value = (value or "").strip()
    if not value:
        return None
    if not value.startswith("#") and value[0] in "CGD":
        return value
    if value in _channel_id_cache:
        return _channel_id_cache[value]

    name = value.lstrip("#")
    cursor = ""
    try:
        for _ in range(10):  # 최대 10페이지(=2000채널)까지만 탐색
            resp = requests.get(
                f"{SLACK_API}/conversations.list",
                headers=_headers(),
                params={
                    "limit": 200,
                    "exclude_archived": "true",
                    "types": "public_channel,private_channel",
                    **({"cursor": cursor} if cursor else {}),
                },
                timeout=TIMEOUT,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning(f"Slack 채널 목록 조회 실패: {data.get('error')}")
                return None
            for ch in data.get("channels", []):
                if ch.get("name") == name:
                    _channel_id_cache[value] = ch["id"]
                    return ch["id"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
    except Exception as e:
        logger.warning(f"Slack 채널 ID 해석 실패: {e}")
        return None

    logger.warning(f"Slack 채널 '{value}' 을(를) 찾지 못했습니다 (봇이 초대돼 있는지 확인)")
    return None


def upload_file(
    file_path: str,
    title: Optional[str] = None,
    initial_comment: Optional[str] = None,
    channel: Optional[str] = None,
) -> bool:
    """
    파일 1개를 Slack 채널에 업로드한다. 실패해도 예외를 올리지 않고 False 를 반환한다
    (리포트 전송 실패가 매매 파이프라인을 막으면 안 된다).
    """
    if not is_configured():
        logger.info("SLACK_BOT_TOKEN/SLACK_REPORT_CHANNEL 미설정 — PDF 업로드 생략")
        return False

    if not os.path.exists(file_path):
        logger.warning(f"업로드할 파일이 없습니다: {file_path}")
        return False

    channel_id = _resolve_channel(channel or settings.SLACK_REPORT_CHANNEL)
    if not channel_id:
        return False

    filename = os.path.basename(file_path)
    length = os.path.getsize(file_path)

    try:
        # ① 업로드 URL 발급
        resp = requests.get(
            f"{SLACK_API}/files.getUploadURLExternal",
            headers=_headers(),
            params={"filename": filename, "length": length},
            timeout=TIMEOUT,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning(f"Slack 업로드 URL 발급 실패: {data.get('error')}")
            return False
        upload_url, file_id = data["upload_url"], data["file_id"]

        # ② 파일 전송 (이 요청에는 Bearer 토큰을 붙이지 않는다 — 서명된 일회성 URL)
        with open(file_path, "rb") as f:
            up = requests.post(upload_url, files={"file": (filename, f)}, timeout=120)
        if up.status_code != 200:
            logger.warning(f"Slack 파일 전송 실패 ({up.status_code}): {up.text[:200]}")
            return False

        # ③ 채널 공유 완료 처리
        payload = {
            "files": [{"id": file_id, "title": title or filename}],
            "channel_id": channel_id,
        }
        if initial_comment:
            payload["initial_comment"] = initial_comment

        done = requests.post(
            f"{SLACK_API}/files.completeUploadExternal",
            headers={**_headers(), "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=TIMEOUT,
        )
        result = done.json()
        if not result.get("ok"):
            logger.warning(f"Slack 업로드 완료 처리 실패: {result.get('error')}")
            return False

        logger.info(f"Slack PDF 업로드 완료: {filename} → {channel_id}")
        return True

    except Exception as e:
        logger.warning(f"Slack 파일 업로드 예외: {e}")
        return False
