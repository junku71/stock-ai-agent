# 국내주식(KOSPI 100) 자동매매 트랙 설계

미국 주식 자동매매 시스템을 KOSPI 시가총액 상위 100개 종목에 적용한 트랙이다.
기존 미국 트랙은 **그대로 유지**되고, 국내 트랙이 병행 운영된다.

---

## 1. 미국 트랙과의 관계

| 구분 | 처리 |
|------|------|
| **공유** | `scoring_service`(z-score·사전필터), `notification_service._send`(Slack 전송), `balance_service.get_access_token`(KIS 토큰), `ml_trigger_service`(Kaggle), `db/supabase` |
| **신규** | 데이터 수집, KIS 국내 주문, 뉴스 감성, 스케줄러, LLM 프롬프트, 전용 테이블(`kr_*`) |
| **격리** | `schedule` 전역 큐를 쓰지 않고 **KR 전용 `schedule.Scheduler()` 인스턴스**를 사용한다. 두 스레드가 같은 큐에 `run_pending()`을 돌리면 잡이 중복 실행돼 이중 주문이 날 수 있다. |

`KR_ENABLED=false`면 국내 스케줄러는 기동하지 않는다. 미국 트랙에는 어떤 영향도 없다.

---

## 2. 데이터 소스

### 2-1. NAVER API Hub — 뉴스 감성

기존 `openapi.naver.com`(네이버 개발자센터) 검색 API는 **NAVER API HUB(ncloud)로 이관**됐다.
호스트와 인증 헤더가 모두 바뀌었으므로 도메인만 갈아끼우면 동작하지 않는다.

| 항목 | 값 |
|------|-----|
| 호스트 | `https://naverapihub.apigw.ntruss.com` |
| 뉴스 검색 | `GET /search/v1/news` — 일 25,000회 |
| 인증 헤더 | `X-NCP-APIGW-API-KEY-ID`, `X-NCP-APIGW-API-KEY` |
| 응답 | `items[]`: `title`, `description`, `originallink`, `link`, `pubDate` |

**AlphaVantage와의 결정적 차이**: AlphaVantage `NEWS_SENTIMENT`는 `ticker_sentiment_score`(-1~+1)를
완제품으로 줬지만, 네이버는 **기사 텍스트만** 준다. 그래서 `kr_sentiment_service.py`가
종목 6개씩 묶어 Claude로 배치 스코어링한다(한국 금융 맥락 루브릭 포함, 프리픽스 캐시 적용).

부가 활용:

- **데이터랩 검색어 트렌드**(`POST /datalab/v1/search`) — 종목명 검색량 급증 = 개인 관심 유입.
  미국판에 없던 신규 신호지만 **점수에는 넣지 않고** LLM 참고 정보로만 넘긴다.
  한국 시장에서 개인 관심 급증은 고점 신호로 작동하는 경우가 많아 부호가 불안정하기 때문이다.
- 블로그 언급량 — 보조 신호.

**Naver API Hub에 주가·재무지표 API는 없다.** 네이버 금융은 웹페이지일 뿐 공식 API가 아니며
크롤링은 ToS 위반 소지가 있다.

### 2-2. AlphaVantage / Finnhub 국내 대체재

| 용도 | 소스 | 비고 |
|------|------|------|
| **시세·일봉·재무비율·투자자매매동향** | **KIS API 국내주식** | 이미 쓰는 인증 그대로. `finance/financial-ratio`, `income-statement`, `balance-sheet`, `profit-ratio`, `stability-ratio`, `growth-ratio` — AlphaVantage 펀더멘털 수준을 KIS가 이미 제공한다 |
| **시장 전체 OHLCV·시총·PER/PBR, KOSPI 지수** | **KRX Open API** (`openapi.krx.co.kr`) | 무료, `AUTH_KEY` 헤더, 2010년~. AlphaVantage에 가장 근접한 대체재. 인증키 신청 후 **API별로 이용신청**을 따로 해야 한다 |
| **공시·재무제표(XBRL)** | **OpenDART** (`opendart.fss.or.kr`) | 무료. 실적 캘린더 대체 |
| **한국 거시지표** | **한국은행 ECOS OpenAPI** | FRED의 한국판 |
| **히스토리 백필·글로벌 지표** | Yahoo Finance | `^KS11`, `069500.KS`(KODEX200), `^KQ11`, `KRW=X`, `005930.KS` … |
| 보조 | 공공데이터포털 금융위 주식시세정보, pykrx / FinanceDataReader | 비공식·스크래핑 기반 |

