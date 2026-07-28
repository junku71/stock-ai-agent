# Slack 연동 가이드

> 자동매매 시스템의 핵심 이벤트 4가지를 Slack 채널로 알리는 통합 가이드.
> Incoming Webhook + `notification_service.py` 한 파일 + 통합 포인트 4곳.

---

## 1. 한 문장 요약

> **"데이터 수집 끝났을 때 / 오늘 매수·홀드 결정 종목 / 매수 체결 / 매도 체결 — 이 4가지를 Slack 으로 받는다. 매수/매도 알림에는 현재 보유 종목 + 수익률 현황표가 같이 붙어서 한눈에 본다."**

---

## 2. 알림 4가지 — 무엇을 받을지

| # | 트리거 | 발송 시점 | 발송 빈도 |
|---|---|---|---|
| ① | **데이터 수집 완료** | 일일 파이프라인 Step 1~3 완료 직후 | 하루 1번 |
| ② | **오늘 매수/홀드 결정 종목** | LLM 검토 끝난 직후 | 하루 1번 |
| ③ | **매수 체결** | KIS 매수 주문 성공 시 | 종목당 1번 |
| ④ | **매도 체결** | KIS 매도 주문 성공 시 | 종목당 1번 |

**③④ 매수/매도 알림에는 현재 보유 종목 + 수익률 현황표를 자동 첨부.**

```
KST 21:00  Step 1 경제데이터
KST 21:01  Step 2 Kaggle ML 시작 ──── 7분 학습
KST 21:08  Step 3 기술지표+감성
KST 21:11  ─── ① 데이터 수집 완료 알림 발송 ✅
KST 21:11  Step 4 LLM 검토
KST 21:11  ─── ② 오늘 매수/홀드 결정 종목 알림 발송 📋
KST 21:11~ KIS 매수 주문 (종목별)
           ─── ③ 매수 체결 알림 + 현황표 🛒📊
KST 22:30~ 매도 모니터링 (1분마다)
           ─── ④ 매도 체결 알림 + 현황표 💰📊 (체결 시만)
```

---

## 3. Slack Incoming Webhook 발급

### 3-1. 채널 + Webhook 만들기

1. https://slack.com 워크스페이스 + 채널 (예: `#trading-bot`)
2. https://api.slack.com/apps → **Create New App**
   - 구버전 화면: **From scratch** 선택
   - 신버전 화면(템플릿 선택창이 뜨는 경우): **Blank app** 선택 ("From scratch"와 동일한 역할)
     - AI agent / Starter app / From a manifest 은 선택하지 않습니다
3. 앱 이름: `banbu-trading-bot` → 워크스페이스 선택 → **Create**
4. 좌측 **Incoming Webhooks** → 토글 **ON**
5. **Add New Webhook to Workspace** → 채널 선택 → **Allow**
6. 생성된 URL 복사 (`https://hooks.slack.com/services/T01.../B02.../xxxx`)

### 3-2. 테스트

```bash
curl -X POST -H 'Content-type: application/json' \
  --data '{"text":"테스트"}' \
  https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

채널에 "테스트" 뜨면 OK.

---

## 4. 환경변수 설정

`.env` 추가:
```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T01.../B02.../xxxx
```

`app/core/config.py` 추가:
```python
SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")
```

> Webhook URL 비어있으면 모든 알림 호출이 자동으로 no-op → 개발 환경에서는 그냥 빈 값으로 둬도 안전.

---

## 5. notification_service.py 전체 코드

`app/services/notification_service.py` 신규 파일:

```python
"""
Slack Incoming Webhook 통합 — 4가지 핵심 알림 + 보유 현황표.

알림 종류:
  1. notify_data_ready()           — 데이터 수집 완료
  2. notify_llm_decisions()        — 오늘 매수/홀드 결정 종목
  3. notify_buy_executed()         — 매수 체결 (보유 현황표 첨부)
  4. notify_sell_executed()        — 매도 체결 (보유 현황표 첨부)

SLACK_WEBHOOK_URL 미설정 시 모든 함수가 no-op.
"""
import logging
import requests
from typing import List, Optional, Dict, Any

from app.core.config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 저수준 전송 함수
# ──────────────────────────────────────────────────────────

