# 뉴스 감성 점수 API (AlphaVantage NEWS_SENTIMENT)

> 모든 코드 인용은 `app/services/stock_recommendation_service.py:437-567` 의 `fetch_and_store_sentiment_for_recommendations()` 함수에서 가져왔습니다.

---

## 1. 한 문장 요약

> **"AlphaVantage라는 외부 API에 종목 티커를 던지면 → 그 종목 관련 최근 3일치 뉴스를 받아오고 → 각 뉴스의 긍정·부정 점수를 평균 내서 → DB에 저장한다."**

이렇게 모인 점수가 매수 종합점수의 **20%** 비중으로 들어가고, 매도 판단에서는 **부정 감성(<-0.15)** 일 때 매도 신호 가산 조건으로 쓰입니다.

---

## 2. AlphaVantage가 뭐예요?

[AlphaVantage](https://www.alphavantage.co)는 무료/유료 금융 데이터 API를 제공하는 외부 서비스입니다. 이 시스템에서 쓰는 **`NEWS_SENTIMENT`** 엔드포인트는 다음을 해줍니다.

- 전 세계 주요 금융 뉴스를 자체 수집
- 각 뉴스에 대해 **자체 NLP 모델로 감성 점수 부여**
- 종목 티커를 주면 그 종목 관련 뉴스만 필터링해서 반환

> 즉, 우리가 직접 뉴스를 크롤링하거나 NLP를 돌리지 않습니다. **AlphaVantage가 다 해서 준비해놓은 결과를 받아 쓸 뿐**입니다.

### API 키는 어디 있나요?

`.env` 파일의 `ALPHA_VANTAGE_API_KEY` 환경 변수에서 읽습니다.

```python
# app/services/stock_recommendation_service.py:470
api_key = settings.ALPHA_VANTAGE_API_KEY
```

이 값은 `app/core/config.py` 의 `Settings` 클래스가 환경 변수에서 자동으로 로드합니다.

---

## 3. 어떤 종목들에 대해 뉴스를 받아오나요?

```python
# stock_recommendation_service.py:443-468
stock_recs = self.get_stock_recommendations()
recommendations = stock_recs.get("recommendations", [])

# 추천 주식의 티커 목록 생성
recommended_tickers = [STOCK_TO_TICKER.get(rec["Stock"]) for rec in recommendations
                       if rec["Stock"] in STOCK_TO_TICKER]

# 보유 주식 정보 가져오기 (전체 거래소: NASD, NYSE, AMEX)
balance_result = get_all_overseas_balances()
holdings = balance_result.get("output1", [])

# 보유 주식의 티커 목록 생성
holding_tickers = [item.get("ovrs_pdno") for item in holdings if item.get("ovrs_pdno")]

# 추천 주식과 보유 주식의 티커를 합치고 중복 제거
all_tickers = list(set(recommended_tickers + holding_tickers))
```

### 두 종류의 종목을 합쳐서 분석합니다

| 종류 | 출처 | 왜 분석? |
|---|---|---|
| **추천 종목** | `stock_analysis_results` 테이블 (ML 예측 통과 종목) | 매수 후보의 분위기 확인 |
| **보유 종목** | KIS 잔고 API (실제 보유 중인 종목) | 매도 판단에 부정 감성 활용 |

> 둘을 합치면 보통 5~15개 정도가 됩니다 (set으로 중복 제거).

---

## 4. API 호출은 어떻게 하나요?

### 4-1. 요청 파라미터 (stock_recommendation_service.py:471-481)

```python
api_key = settings.ALPHA_VANTAGE_API_KEY
relevance_threshold = 0.2          # 관련도 0.2 이상만 채택
sleep_interval = 5                  # 호출 간 5초 대기
yesterday = (datetime.now() - timedelta(days=3)).strftime("%Y%m%dT0000")  # 최근 3일치

base_url = "https://www.alphavantage.co/query"
params = {
    "function": "NEWS_SENTIMENT",
    "time_from": yesterday,
    "limit": 100,
    "apikey": api_key
}
```

#### 파라미터 풀이

| 파라미터 | 값 | 설명 |
|---|---|---|
| `function` | `"NEWS_SENTIMENT"` | AlphaVantage의 뉴스 감성 분석 엔드포인트 지정 |
| `time_from` | 3일 전 (`YYYYMMDDT0000`) | 이 시간 이후 발행된 뉴스만 |
| `limit` | 100 | 한 번에 최대 100개 기사 |
| `apikey` | 사용자 키 | 인증용 |
| `tickers` | (반복문에서 종목별로 추가) | 어느 종목 뉴스를 받을지 |

> ⚠️ **무료 플랜은 분당 5회 / 일 25회 제한** 이 있어서 `time.sleep(5)` 로 호출 간격을 5초씩 띄웁니다.

---

### 4-2. 종목별로 반복 호출 (stock_recommendation_service.py:497-562)

```python
for ticker in all_tickers:
    print(f"{ticker} 처리 중...")
    params["tickers"] = ticker      # ← 매 반복마다 종목만 바꿈

    response = requests.get(base_url, params=params)
    if response.status_code != 200:
        # 실패 시 빈 결과 추가하고 다음 종목으로
        results.append({...})
        time.sleep(sleep_interval)
        continue

    api_data = response.json()
    feed = api_data.get('feed', [])      # ← 뉴스 기사 배열
    ...
```

종목 하나당 한 번씩 API를 호출하고, 응답에서 `feed` 배열을 뽑아냅니다.

#### AlphaVantage 응답은 어떻게 생겼나?

```json
{
  "feed": [
    {
      "title": "Apple's Q4 earnings beat expectations",
      "url": "https://...",
      "time_published": "20260423T143000",
      "source": "Reuters",
      "summary": "...",
      "ticker_sentiment": [
        {
          "ticker": "AAPL",
          "relevance_score": "0.95",
          "ticker_sentiment_score": "0.32",
          "ticker_sentiment_label": "Bullish"
        },
        {
          "ticker": "MSFT",
          "relevance_score": "0.15",
          "ticker_sentiment_score": "0.05",
          "ticker_sentiment_label": "Neutral"
        }
      ]
    },
    ...
  ]
}
```

#### 핵심 필드 설명

| 필드 | 의미 | 범위 |
|---|---|---|
| `ticker_sentiment_score` | **이 종목에 대한 감성 점수** | -1.0 ~ +1.0 |
| `relevance_score` | **이 뉴스가 해당 종목과 얼마나 관련 있는지** | 0.0 ~ 1.0 |
| `ticker_sentiment_label` | 감성 라벨 (참고용) | Bullish / Bearish / Neutral 등 |

> 한 뉴스가 여러 종목을 동시에 언급할 수 있어서 `ticker_sentiment` 가 **배열** 입니다.
> 예: "애플이 마이크로소프트와 협력 발표" → AAPL 0.32 / MSFT 0.05 같이 두 항목이 들어옴.

---

### 4-3. 점수 추출 + 관련도 필터 (stock_recommendation_service.py:518-523)

```python
articles = [
    float(sentiment['ticker_sentiment_score'])
    for article in feed
    for sentiment in article.get('ticker_sentiment', [])
    if sentiment['ticker'] == ticker
       and float(sentiment['relevance_score']) >= relevance_threshold  # 0.2
]
```

이 한 줄(리스트 컴프리헨션)이 핵심입니다. 풀어쓰면:

```python
articles = []
for article in feed:                                    # 100개 기사 순회
    for sentiment in article.get('ticker_sentiment', []):  # 각 기사의 종목별 감성
        if sentiment['ticker'] == ticker:                  # 우리가 원하는 종목 맞으면
            relevance = float(sentiment['relevance_score'])
            if relevance >= 0.2:                          # 관련도가 충분히 높으면
                articles.append(float(sentiment['ticker_sentiment_score']))
```

#### 왜 `relevance_score >= 0.2` 필터를 거는가?

뉴스가 종목을 살짝 언급만 하고 본질적으로 다른 회사 이야기일 수 있습니다.
예: "테슬라 주가 상승, 그 영향으로 AAPL도 0.5% 올라" → 본문이 테슬라 이야기인데 AAPL이 끼어 들어옴.

**관련도가 0.2 미만이면 노이즈**로 보고 버립니다. 이 임계값을 너무 높이면 데이터가 너무 적어지고, 너무 낮추면 무관한 기사가 섞여서 점수가 오염됩니다.

---

### 4-4. 평균 감성 점수 계산 (stock_recommendation_service.py:525-541)

```python
if not articles:
    # 관련 기사가 한 개도 없으면 스킵
    results.append({...})
    time.sleep(sleep_interval)
    continue

average_sentiment = sum(articles) / len(articles)
article_count = len(articles)
calculation_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
```

**산식**: 모든 관련 기사 점수를 단순 평균.

| 예시 | 점수들 | 평균 | 해석 |
|---|---|---|---|
| 강한 호재 | [0.5, 0.4, 0.6, 0.3] | 0.45 | 매우 긍정 |
| 보통 | [0.1, -0.05, 0.15, 0.0] | 0.05 | 거의 중립 |
| 악재 | [-0.3, -0.4, -0.25, -0.5] | -0.36 | 매우 부정 |

> AlphaVantage 점수 기준 (공식 문서):
> - **x ≤ -0.35**: Bearish (부정적)
> - **-0.35 < x ≤ -0.15**: Somewhat-Bearish
> - **-0.15 < x < 0.15**: Neutral
> - **0.15 ≤ x < 0.35**: Somewhat-Bullish
> - **0.35 ≤ x**: Bullish

---

### 4-5. DB에 저장 (stock_recommendation_service.py:543-550)

```python
supabase_data = {
    "ticker": ticker,
    "average_sentiment_score": average_sentiment,
    "article_count": article_count,
    "calculation_date": calculation_date
}
supabase.table("ticker_sentiment_analysis").insert(supabase_data).execute()
```

`ticker_sentiment_analysis` 테이블에 한 줄씩 INSERT 합니다.

#### 테이블 스키마

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `ticker` | text | 종목 티커 (AAPL 등) |
| `average_sentiment_score` | float | 평균 감성 점수 (-1 ~ +1) |
| `article_count` | int | 분석에 포함된 기사 수 |
| `calculation_date` | timestamp | 계산 시각 |

---

## 5. 기존 데이터는 어떻게 처리하나요? (스냅샷 방식)

```python
# stock_recommendation_service.py:491-494
print("기존 감정 분석 데이터 삭제 중...")
supabase.table("ticker_sentiment_analysis").delete().gte("ticker", "").execute()
print("기존 감정 분석 데이터 삭제 완료")
```

**호출 시작 직전에 테이블을 통째로 비우고, 새로 채웁니다.**

> `delete().gte("ticker", "")` 는 "ticker가 빈 문자열보다 크거나 같은 모든 행" → 사실상 **모든 행 삭제**.
> Supabase는 안전상 `delete()` 만으로는 전체 삭제가 안 되어서 `gte()` 같은 항상 참인 조건을 같이 씁니다.

### 왜 누적이 아닌 스냅샷?

- **장점**: 항상 최신 3일치 뉴스만 반영 → 오래된 노이즈 제거
- **단점**: 시간에 따른 감성 변화 추적 불가 (트렌드 분석은 `llm_decision_logs` 테이블에서 별도 추적)

---

## 6. 매수 점수 계산에 어떻게 쓰이나요?

다른 문서(`03_지표_및_종합점수_계산.md`)에서 자세히 다뤘지만, 핵심만 다시 짚으면:

```python
# stock_recommendation_service.py:681-683
raw_sentiment = item["sentiment_score"] if item["sentiment_score"] is not None else 0.0
# 감성점수 정규화: [-1, 1] → [0, 1] (다른 점수와 범위 통일)
sentiment_score = (raw_sentiment + 1) / 2
```

| 원본 (raw_sentiment) | 정규화 후 (sentiment_score) | 종합점수 기여 (× 0.20) |
|---|---|---|
| +0.45 (강한 긍정) | 0.725 | +0.145 |
| +0.05 (중립) | 0.525 | +0.105 |
| -0.36 (강한 부정) | 0.320 | +0.064 |

> ⚠️ 정규화 때문에 **음수 감성도 양수 기여로 변환** 됩니다 (단, 부정일수록 작은 값).
> 즉 감성 점수는 **매수 점수에서 깎이지 않고**, 단지 작게 가산될 뿐입니다.

---

## 7. 매도 판단에서는 어떻게 쓰이나요?

매도 로직(`get_stocks_to_sell()`)에서는 **원본 점수**를 그대로 사용합니다.

```python
# stock_recommendation_service.py:959-964
elif sentiment_score is not None and sentiment_score < -0.15:
    required_signals_2a = 2 - adx_adjustment
    if technical_sell_signals >= required_signals_2a:
        adx_note = f", ADX={adx_value:.1f} 보정" if adx_adjustment else ""
        sell_reasons.append(f"부정적 감성({sentiment_score:.2f}) + 매도 신호 {technical_sell_signals}개/{required_signals_2a}개")
```

### 매도 트리거 조건

> **감성 < -0.15 + 기술적 매도 신호 2개 이상 → 매도**

부정 감성을 매도 신호 카운트의 **트리거**로 쓰는 것이 특징입니다 (점수 합산이 아님).
ADX > 25이면 필요 신호 수가 1개 줄어듭니다.

---

## 8. 전체 흐름도

```
┌──────────────────────────────────────────────────────────────────┐
│ STEP 1. 분석 대상 종목 결정                                        │
│   - ML 추천 종목 (stock_analysis_results) +                       │
│   - 현재 보유 종목 (KIS get_all_overseas_balances)                │
│   = all_tickers (set으로 중복 제거)                                │
└──────────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────────┐
│ STEP 2. 기존 감성 데이터 전체 삭제 (스냅샷 갱신)                    │
│   ticker_sentiment_analysis.delete().gte("ticker", "")            │
└──────────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────────┐
│ STEP 3. 종목마다 반복:                                              │
│                                                                   │
│   ┌─ 3-1. AlphaVantage NEWS_SENTIMENT API 호출                    │
│   │       (time_from = 3일 전, limit=100)                         │
│   ▼                                                               │
│   ┌─ 3-2. 응답에서 'feed' 배열 추출 (최대 100개 기사)               │
│   ▼                                                               │
│   ┌─ 3-3. 각 기사의 ticker_sentiment 배열 순회                     │
│   │       → 우리가 원하는 ticker 매칭                              │
│   │       → relevance_score >= 0.2 필터                           │
│   ▼                                                               │
│   ┌─ 3-4. 통과한 기사들의 ticker_sentiment_score 평균 계산          │
│   ▼                                                               │
│   ┌─ 3-5. ticker_sentiment_analysis 테이블에 INSERT                │
│   │       {ticker, average_sentiment_score, article_count, date}  │
│   ▼                                                               │
│   ┌─ 3-6. time.sleep(5)  ← API 속도 제한 회피                      │
└──────────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────────┐
│ STEP 4. 종합 점수 계산 시 ticker_sentiment_analysis 조회 →         │
│         (raw + 1) / 2 정규화 → composite_score에 0.20 가중 반영    │
└──────────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────────┐
│ STEP 5. 매도 판단 시 ticker_sentiment_analysis 조회 →              │
│         원본 점수 < -0.15 이면 매도 신호 카운트 트리거              │
└──────────────────────────────────────────────────────────────────┘
```

---

## 9. 자주 마주치는 케이스 / 예외 처리

### 9-1. 관련 기사가 한 개도 없을 때

```python
# stock_recommendation_service.py:525-536
if not articles:
    results.append({
        "ticker": ticker,
        "stock_name": ticker_to_stock.get(ticker, ticker),
        "message": "관련 기사 없음",
        ...
    })
    time.sleep(sleep_interval)
    continue
```

DB에는 INSERT 하지 않고 다음 종목으로 넘어갑니다. → 종합 점수 계산 시 `sentiment_score` 가 `None` 이 되어 정규화에서 `0.0` 으로 처리됩니다.

### 9-2. API 호출 실패 (status != 200)

```python
# stock_recommendation_service.py:502-513
if response.status_code != 200:
    results.append({...})
    time.sleep(sleep_interval)
    continue
```

마찬가지로 DB에 저장하지 않고 스킵. 외부 API 다운, 네트워크 끊김, 키 만료 등 다양한 원인이 있을 수 있습니다.

### 9-3. API 키가 빠졌을 때

`.env` 의 `ALPHA_VANTAGE_API_KEY` 가 비어있으면 AlphaVantage가 401/403을 반환하고, 모든 종목이 9-2 케이스로 빠집니다. → DB에는 아무 데이터도 없음 → 매수 점수의 감성 부분이 0(중립)으로 처리됨.

---

## 10. API 호출 주기 (어디서 트리거되나요?)

`fetch_and_store_sentiment_for_recommendations()` 는 다음 위치에서 호출됩니다.

| 위치 | 언제 | 비고 |
|---|---|---|
| `app/api/routes/stock_recommendations.py` | API 엔드포인트로 수동 호출 | 디버깅/테스트용 |
| 매수 스케줄러 (`scheduler.py`) | 자동 매수 직전 (10:30 ET) | 통합 점수 갱신 후 매수 진행 |

> 자동 매수 시 `get_combined_recommendations_with_technical_and_sentiment()` 가 `ticker_sentiment_analysis` 테이블을 읽기만 합니다.
> 그러므로 매수 직전에 `fetch_and_store_sentiment_for_recommendations()` 가 별도로 실행되어 데이터를 갱신해야 합니다.

---

## 11. 알려진 이슈 / 개선 포인트

| 이슈 | 위치 | 영향 |
|---|---|---|
| AlphaVantage 무료 플랜 일 25회 제한 | API 자체 | 분석 대상 25개 넘으면 일부 종목 누락 |
| 정규화로 부정 감성도 양수 기여 | `:683` | 매수 점수에서 부정뉴스 감점 효과 없음 |
| 관련도 0.2 임계값 하드코딩 | `:471` | 시장 상황에 따라 조정 불가 (튜닝 어려움) |
| 평균이 outlier에 취약 | `:538` | 극단치 1개가 평균 크게 끌고 감 (median 고려 가치) |
| 3일 윈도우 고정 | `:473` | 빠르게 변하는 호재/악재 캡처가 늦을 수 있음 |
| 기사별 가중치 없음 | `:518` | 큰 매체와 작은 매체가 동일 비중 |

---

## 12. 코드 한눈에 찾기

| 기능 | 파일 | 라인 |
|---|---|---|
| 메인 함수 | `app/services/stock_recommendation_service.py` | 437-567 |
| API 키 설정 | `app/core/config.py` (Settings) | — |
| 분석 대상 종목 결정 | `app/services/stock_recommendation_service.py` | 443-468 |
| API 호출 + 응답 처리 | `app/services/stock_recommendation_service.py` | 497-562 |
| 관련도 필터 | `app/services/stock_recommendation_service.py` | 518-523 |
| 평균 점수 계산 | `app/services/stock_recommendation_service.py` | 538 |
| DB 저장 | `app/services/stock_recommendation_service.py` | 544-550 |
| 매수 점수 정규화 | `app/services/stock_recommendation_service.py` | 681-683 |
| 매도 판단 사용 | `app/services/stock_recommendation_service.py` | 959-964 |
