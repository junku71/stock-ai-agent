import uvicorn

# Windows cp949 콘솔에서 로그가 UnicodeEncodeError 로 깨지는 것을 막는다.
# (app 패키지 임포트 시에도 적용되지만, 진입점에서 먼저 잡아둔다)
from app.core.console import configure_utf8_console

configure_utf8_console()

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)