def _send(
    title: str,
    message: str,
    color: str = "#36a64f",
    fields: Optional[Dict[str, str]] = None,
) -> bool:
    """Slack Webhook 으로 attachment 1개 발송. 실패해도 본 로직 안 막음."""
    if not settings.SLACK_WEBHOOK_URL:
        return False

    attachment = {
        "color": color,
        "title": title,
        "text": message,
        "mrkdwn_in": ["text", "fields"],
    }
    if fields:
        attachment["fields"] = [
            {"title": k, "value": str(v), "short": True}
            for k, v in fields.items()
        ]

    try:
        resp = requests.post(
            settings.SLACK_WEBHOOK_URL,
            json={"attachments": [attachment]},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning(f"Slack 전송 실패 ({resp.status_code}): {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Slack 전송 예외: {e}")
        return False


# ──────────────────────────────────────────────────────────
# 보유 종목 + 수익률 현황표 (매수/매도 알림에 자동 첨부)
# ──────────────────────────────────────────────────────────

def format_holdings_table() -> str:
    """
    KIS 잔고에서 보유 종목 + 수익률을 모노스페이스 표 형태로 반환.
    Slack 코드블록 마크다운으로 감싸서 정렬 유지.
    """
    try:
        from app.services.balance_service import get_all_overseas_balances
        balance = get_all_overseas_balances()
    except Exception as e:
        return f"_(보유 종목 조회 실패: {e})_"

    if balance.get("rt_cd") != "0":
        return f"_(보유 종목 조회 실패: {balance.get('msg1', '')})_"

    holdings = balance.get("output1", [])
    if not holdings:
        return "_(현재 보유 종목 없음)_"

    lines = ["```"]
    lines.append(f"{'종목':<12} {'수량':>5} {'평단가':>9} {'현재가':>9} {'손익(USD)':>12} {'수익률':>8}")
    lines.append("─" * 60)

    total_pnl_usd = 0.0
    total_buy_usd = 0.0

    for h in holdings:
        ticker = h.get("ovrs_pdno", "")
        name = h.get("ovrs_item_name", "")
        # 한글 이름은 폭이 넓으므로 잘라냄
        if len(name) > 6:
            name = name[:6]
        try:
            qty = int(h.get("ovrs_cblc_qty", 0))
            buy_price = float(h.get("pchs_avg_pric", 0))
            now_price = float(h.get("now_pric2", 0))
            pnl = float(h.get("frcr_evlu_pfls_amt", 0))
            pnl_pct = float(h.get("evlu_pfls_rt", 0))
        except (ValueError, TypeError):
            continue

        total_pnl_usd += pnl
        total_buy_usd += buy_price * qty

        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"{name:<6}({ticker:<5}) {qty:>5} "
            f"${buy_price:>7.2f} ${now_price:>7.2f} "
            f"{sign}${pnl:>9.2f} {sign}{pnl_pct:>6.2f}%"
        )

    lines.append("─" * 60)
    total_sign = "+" if total_pnl_usd >= 0 else ""
    total_pct = (total_pnl_usd / total_buy_usd * 100) if total_buy_usd > 0 else 0
    lines.append(
        f"{'합계':<20} ${total_buy_usd:>8.2f} → "
        f"{total_sign}${total_pnl_usd:>9.2f} ({total_sign}{total_pct:.2f}%)"
    )
    lines.append("```")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────
# ① 데이터 수집 완료
# ──────────────────────────────────────────────────────────

def notify_data_ready(elapsed_sec: int, steps_summary: dict):
    """
    Step 1~3 (경제데이터 + Kaggle ML + 기술지표+감성) 완료 직후 호출.
    """
    _send(
        title="📥 데이터 수집 완료",
        message=f"오늘 매수 판단을 위한 모든 데이터가 갱신됐습니다. ({elapsed_sec}초)",
        color="#2eb886",
        fields={
            "Step 1 경제데이터": f"{steps_summary.get('1_economic', '?')}초",
            "Step 2 Kaggle ML": f"{steps_summary.get('2_kaggle', '?')}초",
            "Step 3 기술+감성": f"{steps_summary.get('3_tech_sent', '?')}초",
        },
    )


# ──────────────────────────────────────────────────────────
# ② 오늘 매수 / 홀드 결정 종목
# ──────────────────────────────────────────────────────────

