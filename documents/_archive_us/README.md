# 미국 트랙 아카이브 (더 이상 동작하지 않는 문서)

이 폴더의 문서들은 **미국 주식 트랙**을 설명한다. 시스템이 국내주식(KOSPI 100) 전용으로
전환되면서 해당 코드가 전부 삭제됐으므로, 여기 적힌 파일 경로·API·테이블은 **현재 저장소에
존재하지 않는다.** 설계 의도와 과거 트러블슈팅 기록을 보존하려고 남겨둘 뿐이며,
현재 시스템의 근거로 삼으면 안 된다.

## 삭제된 것들 (문서 안에서 참조되는 이름)

| 문서 속 이름 | 현재 상태 |
|---|---|
| `app/utils/scheduler.py` | 삭제 (국내 스케줄러는 `app/utils/kr_scheduler.py`) |
| `app/services/stock_recommendation_service.py` | 삭제 (지표 수식만 `app/services/indicators.py` 로 이관) |
| `app/services/balance_service.py` | 삭제 (토큰 관리만 `app/services/kis_auth_service.py` 로 이관) |
| `app/services/notification_service.py` | 삭제 (`_send` 만 `app/services/slack_service.py` 로 이관) |
| `app/services/economic_service.py`, `llm_review_service.py`, `volume_service.py`, `snapshot_service.py` | 삭제 |
| `stock.py`, `predict_colab.py`, `kaggle_notebook/predict.py` | 삭제 |
| `/balance`, `/economic`, `/stocks`, `/volume`, `/llm`, `/pipeline` 라우트 | 삭제 (`/kr/*`, `/buy-switch` 만 남음) |
| `scoring_service` 의 v1/v2 점수·VIX 임계값 | 삭제 (국내 점수는 `app/services/kr/kr_scoring.py`) |
| `economic_and_stock_data`, `stock_recommendations`, `ticker_sentiment_analysis` 등 US 테이블 | Supabase 에는 남아 있으나 코드가 더 이상 읽고 쓰지 않는다 |

## 여전히 유효한 문서

아카이브되지 않고 `documents/` 에 남은 것들은 지금도 동작하는 내용을 담고 있다.

- `08_Kaggle_API_연동.md` — Kaggle push/폴링 메커니즘. 커널만 국내용으로 바뀌었다.
- `09_Slack_연동.md` — Webhook 연동 방식. 메시지 포맷은 국내용으로 다시 쓰였다.
- `10_멀티팩터_변별력_개선_기획.md` — cross-sectional z-score 설계 근거. 국내 점수 체계의 토대다.
- `16_클라우드_배포_및_보안_가이드.md` — 배포/운영.
- `20_국내주식_KOSPI30_설계.md` — **현재 시스템의 설계 문서.**
