"""
진입점 — API 서버 + 매매 스케줄러 + 운영 메뉴를 한 프로세스로 띄운다.

    python run.py            서버(백그라운드 스레드) + 번호 메뉴(포그라운드)
    python run.py --no-menu  서버만 (systemd/nohup 등 입력이 없는 환경)
    python run.py --menu-only 메뉴만 (서버·스케줄러 없이 조회/스크리닝만)

## 왜 스레드로 나누나

uvicorn.run() 은 터미널을 점유해서 같은 화면에 메뉴를 띄울 수 없다. 그래서 서버를
데몬 스레드에 넣고 메인 스레드가 메뉴를 잡는다. 스케줄러는 FastAPI lifespan 에서
기동하므로 서버 스레드 안에서 함께 뜬다.

메뉴를 종료하면(0번) 서버 스레드에 종료 신호를 보내 스케줄러 shutdown 훅까지 돌게
한다. 그냥 프로세스를 죽이면 미체결 주문 정리 같은 마무리가 생략된다.
"""
import argparse
import sys
import threading
import time

from app.core.console import configure_utf8_console

# Windows cp949 콘솔에서 로그가 UnicodeEncodeError 로 깨지는 것을 막는다.
configure_utf8_console()

from app.core.logging_config import setup as setup_logging  # noqa: E402

HOST = "0.0.0.0"
PORT = 8000


def _serve(server) -> None:
    try:
        server.run()
    except Exception:
        import logging

        logging.getLogger(__name__).error("API 서버가 예외로 종료됐습니다", exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="국내주식 자동매매 시스템")
    parser.add_argument("--no-menu", action="store_true",
                        help="번호 메뉴 없이 서버만 실행 (입력이 없는 환경)")
    parser.add_argument("--menu-only", action="store_true",
                        help="서버/스케줄러 없이 메뉴만 실행")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    # 메뉴를 쓸 때는 로그를 파일로만 보낸다 — 콘솔로 흘리면 메뉴가 계속 깨진다.
    log_file = setup_logging(console=args.no_menu)

    if args.menu_only:
        from app.cli.menu import run_menu

        print(f"메뉴 전용 모드 — 스케줄러는 기동하지 않습니다. (로그: {log_file})")
        run_menu()
        return 0

    import uvicorn

    config = uvicorn.Config(
        "app.main:app",
        host=HOST,
        port=args.port,
        reload=False,
        # uvicorn 이 자체 로깅 설정으로 우리 핸들러를 덮어쓰지 않게 한다
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=_serve, args=(server,), name="api-server", daemon=True)
    thread.start()

    if args.no_menu:
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
            thread.join(timeout=20)
        return 0

    # 서버가 뜨고 스케줄러가 기동할 시간을 잠깐 준다 (메뉴 첫 화면의 상태가 정확해지도록)
    for _ in range(50):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)

    if not thread.is_alive():
        print("API 서버 기동에 실패했습니다. 로그를 확인하세요:", log_file)
        return 1

    print(f"API 서버 기동 완료 — http://localhost:{args.port}/docs")

    from app.cli.menu import run_menu

    def shutdown() -> None:
        print("  매매 스케줄러를 정리하는 중...")
        server.should_exit = True
        thread.join(timeout=20)

    run_menu(on_exit=shutdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