def notify_llm_decisions(
    buy_candidates: List[dict],
    held_candidates: List[dict],
    market_analysis: str = "",
):
    """
    LLM 검토 (review_buy_candidates) 직후 호출.
    Args:
        buy_candidates: BUY 판정된 종목 리스트
        held_candidates: HOLD 판정된 종목 리스트
        market_analysis: LLM 의 시장 분석 한 줄
    """
    if not buy_candidates and not held_candidates:
        _send(
            title="📋 오늘 매수 후보 없음",
            message="기술/감성/ML 필터를 통과한 종목이 없습니다.",
            color="#888888",
        )
        return

    # 매수 종목 라인
    buy_lines = []
    for c in buy_candidates:
        buy_lines.append(
            f"• *{c.get('stock_name')}* ({c.get('ticker')}) "
            f"score={c.get('composite_score', 0):.3f} "
            f"rise={c.get('rise_probability', 0):.2f}% "
            f"— _{c.get('llm_reason', '')[:80]}_"
        )

    # 홀드 종목 라인
    hold_lines = []
    for c in held_candidates:
        hold_lines.append(
            f"• {c.get('stock_name')} ({c.get('ticker')}) "
            f"score={c.get('composite_score', 0):.3f} "
            f"— _{c.get('llm_reason', '')[:80]}_"
        )

    body_parts = []
    if market_analysis:
        body_parts.append(f"💬 *시장 분석:* {market_analysis}\n")
    if buy_lines:
        body_parts.append(f"🟢 *BUY ({len(buy_lines)}건)*\n" + "\n".join(buy_lines))
    if hold_lines:
        body_parts.append(f"🟡 *HOLD ({len(hold_lines)}건)*\n" + "\n".join(hold_lines))

    _send(
        title=f"📋 오늘 결정 — BUY {len(buy_candidates)} / HOLD {len(held_candidates)}",
        message="\n\n".join(body_parts),
        color="#3b82f6",
    )


# ──────────────────────────────────────────────────────────
# ③ 매수 체결
# ──────────────────────────────────────────────────────────

def notify_buy_executed(
    ticker: str,
    stock_name: str,
    qty: int,
    price: float,
    composite_score: float,
):
    """매수 주문 성공 직후 호출. 보유 현황표 자동 첨부."""
    holdings_table = format_holdings_table()
    _send(
        title=f"🛒 매수 체결: {stock_name} ({ticker})",
        message=(
            f"*수량:* {qty}주  *체결가:* ${price:.2f}  "
            f"*총액:* ${qty * price:,.2f}\n"
            f"*composite_score:* {composite_score:.4f}\n\n"
            f"*📊 현재 보유 종목 + 수익률*\n{holdings_table}"
        ),
        color="#36a64f",
    )


# ──────────────────────────────────────────────────────────
# ④ 매도 체결
# ──────────────────────────────────────────────────────────

def notify_sell_executed(
    ticker: str,
    stock_name: str,
    qty: int,
    price: float,
    sell_reason: str,
    profit_loss: float,
    profit_loss_pct: float,
):
    """매도 주문 성공 직후 호출. 보유 현황표 자동 첨부."""
    is_profit = profit_loss >= 0
    icon = "💰" if is_profit else "🩸"
    color = "#2eb886" if is_profit else "#ff9800"
    sign = "+" if is_profit else ""

    holdings_table = format_holdings_table()
    _send(
        title=f"{icon} 매도 체결: {stock_name} ({ticker})",
        message=(
            f"*수량:* {qty}주  *체결가:* ${price:.2f}\n"
            f"*손익:* {sign}${profit_loss:,.2f}  "
            f"({sign}{profit_loss_pct:.2f}%)  *사유:* `{sell_reason}`\n\n"
            f"*📊 매도 후 현재 보유 종목 + 수익률*\n{holdings_table}"
        ),
        color=color,
    )
```

---

## 6. 통합 포인트 4곳

### ① 데이터 수집 완료 — `app/utils/scheduler.py:_execute_daily_pipeline`

Step 3 직후, Step 4 직전:

```python
async def _execute_daily_pipeline():
    pipeline_start = time.time()
    steps_elapsed = {}

    # Step 1
    s = time.time()
    await update_economic_data_in_background(force=True)
    steps_elapsed["1_economic"] = int(time.time() - s)

    # Step 2
    s = time.time()
    success, msg, meta = trigger_and_wait()
    if not success:
        return False, "2_kaggle_ml", msg
    steps_elapsed["2_kaggle"] = int(time.time() - s)

    # Step 3
    s = time.time()
    service = StockRecommendationService()
    service.generate_technical_recommendations()
    service.fetch_and_store_sentiment_for_recommendations()
    steps_elapsed["3_tech_sent"] = int(time.time() - s)

    # ★ ① 데이터 수집 완료 알림
    from app.services.notification_service import notify_data_ready
    total = int(time.time() - pipeline_start)
    notify_data_ready(total, steps_elapsed)

    # Step 4: LLM 검토 + 매수
    await stock_scheduler._execute_auto_buy(force=True)
    return True, None, None
