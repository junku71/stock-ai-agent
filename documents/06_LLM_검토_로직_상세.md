# LLM 검토 로직 (`llm_review_service.py`) — 초보자용 상세 가이드

> 이 문서는 **처음 코드를 보는 사람**을 위해 작성된, 매수 직전 Claude AI가 어떻게 종목을 최종 검토하는지에 대한 단계별 설명서입니다.
> 모든 코드 인용은 `app/services/llm_review_service.py:1-253` 에서 가져왔습니다.

---

## 1. 한 문장 요약

> **"종합 점수가 0.3 이상이고 모든 필터를 통과한 매수 후보 종목들을 → Claude API에 던져서 → '진짜 사도 되는지' 마지막으로 검토받고 → BUY 판정 받은 종목만 실제로 주문한다."**

LLM은 **거부권만 있습니다**. 즉, BUY를 HOLD로 바꿀 수는 있어도, 새 종목을 추가하거나 시스템이 거른 종목을 살릴 수는 없습니다.

---

## 2. 왜 LLM 검토가 필요한가?

### 2-1. 시스템 점수 계산의 한계

`composite_score` 는 강력한 필터지만, **데이터로 잡히지 않는 위험**을 알 수 없습니다.

| 시스템이 못 잡는 것 | LLM이 잡을 수 있는 이유 |
|---|---|
| 1주일 내 실적 발표 예정 | ✅ 실제 실적 캘린더 데이터를 프롬프트에 주입 (documents/17 참조) — 학습 데이터 추측 아님 |
| FOMC, CPI 등 매크로 이벤트 임박 | 거시 일정 인지 |
| 종목별 특이 리스크 (CEO 교체, 소송) | 뉴스 컨텍스트 이해 |
| 골든크로스가 형식상 발생했지만 실질적으로 약함 | 시장 맥락에서 판단 |
| 같은 섹터 과집중 | 포트폴리오 전체 균형 시각 |

### 2-2. "팀원 + 팀장" 구조

```
┌────────────────────────────────────────────────┐
│ 팀원 (자동매매 시스템)                            │
│  - ML 예측, 기술적 분석, 감성, VIX 종합          │
│  - composite_score 0.3 이상 + 필터 통과 종목 추출 │
└────────────────────────────────────────────────┘
                    ↓ 후보 명단 전달
┌────────────────────────────────────────────────┐
│ 팀장 (LLM = Claude)                              │
│  - "팀원 분석이 합리적인지" 최종 판단              │
│  - BUY 또는 HOLD                                 │
│  - 거부권만 행사 (새 종목 추가 불가)               │
└────────────────────────────────────────────────┘
                    ↓ BUY 판정만
┌────────────────────────────────────────────────┐
│ 실제 매수 주문 (KIS API)                          │
└────────────────────────────────────────────────┘
```

---

## 3. 어떤 LLM 모델을 쓰나?

```python
# llm_review_service.py:11
MODELS = ["claude-opus-4-7", "claude-sonnet-4-6"]  # Opus 실패 시 Sonnet 폴백
```

| 우선순위 | 모델 ID | 특징 |
|---|---|---|
| 1순위 | `claude-opus-4-7` | Anthropic 최상위 모델 (정확도 ↑, 비용 ↑) |
| 2순위 (폴백) | `claude-sonnet-4-6` | 중급 모델 (속도 ↑, 비용 ↓) |

### 폴백이 뭔가요?

1순위 Opus가 **3번 연속 실패** 하면 → 2순위 Sonnet으로 자동 전환.
이유: API 서버 과부하(529), 속도 제한(429) 같은 일시적 장애 시 시스템이 멈추지 않게 안전장치.

---

## 4. 실패 시 정책: Fail-Close

```python
# llm_review_service.py:58-65
if not settings.ANTHROPIC_API_KEY:
    print("  ANTHROPIC_API_KEY가 설정되지 않았습니다. LLM 검토 불가로 매수 차단.")
    return {
        "reviewed_candidates": [],   # ← 빈 배열 = 매수 차단
        "held_candidates": candidates,
        "llm_reasoning": "API 키 미설정으로 매수 차단",
        "raw_response": []
    }
```

**LLM 호출 실패 = 매수 전부 차단**.

### Fail-Open vs Fail-Close

| 정책 | 동작 | 장점 | 단점 |
|---|---|---|---|
| Fail-Open | LLM 실패 시 그냥 매수 진행 | 시스템 가용성 ↑ | 위험 검증 없이 매수됨 |
| **Fail-Close (이 시스템)** | LLM 실패 시 매수 중단 | 안전성 ↑ | 일시적으로 매수 못 함 |

