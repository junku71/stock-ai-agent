"""국내주식(KOSPI 100) 자동매매 트랙.

시장 무관 공용 모듈은 app/services/ 최상위에 둔다:
  scoring_service   — 사전 필터 + cross-sectional z-score
  indicators        — SMA/EMA/RSI/MACD/ATR/ADX
  position_sizing   — 확신도 가중 배분
  slack_service     — Slack Webhook 저수준 전송
  kis_auth_service  — KIS 토큰 발급/캐싱
이 패키지에는 데이터 수집 / 주문 / 분석처럼 국내 시장에 종속된 것만 둔다.

참조: documents/20_국내주식_KOSPI30_설계.md
"""