```

### ② 오늘 매수/홀드 종목 — `app/utils/scheduler.py:_execute_auto_buy`

`review_buy_candidates()` 호출 직후:

```python
recommendations = self.recommendation_service.get_combined_recommendations_with_technical_and_sentiment()
buy_candidates = recommendations.get("results", [])

vix_value = buy_candidates[0].get("vix_value") if buy_candidates else None
review_result = review_buy_candidates(buy_candidates, vix_value)

# ★ ② LLM 결정 알림
from app.services.notification_service import notify_llm_decisions
notify_llm_decisions(
    buy_candidates=review_result["reviewed_candidates"],
    held_candidates=review_result["held_candidates"],
    market_analysis=review_result.get("llm_reasoning", ""),
)

buy_candidates = review_result["reviewed_candidates"]
if not buy_candidates:
    return
```

### ③ 매수 체결 — `app/utils/scheduler.py:_execute_auto_buy`

KIS 주문 성공 직후 (line ~621):

```python
if order_result.get("rt_cd") == "0":
    logger.info(f"{stock_name}({ticker}) 매수 주문 성공")
    holding_tickers.add(pure_ticker)

    # ★ ③ 매수 체결 알림 (보유 현황표 자동 첨부)
    from app.services.notification_service import notify_buy_executed
    notify_buy_executed(
        ticker=pure_ticker,
        stock_name=stock_name,
        qty=quantity,
        price=current_price,
        composite_score=candidate.get("composite_score", 0),
    )

    # 기존 trade_records 저장 로직...
```

### ④ 매도 체결 — `app/utils/scheduler.py:_execute_auto_sell`

KIS 매도 주문 성공 직후 (line ~402):

```python
if order_result.get("rt_cd") == "0":
    logger.info(f"{stock_name}({ticker}) 매도 주문 성공")

    # 기존 trade_records 업데이트 로직 (profit_loss 계산)...
    purchase_price = candidate.get("purchase_price", 0)
    profit_loss = (current_price - purchase_price) * quantity if purchase_price > 0 else 0
    profit_loss_pct = ((current_price - purchase_price) / purchase_price) * 100 if purchase_price > 0 else 0

    # ★ ④ 매도 체결 알림 (보유 현황표 자동 첨부)
    from app.services.notification_service import notify_sell_executed
    notify_sell_executed(
        ticker=ticker,
        stock_name=stock_name,
        qty=quantity,
        price=current_price,
        sell_reason=sell_reason,
        profit_loss=round(profit_loss, 2),
        profit_loss_pct=round(profit_loss_pct, 2),
    )
```

> 💡 매도 알림은 _체결됐을 때만_ 발송 (1분 폴링 자체에서는 X). 노이즈 방지.

---

## 7. Slack 화면 미리보기

### ① 데이터 수집 완료
```
🟢 📥 데이터 수집 완료
오늘 매수 판단을 위한 모든 데이터가 갱신됐습니다. (672초)

Step 1 경제데이터    63초
Step 2 Kaggle ML     412초
Step 3 기술+감성     186초
```

### ② 오늘 결정 종목
```
🔵 📋 오늘 결정 — BUY 2 / HOLD 1

💬 시장 분석: VIX 18.3 — 평온장. 기술주 중심으로 매수 후보 검토 가능.

🟢 BUY (2건)
• 코스트코 (COST) score=0.617 rise=4.15% — 골든크로스 + 강한 추세 + 긍정 감성
• 어도비 (ADBE) score=0.458 rise=2.04% — RSI 정상, MACD 매수 신호

🟡 HOLD (1건)
• 넷플릭스 (NFLX) score=0.617 rise=5.32% — 다음 주 실적 발표 임박
```

### ③ 매수 체결
```
🟢 🛒 매수 체결: 코스트코 (COST)
수량: 53주  체결가: $1018.34  총액: $53,971.02
composite_score: 0.6170

📊 현재 보유 종목 + 수익률
┌─────────────────────────────────────────────────────────┐
│ 종목         수량   평단가    현재가   손익(USD)   수익률 │
├─────────────────────────────────────────────────────────┤
│ 어도비(ADBE)  178  $240.53  $245.44  +$  874.00  +2.04% │
│ 코스트코(COST) 53  $1018.34 $1011.15 -$  381.07  -0.71% │
│ 넷플릭스(NFLX) 453  $93.68   $92.44  -$  559.46  -1.32% │
├─────────────────────────────────────────────────────────┤
│ 합계   $135,420.18 →    -$66.53 (-0.05%)                │
└─────────────────────────────────────────────────────────┘
```

### ④ 매도 체결 (익절)
```
🟢 💰 매도 체결: 코스트코 (COST)
수량: 53주  체결가: $1018.34
손익: +$2,517.49 (+4.89%)  사유: take_profit