이 시스템은 **안전 우선** 정책이라, LLM이 작동하지 않으면 그날 매수는 없습니다.

---

## 5. 입력 데이터: 어떤 정보를 LLM에게 주는가?

### 5-1. 종목별 요약 만들기 (`llm_review_service.py:75-90`)

```python
stock_summaries = []
for i, c in enumerate(candidates, 1):
    summary = f"""
{i}. {c.get('stock_name', 'N/A')} ({c.get('ticker', 'N/A')})
   - ML 예측: 상승확률 {c.get('rise_probability', 0):.2f}%, 예측가 ${c.get('predicted_price', 0):.2f} (현재가 ${c.get('last_price', 0):.2f})
   - 기술적 지표:
     골든크로스: {'✓' if c.get('golden_cross') else '✗'} (SMA20: {c.get('sma20', 0):.2f}, SMA50: {c.get('sma50', 0):.2f})
     RSI: {c.get('rsi', 0):.2f} {'(과매도 반등)' if c.get('rsi', 50) < 30 else '(강세 진입)' if 50 <= c.get('rsi', 50) <= 65 else '(매수구간 아님)'}
     MACD: {c.get('macd', 0):.4f}, Signal: {c.get('signal', 0):.4f}, 매수신호: {'✓' if c.get('macd_buy_signal') else '✗'}
   - 거래량: 5일 평균 대비 {c.get('volume_ratio', 'N/A')}배
   - ADX(추세강도): {c.get('adx', 'N/A')} {'(강한 추세)' if c.get('adx') and c.get('adx') > 25 else '(추세 약함)' if c.get('adx') and c.get('adx') < 20 else '(보통)'}
   - 감성분석: {c.get('sentiment_score', 'N/A')} (기사 {c.get('article_count', 0)}개)
   - 종합점수: {c.get('composite_score', 0):.4f}
     (상승확률: {c.get('rise_score', 0)}, 기술: {c.get('tech_score', 0)}, 거래량: {c.get('volume_score', 0)}, ADX: {c.get('adx_score', 0)}, VIX: {c.get('vix_score', 0)})"""
    stock_summaries.append(summary)
```

### 5-2. 실제 프롬프트 예시

종목이 2개 후보(NFLX, COST)로 들어왔을 때 LLM이 받는 메시지:

```
1. 넷플릭스 (NFLX)
   - ML 예측: 상승확률 5.32%, 예측가 $98.65 (현재가 $93.68)
   - 기술적 지표:
     골든크로스: ✓ (SMA20: 92.45, SMA50: 91.20)
     RSI: 56.32 (강세 진입)
     MACD: 0.4521, Signal: 0.3210, 매수신호: ✓
   - 거래량: 5일 평균 대비 1.85배
   - ADX(추세강도): 28.5 (강한 추세)
   - 감성분석: 0.25 (기사 12개)
   - 종합점수: 0.6171
     (상승확률: 0.6, 기술: 1.0, 거래량: 0.6, ADX: 0.4, VIX: 0)

2. 코스트코 (COST)
   - ML 예측: 상승확률 3.15%, 예측가 $1001.20 (현재가 $970.84)
   ...
```

LLM은 이 정보만으로 판단합니다 → **데이터의 양과 질이 곧 판단의 질**.

---

## 6. 시스템 프롬프트: LLM에게 무엇을 시키는가?

### 6-1. 역할 부여 (`llm_review_service.py:95-100`)

```
당신은 월스트리트 경력 20년의 미국 주식 트레이딩 전문가이자 최종 의사결정자입니다.

## 당신의 역할
아래 종목들은 자동매매 시스템(팀원)이 ML 예측, 기술적 분석, 감성분석, VIX를 종합하여 매수 후보로 올린 종목입니다.
당신은 팀장으로서 팀원의 분석을 최종 검토하고 BUY 또는 HOLD를 판정합니다.
팀원의 분석이 맞을 수도 있고 틀릴 수도 있으니, 제공된 데이터와 당신의 시장 지식을 종합하여 독립적으로 판단하세요.
```

#### 왜 페르소나(역할)를 부여하나?

LLM은 역할에 따라 답변 톤과 깊이가 달라집니다.
- "월스트리트 20년 트레이더" → 위험관리 의식 ↑, 전문 용어 사용
- "초보 학생" → 안전 우선, 단순 답변
- 역할 없음 → 일반적/모호한 답변

