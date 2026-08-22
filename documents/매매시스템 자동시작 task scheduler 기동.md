



## Task Scheduler 방식 설명

핵심 아이디어: "부팅되면 자동으로 시작되고, 죽으면 자동으로 재시작되는 예약 작업"을 등록하는 것. 
NSSM처럼 진짜 Windows 서비스는 아니지만, 옵션을 잘 잡으면 사실상 비슷하게 동작합니다.

**등록 시 핵심 설정 4가지:**

| 탭 | 설정 | 이유 |
|---|---|---|
| 트리거 | "시스템 시작 시" (로그온 시가 아님) | 로그인 안 해도 부팅되면 바로 실행 |
| 일반 | "사용자 로그온 여부에 관계없이 실행" + "최고 권한으로 실행" | 화면 잠금/로그아웃 상태에서도 유지 |
| 조건 | "AC 전원에 연결된 경우에만 시작" **체크 해제** | 미니PC는 배터리 없어도 기본값이 노트북 기준이라 꺼야 함 |
| 설정 | "작업 실패 시 다시 시작 간격" = 1분, 3회 → "실패 시 계속 재시작"까지 | 크래시 시 자동 복구. "지정된 시간 동안 실행되면 작업 중지"는 **반드시 체크 해제** (장시간 서버니까 안 그러면 강제 종료됨) |

**주의점 2가지:**
- `run.py`가 `stock_scheduler.log`를 상대경로로 남기므로, 작업의 "시작 위치(Start in)"를 프로젝트 루트로 지정해야 로그 위치가 꼬이지 않습니다.
- `python.exe`는 PATH의 것 말고 가상환경 것을 직접 지정해야 합니다: `.venv\Scripts\python.exe`

**등록 명령 (schtasks, 관리자 PowerShell에서 1회 실행):**
```powershell
schtasks /create /tn "StockAutoTrading" /tr "'C:\Users\junku\Desktop\999.project\stock-ai-agent\.venv\Scripts\python.exe' run.py" /sc onstart /ru SYSTEM /rl HIGHEST /f
schtasks /change /tn "StockAutoTrading" /ri 1 /du 9999:59
```
(GUI에서 "시작 위치"를 지정하는 옵션은 schtasks에 없어서, `python.exe`를 절대경로로 쓰거나 GUI로 등록 후 "동작 편집"에서 "시작 위치"란에 프로젝트 폴더를 채우는 걸 추천 — GUI 쪽이 더 정확합니다.)

원하시면 GUI 단계별 스크린샷 없이 클릭 순서로도 안내해드릴 수 있어요.

## 상태 체크 스크립트

기존 [status2.sh](status2.sh)는 SSH로 Lightsail 원격 systemd 서비스를 점검하는 구조였는데, 이건 로컬 Windows에서 도는 프로세스라 systemd/SSH가 없습니다. 그래서 같은 8단계 구성을 PowerShell로 재구현했습니다 (Task Scheduler 상태, 프로세스, HTTP 응답, 스케줄러 로그, 메모리/디스크, 최근 에러, 매매 활동, 최근 로그).

인코딩 문제(PowerShell 5.1이 BOM 없는 UTF-8을 한글로 못 읽음)가 있네요. BOM 붙여서 재저장하고 다시 테스트하겠습니다.

로그 파일 자체가 cp949(시스템 기본 인코딩)로 저장되고 있어서, 스크립트에서 명시적으로 인코딩을 지정해야 합니다. 수정하겠습니다.

완벽하게 동작합니다. 실제로 지금 이 미니PC에서 [run.py](run.py)가 이미 돌고 있었고 (PID 42120), 어제 매수 주문(MSFT, AMZN)이 실제로 실행된 로그까지 확인됐어요.

## 결과: [status_local.ps1](status_local.ps1)

## Local server status check할 때 쓰는 명령어( 반드시 PS관리자모드실행필요)
powershell -ExecutionPolicy Bypass -File status_local.ps1
기존 [status2.sh](status2.sh)와 같은 8단계 구조로 만들었습니다:

| 항목 | 확인 내용 |
|---|---|
| [1] 작업 스케줄러 | Task Scheduler 등록 상태 / 마지막·다음 실행 |
| [2] 프로세스 | `run.py` 프로세스 PID, 메모리, 시작 시각 |
| [3] HTTP 응답 | `localhost:8000` 응답 확인 |
| [4] 스케줄러 기동 확인 | 매수/매도/파이프라인 스케줄러 시작 로그 |
| [5] 메모리/디스크 | 시스템 리소스 여유량 |
| [6] 최근 에러 | ERROR/Traceback/Exception |
| [7] 최근 매매 활동 | 매수/매도 주문, LLM 검토, 파이프라인 로그 |
| [8] 최근 로그 5줄 | tail |

**사용법:**
```bash
powershell -ExecutionPolicy Bypass -File status_local.ps1
```

**설정 덮어쓰기** (status2.sh의 `DEPLOY_KEY` 방식과 동일하게 환경변수로):
```powershell
$env:APP_TASK_NAME = "다른작업이름"
$env:APP_DIR = "D:\other-path"
```

**참고:**
- `stock_scheduler.log`가 시스템 기본 인코딩(cp949)으로 저장되고 있어서, 스크립트에서 `-Encoding Default`로 명시해 한글이 깨지지 않게 처리했습니다.
- `[6] 최근 에러`에 보이는 `httpx.ConnectError [Errno 11001]`는 일시적 DNS 조회 실패로, 재시도 로직이 있다면 큰 문제는 아닐 수 있습니다 — 반복되면 확인이 필요합니다.
- 아직 Task Scheduler에 `StockAutoTrading` 작업이 등록되어 있지 않은 것 같은데(⚠️ 표시), 지금은 프로세스가 어떻게 떠 있는 상태인지 궁금하신가요? 등록을 진행해드릴까요?



##  윈도우 시작시 Task Scheduling이 제대로 되어 있는지 확인하는 방법.

확인 방법을 알려드리기 전에, 제 세션에서 직접 조회해봤는데 **"StockAutoTrading" 작업이 안 보입니다** — 이 부분부터 짚어야 할 것 같아요.

확인해보니 **제 쪽에서는 "StockAutoTrading" 작업이 전혀 안 보입니다.** 이 세션은 관리자 권한은 아니지만(`IsInRole Administrator = False`), 
현재 이 PC에 등록된 작업 210개는 정상적으로 다 조회되는 걸 봐서 권한 문제가 아니라 **실제로 등록이 안 됐거나 실패했을 가능성**이 높아요.

## 확인 방법 3가지

**1) GUI (가장 확실)**
```
Win + R → taskschd.msc → 엔터
```
왼쪽 트리에서 "작업 스케줄러 라이브러리" 클릭 → 가운데 목록에서 `StockAutoTrading` 검색. 있다면 상태/트리거/마지막 실행 결과가 표에 바로 보입니다.

**2) CLI — schtasks**
```bash
schtasks /query /tn "StockAutoTrading" /v /fo list
```
등록됐다면 상태, 마지막 실행 시각, 다음 실행 시각 등이 쭉 나옵니다.

**3) CLI — PowerShell**
```powershell
Get-ScheduledTask -TaskName "StockAutoTrading"
```


