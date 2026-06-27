#!/usr/bin/env bash
#
# status.sh — Lightsail 서버의 자동매매 시스템이 정상 작동 중인지 점검한다.
# 사용법:  bash status.sh
# 설정 덮어쓰기:  DEPLOY_KEY, DEPLOY_HOST, DEPLOY_SERVICE 환경변수
#
set -uo pipefail   # -e 제외: 일부 점검이 실패해도 끝까지 출력

KEY="${DEPLOY_KEY:-C:/Users/user/Desktop/key/bangab-app.pem}"
HOST="${DEPLOY_HOST:-ec2-user@11.111.11.11}"
SERVICE="${DEPLOY_SERVICE:-stocktrading}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null"

ssh -i "$KEY" $SSH_OPTS "$HOST" "bash -s" <<REMOTE
echo "════════════════════════════════════════════════════"
echo " 자동매매 서버 상태   ( \$(date '+%Y-%m-%d %H:%M:%S %Z') )"
echo "════════════════════════════════════════════════════"

echo ""
echo "[1] 서비스"
echo "  active : \$(systemctl is-active $SERVICE)"
echo "  enabled: \$(systemctl is-enabled $SERVICE)"
echo "  started: \$(systemctl show $SERVICE -p ActiveEnterTimestamp --value)"

echo ""
echo "[2] 프로세스 (CPU/MEM/구동시간)"
PROC=\$(ps -eo pid,%cpu,%mem,etime,args | grep '[u]vicorn app.main')
if [ -n "\$PROC" ]; then echo "\$PROC" | sed -E 's#/home/[^ ]*/uvicorn#uvicorn#; s/^/  /'; else echo "  ⚠️ uvicorn 프로세스 없음!"; fi

echo ""
echo "[3] 앱 HTTP 응답"
R=\$(curl -s -m 5 http://127.0.0.1:8000/ 2>/dev/null || true)
if [ -n "\$R" ]; then echo "  ✅ \$R"; else echo "  ⚠️ 응답 없음 (기동 중이거나 다운)"; fi

echo ""
echo "[4] 스케줄러 기동 확인"
journalctl -u $SERVICE --no-pager | grep -E "스케줄러가 시작|파이프라인 스케줄러 시작" | tail -3 | sed 's/^.*INFO - /  /' || echo "  (로그 없음)"

echo ""
echo "[5] 메모리 / 스왑 / 디스크"
free -h | awk 'NR==1 || /Mem|Swap/ {print "  " \$0}'
df -h / | awk 'NR==1 || /\// {print "  " \$0}' | head -2

echo ""
echo "[6] 최근 24h 에러 (ERROR/Traceback/HTTP 4xx·5xx)"
ERRS=\$(journalctl -u $SERVICE --no-pager --since "24 hours ago" | grep -E "ERROR|Traceback|Exception|HTTP/2 4[0-9][0-9]|HTTP/2 5[0-9][0-9]" | tail -5)
if [ -n "\$ERRS" ]; then echo "\$ERRS" | sed 's/^.*: /  /'; else echo "  ✅ 에러 없음"; fi

echo ""
echo "[7] 최근 매매/파이프라인 활동"
ACT=\$(journalctl -u $SERVICE --no-pager | grep -E "자동 매수|자동 매도|매수 주문|매도 주문|LLM 검토|Daily Pipeline|파이프라인 전체|매수 후보" | tail -5)
if [ -n "\$ACT" ]; then echo "\$ACT" | sed 's/^.*: /  /'; else echo "  (아직 없음 — 21:00 KST 파이프라인 / 미국 장중 매도 시 생성)"; fi

echo ""
echo "[8] 최근 로그 5줄"
journalctl -u $SERVICE --no-pager -n 5 | sed -E 's/.*(uvicorn\[[0-9]+\]|systemd\[1\]): /  /'
echo "════════════════════════════════════════════════════"
REMOTE
