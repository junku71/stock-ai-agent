#!/usr/bin/env bash
# 시장별 매수 원격 on/off — 짧게 쓰기용 래퍼.
#
# 사용법:
#   ./scripts/market_switch.sh off [US|KR|both] ["사유"]
#   ./scripts/market_switch.sh on  [US|KR|both]
#   ./scripts/market_switch.sh status
#
# 인자 생략 시 기본값: off/on 은 both, 사유는 "외출중".
# 서버 주소를 바꾸려면 MARKET_SWITCH_HOST 환경변수로 override (기본 http://localhost:8000).
#
# Windows(PowerShell)에서는 scripts/market_switch.ps1 을 쓰는 편이 안전하다 —
# bash/curl.exe 를 거치지 않고 UTF-8 인코딩을 고정해 한글 사유가 깨질 여지를 없앴다.
# Git Bash(LANG=ko_KR.UTF-8)에서는 이 스크립트를 그대로 써도 정상이다(실측 확인).
set -euo pipefail

ACTION="${1:-off}"
MARKET="${2:-both}"
REASON="${3:-외출중}"
HOST="${MARKET_SWITCH_HOST:-http://localhost:8000}"

toggle() {
    local market="$1"
    case "$ACTION" in
        off)
            curl -sS -X POST -G "$HOST/market-switch/$market/disable" \
                --data-urlencode "reason=$REASON"
            ;;
        on)
            curl -sS -X POST "$HOST/market-switch/$market/enable"
            ;;
        *)
            echo "첫 인자는 off 또는 on 이어야 합니다 (status 는 인자 없이)" >&2
            exit 1
            ;;
    esac
    echo
}

if [ "$ACTION" = "status" ]; then
    curl -sS "$HOST/market-switch"
    echo
    exit 0
fi

case "$MARKET" in
    US|KR) toggle "$MARKET" ;;
    both)  toggle US; toggle KR ;;
    *) echo "두번째 인자는 US, KR, both 중 하나여야 합니다" >&2; exit 1 ;;
esac