Finnhub는 한국 종목 커버리지가 빈약하고, AlphaVantage는 `005930.KS`를 일부 받지만 품질이 낮아
**둘 다 권장하지 않는다.**

### 2-3. 역할 분담

```
Yahoo Finance  -> 지수/환율/글로벌/100종목 일별 종가 (최근 KR_HISTORY_YEARS 년)
한국은행 ECOS  -> 기준금리/국고채/CD/CPI/M2 (best-effort)
KIS API        -> 당일 시세, 일봉 OHLCV(ADX/ATR/거래량비율), 투자자 수급, 주문/잔고
NAVER API Hub  -> 뉴스 원문 + 검색 트렌드
```

---

## 3. KIS 국내주식 API (검증 완료)

출처: `github.com/koreainvestment/open-trading-api` — `examples_user/domestic_stock/domestic_stock_functions.py`

| 기능 | 엔드포인트 | 실전 | 모의 |
|------|-----------|------|------|
| 주문(현금) 매수 | `/uapi/domestic-stock/v1/trading/order-cash` | `TTTC0012U` | `VTTC0012U` |
| 주문(현금) 매도 | 위와 동일 | `TTTC0011U` | `VTTC0011U` |
| 잔고 | `/trading/inquire-balance` | `TTTC8434R` | `VTTC8434R` |
| 매수가능금액 | `/trading/inquire-psbl-order` | `TTTC8908R` | `VTTC8908R` |
| 현재가 | `/quotations/inquire-price` | `FHKST01010100` (공통) | |
| 기간별시세(일봉) | `/quotations/inquire-daily-itemchartprice` | `FHKST03010100` (공통, 1회 100건) | |
| 업종 기간별시세 | `/quotations/inquire-daily-indexchartprice` | `FHKUP03500100` (공통) | |
| 종목별 투자자매매동향 | `/quotations/inquire-investor` | `FHKST01010900` (공통) | |
| 휴장일 조회 | `/quotations/chk-holiday` | `CTCA0903R` (1일 1회 권장) | |
| 재무비율 | `/finance/financial-ratio` | `FHKST66430300` (실전만) | |

**주의사항**

- 주문 body에 `EXCG_ID_DVSN_CD="KRX"`는 **필수**다. 누락하면 주문이 거부된다.
- POST body의 key는 모두 대문자여야 한다.
- 초당 호출 제한(모의 2건/초)이 있어 `kis_domestic_service`가 전역 0.6초 간격을 강제한다.

### 호가가격단위 (유가증권시장, 2023-01-25 개정)

지정가가 호가단위에 맞지 않으면 주문이 거부되므로 `round_to_tick()`을 반드시 통과시킨다.

| 주가 | 호가단위 |
|------|---------|
| 2,000원 미만 | 1원 |
| 2,000 ~ 5,000 | 5원 |
| 5,000 ~ 20,000 | 10원 |
| 20,000 ~ 50,000 | 50원 |
| 50,000 ~ 200,000 | 100원 |
| 200,000 ~ 500,000 | 500원 |
| 500,000 이상 | 1,000원 |

매수는 **올림**(최대 1틱 양보, 0.2% 이하 — 체결 확률을 크게 높인다), 매도는 **내림**으로 정규화한다.

---

## 4. 시간 구조 — 왜 2단계인가

미국 트랙은 KST 21:00 파이프라인이 곧 NY 장 시작 전이라 분석과 매수를 한 번에 끝낼 수 있다.
한국 증시는 **15:30 KST에 닫히므로 장 마감 후 분석 시점에는 주문을 낼 수 없다.**

```
평일 09:00 ------------------ 15:20 -- 15:30      16:30 ---------- (다음 개장일) 09:05
   |                            |        |          |                        |
   |  <--- 매도 감시 1분 주기 --->|      장마감     Phase A                 Phase B
   |                            |                  (분석)                 (매수 집행)
   +- Phase B 매수 주문                        1 시장데이터          kr_buy_queue 읽기
                                              2 Kaggle ML           -> 현재가 재조회
                                              3 기술지표+뉴스감성    -> 수량 재계산
                                              4 LLM 검토 -> 큐 저장  -> 지정가 매수
```