📊 매도 후 현재 보유 종목 + 수익률
┌─────────────────────────────────────────────────────────┐
│ 종목         수량   평단가    현재가   손익(USD)   수익률 │
├─────────────────────────────────────────────────────────┤
│ 어도비(ADBE)  178  $240.53  $245.44  +$  874.00  +2.04% │
│ 넷플릭스(NFLX) 453  $93.68   $92.44  -$  559.46  -1.32% │
├─────────────────────────────────────────────────────────┤
│ 합계   $84,000.00 →    +$314.54 (+0.37%)                │
└─────────────────────────────────────────────────────────┘
```

### ④' 매도 체결 (손절)
```
🟠 🩸 매도 체결: 펩시코 (PEP)
수량: 267주  체결가: $152.48
손익: -$1,291.21 (-3.07%)  사유: stop_loss

📊 매도 후 현재 보유 종목 + 수익률
... (동일 형태)
```

---

## 8. 한계 + 함정

### 8-1. Slack 코드블록 폭

폰 Slack 앱에서 코드블록이 가로로 길면 줄바꿈됨 → 표가 깨질 수 있음.
대응: 종목 이름은 6자로 자르고, 필드 폭을 좁게 잡음 (위 코드 적용됨).

### 8-2. KIS 잔고 조회 실패

매수/매도 알림 발송 시점에 KIS 토큰 만료 등으로 잔고 조회 실패할 수 있음.
→ `format_holdings_table()` 이 try/except 로 감싸져있어 알림은 보내고 표 자리에 _(조회 실패)_ 메시지.

### 8-3. Webhook URL 노출 시

`.gitignore` 의 `.env` 가 잘 작동하는지 한 번 확인. 노출됐다 싶으면 Slack 앱 설정에서 Webhook 재발급.

### 8-4. Rate Limit

Slack Webhook 은 분당 약 1회 burst. 매도 모니터링이 1분 주기지만 _체결될 때만_ 알림 발송하니 안전.

### 8-5. 외부 의존성

Slack 서버 다운 시에도 본 로직은 진행 (timeout 5초 + 예외 swallow).

---

## 9. 마이그레이션 체크리스트

### Phase 1: 셋업 (20분)
- [ ] Slack 채널 (`#trading-bot`) 만들기
- [ ] Incoming Webhook 발급 + URL 복사
- [ ] curl 로 테스트 발송 확인
- [ ] `.env` 에 `SLACK_WEBHOOK_URL` 추가
- [ ] `app/core/config.py` 에 `SLACK_WEBHOOK_URL` 등록

### Phase 2: 서비스 작성 (10분)
- [ ] `app/services/notification_service.py` 작성 (위 코드 그대로)
- [ ] FastAPI 한 번 재시작 후 import 정상 확인

### Phase 3: 통합 4곳 (30분)
- [ ] `_execute_daily_pipeline()` Step 3 직후 → `notify_data_ready()`
- [ ] `_execute_auto_buy()` LLM 검토 직후 → `notify_llm_decisions()`
- [ ] `_execute_auto_buy()` KIS 주문 성공 직후 → `notify_buy_executed()`
- [ ] `_execute_auto_sell()` KIS 주문 성공 직후 → `notify_sell_executed()`

### Phase 4: 검증 (10분)
- [ ] `POST /pipeline/run-full-daily` 수동 트리거
- [ ] 채널에 4가지 알림 모두 도착 확인
- [ ] 보유 현황표 폰 화면에서 깔끔히 보이는지 확인

---

## 10. 코드 위치 인덱스

| 파일 | 변경 내용 |
|---|---|
| `.env` | `SLACK_WEBHOOK_URL` |
| `app/core/config.py` | `SLACK_WEBHOOK_URL: str` |
| `app/services/notification_service.py` | 🆕 신규 (4개 알림 함수 + 보유 현황표 헬퍼) |
| `app/utils/scheduler.py:_execute_daily_pipeline` | `notify_data_ready()` 호출 1줄 |
| `app/utils/scheduler.py:_execute_auto_buy` | `notify_llm_decisions()` + `notify_buy_executed()` 호출 |
| `app/utils/scheduler.py:_execute_auto_sell` | `notify_sell_executed()` 호출 |

---

## 11. 관련 문서

- `07_자동화_방안.md` — 전체 자동화 구조
- `08_Kaggle_API_연동.md` — Kaggle 파이프라인 (Step 2)
