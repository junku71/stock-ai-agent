"""
Kaggle API로 ML 예측 노트북을 트리거하는 서비스.

참조: documents/08_Kaggle_API_연동.md

핵심 기능:
  - push_kernel():     노트북을 Kaggle에 push (= 자동 실행 트리거)
  - get_status():      현재 실행 상태 조회 (queued/running/complete/error 등)
  - trigger_and_wait(): push 후 완료될 때까지 폴링 대기

인증:
  - .env 의 KAGGLE_API_TOKEN (또는 KAGGLE_USERNAME+KAGGLE_KEY) 를 subprocess 환경변수로 주입

Secrets 주입:
  - Kaggle UserSecretsClient 는 API push 로 만들어진 kernel 버전에서 작동하지 않음
  - push 직전에 predict.py 를 .ipynb 로 변환하면서 .env 의 SUPABASE_URL/KEY 를
    첫 셀 os.environ 으로 박아서 보냄 → 매번 fresh 한 값으로 전송
  - 결과 .ipynb 는 .gitignore 처리 (secrets 가 들어가있어서 git 절대 커밋 금지)
"""
import json
import os
import sys
import subprocess
import time
import logging
from pathlib import Path
from typing import Tuple, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# 폴링 간격 / 최대 대기 시간
POLL_INTERVAL_SEC = 10
MAX_WAIT_SEC = 900  # 15분

# 종료 상태로 간주하는 키워드들
TERMINAL_OK = {"complete"}
TERMINAL_ERR = {"error", "cancel_acknowledged", "cancel_requested"}


def _kernel_ref(kernel_slug: Optional[str] = None) -> str:
    """`username/slug` 형태의 kernel reference 반환 (Kaggle 실제 username 기준)

    kernel_slug 를 주면 그 커널을, 생략하면 .env 의 KAGGLE_KERNEL_SLUG(미국 트랙)를 쓴다.
    국내 트랙은 별도 커널(KAGGLE_KERNEL_SLUG_KR)을 쓰므로 이 인자를 넘긴다.
    """
    if not settings.KAGGLE_USERNAME:
        raise RuntimeError(
            "KAGGLE_USERNAME 이 .env 에 설정되지 않았습니다 "
            "(토큰 이름이 아닌 실제 Kaggle 계정 username 사용)"
        )
    return f"{settings.KAGGLE_USERNAME}/{kernel_slug or settings.KAGGLE_KERNEL_SLUG}"


def _notebook_dir(notebook_dir: Optional[str] = None) -> Path:
    """노트북 폴더 절대경로 (생략 시 .env 의 KAGGLE_NOTEBOOK_DIR)"""
    p = Path(notebook_dir or settings.KAGGLE_NOTEBOOK_DIR)
    if not p.is_absolute():
        # 프로젝트 루트(이 파일의 부모의 부모의 부모) 기준 상대경로 해석
        project_root = Path(__file__).resolve().parents[2]
        p = project_root / p
    return p


