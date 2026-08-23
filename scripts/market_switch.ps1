<#
.SYNOPSIS
    시장별 매수 원격 on/off — Windows(PowerShell) 용 래퍼.

.DESCRIPTION
    market_switch.sh 의 PowerShell 판. Windows 에서 bash/curl.exe 를 거치지 않고
    쓰기 위한 것이며, 한글 사유가 어떤 콘솔 설정에서도 깨지지 않도록 인코딩을 고정한다.

    한글이 깨질 수 있는 지점이 두 군데다:

      1) 이 .ps1 파일 자체 — PowerShell 5.1 은 BOM 없는 UTF-8 파일을 시스템 ANSI 로
         읽는다. 그래서 이 파일은 반드시 **UTF-8 with BOM** 으로 저장해야 한다.
         (편집 후 BOM 이 사라지면 기본 사유 '외출중' 부터 깨진다)
      2) 요청 인코딩 — PowerShell 5.1 의 -Body 해시테이블 인코딩은 환경에 따라
         Latin-1 로 떨어질 수 있다. 그래서 [Uri]::EscapeDataString 으로 UTF-8
         퍼센트 인코딩을 직접 만들어 URL 에 붙인다 (콘솔 코드페이지와 무관).

    Git Bash(LANG=ko_KR.UTF-8)에서는 market_switch.sh 를 그대로 써도 정상이다.

    ※ 참고 — 과거 US 스위치에 사유가 "???" 로 저장된 적이 있는데, 위 두 경로 모두
      이 환경에서 재현되지 않았다(셋 다 정상 UTF-8 전송 확인). 원인 미상이므로
      사유가 물음표로만 남아 있으면 그대로 믿지 말고 다시 설정할 것.

.PARAMETER Action
    off | on | status   (기본 off)

.PARAMETER Market
    US | KR | both      (기본 both)

.PARAMETER Reason
    중단 사유. off 일 때만 쓰인다. (기본 "외출중")

.EXAMPLE
    .\scripts\market_switch.ps1 status
.EXAMPLE
    .\scripts\market_switch.ps1 off both "출장 - 8/25 복귀"
.EXAMPLE
    .\scripts\market_switch.ps1 on US
.NOTES
    서버 주소는 MARKET_SWITCH_HOST 환경변수로 바꾼다 (기본 http://localhost:8000).
#>
[CmdletBinding()]
param(
    [ValidateSet('off', 'on', 'status')]
    [string]$Action = 'off',

    [ValidateSet('US', 'KR', 'both')]
    [string]$Market = 'both',

    [string]$Reason = '외출중'
)

$ErrorActionPreference = 'Stop'

$Host_ = if ($env:MARKET_SWITCH_HOST) { $env:MARKET_SWITCH_HOST } else { 'http://localhost:8000' }

function Show-Result {
    param($Response)
    # PSCustomObject 를 사람이 읽을 형태로. 깊이가 얕아 Depth 5 면 충분하다.
    $Response | ConvertTo-Json -Depth 5
}

function Invoke-Toggle {
    param([string]$M)

    if ($Action -eq 'off') {
        # 사유는 UTF-8 로 퍼센트 인코딩해서 URL 에 직접 붙인다.
        # (-Body 해시테이블은 PowerShell 판본/환경에 따라 인코딩이 달라질 수 있다)
        $encoded = [Uri]::EscapeDataString($Reason)
        $uri = "$Host_/market-switch/$M/disable?reason=$encoded"
    }
    else {
        $uri = "$Host_/market-switch/$M/enable"
    }

    try {
        $resp = Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json; charset=utf-8'
        Show-Result $resp
    }
    catch {
        Write-Error "$M 요청 실패: $($_.Exception.Message)"
        exit 1
    }
}

if ($Action -eq 'status') {
    try {
        Show-Result (Invoke-RestMethod -Method Get -Uri "$Host_/market-switch")
    }
    catch {
        Write-Error "상태 조회 실패: $($_.Exception.Message)"
        exit 1
    }
    exit 0
}

if ($Action -eq 'off' -and [string]::IsNullOrWhiteSpace($Reason)) {
    Write-Error 'off 에는 사유가 필요합니다 (감사 로그에 남습니다)'
    exit 1
}

switch ($Market) {
    'both' { Invoke-Toggle 'US'; Invoke-Toggle 'KR' }
    default { Invoke-Toggle $Market }
}