### 6-2. 검토 기준 (`llm_review_service.py:111-125`)

```
## 검토 기준
아래 항목들을 종합적으로 검토하여 BUY 또는 HOLD를 판정하세요.

### 기술적 지표 검증
- 골든크로스가 발생했지만 현재가가 이동평균선보다 크게 하회하면 유효한 신호인지 의심
- RSI 과매도(< 30)는 반등 기회일 수 있지만, ADX가 약하면(< 20) 추세 없는 횡보일 수 있음
- RSI 과매수(> 70)인 종목이 매수 후보에 포함되었다면 시스템 오류 가능성 → HOLD

### 외부 리스크 확인
- 해당 종목의 실적 발표(Earnings)가 1주일 이내에 예정 → HOLD
- FOMC, CPI 등 주요 매크로 이벤트가 1~2일 내 → HOLD 고려
- 해당 종목의 특이 리스크(CEO 교체, 소송, 규제 등) → HOLD

### 포트폴리오 균형
- 같은 섹터 3개 이상 집중 시 → 가장 약한 종목을 HOLD (전부 HOLD하지 말 것)
```

#### 3대 검토 영역 정리

| 영역 | LLM이 보는 것 |
|---|---|
| **기술적 지표 검증** | 시스템이 계산한 신호가 진짜 유효한지 재검증 |
| **외부 리스크** | 학습 데이터에서 알고 있는 실적/매크로 일정 |
| **포트폴리오 균형** | 같은 섹터 과집중 방지 |

### 6-3. 판정 원칙 (`llm_review_service.py:127-130`)

```
## 판정 원칙
- BUY와 HOLD 모두 구체적인 근거를 제시하세요.
- 막연한 불안감이 아닌, 명확한 데이터와 사실에 기반하여 판단하세요.
- 매수할 만한 종목은 매수하고, 위험한 종목은 거부하는 균형 잡힌 판단을 하세요.
```

> 이 부분이 중요. **"보수적으로 다 거부해라"가 아니라 "균형 잡힌 판단"** 으로 유도. LLM이 무조건 모든 종목을 HOLD하면 시스템이 절대 매수 못 함.

### 6-4. 응답 형식 강제 (`llm_review_service.py:132-144`)

```
## 응답 형식
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요.
{
  "market_analysis": "시장 전체에 대한 간단한 분석 (1~2문장)",
  "decisions": [
    {
      "ticker": "종목 티커",
      "stock_name": "종목명",
      "decision": "BUY 또는 HOLD",
      "reason": "판정 이유 (1~2문장)"
    }
  ]
}
```

#### 왜 JSON으로 강제?

LLM은 자유 형식(자연어)으로 답하는 게 기본인데, 자동매매 시스템에서는:
- 자유 형식 → 코드가 파싱하기 어렵고 실수 발생 가능
- **JSON 형식** → `json.loads()` 로 깔끔하게 구조화된 데이터로 변환 가능

---

## 7. API 호출 + 재시도 로직

### 7-1. 호출 설정 (`llm_review_service.py:9-11, 154-159`)

```python
MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]  # 5초 → 15초 → 30초
MODELS = ["claude-opus-4-7", "claude-sonnet-4-6"]

# ...
message = client.messages.create(
    model=model,
    max_tokens=2000,           # 응답 최대 2000 토큰
    temperature=0,             # 결정론적 (같은 입력 → 같은 출력)
    messages=[{"role": "user", "content": prompt}]
)
```

#### `temperature=0` 이 뭔가?

| temperature | 동작 |
|---|---|
| 0 | **항상 같은 답** (결정론적) — 자동매매에는 이게 적합 |
| 0.5 | 약간의 창의성 |
| 1.0+ | 매우 창의적/다양한 답 |

자동매매 시스템에서는 **재현성** 이 중요하니까 0으로 고정.

### 7-2. 재시도 흐름 (`llm_review_service.py:150-238`)

