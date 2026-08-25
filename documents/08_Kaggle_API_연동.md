# Kaggle API 연동 — 우리 시스템 ML 자동화 가이드

> **적용 범위** — push/폴링 메커니즘과 트러블슈팅은 지금도 그대로 유효하다. 다만 이 문서가
> 쓰인 시점의 대상은 미국 트랙 커널(`kaggle_notebook/predict.py`, `KAGGLE_KERNEL_SLUG`)이었고,
> **현재 대상은 국내 커널**(`kaggle_notebook_kr/predict_kr.py`, `KAGGLE_KERNEL_SLUG_KR`)이다.
> `ml_trigger_service` 의 기본값도 국내 커널로 바뀌었으니 경로만 바꿔 읽으면 된다.


> 매일 수동으로 Colab에서 돌리던 `predict_colab.py` 를 Kaggle 무료 GPU + 우리 FastAPI 에서 자동 트리거하기 위한 통합 가이드.
> 실제 적용 과정에서 부딪힌 6단계 트러블슈팅 모두 포함.

---

## 1. 한 문장 요약

> **"Kaggle API로 노트북을 push하면 새 버전이 만들어지고 자동 실행된다. status를 폴링해서 complete를 기다린다. 단, 신규 계정은 한 번 UI에서 GPU+Internet을 명시 토글해야 sticky하게 적용된다."**

---

## 2. Kaggle API 핵심 명령

| 명령 | 용도 |
|---|---|
| `kaggle kernels push -p <폴더>` | 노트북 업로드 + **자동 실행 트리거** |
| `kaggle kernels status <user>/<slug>` | 실행 상태 (`queued`/`running`/`complete`/`error`) |
| `kaggle kernels pull <user>/<slug> -m -p <폴더>` | 노트북 + 적용된 메타데이터 다운로드 (디버깅용) |

`kaggle kernels list -m --page-size 1` 로 인증 확인.

---

## 3. 사전 준비

### 3-1. Kaggle 계정 + Access Token 발급

