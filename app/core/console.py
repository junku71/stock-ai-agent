"""
콘솔 출력 인코딩 정규화.

Windows 기본 콘솔 인코딩은 cp949 라서 로그 메시지에 '—'(em dash), '→', '⚠️' 같은
문자가 하나라도 있으면 UnicodeEncodeError 로 **로깅 자체가 실패한다**.
이때 메시지는 유실되고 대신 긴 스택트레이스만 찍혀서, 진짜 오류를 가리고
정상 동작 중인데도 서버가 깨진 것처럼 보인다.

실행 방법마다 `PYTHONIOENCODING=utf-8` 을 붙이게 하는 대신 여기서 한 번에 정규화한다.
app/__init__.py 가 임포트 시점에 호출하므로, `python run.py` 든 `python app/main.py` 든
`python -c "from app.services..."` 든 모두 적용된다.
"""
import sys


def configure_utf8_console() -> None:
    """stdout/stderr 을 UTF-8 로 강제한다. 실패해도 예외를 올리지 않는다."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # 파이프·리다이렉트 등 reconfigure 를 지원하지 않는 스트림
            continue
        try:
            # errors="replace" — 혹시 남는 문자가 있어도 예외 대신 대체 문자로 출력
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