def _kaggle_env() -> dict:
    """
    kaggle CLI 호출 시 주입할 환경변수 (인증 + Windows 인코딩 강제)

    인증 우선순위:
      1. KAGGLE_API_TOKEN (신형 Access Token, "KGAT_" 접두사) — 단독 사용
      2. KAGGLE_USERNAME + KAGGLE_KEY (기존 32자리 hex) — 두 개 다 필요
    """
    env = os.environ.copy()

    # Windows 콘솔 cp949 인코딩 충돌 방지 (predict.py 의 한글 변수명 등)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    # .env 에서 신형 토큰(KGAT_*)을 구형 변수명 KAGGLE_KEY 에 넣어둔 경우도 구제한다.
    # (구형 KAGGLE_KEY 경로로 KGAT_ 토큰을 넘기면 CLI 가 "Authentication required" 로 거부함)
    api_token = settings.KAGGLE_API_TOKEN
    if not api_token and settings.KAGGLE_KEY.startswith("KGAT_"):
        api_token = settings.KAGGLE_KEY
        logger.warning(
            "KAGGLE_KEY 에 신형 Access Token(KGAT_*)이 들어있습니다. "
            "KAGGLE_API_TOKEN 으로 처리합니다 — .env 변수명을 KAGGLE_API_TOKEN 으로 바꾸세요."
        )

    if api_token:
        env["KAGGLE_API_TOKEN"] = api_token
        # 셸/systemd 에 남아있는 구형 KAGGLE_KEY 가 신형 인증을 방해하지 않도록 제거
        env.pop("KAGGLE_KEY", None)
        # KAGGLE_API_TOKEN 만으로 인증되지만, username 도 같이 넘기면 메타데이터 검증에 도움
        if settings.KAGGLE_USERNAME:
            env["KAGGLE_USERNAME"] = settings.KAGGLE_USERNAME
        return env

    if settings.KAGGLE_USERNAME and settings.KAGGLE_KEY:
        env["KAGGLE_USERNAME"] = settings.KAGGLE_USERNAME
        env["KAGGLE_KEY"] = settings.KAGGLE_KEY
        return env

    raise RuntimeError(
        "Kaggle 인증 정보가 .env 에 없습니다. "
        "(KAGGLE_API_TOKEN=KGAT_... 또는 KAGGLE_USERNAME+KAGGLE_KEY(32자리 hex) 둘 중 하나 설정 필요)"
    )


def _kaggle_bin() -> str:
    """
    kaggle CLI 실행 경로를 반환한다.
    systemd 가 venv/bin 을 PATH 에 넣지 않아도(서비스 PATH 미설정 시 rc=127 발생) 동작하도록,
    실행 중인 파이썬(venv) 옆의 kaggle 바이너리를 우선 사용한다. (Windows/Linux 모두 호환)
    """
    bindir = os.path.dirname(sys.executable)
    for name in ("kaggle", "kaggle.exe"):
        cand = os.path.join(bindir, name)
        if os.path.exists(cand):
            return cand
    return "kaggle"  # PATH 폴백


def _run_kaggle_cmd(args: list, timeout: int = 60) -> Tuple[int, str, str]:
    """
    kaggle CLI 실행. (returncode, stdout, stderr) 반환.
    Args:
        args: ["kernels", "push", "-p", "..."] 같은 인자 리스트 (앞에 kaggle 바이너리 자동 추가)
        timeout: 단일 명령 타임아웃 (초)
    """
    try:
        proc = subprocess.run(
            [_kaggle_bin()] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_kaggle_env(),
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "kaggle CLI 미설치 (pip install kaggle 필요)"
    except subprocess.TimeoutExpired as e:
        return 124, "", f"kaggle 명령 타임아웃 ({timeout}초): {e}"


def check_auth() -> Tuple[bool, str]:
    """Kaggle 인증이 정상 작동하는지 확인 (kernels list -m 호출)"""
    rc, out, err = _run_kaggle_cmd(["kernels", "list", "-m", "--page-size", "1"])
    if rc == 0:
        return True, "Kaggle 인증 OK"
    return False, f"Kaggle 인증 실패 (rc={rc}): {err.strip() or out.strip()}"


def _build_ipynb_with_injected_secrets(py_path: Path, ipynb_path: Path) -> None:
    """
    predict.py 를 읽어서 secrets 주입 셀을 prepend 한 .ipynb 로 변환.

    Kaggle UserSecretsClient 가 API push 로 만든 kernel 버전에서
    "Connection error trying to communicate with service" 로 실패하는
    이슈 우회용. .env 값을 첫 셀에 os.environ 으로 박아서 보냄.

    결과 ipynb 는 secrets 가 들어있으므로 .gitignore 필수.
    """
    # ★ Kaggle Secrets(user_secrets)는 API push 로 만든 kernel 에서 "Connection error" 로 실패한다(실측 확인).
    #   따라서 SUPABASE_URL/KEY 를 노트북 첫 셀에 직접 주입한다.
    #   RLS(Row Level Security) ON 환경에서는 anon 키면 0행이 되므로 service_role 키를 주입한다.
    #   (service_role 키 없으면 anon 폴백 — RLS OFF 환경 호환)
    supa_key = settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_KEY
    if not settings.SUPABASE_URL or not supa_key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_(SERVICE_ROLE_)KEY 가 .env 에 없습니다. "
            "predict.ipynb 에 주입할 값이 없어 push 불가."
        )

    with open(py_path, "r", encoding="utf-8") as f:
        code = f.read()

    # 첫 셀: secrets 를 os.environ 에 주입 (predict.py 가 os.environ.get 으로 읽음)
    #   SUPABASE_SERVICE_ROLE_KEY + SUPABASE_KEY 둘 다 service_role 값으로 주입한다.
    #   (predict.py 가 둘 중 어느 이름으로 읽어도 동작 → 호환/안전)
    secret_cell = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "# AUTO-INJECTED by ml_trigger_service. Do NOT edit / do NOT commit.\n",
            "import os\n",
            f"os.environ['SUPABASE_URL'] = {settings.SUPABASE_URL!r}\n",
            f"os.environ['SUPABASE_SERVICE_ROLE_KEY'] = {supa_key!r}\n",
            f"os.environ['SUPABASE_KEY'] = {supa_key!r}\n",
        ],
    }

    # 두 번째 셀: 원본 predict.py 코드 그대로
    main_cell = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": code.splitlines(keepends=True),
    }

    nb = {
        "cells": [secret_cell, main_cell],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }

    with open(ipynb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)