- **동시호가(15:20~15:30)** 는 지정가 주문의 체결 성격이 달라 매도 감시 창에서 제외한다.
- **정합성 확인**(체결/미체결 정리)은 장 시간 밖에서도 1분마다 돈다. 15:40 이후 미체결 매수는
  `buy_failed`로, 미체결 매도는 `holding`으로 복원한다(당일 유효 주문).
- 큐에 남은 예약은 **2일 초과 시 만료**된다. 연휴로 밀린 낡은 판단으로 매수하지 않기 위해서다.
- 휴장일은 `chk-holiday`(CTCA0903R) 결과를 하루 1회 캐시해 판단하고, 조회 실패 시 주말 여부로 폴백한다.

---

## 5. 점수 체계 (`kr_scoring.py`)

미국 트랙의 cross-sectional z-score 방식에 **수급 팩터**를 추가했다.

| 팩터 | 가중치 | 근거 |
|------|--------|------|
| 기술 모멘텀 (MACD diff + SMA 괴리 + RSI 평균) | 0.27 | 가장 검증된 신호 |
| ML 예측 상승률 | 0.18 | 보수적 |
| **수급 (외국인+기관 5일 순매수)** | **0.15** | **한국 시장에서 가장 검증된 단기 신호** |
| 거래량 비율 | 0.15 | 가격-거래량 컨펌 |
| ADX 추세 강도 | 0.15 | trend persistence |
| 뉴스 감성 | 0.10 | 학술 IC 약함 |

**사전 필터**(미국과 공유): RSI > 80 하드블록 + 기술 신호 2개 이상.

**공포 게이트**: 한국에는 VIX에 대응하는 무료 실시간 지수를 붙이기 번거로워
**코스피 일별 수익률의 20일 실현변동성(연율 %)** 을 쓴다.

수집 시 `kr_economic_and_stock_data."코스피 변동성 20일"` 컬럼에 적재하고
(`compute_realized_volatility`), 조회 시에는 이 값을 우선 사용한다. 컬럼이 비어 있으면
그 자리에서 계산하는 폴백이 있다. 계산은 **거래일만 있는 원본 시계열**로 해야 한다 —
달력일로 `ffill` 된 시계열을 쓰면 휴장일의 수익률 0이 표준편차를 끌어내려 과소평가된다.
산출값이 0~150% 밖이면 소스 오염으로 보고 `None`(게이트 해제)으로 처리한다.

2006~2026 실측 분포 — 중위 15.0%, 75%ile 20.4%, 90%ile 29.7%, 95%ile 41.7%,
99%ile 75.6% (리먼 2008-10 = 88.5%, 코로나 2020-03 = 69.9%).

| 20일 실현변동성 | 백분위 | 임계값 |
|-----------------|--------|--------|
| < 20 | ~하위 74% | 0.40 |
| 20 ~ 25 | 74~82% | 0.45 |
| 25 ~ 30 | 82~90% | 0.55 |
| 30 ~ 40 | 90~95% | 0.70 |
| 40 ~ 55 | 95~98% | 0.90 |
| 55 ~ 90 | 98~99.9% | 1.10 |
| > 90 | 상위 0.1% | **매수 전면 중단** |

**하드블록 90%는 운영자 지정값이다.** 실측 분포상 99.9%ile 근처라 매수를 멈추는 국면은
사실상 리먼을 넘어서는 극단뿐이고, 리먼(88.5%)·코로나(69.9%)급 폭락장에서도 매수는
진행된다. 대신 30% 위 구간을 계단으로 쪼개 변동성이 오를수록 상위 종목만 남도록 해
위험을 흡수한다. 더 보수적으로 운영하려면 `kr_scoring.FEAR_HARD_BLOCK` 을 낮추면 된다.

코스피200이 아니라 코스피를 쓰는 이유는 히스토리가 가장 길고, 추종 ETF 교체 같은
소스 변경에 영향을 받지 않기 때문이다.

---

### 변동성 게이트 수동 오버라이드

