# 국내주식(KOSPI 100) 자동매매 트랙 설계

KOSPI 시가총액 상위 100개 종목을 대상으로 하는 자동매매 시스템의 설계 문서다.
**이 시스템이 다루는 시장은 국내 하나뿐이다.**

> 과거에는 미국 주식 트랙과 병행 운영됐다. 미국 트랙은 전부 삭제됐고, 관련 문서는
> `documents/_archive_us/` 에 보존돼 있다 (현재 코드와 대조하면 안 된다).

---

## 1. 모듈 구성

시장에 종속되지 않는 것만 `app/services/` 최상위에 두고, 국내 시장에 종속된 것은
전부 `app/services/kr/` 아래로 모은다.

| 위치 | 모듈 | 역할 |
|------|------|------|
| **공용** | `scoring_service` | 매수 후보 사전 필터 + cross-sectional z-score |
| | `indicators` | SMA / EMA / RSI / MACD / ATR / ADX 수식 |
| | `position_sizing` | 확신도 가중 포지션 배분 |
| | `kis_auth_service` | KIS 토큰 발급·캐싱 (메모리 + Supabase, 1분 스로틀) |
| | `slack_service` | Slack Webhook 저수준 전송 |
| | `ml_trigger_service` | Kaggle 커널 push + 완료 폴링 |
| | `buy_switch_service` | 신규 매수 원격 on/off (영속 스위치) |
| **국내 전용** | `kr/` 패키지 전체 | 데이터 수집, KIS 국내 주문, 뉴스 감성, 점수, LLM 프롬프트, 리포트 |
| | `app/utils/kr_scheduler.py` | 2단계 파이프라인 + 매도 감시 + 주문 정합성 |
| | `kr_*` 테이블 | `sql/kr/setup_kr.sql` |

**스케줄러 격리**: `schedule` 전역 큐를 쓰지 않고 전용 `schedule.Scheduler()` 인스턴스를
쓴다. 다른 워커와 같은 큐를 공유하면 두 스레드가 `run_pending()` 을 동시에 돌려 잡이
중복 실행되고 이중 주문이 날 수 있다.

`KR_ENABLED=false` 면 API 서버만 뜨고 스케줄러는 기동하지 않는다 — 점검이나 수동 백필처럼
자동매매가 돌면 곤란한 상황에서 쓰는 킬 스위치다.

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
  **점수에는 넣지 않고** LLM 참고 정보로만 넘긴다.
  한국 시장에서 개인 관심 급증은 고점 신호로 작동하는 경우가 많아 부호가 불안정하기 때문이다.
- 블로그 언급량 — 보조 신호.

**Naver API Hub에 주가·재무지표 API는 없다.** 네이버 금융은 웹페이지일 뿐 공식 API가 아니며
크롤링은 ToS 위반 소지가 있다.

### 2-2. 데이터 소스 선택

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

한국 증시는 **15:30 KST에 닫히므로 장 마감 후 분석 시점에는 주문을 낼 수 없다.**
그래서 분석을 장 마감 뒤에 돌려 큐에 쌓아두고, 집행은 다음 거래일 개장 직후로 미룬다.

```
평일 09:00 ------------------ 15:20 -- 15:30      16:30 ------------------------- (다음 개장일) 09:15
   |                            |        |          |                                       |
   |  <--- 매도 감시 1분 주기 --->|      장마감     Phase A                                Phase B
   |                            |                  (분석)                               (매수 집행)
   +- Phase B 매수 주문                        1 시장데이터                     kr_buy_queue 읽기
   +- LLM 매도판정(4단계) 집행                  2 Kaggle ML                      -> 현재가 재조회
                                              3 기술지표+뉴스감성                -> 수량 재계산
                                              4 보유종목 LLM 매도검토 -> 로그    -> 지정가 매수
                                              5 LLM 매수검토 -> 큐 저장
                                              6 분석 리포트 PDF -> Slack
```

- **동시호가(15:20~15:30)** 는 지정가 주문의 체결 성격이 달라 매도 감시 창에서 제외한다.
- **정합성 확인**(체결/미체결 정리)은 장 시간 밖에서도 1분마다 돈다. 15:40 이후 미체결 매수는
  `buy_failed`로, 미체결 매도는 `holding`으로 복원한다(당일 유효 주문).