def _sync_metadata_id(meta_path: Path, kernel_slug: Optional[str] = None) -> Optional[str]:
    """
    kernel-metadata.json 의 "id" 를 .env 기준(`KAGGLE_USERNAME/KAGGLE_KERNEL_SLUG`)으로 맞춘다.

    저장소에 커밋된 id 는 다른 계정(예: 강사 계정)으로 박혀있을 수 있는데,
    그대로 push 하면 남의 kernel 을 건드리려다 권한 거부로 실패한다.
    각자 자기 계정으로 push 되도록 push 직전에 교정한다.

    Returns: 교정한 경우 이전 id, 이미 맞으면 None
    """
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    want = _kernel_ref(kernel_slug)
    old = meta.get("id")
    if old == want:
        return None

    meta["id"] = want
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return old


def push_kernel(
    kernel_slug: Optional[str] = None,
    notebook_dir: Optional[str] = None,
    script_name: str = "predict.py",
) -> Tuple[bool, str]:
    """
    노트북 push (= 새 버전 + 실행 트리거).
    push 직전에 {script_name} + .env secrets 로 동명의 .ipynb 를 새로 생성.

    Args:
        kernel_slug:  대상 커널 slug (생략 시 미국 트랙 KAGGLE_KERNEL_SLUG)
        notebook_dir: 노트북 폴더 (생략 시 KAGGLE_NOTEBOOK_DIR)
        script_name:  변환할 파이썬 스크립트 파일명

    Returns: (success, message)
    """
    nb_dir = _notebook_dir(notebook_dir)
    if not nb_dir.exists():
        msg = f"노트북 폴더가 없음: {nb_dir} (kernel-metadata.json + {script_name} 필요)"
        logger.error(msg)
        return False, msg

    meta_path = nb_dir / "kernel-metadata.json"
    if not meta_path.exists():
        msg = f"kernel-metadata.json 없음: {nb_dir}"
        logger.error(msg)
        return False, msg

    # 메타데이터 id 를 내 계정 기준으로 교정 (남의 계정 id 로 push 시 권한 거부 방지)
    try:
        replaced = _sync_metadata_id(meta_path, kernel_slug)
        if replaced:
            logger.warning(
                f"kernel-metadata.json id 교정: {replaced!r} -> {_kernel_ref(kernel_slug)!r} "
                "(.env 의 KAGGLE_USERNAME / 커널 slug 기준)"
            )
    except Exception as e:
        msg = f"kernel-metadata.json id 교정 실패: {e}"
        logger.error(msg, exc_info=True)
        return False, msg

    # secrets 주입된 ipynb 생성
    py_path = nb_dir / script_name
    ipynb_path = nb_dir / f"{Path(script_name).stem}.ipynb"
    if not py_path.exists():
        msg = f"{script_name} 없음: {py_path} (secrets 주입 위해 .py 원본 필요)"
        logger.error(msg)
        return False, msg

    try:
        _build_ipynb_with_injected_secrets(py_path, ipynb_path)
        logger.info(f"{ipynb_path.name} 재생성 완료 (secrets 주입됨)")
    except Exception as e:
        msg = f"ipynb 생성 실패: {e}"
        logger.error(msg, exc_info=True)
        return False, msg

    rc, out, err = _run_kaggle_cmd(["kernels", "push", "-p", str(nb_dir)], timeout=120)
    if rc != 0:
        msg = f"Kaggle push 실패 (rc={rc}): {err.strip() or out.strip()}"
        logger.error(msg)
        return False, msg

    out_msg = out.strip()
    logger.info(f"Kaggle 노트북 push 성공: {out_msg}")
    return True, out_msg