폭락장이라도 운영자 판단으로 매수를 진행해야 할 때가 있다. 하드블록을 **한시적으로만**
해제하는 경로를 별도로 뒀다 (`kr_override_service`).

```
POST /kr/fear-gate/override?confirm=OVERRIDE&reason=...&minutes=120
POST /kr/fear-gate/revoke          # 즉시 복구
GET  /kr/fear-gate                 # 현재 변동성/임계값/해제 여부
```

조용히 열려 있는 상태가 가장 위험하므로 안전장치를 겹쳐 뒀다.

| 장치 | 내용 |
|------|------|
| 확인 문구 | `confirm=OVERRIDE` 정확 일치 (오타 방지) |
| 사유 필수 | 5자 이상. `kr_fear_gate_overrides` 에 감사 기록 |
| 자동 만료 | 기본 120분, 최대 24시간. 지나면 게이트 자동 복구 |
| 단일 활성 | 새로 발급하면 기존 오버라이드는 즉시 만료 |
| Slack 알림 | 발급/해제 즉시 통지 + 매일 LLM 결정 알림에 "해제 중" 배너 |
| DB 영속 | 서버 재시작에도 유지, 이력 추적 가능 |
| fail-safe | 조회 실패 시 오버라이드 없음으로 간주 (게이트 정상 작동 쪽으로) |

**기본 동작은 하드블록만 해제**한다. 변동성 기반 적응형 임계값은 그대로 유지되므로
폭락장에서는 여전히 상위 종목만 통과한다. `relax_threshold=true` 를 주면 임계값까지
평온장 기준(0.40)으로 낮추는데, 이건 파이프라인 전 구간을 관통시켜 확인할 때 쓰는
테스트 옵션이며 실매매에서는 권장하지 않는다.

---

## 6. 매도 조건 (`kr_recommendation_service.get_sell_candidates`)

1. **ATR 익절/손절** — 매수 시점 `buy_price ± ATR×(2.5 / 1.5)`. 기록이 없으면 고정비율(+6% / −7%) 폴백.
2. **기술적 매도 신호** — 데드크로스 / RSI>70 / MACD 매도 / 패닉셀(거래량 2배 + 당일 −3%) /
   **외국인·기관 5일 순매도**(국내 전용).
   - ADX > 25면 필요 신호 수 1개 차감
   - 감성 < −0.15이면 2개(ADX 보정 시 1개), 아니면 3개(보정 시 2개)
3. **공포장** — 변동성 > 40 + 신호 1개, 변동성 > 30 + 신호 2개

매도 알림에는 **T+2 결제**(매도 대금은 2영업일 후 인출 가능)를 함께 표기한다.

---

## 7. 파일 구조

```
app/services/kr/
  universe.py                  KOSPI 30 종목 마스터 (코드/이름/섹터/뉴스 검색어 보정)
  kis_domestic_service.py      KIS 국내 API 래퍼 + 호가단위 + 장시간/휴장일
  naver_service.py             NAVER API Hub (뉴스/블로그/데이터랩)
  kr_sentiment_service.py      뉴스 -> Claude 배치 스코어링 -> 감성 점수
  kr_market_data_service.py    Yahoo + ECOS -> kr_economic_and_stock_data
  kr_scoring.py                z-score 채점 (+수급 팩터, 변동성 적응형 임계값)
  kr_override_service.py       변동성 게이트 수동 오버라이드 (한시적, 감사 기록)
  kr_recommendation_service.py 기술지표 생성 / 매수 후보 / 매도 후보
  kr_llm_review_service.py     LLM 최종 검토 (한국 시장 프롬프트, Fail-Close)
  kr_notification_service.py   Slack (원화 포맷)
app/utils/kr_scheduler.py      2단계 파이프라인 + 매도 감시 + 정합성
app/api/routes/kr.py           /kr/* 라우트 21개
sql/kr/setup_kr.sql            전체 스키마 (테이블 9개 + 컬럼 마이그레이션 + RLS/권한, 멱등)
kaggle_notebook_kr/            국내 전용 ML 커널 (predict_kr.py)
```

---

## 8. 테이블 (`sql/kr/setup_kr.sql`)