- 큐에 남은 예약은 **2일 초과 시 만료**된다. 연휴로 밀린 낡은 판단으로 매수하지 않기 위해서다.
- 휴장일은 `chk-holiday`(CTCA0903R) 결과를 하루 1회 캐시해 판단하고, 조회 실패 시 주말 여부로 폴백한다.

---

## 5. 점수 체계 (`kr_scoring.py`)

공용 모듈(`scoring_service`)의 cross-sectional z-score 위에 **수급 팩터**를 얹었다.

| 팩터 | 가중치 | 근거 |
|------|--------|------|
| 기술 모멘텀 (MACD diff + SMA 괴리 + RSI 평균) | 0.27 | 가장 검증된 신호 |
| ML 예측 상승률 | 0.18 | 보수적 |
| **수급 (외국인+기관 5일 순매수)** | **0.15** | **한국 시장에서 가장 검증된 단기 신호** |
| 거래량 비율 | 0.15 | 가격-거래량 컨펌 |
| ADX 추세 강도 | 0.15 | trend persistence |
| 뉴스 감성 | 0.10 | 학술 IC 약함 |

**사전 필터**(`scoring_service.apply_prefilters`): RSI > 80 하드블록 + 기술 신호 2개 이상.

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

오버라이드는 매수 하드블록과 **매도 조건 3(공포장 강제청산)을 함께** 해제한다 (6-1 참조).
`relax_threshold` 와 무관하게 오버라이드가 살아 있기만 하면 조건 3 은 보류된다.

**기본 동작은 하드블록만 해제**한다. 변동성 기반 적응형 임계값은 그대로 유지되므로
폭락장에서는 여전히 상위 종목만 통과한다. `relax_threshold=true` 를 주면 임계값까지
평온장 기준(0.40)으로 낮추는데, 이건 파이프라인 전 구간을 관통시켜 확인할 때 쓰는
테스트 옵션이며 실매매에서는 권장하지 않는다.

---

## 6. 매도 — 기계적 규칙 + LLM 종합판단 두 층

매도는 **성격이 다른 두 층**으로 나뉜다. 자금 보호와 관련된 것은 LLM 없이 항상 기계적으로 돌고,
"지금 팔 만한 상황인가"라는 정성적 판단만 LLM에게 맡긴다.

| 층 | 실행 시점 | LLM 장애 시 |
|----|-----------|-------------|
| **기계적 규칙** (`kr_recommendation_service.get_mechanical_sell_candidates`) | 1분 주기, 09:00~15:20 | 영향 없음 — LLM 을 아예 안 씀 |
| **LLM 종합판단** (`kr_llm_sell_review_service`) | 매일 16:30(Phase A 4단계), 판정만 저장 → 다음 매도 감시 사이클에 집행 | Fail-Close = **추가 매도 안 함**(HOLD). 기계적 규칙이 이미 하방을 지키고 있어 안전하다 |

### 6-1. 기계적 규칙 (조건 1~3, 항상 실행)

> **매도는 언제나 전량이다.** 어떤 조건으로 팔든 보유수량 전부를 판다. 부분매도, 트레일링
> 잔량 보유, LLM 의 일부매도 판정은 모두 없다. 판단은 "팔 것인가 말 것인가" 하나뿐이다.

**조건 1 — ATR 전량 익절 / 전량 손절**

```
매수 시점 ATR 을 그대로 고정해서 쓴다 (장중 재계산하지 않는다)
  손절선 = buy_price − 1.5×ATR  도달 시 → 전량 손절
  익절선 = buy_price + 2.5×ATR  도달 시 → 전량 익절
```

손절선을 먼저 검사하고, 걸리지 않으면 익절선을 본다. 둘 중 하나라도 걸리면 그 종목은
보유수량 전부가 그 사이클에 매도 주문으로 나간다.

**트레이드오프** — 익절선에서 전량을 털기 때문에, 그 뒤로 추세가 더 이어져도 그 상승은 못
가져간다. 대신 판단과 상태가 단순해진다: 포지션은 "보유 중" 아니면 "청산"이고, 잔량·트레일링
고점·누적 실현손익 같은 중간 상태를 추적할 필요가 없다.

ATR/거래기록이 없는 보유분(예: 수동 매수, 이관된 옛 데이터)은 고정비율(+6% / −7%) 폴백을
쓴다 — 이 역시 전량 익절/손절이다.

**조건 2 — 기술적 매도 신호** (기존과 동일, 변경 없음)

