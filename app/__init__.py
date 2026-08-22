"""
주식 자동매매 시스템 (미국 + 한국 트랙).

패키지 임포트 시 콘솔 인코딩을 UTF-8 로 정규화한다.
Windows cp949 콘솔에서 로그의 '—' / '→' / '⚠️' 때문에 로깅이 실패하는 것을 막는다.
여기서 처리해야 실행 방법(run.py / app/main.py / ad-hoc 스크립트)에 관계없이 적용된다.
"""
from app.core.console import configure_utf8_console

configure_utf8_console()
