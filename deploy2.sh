#!/usr/bin/env bash
#
# deploy.sh — 로컬 코드를 AWS Lightsail 서버로 배포하고 서비스를 재시작한다.
# (rsync 미설치 환경용: tar + scp 방식)
#
# 사용법 (Git Bash):
#   bash deploy.sh                 코드만 동기화 + 서비스 재시작 + 헬스체크
#   bash deploy.sh --deps          + requirements.txt 재설치 (의존성 바뀐 경우)
#   bash deploy.sh --env           + 로컬 .env 도 전송 (권한 600)
#   bash deploy.sh --no-restart    재시작 생략 (동기화만)
#   조합 가능:  bash deploy.sh --deps --env
#
# 설정은 환경변수로 덮어쓸 수 있음:
#   DEPLOY_KEY, DEPLOY_HOST, DEPLOY_REMOTE_DIR, DEPLOY_SERVICE
#
set -euo pipefail

# ── 설정 (기본값: 현재 서버) ───────────────────────────────────
KEY="${DEPLOY_KEY:-C:/Users/user/Desktop/key/bangab-app.pem}"
HOST="${DEPLOY_HOST:-ec2-user@11.111.11.11}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-~/stockTrading}"
SERVICE="${DEPLOY_SERVICE:-stocktrading}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null"

# 스크립트 위치 = 프로젝트 루트
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARBALL="$(dirname "$PROJECT_DIR")/.st_deploy_$$.tar.gz"

# ── 플래그 파싱 ────────────────────────────────────────────────
DO_DEPS=0; DO_ENV=0; DO_RESTART=1
for arg in "$@"; do
  case "$arg" in
    --deps)       DO_DEPS=1 ;;
    --env)        DO_ENV=1 ;;
    --no-restart) DO_RESTART=0 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $arg (도움말: bash deploy.sh --help)"; exit 1 ;;
  esac
done

SSH="ssh -i $KEY $SSH_OPTS $HOST"

# ── [1] 코드 압축 ──────────────────────────────────────────────
echo "▶ [1/5] 코드 압축 (venv/.git/.env/로그 제외)"
tar czf "$TARBALL" -C "$PROJECT_DIR" \
  --exclude='./venv' --exclude='./.venv' --exclude='./.venv_predict' \
  --exclude='./.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
  --exclude='*.log' --exclude='./.env' --exclude='./lecture' \
  --exclude='./.st_deploy_*.tar.gz' .
echo "  생성: $(du -h "$TARBALL" | cut -f1)"

# ── [2] 전송 + 전개 ────────────────────────────────────────────
echo "▶ [2/5] 서버 전송 + 전개"
$SSH "mkdir -p $REMOTE_DIR"
scp -i "$KEY" $SSH_OPTS "$TARBALL" "$HOST:$REMOTE_DIR/_deploy.tar.gz"
$SSH "cd $REMOTE_DIR && tar xzf _deploy.tar.gz && rm -f _deploy.tar.gz && echo '  전개 완료'"
rm -f "$TARBALL"
# 참고: tar 전개는 로컬에서 '삭제한' 파일을 서버에서 지우지 않는다(누적). 정리가 필요하면 수동 삭제.

# ── [+] .env 전송 (옵션) ───────────────────────────────────────
if [ "$DO_ENV" -eq 1 ]; then
  echo "▶ [+] .env 전송 (권한 600)"
  scp -i "$KEY" $SSH_OPTS "$PROJECT_DIR/.env" "$HOST:$REMOTE_DIR/.env"
  $SSH "chmod 600 $REMOTE_DIR/.env && echo '  .env 갱신 완료'"
fi

# ── [3] 의존성 (옵션) ──────────────────────────────────────────
if [ "$DO_DEPS" -eq 1 ]; then
  echo "▶ [3/5] 의존성 설치 (--no-cache-dir)"
  $SSH "cd $REMOTE_DIR && source venv/bin/activate && pip install --no-cache-dir -q -r requirements.txt && echo '  의존성 갱신 완료'"
else
  echo "▶ [3/5] 의존성 설치 건너뜀 (필요하면 --deps)"
fi

# ── [4] 재시작 ─────────────────────────────────────────────────
if [ "$DO_RESTART" -eq 0 ]; then
  echo "▶ [4/5] 재시작 건너뜀 (--no-restart)"
  echo "✔ 동기화 완료"
  exit 0
fi
echo "▶ [4/5] 서비스 재시작"
$SSH "sudo systemctl restart $SERVICE && echo '  재시작 명령 완료'"

# ── [5] 헬스체크 (기동 시 경제데이터 수집으로 수 분 소요 가능) ──
echo "▶ [5/5] 헬스체크 (최대 ~5분 대기)"
$SSH "bash -s" <<REMOTE
for i in \$(seq 1 30); do
  R=\$(curl -s -m 5 http://127.0.0.1:8000/ 2>/dev/null || true)
  if [ -n "\$R" ]; then echo "  ✅ READY (~\$((i*10))초): \$R"; break; fi
  sleep 10
done
echo "  서비스 상태: \$(systemctl is-active $SERVICE) / \$(systemctl is-enabled $SERVICE)"
echo "  최근 에러(있으면):"; journalctl -u $SERVICE --no-pager | grep -E "ERROR|Traceback|HTTP/2 4|HTTP/2 5" | tail -3 || echo "    (없음)"
REMOTE

echo "✔ 배포 완료"