| 테이블 | 역할 |
|--------|------|
| `kr_economic_and_stock_data` | 날짜별 지수·환율·글로벌·거시 + **코스피 20일 변동성** + 30종목 종가 (ML 입력) |
| `kr_stock_recommendations` | 기술적 지표 스냅샷 (매 분석 시 전량 교체) |
| `kr_stock_analysis_results` | Kaggle ML 예측 결과 |
| `kr_ticker_sentiment_analysis` | 뉴스 감성 점수 |
| `kr_news_articles` | 감성 점수 근거 기사 (사후 추적용) |
| `kr_buy_queue` | 다음 개장일 매수 예약 |
| `kr_trade_records` | 매매 기록 + ATR 익절/손절 (원화) |
| `kr_llm_decision_logs` | LLM 판단 로그 |
| `kr_fear_gate_overrides` | 변동성 게이트 수동 해제 이력 (감사) |

---

## 9. 시작 절차

1. **스키마 설치** — Supabase SQL Editor에 `sql/kr/setup_kr.sql` 전체 붙여넣고 Run
   (멱등이라 기존 DB에 다시 돌려도 안전하다 — 유니버스를 늘렸을 때도 이 파일만 재실행하면 된다)
2. **`.env` 채우기** — `NAVER_API_KEY_ID` / `NAVER_API_KEY`(필수), `ECOS_API_KEY`(권장),
   `KAGGLE_KERNEL_SLUG_KR`
3. **Kaggle 커널 생성** — `kaggle_notebook_kr/kernel-metadata.json`의 `id`를 본인 계정으로.
   `ml_trigger_service`가 push 직전에 자동 교정도 한다
4. **히스토리 백필** (수십 분): `POST /kr/market-data/collect?full=true`
5. **드라이런 검증** — `KR_ENABLED=true`, `KR_DRY_RUN=true`로 두고 서버 재시작

   ```
   GET  /kr/status                  설정/연동 상태 확인
   GET  /kr/economic/ecos-check     ECOS 통계코드 유효성 진단
   POST /kr/technical/generate      기술 지표 생성
   POST /kr/sentiment/collect       뉴스 감성 수집
   GET  /kr/candidates/buy          매수 후보 확인
   POST /kr/pipeline/analysis       전체 분석 파이프라인 (Kaggle 포함)
   POST /kr/pipeline/execute-buy    매수 집행 (드라이런이면 로그만)
   ```

6. **모의투자 전환** — `KR_DRY_RUN=false`, `KIS_USE_MOCK=true`
7. **실전 전환** — 충분히 관찰한 뒤 `KIS_USE_MOCK=false`

---

## 10. 확인이 필요한 항목

| 항목 | 내용 |
|------|------|
| **ECOS 통계항목코드** | `kr_market_data_service.ECOS_SERIES`의 항목코드는 한국은행 개편 시 바뀔 수 있다. `GET /kr/economic/ecos-check`로 진단하고 실패한 코드만 고치면 된다. 실패해도 파이프라인은 진행된다 |
| **데이터랩 경로** | API Hub의 `/datalab/v1/search` 경로가 변경될 수 있다. 실패 시 검색 관심도만 빠지고 나머지는 정상 동작한다 |
| **모의투자 미지원 API** | `inquire-index-price`(업종 현재지수), `inquire-investor-daily-by-market`(시장 수급), `financial-ratio`는 실전 계좌에서만 동작한다. 현재 매매 로직은 이들 없이도 완결되도록 짜여 있다 |
| **유니버스 갱신** | 시총 순위는 분기 단위로 바뀐다. 종목을 교체하면 `universe.py`, `setup_kr.sql` 컬럼, `predict_kr.py`의 `TARGET_COLUMNS` **세 곳을 함께** 고쳐야 한다 |
| **수집 기간** | `KR_HISTORY_YEARS`(기본 5년). 종목이 100개라 기간이 길수록 수집·학습 시간이 선형으로 늘어난다 |
| **신규 상장 종목** | 상장이 늦은 종목 하나가 학습 구간 전체를 잘라먹지 않도록, `predict_kr.py` 가 `MIN_TRAIN_ROWS`(900행 ≈ 2.5년)를 확보하지 못하는 종목을 **ML 타깃에서 자동 제외**한다. 제외 종목은 ML 예측이 없어 매수 후보에서도 빠진다 |
