"""국내주식(KOSPI 100) 자동매매 트랙.

기존 미국 트랙(app/services/*.py)과 병행 운영되며, 시장 무관 모듈
(scoring_service, notification_service._send, balance_service.get_access_token)은
공유하고 데이터 수집 / 주문 / 스케줄만 국내용으로 새로 갖는다.

참조: documents/20_국내주식_KOSPI30_설계.md
"""