데드크로스 / RSI>70 / MACD 매도 / 패닉셀(거래량 2배 + 당일 −3%) / **외국인·기관 5일 순매도**(국내 전용).
- ADX > 25면 필요 신호 수 1개 차감
- 감성 < −0.15이면 2개(ADX 보정 시 1개), 아니면 3개(보정 시 2개)

**조건 3 — 공포장** (문턱 상향)

변동성 > 60 + 신호 1개(극단), 변동성 > 40 + 신호 2개(완화). *(기존 40/30 이었으나 상향 —
LLM 매도검토가 조건 2/3 과 동일한 원본 신호를 근거로 더 정교하게 판단하는데, 문턱이 낮으면
"불안한 정도"에도 신호 1개로 전량매도가 나가버려 LLM 의 더 정밀한 판단이
실행 기회조차 못 얻는 문제가 있었다. 진짜 극단적 국면에서만 기계적으로 개입하도록 좁혔다.)*

**손실 게이트 — 조건 3 은 손실 중인 포지션에만 발동한다** (`KR_FEAR_SELL_LOSS_PCT`, 기본 3.0
→ 매입가 대비 -3.00% 이하). 조건 3 의 취지는 패닉 국면에서 위험을 줄이는 것이지, 본전이거나
수익 중인 포지션을 국면만 보고 털어내는 게 아니다. 이 게이트가 없던 시절 HMM 이 22,650원에
매수돼 같은 날 22,650원에(손익 0원, 손절가 21,300원 근처에도 가지 않은 채) 청산됐다.
게이트는 오버라이드와 무관하게 **상시** 적용되므로, 오버라이드 만료 순간 그 창 안에서 매수한
포지션이 한꺼번에 조건 3 에 노출되는 절벽도 함께 사라진다.

**변동성 게이트가 수동 해제된 동안에는 조건 3 을 잠재운다.** 공포장 강제청산과 매수 하드블록은
같은 축의 규칙("공포 국면이니 기계적으로 개입한다")이라, 매수만 열고 매도를 그대로 두면 방금
매수한 포지션이 신호 1개에 즉시 전량청산돼 왕복매매가 난다 (2026-08-24 HMM 이 매수 1분 만에
손절가 근처에도 가지 않은 채 청산됐다). 억제되는 것은 조건 3 뿐이고 조건 1(ATR 전량 익절/손절)과
조건 2(기술신호 개수)는 공포지수를 쓰지 않으므로 그대로 작동한다 — 하방 방어가
사라지는 것이 아니다. LLM 매도검토(`get_llm_sell_context`)에는 실제 변동성이 그대로 전달된다.
국면을 알고 판단하는 것과 판단 없이 청산되는 것은 다르기 때문이다.

매도 알림에는 **T+2 결제**(매도 대금은 2영업일 후 인출 가능)를 함께 표기한다.

### 6-2. LLM 종합판단 (`kr_llm_sell_review_service`, 신규)

기계적 규칙 3가지는 "가격이 얼마나 움직였는가"만 본다. 아래 세 가지는 **맥락 판단이 필요해서
고정 공식으로는 못 잡는 신호**라 LLM 에게 맡긴다.

1. **점수감쇠** — 보유 종목을 매일 `get_scored_universe()`(임계값 컷 없는 전체 100종목 채점)로
   재점수화해서, 오늘 매수 후보였다면 몇 위였을지를 본다. 순위가 계속 밀리고 있으면 처음 산 이유가
   약해지고 있다는 신호다.
2. **개별 팩터반전 상세** — 조건 2/3 계산에 쓰는 것과 **동일한** 신호(데드크로스/RSI/MACD/수급/감성)를
   판정 근거로 다시 보여준다. 다만 "몇 개면 자동매도"라는 공식 없이, LLM 이 왜 그 신호가 떴는지
   맥락(실적 이슈인지 노이즈인지)까지 고려해 판단한다.
3. **교체매매** — 보유 슬롯(`KR_MAX_POSITIONS`)이 가득 찬 상태에서, 대기 중인 신규 후보 점수가
   보유 종목보다 `KR_ROTATION_MIN_SCORE_GAP`(0.30) 이상 높으면 "이 종목을 팔고 슬롯을 넘길
   가치가 있는지" 판단 재료로 제시한다.

LLM 은 종목별로 `HOLD` / `SELL_ALL` 둘 중 하나를 결정하고 `kr_llm_sell_decision_logs` 에
저장한다. 16:30 시점엔 장이 닫혀 있어 주문을 못 내므로, 실제 매도 주문은 **다음 매도 감시
사이클**(다음 개장일 09:00~15:20)에 집행된다. 그 사이 기계적 규칙이 먼저 그 종목을 이미 팔았다면
LLM 판정은 `skipped` 처리되고 중복 매도되지 않는다.

