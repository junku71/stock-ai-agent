"""
로깅 설정 — 파일 기본, 콘솔은 필요할 때만.

메뉴 UI 와 매매 스케줄러가 한 프로세스에서 같이 돌기 때문에 로그를 그냥 콘솔로
흘리면 메뉴가 계속 깨진다. 1분 주기 매도 감시가 사용자가 번호를 입력하는 중에도
로그를 뱉기 때문이다.

그래서 기본은 **파일에만** 남기고, 사용자가 메뉴에서 오래 걸리는 작업(스크리닝,
유니버스 갱신 등)을 고른 동안에만 콘솔 핸들러를 잠깐 붙였다 뗀다. 그 시간에는
어차피 입력을 기다리지 않으므로 화면이 섞이지 않는다.

    with console_logging():
        ... 오래 걸리는 작업 ...   # 진행 상황이 화면에 보인다
    # 빠져나오면 다시 파일로만
"""
import logging
import logging.handlers
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

_CONSOLE_TAG = "_menu_console"
_configured = False


def log_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "kr_trading.log"


def setup(level: int = logging.INFO, console: bool = False) -> Path:
    """
    루트 로거에 회전 파일 핸들러를 붙인다. 두 번 불러도 핸들러가 겹치지 않는다.

    console=True 면 콘솔 핸들러도 상시로 붙인다 (메뉴 없이 서버만 띄울 때).
    """
    global _configured
    path = log_path()
    root = logging.getLogger()
    root.setLevel(level)

    if not _configured:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
        )
        root.addHandler(handler)

        # 서드파티 소음 억제 — KIS/네이버/Supabase/Claude 호출마다 나오는 접속 로그가
        # 우리 로그를 덮는다. anthropic SDK 는 내부적으로 httpx 가 아니라 httpx2 를
        # 쓰므로("HTTP Request: POST ..." 가 "httpx2" 로거로 찍힌다) 같이 잠가야 한다.
        for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "hpack", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _configured = True

    if console and not _find_console_handler():
        root.addHandler(_make_console_handler())

    return path


def _make_console_handler() -> logging.Handler:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.set_name(_CONSOLE_TAG)
    return h


def _find_console_handler() -> Optional[logging.Handler]:
    for h in logging.getLogger().handlers:
        if h.get_name() == _CONSOLE_TAG:
            return h
    return None


@contextmanager
def console_logging(level: int = logging.INFO):
    """블록 안에서만 로그를 콘솔에도 내보낸다."""
    root = logging.getLogger()
    existing = _find_console_handler()
    if existing is not None:
        # 이미 붙어 있으면(서버 단독 모드) 그대로 두고 아무것도 하지 않는다
        yield
        return

    handler = _make_console_handler()
    handler.setLevel(level)
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