```python
# 모델별 재시도: Opus 3회 → Sonnet 3회 (총 최대 6회)
for model in MODELS:                       # ① 모델 순회 (Opus → Sonnet)
    for attempt in range(MAX_RETRIES):    # ② 모델당 3번 재시도
        try:
            print(f"  LLM 호출 시도 {attempt + 1}/{MAX_RETRIES} (모델: {model})")
            message = client.messages.create(...)
            # ... 성공 시 return ...

        except json.JSONDecodeError as e:
            # JSON 파싱 실패는 재시도해도 같으니 다음 모델로
            break

        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            error_code = getattr(e, 'status_code', 0)
            delay = RETRY_DELAYS[attempt]

            if error_code in (529, 429, 503):    # 과부하/속도제한
                time.sleep(delay)                 # 대기 후 재시도
                continue
            else:
                break  # 다른 에러는 재시도 무의미

        except Exception as e:
            time.sleep(delay)
            continue

    # 현재 모델 전체 실패 → 다음 모델로 폴백
    if model != MODELS[-1]:
        print(f"  {model} 전체 실패. 폴백 모델 {MODELS[MODELS.index(model) + 1]}로 전환합니다.")
```

### 7-3. 재시도 시나리오 시각화

```
시도 1: Opus 호출
  ├─ 성공 → ✅ 결과 반환
  ├─ 529 (과부하) → 5초 대기 → 시도 2로
  ├─ JSON 오류 → 재시도 무의미 → Sonnet으로 폴백
  └─ 다른 에러 → 즉시 break → Sonnet으로 폴백

시도 2: Opus 호출 (5초 후)
  ├─ 성공 → ✅
  ├─ 529 → 15초 대기 → 시도 3로
  └─ ...

시도 3: Opus 호출 (15초 후)
  ├─ 성공 → ✅
  ├─ 529 → 30초 대기 → Opus 포기, Sonnet 시도 1로

시도 4~6: Sonnet (5초 → 15초 → 30초)
  └─ 다 실패 → ❌ Fail-Close (매수 차단)
```

### 7-4. 처리하는 에러 코드들

| HTTP 코드 | 의미 | 대응 |
|---|---|---|
| **429** | Rate Limit (속도 제한) | 대기 후 재시도 |
| **503** | Service Unavailable | 대기 후 재시도 |
| **529** | Anthropic 서버 과부하 | 대기 후 재시도 |
| 기타 | 잘못된 요청 등 | 즉시 break |

---

## 8. 응답 파싱: LLM 답변 처리

### 8-1. JSON 코드블록 제거 (`llm_review_service.py:161-167`)

```python
response_text = message.content[0].text.strip()
# JSON 파싱 (```json ... ``` 래핑 처리)
if response_text.startswith("```"):
    response_text = response_text.split("```")[1]
    if response_text.startswith("json"):
        response_text = response_text[4:]
response_data = json.loads(response_text)
```

#### 왜 필요?

LLM이 가끔 응답을 마크다운 코드블록으로 감싸서 보냅니다.
```
```json
{"decisions": [...]}
```
```

이걸 그대로 `json.loads()` 하면 파싱 실패 → 백틱 부분을 미리 잘라냄.

### 8-2. 종목별 판정 매칭 (`llm_review_service.py:174-191`)

```python
decisions = response_data.get("decisions", [])
market_analysis = response_data.get("market_analysis", "")

# 티커별 판정 매핑
decision_map = {d["ticker"]: d for d in decisions}

reviewed = []
held = []
for candidate in candidates:
    ticker = candidate["ticker"]
    decision = decision_map.get(ticker, {})
    llm_decision = decision.get("decision", "HOLD").upper()  # 기본값 HOLD ★
    llm_reason = decision.get("reason", "LLM 응답 없음")

    candidate["llm_decision"] = llm_decision
    candidate["llm_reason"] = llm_reason

    if llm_decision == "BUY":
        reviewed.append(candidate)
    else:
        held.append(candidate)
```

#### 안전장치: `default="HOLD"`

LLM이 후보 5개 중 4개만 응답하고 1개를 빠뜨리면 → 빠진 종목은 자동으로 **HOLD** 처리.
즉 **"애매하면 HOLD"** 라는 보수적 정책.

---

## 9. 결과 저장: `llm_decision_logs` 테이블

### 9-1. 저장 함수 (`llm_review_service.py:14-37`)

```python
def _save_llm_decision_logs(candidates: list, decision_map: dict, market_analysis: str, vix_value: float = None):
    today = datetime.now().strftime("%Y-%m-%d")
    for candidate in candidates:
        ticker = candidate["ticker"]
        decision_data = decision_map.get(ticker, {})
        supabase.table("llm_decision_logs").upsert({
            "decision_date": today,
            "ticker": ticker,
            "stock_name": candidate.get("stock_name"),
            "decision": decision_data.get("decision", "N/A"),
            "reason": decision_data.get("reason", ""),
            "market_analysis": market_analysis,
            "composite_score": candidate.get("composite_score"),
            "rise_probability": candidate.get("rise_probability"),
            "rsi": candidate.get("rsi"),
            "adx": candidate.get("adx"),
            "vix_value": vix_value,
            "updated_at": datetime.now().isoformat(),
        }, on_conflict="decision_date,ticker").execute()