### 6-3. 판단 품질 보강

- **점수/순위 추이 제공** (`get_score_trend`) — `kr_llm_sell_decision_logs` 는 매일 판단마다
  종목별 점수/순위를 쌓아둔다. 이 이력 최근 `KR_SCORE_TREND_DAYS`(기본 5)일치를 프롬프트에
  그대로 보여줘서, "하루짜리 노이즈인지 추세적 악화인지 구분하라"는 지시를 LLM 이 실제로
  수행할 수 있는 데이터를 준다. 새 수집 없이 기존 로그 재활용이라 비용이 없다.
- **중간값이 없다는 점을 명시** — 프롬프트에서 "팔기로 하면 전량"임을 못박고, "조금 줄이고
  싶다"는 애매한 상태는 SELL_ALL 이 아니라 HOLD 라고 안내한다. 선택지가 둘뿐일 때 애매한
  판단이 전량청산으로 흘러가는 것을 막기 위한 장치다.
- **집행 시점 재검증** (`price_at_decision`/`signal_count_at_decision` + `KR_SELL_REVALIDATE_PCT`,
  기본 3%) — 판단은 16:30 스냅샷 기준인데 집행은 최대 거의 하루 뒤다. 그 사이 (1) 가격이 판단
  시점보다 `KR_SELL_REVALIDATE_PCT` 이상 유리한 방향(상승)으로 이미 움직였거나, (2) 판단 근거였던
  기술신호 개수가 그새 줄었다면(근거 개선, `get_current_signal_count`로 가볍게 재확인), 판단이
  낡았다고 보고 이번 사이클 집행을 보류한다(취소 아님 — 매 사이클 다시 검사하고, 마감까지 계속
  그렇다면 다음날 새 판단으로 자연 대체된다).
- **손절/익절 거리 제공** (`stop_distance_pct`/`take_profit_distance_pct`) — 손절선·익절선까지
  남은 폭(%)을 보여줘서, LLM 이 "지금 내 판단이 실질적으로 얼마나 중요한지"(여유가
  적으면 곧 기계적으로 정리될 테니 판단 부담이 낮고, 여유가 크면 판단이 더 중요함)를 가늠하게 한다.
- **점수 추이의 보유기간 스코핑** — `get_score_trend()`는 매수일(`buy_date`) 이후 기록만 본다.
  같은 종목을 예전에 샀다 판 이력이 지금 보유분의 추세인 것처럼 섞이는 것을 막는다.

### 6-4. 장중 추가 매도검토 (조건부 주기체크)

조건 2/3 문턱을 올리면서(6-1) 넓어진 "회색지대"(공포 40~60%, 신호 1~2개)는 하루 1번(16:30) 판단
만으로는 낮 동안 사각지대가 된다. 그런데 `kospi_vol_20d`(공포지수)는 Phase A(16:30)에만 갱신되는
값이라 **장중에 "막 문턱을 넘어선 순간"은 관측할 수 없다** — 그래서 이벤트 트리거가 아니라
**"오늘 이미 넘어선 상태가 지속되는 동안 주기적으로 재점검"**하는 방식을 쓴다.

- 매도 감시 루프(1분 주기)마다 `_maybe_run_intraday_sell_review()` 가 공포지수를 확인한다.
- `kospi_vol_20d > KR_INTRADAY_FEAR_REVIEW_THRESHOLD`(기본 40 — 6-1의 완화등급과 일치)이고,
  마지막 매도검토(정기든 이 재점검이든) 이후 `KR_INTRADAY_REVIEW_INTERVAL_HOURS`(기본 2)시간이
  지났으면 `_build_sell_review(is_intraday=True)` 를 한 번 더 실행한다.
- **재료는 정기검토와 동일하게 재사용**하되(새로 만들지 않는다), 프롬프트에 "점수/순위/기술신호/
  감성은 전날 마감 스냅샷 그대로이며 장중 갱신되지 않는다 — 실질적으로 바뀐 건 현재가와 손절/
  익절선까지의 거리뿐이니 그것만 보고 이전 판단을 뒤집을지 판단하라"는 안내를 추가한다. 안 바뀐
  데이터를 새 정보인 것처럼 주면 LLM 이 근거를 새로 지어낼 위험이 있어, "뭐가 진짜 새 정보인지"를
  정직하게 알려주는 쪽을 택했다.