def get_status(kernel_slug: Optional[str] = None) -> str:
    """
    현재 실행 상태 조회.
    Returns: 'complete' / 'running' / 'queued' / 'error' /
             'cancel_requested' / 'cancel_acknowledged' / 'unknown'
    """
    rc, out, err = _run_kaggle_cmd(["kernels", "status", _kernel_ref(kernel_slug)])
    if rc != 0:
        logger.warning(f"status 조회 실패 (rc={rc}): {err.strip() or out.strip()}")
        return "unknown"

    text = (out + " " + err).lower()
    # 정렬 순서 중요: 더 구체적인 것을 먼저
    for state in ("complete", "error", "cancel_acknowledged", "cancel_requested",
                  "running", "queued"):
        if state in text:
            return state
    return "unknown"


def trigger_and_wait(
    poll_interval: int = POLL_INTERVAL_SEC,
    max_wait: int = MAX_WAIT_SEC,
    kernel_slug: Optional[str] = None,
    notebook_dir: Optional[str] = None,
    script_name: str = "predict.py",
) -> Tuple[bool, str, dict]:
    """
    push로 트리거 → 완료될 때까지 폴링.

    kernel_slug / notebook_dir / script_name 을 생략하면 미국 트랙 커널을 쓴다.
    국내 트랙은 KAGGLE_KERNEL_SLUG_KR / KAGGLE_NOTEBOOK_DIR_KR / predict_kr.py 를 넘긴다.

    Returns:
        (success: bool, message: str, meta: dict)
        meta: {
          "elapsed_sec": int,
          "final_status": str,
          "push_output": str,
        }
    """
    start = time.time()

    # 1) push (= 트리거)
    pushed, push_msg = push_kernel(kernel_slug, notebook_dir, script_name)
    if not pushed:
        return False, push_msg, {"elapsed_sec": 0, "final_status": "push_failed", "push_output": push_msg}

    logger.info(f"Kaggle 실행 시작 - 완료 대기 중 (최대 {max_wait}초)")
    last_status: Optional[str] = None

    # 2) 폴링
    while True:
        elapsed = int(time.time() - start)
        if elapsed > max_wait:
            msg = f"Kaggle 실행 타임아웃 ({max_wait}초)"
            logger.error(msg)
            return False, msg, {
                "elapsed_sec": elapsed,
                "final_status": "timeout",
                "push_output": push_msg,
            }

        time.sleep(poll_interval)
        status = get_status(kernel_slug)

        if status != last_status:
            logger.info(f"  [{elapsed}s] 상태: {status}")
            last_status = status

        if status in TERMINAL_OK:
            msg = f"Kaggle 실행 완료 ({elapsed}초)"
            logger.info(msg)
            return True, msg, {
                "elapsed_sec": elapsed,
                "final_status": status,
                "push_output": push_msg,
            }

        if status in TERMINAL_ERR:
            msg = f"Kaggle 실행 실패: {status}"
            logger.error(msg)
            return False, msg, {
                "elapsed_sec": elapsed,
                "final_status": status,
                "push_output": push_msg,
            }
        # running / queued / unknown → 계속 대기
