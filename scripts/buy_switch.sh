#!/usr/bin/env bash
# 신규 매수 원격 on/off — 짧게 쓰기용 래퍼.
#
# 사용법:
#   ./scripts/buy_switch.sh off ["사유"]
#   ./scripts/buy_switch.sh on
#   ./scripts/buy_switch.sh status
#
# 사유 생략 시 기본값은 "외출중".
# 서버 주소를 바꾸려면 BUY_SWITCH_HOST 환경변수로 override (기본 http://localhost:8000).
#
# Windows(PowerShell)에서는 scripts/buy_switch.ps1 을 쓰는 편이 안전하다 —
# bash/curl.exe 를 거치지 않고 UTF-8 인코딩을 고정해 한글 사유가 깨질 여지를 없앴다.
# Git Bash(LANG=ko_KR.UTF-8)에서는 이 스크립트를 그대로 써도 정상이다(실측 확인).
set -euo pipefail

ACTION="${1:-off}"
REASON="${2:-외출중}"
HOST="${BUY_SWITCH_HOST:-http://localhost:8000}"

case "$ACTION" in
    status)
        curl -sS "$HOST/buy-switch"
        ;;
    off)
        curl -sS -X POST -G "$HOST/buy-switch/disable" \
            --data-urlencode "reason=$REASON"
        ;;
    on)
        curl -sS -X POST "$HOST/buy-switch/enable"
        ;;
    *)
        echo "첫 인자는 off, on, status 중 하나여야 합니다" >&2
        exit 1
        ;;
esac
echo