- `decision_date` 는 정기검토와 동일하게 **오늘 날짜**로 저장되므로, 정기검토(다음날에야 집행)와
  달리 **같은 날 남은 매도 감시 사이클에서 바로 집행 대상**이 된다 — 실행 엔진(`_execute_llm_sell_decisions`)
  쪽은 코드 변경이 전혀 필요 없다.
- 쿨다운 상태(`_last_intraday_review_at`)는 스케줄러 인스턴스 메모리에만 둔다 — 서버 재시작으로
  리셋돼도 최악의 경우 한 번 더 도는 것뿐이라 영속화하지 않는다.
- `KR_INTRADAY_FEAR_REVIEW_THRESHOLD` 를 0 이하로 두면 기능을 끈다.

---

## 6-5. 분석 리포트 (Phase A 6단계)

Slack 텍스트 알림은 "무엇을 샀다/판다"는 결과만 전한다. 운용자가 다음 영업일 아침에
**판단 근거까지 한 장으로** 훑을 수 있게, Phase A 마지막에 리포트 PDF 를 만들어 채널에 올린다.

```
Step 1~5 결과 (스케줄러 _artifacts 에 누적)
   ├─ 시장환경 / 채점 통과 후보 전체 / LLM 매수판정(승인·보류 + 사유)
   └─ 보유종목 LLM 매도판정
        ↓
kr_report_service.build_buy_quote()      매수 견적서 (Phase B 와 동일한 배분 로직)
        ↓
kr_report_service.generate_narrative()   Claude — structured outputs 로 서술 JSON 강제
        ↓
kr_pdf_service.build_report_pdf()        A4 PDF (표지·시장진단·견적서·종목논거·매도검토·리스크·부록)
        ↓
slack_file_service.upload_file()         Bot Token 3단계 업로드 + 요약 코멘트
```

**설계 원칙 — 리포트는 매매에 영향을 주지 않는다.**

| 실패 지점 | 동작 |
|-----------|------|
| LLM 서술 생성 실패 | 기계적 데이터만으로 PDF 생성 (Fail-Open). 표지에 실패 사유를 명시한다 |
| PDF 생성 실패 | Webhook 으로 실패만 알리고 파이프라인은 성공으로 종료 |
| Slack Bot Token 미설정 | PDF 는 서버에 저장하고 Webhook 으로 요약 + 저장 경로만 통지 |
| Step 6 전체 예외 | 로그만 남기고 파이프라인 결과(`success: True`)는 유지 |

매수·매도 결정은 Step 5 에서 이미 끝났고 리포트는 사람이 읽는 산출물이라, Fail-Close 를
적용할 이유가 없다.

**왜 원자료를 스케줄러에 들고 다니는가** — Step 6 에서 DB 를 다시 읽으면 같은 값을 두 번
계산하게 되고, LLM 판정 사유·시장 코멘트처럼 **응답에만 존재하고 어디에도 그대로 남지 않는
정보**는 애초에 복원할 수 없다. 그래서 각 단계가 `KrScheduler._artifacts` 에 결과를 남긴다.

**매수 견적서의 수량은 참고치다.** Phase B(09:15)가 현재가를 재조회해 다시 계산하므로,
견적서는 같은 배분 로직(`compute_weighted_slots`)에 **분석 시점 최신 체결가**를 넣은 값이다.
집행 시각까지 가격이 움직이면 수량은 달라진다.

**Slack 파일 업로드** — Incoming Webhook 은 텍스트 전용이라 파일을 붙일 수 없다.
Bot Token(`files:write`)으로 `files.getUploadURLExternal` → 업로드 → `files.completeUploadExternal`
3단계를 거친다(구 `files.upload` 는 2025-03 폐지). `SLACK_REPORT_CHANNEL` 은 채널 ID(`C…`) 권장이며,
`#채널명` 으로 주면 `conversations.list` 로 1회 해석 후 캐시한다(`channels:read` 필요).

