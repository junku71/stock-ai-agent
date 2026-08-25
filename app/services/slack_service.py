"""
Slack Incoming Webhook 저수준 전송.

첨부(attachment) 1개짜리 메시지를 보내는 것만 담당한다. 어떤 문구를 어떤 색으로
보낼지는 호출부(app/services/kr/kr_notification_service.py 등)가 정한다.

SLACK_WEBHOOK_URL 미설정 시 조용히 no-op 한다 — 알림은 부가 기능이므로 설정이
없다고 해서 매매 로직이 멈추면 안 된다. 전송 실패도 예외를 던지지 않고 False 만
돌려준다.

파일(PDF 리포트) 첨부는 Webhook 으로 불가능하다. Bot Token 이 필요한 별도 경로로,
app/services/kr/slack_file_service.py 가 담당한다.

참조: documents/09_Slack_연동.md
"""
import logging
import requests
from typing import Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

def _send(
    title: str,
    message: str,
    color: str = "#36a64f",
    fields: Optional[Dict[str, str]] = None,
) -> bool:
    """Slack Webhook 으로 attachment 1개 발송. 실패해도 본 로직 안 막음."""
    if not settings.SLACK_WEBHOOK_URL:
        return False

    attachment = {
        "color": color,
        "title": title,
        "text": message,
        "mrkdwn_in": ["text", "fields"],
    }
    if fields:
        attachment["fields"] = [
            {"title": k, "value": str(v), "short": True}
            for k, v in fields.items()
        ]

    try:
        resp = requests.post(
            settings.SLACK_WEBHOOK_URL,
            json={"attachments": [attachment]},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning(f"Slack 전송 실패 ({resp.status_code}): {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Slack 전송 예외: {e}")
        return False