```

#### `upsert` + `on_conflict`의 의미

```
INSERT 시도
   ├─ 기존 레코드 없음 → 새로 INSERT
   └─ (decision_date, ticker) 조합이 이미 있으면 → UPDATE
```

같은 날 같은 종목을 여러 번 검토해도 **마지막 결정만 남음**. 디버깅/추적 시 깔끔.

### 9-2. 저장되는 정보

| 컬럼 | 의미 |
|---|---|
| `decision_date` | 검토 날짜 |
| `ticker` | 종목 티커 |
| `stock_name` | 한글명 |
| `decision` | BUY / HOLD / FAIL / N/A |
| `reason` | LLM이 제시한 이유 |
| `market_analysis` | LLM의 시장 분석 (전체 1~2 문장) |
| `composite_score`, `rise_probability`, `rsi`, `adx`, `vix_value` | 검토 시점의 지표 스냅샷 |
| `updated_at` | 갱신 시각 |

> 이 테이블이 있어야 **사후에 "왜 이 종목을 사거나 안 샀는지"** 를 추적할 수 있습니다.

---

## 10. 실패 시에도 로그 저장 (`llm_review_service.py:240-253`)

```python
# 모든 모델, 모든 재시도 실패
fail_reason = f"LLM 호출 전체 실패 (Opus {MAX_RETRIES}회 + Sonnet {MAX_RETRIES}회): {str(last_error)}"

# 실패 시에도 로그 저장 (사유 기록)
fail_decision_map = {c["ticker"]: {"decision": "FAIL", "reason": fail_reason} for c in candidates}
_save_llm_decision_logs(candidates, fail_decision_map, fail_reason, vix_value)

return {
    "reviewed_candidates": [],         # 빈 배열 (매수 차단)
    "held_candidates": candidates,
    "llm_reasoning": fail_reason,
    "raw_response": []
}
```

> LLM이 호출되지 않았어도 "왜 호출 못 했는지" 기록 → 운영자가 사후에 원인 분석 가능.

---

## 11. 호출 흐름도

```
┌────────────────────────────────────────────────────────────┐
│ 매수 스케줄러 (10:30 ET)                                     │
│ scheduler.py → review_buy_candidates(buy_candidates, vix)   │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ 1. API 키 체크                                               │
│   - 없으면 → reviewed=[] 반환 (매수 차단)                     │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ 2. 프롬프트 구성                                             │
│   - 종목별 요약 텍스트 생성 (지표 + 점수)                      │
│   - 시스템 메시지 + 검토 기준 + 응답 형식                      │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ 3. Claude API 호출 (재시도 포함)                              │
│   ├─ Opus 3회 시도 (5s → 15s → 30s 백오프)                  │
│   └─ 폴백: Sonnet 3회 시도                                  │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ 4. 응답 파싱                                                 │
│   - 코드블록 제거 → json.loads()                             │
│   - 종목별 BUY/HOLD 매핑 (default=HOLD)                     │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ 5. llm_decision_logs 테이블에 upsert                        │
│   (decision_date, ticker) 충돌 시 UPDATE                    │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ 6. 결과 반환                                                 │
│   - reviewed_candidates: BUY 판정 종목                       │
│   - held_candidates: HOLD 판정 종목                          │
│   - llm_reasoning: 시장 분석                                 │
└────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────┐
│ scheduler.py → reviewed_candidates만 매수 진행                │
└────────────────────────────────────────────────────────────┘
```

---

## 12. 실제 호출 위치

```python
# app/utils/scheduler.py:518-526
# LLM 최종 검토 (거부권만 행사)
vix_value = buy_candidates[0].get("vix_value") if buy_candidates else None
review_result = review_buy_candidates(buy_candidates, vix_value)
buy_candidates = review_result["reviewed_candidates"]   # ← BUY만 남김

if not buy_candidates:
    logger.info("LLM 검토 결과 매수 대상이 없습니다.")
    return