1. [https://www.kaggle.com](https://www.kaggle.com) 가입 + **이메일 인증** + **휴대폰 인증** (둘 다 필수)
2. 우측 상단 프로필 → **Settings**
3. **API Tokens (Recommended)** 섹션 → **+ Create new token**
4. Token Name 입력 → **Generate**
5. 노출되는 토큰 값 복사 (예: `KGAT_3b6.............07a4e`)

### 3-2. ⚠️ Token Name vs Kaggle Username 구분

```
Token Name = 토큰의 라벨 (관리용 이름, 아무거나 가능)
Kaggle Username = 실제 계정 ID (kernel id 에 들어가는 값)
```

**이 둘이 다를 수 있음.** Token Name 으로 `banbu` 를 적었어도 실제 username 은 `cheatkeyman` 일 수 있음.

#### 실제 username 확인 방법

```bash
KAGGLE_API_TOKEN=KGAT_xxx kaggle kernels list -m --page-size 1
```

결과의 `author` 컬럼이 진짜 username:
```
ref                                title  author       lastRunTime
cheatkeyman/some-notebook          ...    cheatkeyman  ...
                                          ^^^^^^^^^^^ ← 이게 username
```

또는 https://www.kaggle.com/me 접속하면 URL 의 `/me` 자리에 username 표시됨.

### 3-3. .env 에 추가

```bash
# 신형 Access Token (KGAT_ 접두사) → KAGGLE_API_TOKEN 사용
KAGGLE_USERNAME=cheatkeyman                              # 실제 username (Token Name 아님!)
KAGGLE_API_TOKEN=KGAT_3b6aa225.............07a4e
KAGGLE_KERNEL_SLUG=stock-prediction
KAGGLE_NOTEBOOK_DIR=kaggle_notebook
```

> 🔴 **신/구 토큰 포맷 차이 (중요!)**
> - **신형 (Access Token, `KGAT_` 접두사)** → `KAGGLE_API_TOKEN` 환경변수
> - **구형 (32자리 hex)** → `KAGGLE_USERNAME` + `KAGGLE_KEY` 두 개 같이
>
> 신형 토큰을 `KAGGLE_KEY` 로 넣으면 **401 Unauthorized** 발생. 우리가 첫 시도에서 막혔던 지점.

### 3-4. 패키지 설치

```bash
pip install kaggle
```

`requirements.txt`:
```
kaggle>=1.6.0
```

---

## 4. Kaggle 노트북 만들기

### 4-1. ⚠️ Script vs Notebook 타입 — Notebook 권장

| 타입 | 장점 | 단점 |
|---|---|---|
| `script` (.py) | git 관리 깔끔 | UI 사이드바 옵션 접근 어려움 |
| **`notebook` (.ipynb)** | **UI에서 GPU/Internet 토글 직관적** | JSON 포맷 다소 번거로움 |

> 우리는 처음 script 로 시작했다가 **Notebook 옵션 패널이 안 보여** notebook 타입으로 전환함. **신규 계정은 무조건 notebook 으로 시작 권장**.

### 4-2. 폴더 구조

```
banbu-stocktrading-final/
└── kaggle_notebook/
    ├── kernel-metadata.json
    └── predict.ipynb
```

### 4-3. `kernel-metadata.json`
- "id": "{여러분의id}/stock-prediction",
```json
{
  "id": "cheatkeyman/stock-prediction",
  "title": "stock-prediction",
  "code_file": "predict.ipynb",
  "language": "python",
  "kernel_type": "notebook",
  "is_private": true,
  "enable_gpu": true,
  "enable_internet": true,
  "dataset_sources": [],
  "competition_sources": [],
  "kernel_sources": []
}
```

#### 필드 주의

- `id`: 본인 username 사용 (Token Name 아님)
- `title`: slug 와 거의 같게 → 안 그러면 "title does not resolve to specified id" 경고
- `is_private`/`enable_gpu`/`enable_internet`: **boolean true** (string `"true"` 도 되지만 일부 Kaggle 빌드에서 무시됨)

### 4-4. `predict.ipynb` 변환

기존 `predict_colab.py` 한 셀짜리 노트북으로 변환:

```python
# convert.py (한 번만 실행)
import json
with open('kaggle_notebook/predict.py', 'r', encoding='utf-8') as f:
    code = f.read()
nb = {
    'cells': [{
        'cell_type': 'code', 'execution_count': None, 'metadata': {}, 'outputs': [],
        'source': code.splitlines(keepends=True),
    }],
    'metadata': {
        'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
        'language_info': {'name': 'python'},
    },
    'nbformat': 4, 'nbformat_minor': 5,
}
with open('kaggle_notebook/predict.ipynb', 'w', encoding='utf-8') as f:
    json.dump(nb, f, ensure_ascii=False, indent=1)
```

`predict.py` 코드에서 두 가지만 수정:

```python
# ❌ 기존 (Colab 전용)
!pip install supabase tensorflow

# ✅ Kaggle 용
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "supabase"])
```

```python
# ❌ 기존 (하드코딩)
SUPABASE_URL = "https://hcrymjkdgvvsttjecype.supabase.co"
SUPABASE_KEY = "eyJ..."

# ✅ Kaggle Secrets / 환경변수
import os
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Kaggle Secrets API 폴백
if not SUPABASE_URL or not SUPABASE_KEY:
    from kaggle_secrets import UserSecretsClient
    user_secrets = UserSecretsClient()
    SUPABASE_URL = SUPABASE_URL or user_secrets.get_secret("SUPABASE_URL")
    SUPABASE_KEY = SUPABASE_KEY or user_secrets.get_secret("SUPABASE_KEY")
```

### 4-5. 첫 푸시

```bash
cd kaggle_notebook
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
  KAGGLE_API_TOKEN=KGAT_xxx \
  kaggle kernels push -p .
```

> ⚠️ **Windows 사용자 필수**: `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` 안 붙이면 한글 변수명 때문에 `cp949 codec can't decode` 에러 발생.

성공 시:
```
Kernel version 1 successfully pushed.
Please check progress at https://www.kaggle.com/code/cheatkeyman/stock-prediction
```

---

## 5. ★★★ 첫 수동 run — Kaggle 신규 계정 필수 단계

> **이게 이 가이드 전체에서 가장 중요한 부분. 우리가 5번 실패하고 발견한 사실:**

```
☆ Kaggle 은 metadata 의 enable_gpu / enable_internet 을 신규 계정에 자동 적용하지 않음.
  반드시 사용자가 UI에서 한 번 명시적으로 토글한 뒤 첫 run 을 해야 sticky 적용됨.
```

### 5-1. UI에서 GPU + Internet 켜기

1. https://www.kaggle.com/code/cheatkeyman/stock-prediction 접속
2. 우측 상단 **Edit** 클릭
3. 우측 상단 **파란 [Save Version] 버튼** 클릭
4. **Save Version 모달이 뜸** — 여기 **Advanced Settings** 펼치기

```
┌──────────────────────────────────────────────┐
│  Save Version                              ✕ │
├──────────────────────────────────────────────┤
│  Version Type:                                │
│   ● Save & Run All (Commit)   ← 선택         │
│                                               │
│  ▼ Advanced Settings  ← 펼치기                │
│    Accelerator: [None ▼]   ★ GPU P100/T4 선택│
│    Internet:    [⚪ off ]   ★ ON 으로 토글    │
│    Persistence: [Variables and Files]        │
│                                               │
│              [Cancel]  [Save]                │
└──────────────────────────────────────────────┘
```

또는 우측 사이드바 ⚙️ Settings 에 동일 토글 있음.

### 5-2. Add-ons → Secrets 등록

같은 Edit 화면 우측 사이드바:

1. **Add-ons → Secrets → Add a new secret**
2. 두 개 등록:
   - Label: `SUPABASE_URL`, Value: `https://xxxxxxxxxxx.supabase.co`
   - Label: `SUPABASE_KEY`, Value: (`.env` 의 SUPABASE_KEY 값)

> ⚠️ Internet 켜진 후에야 Secrets 메뉴가 의미 있음. (Internet 차단 시 Secrets 등록은 가능하지만 코드에서 외부 호출 불가)

### 5-3. Save & Run All 클릭

5~7분 기다리면 Accelerator: GPU P100, status: complete → Supabase 에 결과 저장 완료.

**이 한 번의 수동 run 이 끝나야 그 후 API push 들이 GPU/Internet 자동 적용됨.**

---

## 6. FastAPI 통합 (현재 구현 상태)

### 6-1. 환경변수 (`app/core/config.py`)

```python
# Kaggle API
KAGGLE_USERNAME: str = os.getenv("KAGGLE_USERNAME", "")
KAGGLE_API_TOKEN: str = os.getenv("KAGGLE_API_TOKEN", "")  # 신형 KGAT_*
KAGGLE_KEY: str = os.getenv("KAGGLE_KEY", "")              # 구형 32자리 hex (폴백)
KAGGLE_KERNEL_SLUG: str = os.getenv("KAGGLE_KERNEL_SLUG", "stock-prediction")
KAGGLE_NOTEBOOK_DIR: str = os.getenv("KAGGLE_NOTEBOOK_DIR", "kaggle_notebook")
```

### 6-2. 트리거 서비스 (`app/services/ml_trigger_service.py`)

핵심 함수 3개:
- `check_auth()` → `kaggle kernels list -m` 으로 인증 확인
- `get_status()` → 현재 실행 상태
- `trigger_and_wait(poll_interval, max_wait)` → push 후 complete 까지 폴링

요점:
- subprocess `env=` 로 `KAGGLE_API_TOKEN`(신형) 우선, 없으면 `KAGGLE_USERNAME+KAGGLE_KEY` 폴백
- `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1` 강제 (Windows 대응)
- 폴링 10초 간격, 기본 15분 타임아웃

### 6-3. API 엔드포인트 (`app/api/routes/pipeline.py`)

| 엔드포인트 | 용도 |
|---|---|
| `GET /pipeline/kaggle/auth-check` | 인증 확인 (실행 안 함, 빠름) |
| `GET /pipeline/kaggle/status` | 현재 노트북 상태 조회 |
| `POST /pipeline/kaggle/trigger-ml?max_wait_sec=900` | 트리거 + 완료 대기 |

### 6-4. 사용 예시

```bash
# 1. 인증 확인
curl http://localhost:8000/pipeline/kaggle/auth-check
# → {"ok": true, "message": "Kaggle 인증 OK"}

# 2. 현재 상태
curl http://localhost:8000/pipeline/kaggle/status
# → {"kernel": "cheatkeyman/stock-prediction", "status": "complete"}

# 3. 트리거 + 대기 (5~10분 소요)
curl -X POST 'http://localhost:8000/pipeline/kaggle/trigger-ml?max_wait_sec=900'
# → {"success": true, "message": "Kaggle 실행 완료 (412초)", "meta": {...}}
```

---

## 7. 우리가 실제로 겪은 트러블슈팅 (전부)

| # | 증상 | 원인 | 해결 |
|---|------|------|------|
| 1 | `cp949 codec can't decode byte 0xb0` | Windows + 한글 변수명 | `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` 환경변수 |
| 2 | `401 Unauthorized for url: api.kaggle.com` | 신형 토큰을 `KAGGLE_KEY` 로 넣음 | `KAGGLE_API_TOKEN` 환경변수 사용 |
| 3 | `kernels list -m` 결과 author 가 다름 | Token Name(라벨) 을 username 으로 오해 | 결과 author 컬럼이 실제 username |
| 4 | `Permission 'kernelSessions.enableInternet' was denied` | 휴대폰 인증 누락 | Kaggle Settings → Phone Verification |
| 5 | `Accelerator: None` + `pip install supabase` DNS error 반복 | metadata `enable_gpu`/`enable_internet` 신규 계정 자동 적용 안 됨 | UI에서 한 번 수동 토글 후 Save & Run All |
| 6 | `Kernel push error: You cannot change the editor type` | script ↔ notebook 타입 변경 시도 | Kaggle UI에서 노트북 삭제 후 새 타입으로 재푸시 |

### 7-1. metadata 가 적용됐는지 확인하는 방법

```bash
mkdir tmp && cd tmp
KAGGLE_API_TOKEN=KGAT_xxx kaggle kernels pull cheatkeyman/stock-prediction -m -p .
cat kernel-metadata.json
```

결과에 `"enable_gpu": true, "enable_internet": true, "machine_shape": "Gpu"` 가 있어야 정상.

> 우리 케이스: pull 결과는 정상이었는데도 실제 run 은 Accelerator: None 이었음. 이래서 #5 (UI 수동 토글)가 필요.

---

## 8. Kaggle 한계 + 함정

### 8-1. 무료 GPU 시간 제한

- 주당 **30 GPU 시간**
- 매일 7분 × 7일 ≈ 50분/주 → 여유 충분

### 8-2. 큐 대기 시간 변동

- 평소 30초 ~ 2분, 피크 시 5분 이상
- 한국 새벽 ~ 오후 (UTC 21시 ~ 06시) 가 대체로 한가함

### 8-3. 노트북 versioning 누적

- `kaggle kernels push` 매번 새 버전 생성 (자동 정리 X)
- 1년이면 v365. 가끔 UI에서 Versions 탭 → 옛 버전 삭제

### 8-4. API rate limit

- 비공식 분당 약 30회
- 우리 폴링 간격 10초 → 분당 6회 → 안전

### 8-5. Kernel type 변경 불가

- script → notebook (또는 그 반대) 변경 시 push 거부됨
- **UI에서 노트북 삭제 후 재푸시** 또는 **새 slug 사용** (예: `stock-prediction-v2`)

---

## 9. 보안

- `~/.kaggle/kaggle.json` git 커밋 금지 (`.gitignore`)
- Supabase 키는 **Kaggle Secrets** 에만 등록, `kernel-metadata.json` 에 절대 적지 않음
- `is_private: true` 필수 (Public 이면 코드 노출됨)

---

## 10. 코드 위치 인덱스

| 파일 | 역할 |
|---|---|
| `kaggle_notebook/kernel-metadata.json` | Kaggle 노트북 설정 |
| `kaggle_notebook/predict.ipynb` | predict_colab.py 정리본 (1셀 노트북) |
| `app/core/config.py` | KAGGLE_USERNAME / KAGGLE_API_TOKEN 등 환경변수 |
| `app/services/ml_trigger_service.py` | Kaggle CLI 호출 + 폴링 |
| `app/api/routes/pipeline.py` | `/pipeline/kaggle/*` 엔드포인트 3개 |
| `requirements.txt` | `kaggle>=1.6.0` |
| `.env` | KAGGLE_* 인증 정보 |

---

## 11. 관련 문서

- `07_자동화_방안.md` — 전체 자동화 전략
- `05_ML_예측_모델_상세.md` — predict.ipynb 의 원본 코드 해설
