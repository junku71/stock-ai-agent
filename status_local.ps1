<#
.SYNOPSIS
  status_local.ps1 — 로컬 미니PC(Task Scheduler)의 자동매매 시스템이 정상 작동 중인지 점검한다.
.사용법
  powershell -ExecutionPolicy Bypass -File status_local.ps1
.설정 덮어쓰기
  $env:APP_TASK_NAME, $env:APP_DIR, $env:APP_PORT, $env:APP_LOG
#>

$TaskName = if ($env:APP_TASK_NAME) { $env:APP_TASK_NAME } else { "StockAutoTrading" }
$AppDir   = if ($env:APP_DIR) { $env:APP_DIR } else { "C:\Users\junku\Desktop\999.project\stock-ai-agent" }
$Port     = if ($env:APP_PORT) { $env:APP_PORT } else { 8000 }
$LogFile  = if ($env:APP_LOG) { $env:APP_LOG } else { Join-Path $AppDir "stock_scheduler.log" }

Write-Host "════════════════════════════════════════════════════"
Write-Host " 자동매매 서버 상태   ( $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') )"
Write-Host "════════════════════════════════════════════════════"

# [1] 작업 스케줄러
Write-Host "`n[1] 작업 스케줄러"
try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "  상태        : $($task.State)"
    Write-Host "  마지막 실행 : $($info.LastRunTime)  (결과코드: $($info.LastTaskResult))"
    Write-Host "  다음 실행   : $($info.NextRunTime)"
} catch {
    Write-Host "  ⚠️ 작업 '$TaskName' 을(를) 찾을 수 없음"
}

# [2] 프로세스 (CPU/MEM/구동시간)
Write-Host "`n[2] 프로세스 (MEM/시작시각)"
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "run\.py" }
if ($procs) {
    foreach ($p in $procs) {
        $mem = [math]::Round($p.WorkingSetSize / 1MB, 1)
        Write-Host "  PID $($p.ProcessId)  MEM ${mem}MB  시작: $($p.CreationDate)"
    }
} else {
    Write-Host "  ⚠️ run.py 프로세스 없음!"
}

# [3] 앱 HTTP 응답
Write-Host "`n[3] 앱 HTTP 응답"
try {
    $res = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 5 -UseBasicParsing
    Write-Host "  ✅ HTTP $($res.StatusCode)"
} catch {
    Write-Host "  ⚠️ 응답 없음 (기동 중이거나 다운): $($_.Exception.Message)"
}

# [4] 스케줄러 기동 확인
Write-Host "`n[4] 스케줄러 기동 확인"
if (Test-Path $LogFile) {
    $started = Select-String -Path $LogFile -Encoding Default -Pattern "스케줄러가 시작|파이프라인 스케줄러 시작" -ErrorAction SilentlyContinue |
        Select-Object -Last 3
    if ($started) { $started | ForEach-Object { Write-Host "  $($_.Line)" } } else { Write-Host "  (로그 없음)" }
} else {
    Write-Host "  ⚠️ 로그 파일 없음: $LogFile"
}

# [5] 메모리 / 디스크
Write-Host "`n[5] 메모리 / 디스크"
$os = Get-CimInstance Win32_OperatingSystem
$totalMemGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$freeMemGB  = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
Write-Host "  메모리 : $freeMemGB GB free / $totalMemGB GB total"
$driveLetter = (Split-Path -Qualifier $AppDir).TrimEnd(':')
$drive = Get-PSDrive -Name $driveLetter
$driveTotalGB = [math]::Round(($drive.Free + $drive.Used) / 1GB, 1)
$driveFreeGB  = [math]::Round($drive.Free / 1GB, 1)
Write-Host "  디스크($driveLetter`:) : $driveFreeGB GB free / $driveTotalGB GB total"

# [6] 최근 에러
Write-Host "`n[6] 최근 에러 (ERROR/Traceback/Exception)"
if (Test-Path $LogFile) {
    $errs = Select-String -Path $LogFile -Encoding Default -Pattern "ERROR|Traceback|Exception" -ErrorAction SilentlyContinue |
        Select-Object -Last 5
    if ($errs) { $errs | ForEach-Object { Write-Host "  $($_.Line)" } } else { Write-Host "  ✅ 에러 없음" }
}

# [7] 최근 매매/파이프라인 활동
Write-Host "`n[7] 최근 매매/파이프라인 활동"
if (Test-Path $LogFile) {
    $act = Select-String -Path $LogFile -Encoding Default -Pattern "자동 매수|자동 매도|매수 주문|매도 주문|LLM 검토|Daily Pipeline|파이프라인 전체|매수 후보" -ErrorAction SilentlyContinue |
        Select-Object -Last 5
    if ($act) { $act | ForEach-Object { Write-Host "  $($_.Line)" } } else { Write-Host "  (아직 없음)" }
}

# [8] 최근 로그 5줄
Write-Host "`n[8] 최근 로그 5줄"
if (Test-Path $LogFile) {
    Get-Content -Path $LogFile -Tail 5 -Encoding Default | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "  ⚠️ 로그 파일 없음: $LogFile"
}

Write-Host "════════════════════════════════════════════════════"