logger.info(f"LLM 검토 통과: {len(buy_candidates)}개 종목 매수 진행")
```

> 이게 자동매수 파이프라인의 **마지막 게이트** 입니다. 통과한 종목만 KIS 주문 API 호출로 진행.

---

## 13. 비용 (대략)

Claude Opus 4.7 기준:
- Input: ~3000 tokens (프롬프트 + 종목 요약 5개)
- Output: ~500 tokens (JSON 응답)

비용 계산 예시 (변경될 수 있음):
- Input: $15/1M tokens × 3000 = $0.045
- Output: $75/1M tokens × 500 = $0.0375
- **호출 1회당 약 $0.08 (~110원)**

매일 1회 호출 → **월 ~$2.5 (~3,400원)** 수준.
재시도/폴백까지 포함해도 월 $5 이내.

---

## 14. 알려진 이슈 / 개선 포인트

| 이슈 | 위치 | 영향 |
|---|---|---|
| LLM 학습 컷오프 이후 이벤트 모름 | 본질적 한계 | 최근 1~2달 발생 사건은 LLM이 모를 수 있음 (실적 발표일은 documents/17 연동으로 해결) |
| temperature=0이지만 모델 업데이트로 답변 바뀔 수 있음 | API 자체 | 같은 날 두 번 호출하면 다를 수 있음 (이론상 동일이지만) |
| HOLD 사유가 1~2 문장으로 짧음 | 프롬프트 | 상세한 추론 과정 추적 어려움 |
| max_tokens=2000 한계 | `:156` | 후보 종목 많으면(20+) 응답 잘릴 가능성 |
| `llm_reason="LLM 응답 없음"` 케이스 처리 모호 | `:182` | 누락된 종목이 자동 HOLD 처리되어도 디버깅 어려움 |
| 재시도 6회 후 실패 시 사용자 알림 없음 | `:240-246` | 알람/슬랙 통보 추가 권장 |
| Anthropic API 키가 .env에 평문 | `core/config.py` | 외부 노출 시 비용 폭증 위험 |
| 프롬프트가 한글이라 LLM이 영문 응답 시 파싱 실패 가능 | `:95-144` | JSON 형식만 강제하니 큰 문제는 없음 |
| ML 학습 데이터 컷오프 후 새 종목 모름 | 본질적 | 후보에 없으니 큰 문제는 아님 |

---

## 15. 코드 한눈에 찾기

| 기능 | 파일 | 라인 |
|---|---|---|
| 모델 설정 | `app/services/llm_review_service.py` | 9-11 |
| 결정 로그 저장 | `app/services/llm_review_service.py` | 14-37 |
| **메인 함수** | `app/services/llm_review_service.py` | **40-253** |
| API 키 체크 (Fail-Close) | `app/services/llm_review_service.py` | 58-65 |
| 종목별 요약 생성 | `app/services/llm_review_service.py` | 75-90 |
| 프롬프트 (역할 + 기준 + 형식) | `app/services/llm_review_service.py` | 95-144 |
| API 호출 + 재시도 | `app/services/llm_review_service.py` | 150-238 |
| 응답 파싱 | `app/services/llm_review_service.py` | 161-167 |
| 종목별 BUY/HOLD 매핑 | `app/services/llm_review_service.py` | 174-191 |
| 실패 시 로그 + 차단 | `app/services/llm_review_service.py` | 240-253 |
| 호출 위치 (스케줄러) | `app/utils/scheduler.py` | 518-526 |

---

## 16. 마지막 정리: 이 시스템의 LLM 활용 철학

이 시스템에서 LLM은 **"매수 의사결정자"가 아닙니다**. LLM은 **"마지막 안전장치"** 역할만 합니다.

### LLM이 하는 일
- 시스템이 골라낸 매수 후보 중 위험한 종목 거부
- 시장 환경에 대한 짧은 코멘트 생성
- 결정에 대한 근거 텍스트 제공

### LLM이 하지 않는 일
- 새로운 매수 후보 발굴 (시스템이 안 고른 종목 추가 X)
- 매수 수량/가격 결정 (KIS API가 자동 계산)
- 매도 판단 (별도 시스템 매도 로직 사용)
- 종목 점수 계산 (정량 지표만 사용)

### 왜 이렇게 설계했나?

1. **LLM은 환각(Hallucination) 위험** → 정량 데이터 기반 시스템에 끼워 넣되, 결정권 제한
2. **재현성/추적성** → 결정 로그가 남아 사후 분석 가능
3. **비용 통제** → LLM 호출 횟수가 명확히 제한됨 (매일 1회)
4. **시스템 안정성** → LLM 장애 시에도 기존 분석은 유지, 단지 매수만 일시 중단

이런 **"AI를 보조로만 쓰는 보수적 설계"** 가 자동매매 시스템의 표준 패턴입니다.