**한글 폰트** — Windows 맑은고딕 / Linux 나눔고딕·Noto CJK 를 순서대로 찾고, 서버에 폰트 파일이
없으면 ReportLab 내장 CID 폰트(HYSMyeongJo-Medium)로 폴백한다. 배포 환경에 폰트를 깔지 않아도
글자가 깨지지 않는다.

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
  kr_recommendation_service.py 기술지표 생성 / 매수 후보 / 점수 유니버스 / 기계적 매도 후보 / LLM 매도검토 컨텍스트
  kr_llm_review_service.py     LLM 매수 최종 검토 (한국 시장 프롬프트, Fail-Close = 매수 차단)
  kr_llm_sell_review_service.py LLM 보유종목 매도검토 (점수감쇠/팩터반전/교체매매, Fail-Close = HOLD)
  kr_notification_service.py   Slack 텍스트 알림 (원화 포맷)
  kr_report_service.py         분석 리포트: 매수 견적서 산출 + LLM 서술 생성 + 전송 오케스트레이션
  kr_pdf_service.py            리포트 PDF 렌더러 (ReportLab, 한글 폰트 자동 탐색)
  slack_file_service.py        Slack 파일 업로드 (Bot Token 3단계 API — Webhook 은 첨부 불가)
app/services/                  ── 시장 무관 공용 모듈 ──
  scoring_service.py           사전 필터 + cross-sectional z-score
  indicators.py                SMA/EMA/RSI/MACD/ATR/ADX 수식
  position_sizing.py           확신도 가중 포지션 배분
  kis_auth_service.py          KIS 토큰 발급/캐싱 (메모리 + Supabase, 1분 스로틀)
  slack_service.py             Slack Webhook 저수준 전송
  ml_trigger_service.py        Kaggle 커널 push + 완료 폴링
  buy_switch_service.py        신규 매수 원격 on/off (영속 스위치)
app/utils/kr_scheduler.py      2단계 파이프라인 + 매도 감시(기계적+LLM 판정 집행) + 정합성
app/api/routes/kr.py           /kr/* 라우트
app/api/routes/buy_switch.py   /buy-switch/* 라우트
sql/kr/setup_kr.sql            전체 스키마 (테이블 10개 + 컬럼 마이그레이션 + RLS/권한, 멱등)
sql/setup_market_switches.sql  매수 스위치 테이블
kaggle_notebook_kr/            ML 커널 (predict_kr.py)
scripts/buy_switch.{sh,ps1}    매수 스위치 CLI 래퍼
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
| `kr_trade_records` | 매매 기록 + ATR 전량 익절/손절선 (원화). 부분매도 관련 컬럼(`partial_exit_done`, `chandelier_*`, `realized_partial_*`)은 과거 기록 보존을 위해 스키마에 남아 있으나 더 이상 새로 쓰이지 않는다 |
| `kr_llm_decision_logs` | LLM 매수검토 판단 로그 |
| `kr_llm_sell_decision_logs` | LLM 매도검토 판단 로그 (HOLD/SELL_ALL, 점수/순위/교체매매 근거 포함) |
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
5. **드라이런 검증** — `KR_DRY_RUN=true`로 두고 서버 재시작
   (`KR_ENABLED`는 기본이 `true`다. `false`로 두면 API만 뜨고 스케줄러는 멈춘다)

   ```
   GET  /kr/status                  설정/연동 상태 확인
   GET  /kr/economic/ecos-check     ECOS 통계코드 유효성 진단
   POST /kr/technical/generate      기술 지표 생성
   POST /kr/sentiment/collect       뉴스 감성 수집
   GET  /kr/candidates/buy          매수 후보 확인
   GET  /kr/candidates/sell         기계적 매도 후보 확인 (전량 익절/손절/기술신호/공포장)
   GET  /kr/candidates/scored-universe  점수 유니버스 확인 (임계값 컷 없음, 점수감쇠 디버깅용)
   GET  /kr/holdings/sell-review    LLM 매도검토 컨텍스트 확인 (LLM 호출 없이 입력만)
   POST /kr/pipeline/analysis       전체 분석 파이프라인 (Kaggle 포함, 매도검토 4단계 포함)
   POST /kr/pipeline/execute-sell-review  보유종목 LLM 매도검토만 즉시 실행
   POST /kr/pipeline/execute-buy    매수 집행 (드라이런이면 로그만)
   POST /kr/pipeline/execute-sell   매도 감시 즉시 실행 (기계적 규칙 + LLM 판정 집행)
   GET  /kr/report/config           리포트 설정 진단 (LLM 키 / Slack 업로드 준비 여부)
   POST /kr/report/send             직전 파이프라인 결과로 리포트 재생성 + 전송
   GET  /buy-switch                 신규 매수 허용 여부 (외출 시 원격 차단용)
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
