"""
국내주식(KOSPI 100) 자동매매 스케줄러.

분석과 집행을 시간적으로 분리한 2단계 구조다. 한국 증시는 15:30 KST 에 닫히므로
장 마감 후 분석 시점에는 주문을 낼 수 없다. 그래서 분석은 장 마감 뒤에 돌려
큐에 쌓아두고, 집행은 다음 거래일 개장 직후로 미룬다.

  Phase A — 분석 (평일 KR_ANALYSIS_TIME, 기본 16:30 KST)
     1) 시장 데이터 수집 (지수/환율/글로벌/30종목 종가/ECOS)
     2) Kaggle ML 예측 (국내 전용 커널)
     3) 기술적 지표 + 네이버 뉴스 감성 + 수급
     4) 보유 종목 LLM 매도검토 → kr_llm_sell_decision_logs 에 HOLD/SELL_ALL 저장 + Slack 보고
        (손절/부분익절/샹들리에/기술신호개수/공포장 기계적 규칙과 무관하게 별도로 판단만 적재,
         실제 매도 주문은 다음 매도 감시 사이클에 집행된다)
     5) LLM 매수 최종 검토 → kr_buy_queue 에 '다음 개장일 매수 예약' 저장 + Slack 보고
     6) 분석 리포트: 1~5 결과 + 매수 견적서를 LLM 이 리포트로 정리 → PDF → Slack 채널 업로드
        (사람이 읽는 산출물이라 실패해도 파이프라인을 실패로 처리하지 않는다)

  Phase B — 집행 (평일 KR_EXECUTION_TIME, 기본 09:05 KST)
     큐를 읽어 현재가 재조회 → 수량 재계산 → 지정가 매수 주문

  매도 감시 — 1분마다, 09:00~15:20 KST
     기계적 규칙(ATR 전량 익절/손절 / 기술적 매도 신호 / 공포장 조건, 항상 LLM
     무관하게 실행) ∪ 전날 16:30 LLM 매도검토에서 나온 미집행 SELL_ALL 판정을 집행한다.
     매도는 언제나 **전량**이다 — 익절이면 전량 익절, 손절이면 전량 손절.
     동시에 KIS 원장과 kr_trade_records 정합성을 맞춘다 (체결 확인 / 미체결 정리)

전역 schedule 큐를 다른 워커와 공유하면 두 스레드가 run_pending() 을 동시에 돌려
같은 잡이 중복 실행될 수 있다. 그래서 KR 전용 Scheduler 인스턴스를 따로 만든다.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz
import schedule

from app.core.config import settings
from app.db.supabase import supabase
from app.services import buy_switch_service, ml_trigger_service
from app.services.position_sizing import compute_weighted_slots, describe_allocation
from app.services.kr import (
    kis_domestic_service as kis,
    kr_llm_review_service,
    kr_llm_sell_review_service,
    kr_market_data_service,
    kr_notification_service as notify,
    kr_recommendation_service as recommend,
    kr_report_service,
    kr_sentiment_service,
    universe,
)

logger = logging.getLogger("kr_scheduler")

KST = pytz.timezone("Asia/Seoul")

TABLE_QUEUE = "kr_buy_queue"
TABLE_TRADES = "kr_trade_records"
TABLE_SELL_LOGS = "kr_llm_sell_decision_logs"

# 큐에 남은 예약은 2일이 지나면 만료시킨다 (연휴 등으로 집행이 밀린 경우 낡은 판단으로 사지 않도록)
QUEUE_MAX_AGE_DAYS = 2


class KrScheduler:
    """국내주식 자동매매 스케줄러 (전용 schedule 인스턴스 사용)."""

    def __init__(self):
        self._sched = schedule.Scheduler()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # 잡별 재진입 방지 — 매도 감시가 1분보다 오래 걸려도 겹쳐 돌지 않게
        self._locks = {
            "sell": threading.Lock(),
            "analysis": threading.Lock(),
            "execution": threading.Lock(),
        }
        # Phase A 각 단계가 남기는 원자료 — 마지막 Step(분석 리포트)에서 재조회 없이 쓴다.
        # DB 를 다시 읽으면 같은 값을 두 번 계산하게 되고, LLM 판정처럼 응답에만 존재하는
        # 정보(승인 사유·시장 코멘트)는 애초에 복원할 수 없다.
        self._artifacts: dict = {}

    # ── 스레드 관리 ────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._sched.run_pending()
            except Exception as e:
                logger.error(f"스케줄 루프 오류: {e}", exc_info=True)
            time.sleep(1)

    def start(self) -> bool:
        if self._running:
            logger.warning("국내 스케줄러가 이미 실행 중입니다.")
            return False

        self._sched.clear()
        self._sched.every(1).minutes.do(self._job_sell)
        self._sched.every().day.at(settings.KR_ANALYSIS_TIME).do(self._job_analysis)
        self._sched.every().day.at(settings.KR_EXECUTION_TIME).do(self._job_execution)

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="kr-scheduler")
        self._thread.start()

        mode = (
            "드라이런" if settings.KR_DRY_RUN
            else ("모의투자" if settings.KIS_USE_MOCK else "실전투자")
        )
        logger.info(
            f"국내 스케줄러 시작 [{mode}] — "
            f"분석 매일 {settings.KR_ANALYSIS_TIME} KST, "
            f"매수 집행 매일 {settings.KR_EXECUTION_TIME} KST, "
            f"매도 감시 1분 주기(09:00~15:20 KST)"
        )
        return True

    def stop(self) -> bool:
        if not self._running:
            logger.warning("국내 스케줄러가 실행 중이 아닙니다.")
            return False
        self._running = False
        self._sched.clear()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("국내 스케줄러가 중지되었습니다.")
        return True

    def status(self) -> dict:
        """
        스케줄러 상태.

        jobs 에 last_run/next_run 을 함께 내보낸다. 휴장일에는 매도 감시가 아무 로그도
        남기지 않고 조용히 스킵하기 때문에, 로그만으로는 스레드가 살아있는지 알 수 없다.
        last_run 이 갱신되는지 보면 실제 동작 여부를 밖에서 확인할 수 있다.
        """
        jobs = []
        for j in self._sched.jobs:
            fn = getattr(j.job_func, "__name__", str(j.job_func))
            jobs.append({
                "job": fn,
                "interval": f"{j.interval} {j.unit}",
                "at": j.at_time.strftime("%H:%M") if getattr(j, "at_time", None) else None,
                "last_run": j.last_run.strftime("%Y-%m-%d %H:%M:%S") if j.last_run else None,
                "next_run": j.next_run.strftime("%Y-%m-%d %H:%M:%S") if j.next_run else None,
            })
        return {
            "running": self._running,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "dry_run": settings.KR_DRY_RUN,
            "account_mode": kis.current_account_type(),
            "analysis_time_kst": settings.KR_ANALYSIS_TIME,
            "execution_time_kst": settings.KR_EXECUTION_TIME,
            "server_time_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "jobs": jobs,
        }

    # ── 잡 진입점 (재진입 방지 래퍼) ──────────────────────────

    def _guarded(self, key: str, fn):
        lock = self._locks[key]
        if not lock.acquire(blocking=False):
            logger.warning(f"이전 '{key}' 작업이 아직 실행 중 — 이번 회차 스킵")
            return False
        try:
            fn()
            return True
        except Exception as e:
            logger.error(f"'{key}' 작업 중 오류: {e}", exc_info=True)
            return False
        finally:
            lock.release()

    def _job_sell(self):
        return self._guarded("sell", lambda: asyncio.run(self.execute_auto_sell()))

    def _job_analysis(self):
        return self._guarded("analysis", lambda: asyncio.run(self.execute_analysis_pipeline()))

    def _job_execution(self):
        return self._guarded("execution", lambda: asyncio.run(self.execute_buy_queue()))

    # ══════════════════════════════════════════════════════════
    # Phase A — 분석 파이프라인
    # ══════════════════════════════════════════════════════════

    async def execute_analysis_pipeline(self, force: bool = False) -> dict:
        """
        6단계 순차 실행. 1~3, 5(매수)는 실패 시 즉시 중단하고 Slack 장애 알림.
        4(보유종목 LLM 매도검토)는 실패해도 매수 파이프라인을 막지 않는다 — 기계적 손절/트레일링이
        이미 자금을 보호하고 있어 Fail-Close 가 "추가 매도 보류"만 의미하기 때문이다.
        6(분석 리포트 PDF)은 매매 결정이 모두 끝난 뒤의 보고용이라 실패해도 무시한다.

        Args:
            force: True 면 휴장일 가드를 무시하고 실행 (수동 트리거용)
        """
        now = datetime.now(KST)
        if not force and not kis.is_business_day(now):
            logger.info(f"휴장일({now:%Y-%m-%d}) — 분석 파이프라인 스킵")
            return {"success": True, "skipped": "holiday"}

        logger.info("===== 국내 분석 파이프라인 시작 =====")
        started = time.time()
        completed: dict = {}
        self._artifacts = {}

        def _fail(key: str, name: str, error: str) -> dict:
            try:
                notify.notify_pipeline_failure(key, name, error, completed)
            except Exception as e:
                logger.warning(f"파이프라인 실패 알림 발송 실패: {e}")
            return {
                "success": False,
                "failed_step": key,
                "step_name": name,
                "error": error,
                "completed_steps": completed,
                "total_elapsed_sec": int(time.time() - started),
            }

        # ── Step 1: 시장 데이터 ──────────────────────────────
        name, key = "시장 데이터 수집 (지수·환율·종목·거시)", "1_market_data"
        logger.info(f"[1/6] {name}")
        t0 = time.time()
        try:
            result = kr_market_data_service.collect_market_data()
            if not result.get("success"):
                raise RuntimeError(result.get("message", "수집 실패"))
            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(f"[1/6] 완료 ({completed[key]['elapsed_sec']}초) — {result['message']}")
        except Exception as e:
            logger.error(f"[1/6] 실패: {e}", exc_info=True)
            return _fail(key, name, str(e))

        # ── Step 2: Kaggle ML ────────────────────────────────
        name, key = "Kaggle ML 예측 (국내 커널)", "2_kaggle_ml"
        logger.info(f"[2/6] {name}")
        t0 = time.time()
        try:
            ok, msg, meta = ml_trigger_service.trigger_and_wait(
                kernel_slug=settings.KAGGLE_KERNEL_SLUG_KR,
                notebook_dir=settings.KAGGLE_NOTEBOOK_DIR_KR,
                script_name="predict_kr.py",
            )
            if not ok:
                raise RuntimeError(f"Kaggle 실행 실패: {msg} (meta={meta})")
            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(f"[2/6] 완료 ({completed[key]['elapsed_sec']}초)")
        except Exception as e:
            logger.error(f"[2/6] 실패: {e}", exc_info=True)
            return _fail(key, name, str(e))

        # ── Step 3: 기술 지표 + 뉴스 감성 ────────────────────
        name, key = "기술적 지표 + 뉴스 감성 분석", "3_tech_sentiment"
        logger.info(f"[3/6] {name}")
        t0 = time.time()
        try:
            tech_result = recommend.generate_technical_recommendations()
            logger.info(f"  기술 지표: {tech_result['message']}")

            # 보유 종목이 유니버스 밖이어도 감성은 봐야 매도 판단이 정상 동작한다
            extra = []
            try:
                balance = kis.get_balance()
                extra = [
                    h.get("pdno")
                    for h in balance.get("output1", [])
                    if h.get("pdno") and h["pdno"] not in universe.CODE_TO_NAME
                ]
            except Exception as e:
                logger.warning(f"  보유 종목 조회 실패(감성 대상 확장 생략): {e}")

            sent_result = kr_sentiment_service.fetch_and_store_sentiment(extra_codes=extra)
            logger.info(f"  뉴스 감성: {sent_result['message']}")

            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(f"[3/6] 완료 ({completed[key]['elapsed_sec']}초)")
        except Exception as e:
            logger.error(f"[3/6] 실패: {e}", exc_info=True)
            return _fail(key, name, str(e))

        try:
            notify.notify_data_ready(
                elapsed_sec=int(time.time() - started),
                steps_summary={
                    "1_market": completed["1_market_data"]["elapsed_sec"],
                    "2_ml": completed["2_kaggle_ml"]["elapsed_sec"],
                    "3_tech_sent": completed["3_tech_sentiment"]["elapsed_sec"],
                },
            )
        except Exception as e:
            logger.warning(f"데이터 완료 알림 발송 실패: {e}")

        # ── Step 4: 보유 종목 LLM 매도검토 (실패해도 매수 파이프라인은 계속) ──
        name, key = "보유 종목 LLM 매도검토", "4_sell_review"
        logger.info(f"[4/6] {name}")
        t0 = time.time()
        try:
            sell_reviewed = self._build_sell_review()
            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(f"[4/6] 완료 ({completed[key]['elapsed_sec']}초) — 검토 {sell_reviewed}건")
        except Exception as e:
            # Fail-Close: 매도검토 자체가 실패해도 기계적 손절/트레일링이 자금을 보호하므로
            # 매수 파이프라인을 막을 이유가 없다. 경고만 남기고 계속 진행한다.
            logger.error(f"[4/6] 실패(매수 파이프라인은 계속 진행): {e}", exc_info=True)

        # ── Step 5: LLM 매수 최종 검토 → 매수 큐 ─────────────
        name, key = "LLM 매수 최종 검토 + 매수 예약", "5_llm_queue"
        logger.info(f"[5/6] {name}")
        t0 = time.time()
        try:
            queued = self._build_buy_queue()
            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(f"[5/6] 완료 ({completed[key]['elapsed_sec']}초) — 예약 {len(queued)}건")
        except Exception as e:
            logger.error(f"[5/6] 실패: {e}", exc_info=True)
            return _fail(key, name, str(e))

        # ── Step 6: 분석 리포트 PDF → Slack ──────────────────
        # 매매 결정은 Step 5 에서 이미 끝났다. 리포트는 사람이 읽는 산출물이라
        # 실패해도 파이프라인을 실패로 처리하지 않는다 (경고만 남기고 계속).
        name, key = "분석 리포트 PDF 생성 + Slack 전송", "6_report"
        logger.info(f"[6/6] {name}")
        t0 = time.time()
        try:
            self._artifacts["steps"] = {
                k: v["elapsed_sec"] for k, v in completed.items()
            }
            report = kr_report_service.build_and_send_report(self._artifacts)
            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(
                f"[6/6] 완료 ({completed[key]['elapsed_sec']}초) — "
                f"PDF {report.get('pdf_path')} / 업로드 {report.get('uploaded')}"
            )
        except Exception as e:
            logger.error(f"[6/6] 실패(파이프라인은 정상 종료): {e}", exc_info=True)
            report = {"success": False, "error": str(e)}

        total = int(time.time() - started)
        logger.info(f"===== 국내 분석 파이프라인 완료 (총 {total}초) =====")
        return {
            "success": True,
            "failed_step": None,
            "completed_steps": completed,
            "queued_count": len(queued),
            "report": report,
            "total_elapsed_sec": total,
        }

    def _build_sell_review(self, is_intraday: bool = False) -> int:
        """
        보유 종목 LLM 매도검토 → kr_llm_sell_decision_logs 저장 → Slack 보고. 반환값: 검토 종목 수.

        is_intraday=True 면 정기(16:30) 검토가 아니라 장중 추가 재점검이다 — 결정_date 는
        오늘 날짜로 동일하게 저장되므로, 이 사이클 이후 매도 감시 루프에서 바로 집행 대상이 될
        수 있다(정기 검토는 장 마감 후라 다음날에야 집행되는 것과 다르다).
        """
        context = recommend.get_llm_sell_context()
        holdings_context = context.get("holdings", [])
        market = context.get("market", {})

        if not holdings_context:
            logger.info(f"  매도검토 대상 없음: {context.get('message')}")
            self._artifacts["sell_decisions"] = []
            self._artifacts["sell_market_analysis"] = context.get("message")
            try:
                notify.notify_llm_sell_decisions([], "", market, is_intraday=is_intraday)
            except Exception as e:
                logger.warning(f"  '검토 대상 없음' 알림 발송 실패: {e}")
            return 0

        logger.info(f"  보유 종목 {len(holdings_context)}개 → LLM 매도검토{'(장중 재점검)' if is_intraday else ''}")
        review = kr_llm_sell_review_service.review_sell_candidates(
            holdings_context, market, is_intraday=is_intraday
        )
        self._artifacts["sell_decisions"] = review["decisions"]
        self._artifacts["sell_market_analysis"] = review.get("market_analysis")

        try:
            notify.notify_llm_sell_decisions(
                review["decisions"], review.get("market_analysis", ""), market, is_intraday=is_intraday
            )
        except Exception as e:
            logger.warning(f"  LLM 매도검토 알림 발송 실패: {e}")

        return len(holdings_context)

    def _get_last_sell_review_at(self) -> Optional[datetime]:
        """
        가장 최근 매도검토(정기든 장중 재점검이든) 실행 시각. kr_llm_sell_decision_logs 는
        이제 append-only 라 실행할 때마다 새 행이 남으므로, 그 최신 created_at 을 그대로
        쿨다운 기준으로 쓴다 — 스케줄러 메모리 대신 DB 에서 복구하므로 서버가 재시작돼도
        직전 실행 시각을 그대로 안다(재시작 직후 스팸성 재실행이 나지 않는다).
        """
        try:
            resp = (
                supabase.table(TABLE_SELL_LOGS)
                .select("created_at")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
        except Exception as e:
            logger.warning(f"  마지막 매도검토 시각 조회 실패: {e}")
            return None
        if not rows:
            return None
        try:
            dt = datetime.fromisoformat(str(rows[0]["created_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = KST.localize(dt)
            return dt.astimezone(KST)
        except (ValueError, TypeError) as e:
            logger.warning(f"  마지막 매도검토 시각 파싱 실패: {e}")
            return None

    def _maybe_run_intraday_sell_review(self, now: datetime):
        """
        조건부 주기체크: 공포지수가 KR_INTRADAY_FEAR_REVIEW_THRESHOLD 를 넘는 날은, 마지막
        매도검토 이후 KR_INTRADAY_REVIEW_INTERVAL_HOURS 시간마다 LLM 매도검토를 한 번 더 돌린다.

        공포지수(kospi_vol_20d)는 Phase A(16:30)에만 갱신되므로 장중에는 하루 종일 고정값이다
        — "장중에 막 넘어선 순간"을 관측할 수 없어 이벤트 트리거 대신 주기체크로 설계했다.
        """
        threshold = settings.KR_INTRADAY_FEAR_REVIEW_THRESHOLD
        if threshold <= 0:
            return
        try:
            fear_index = kr_market_data_service.get_market_context().get("kospi_vol_20d")
        except Exception as e:
            logger.warning(f"  장중 매도검토 트리거용 시장환경 조회 실패: {e}")
            return
        if fear_index is None or fear_index <= threshold:
            return

        last_review_at = self._get_last_sell_review_at()
        if last_review_at is not None:
            elapsed_hours = (now - last_review_at).total_seconds() / 3600
            if elapsed_hours < settings.KR_INTRADAY_REVIEW_INTERVAL_HOURS:
                return

        logger.info(
            f"  공포지수 {fear_index:.1f}% > {threshold:.0f}% 지속 — 장중 매도검토 실행 "
            f"(주기 {settings.KR_INTRADAY_REVIEW_INTERVAL_HOURS}시간)"
        )
        try:
            self._build_sell_review(is_intraday=True)
        except Exception as e:
            logger.error(f"  장중 매도검토 실패: {e}", exc_info=True)

    def _latest_sell_decisions_today(self) -> Dict[str, dict]:
        """
        오늘자 kr_llm_sell_decision_logs 를 코드별 가장 최신(created_at) 행만 남겨 반환한다.

        이 테이블은 append-only 다(장중 추가 매도검토가 같은 날 여러 번 쌓일 수 있음) — 그래서
        "오늘의 유효 판단"은 항상 코드별 최신 행이어야 하고, 이 함수를 거치지 않고 status/decision
        으로만 단순 필터링하면 이미 새 판단으로 대체된 낡은 행까지 잘못 집어올 수 있다.
        """
        today = datetime.now(KST).strftime("%Y-%m-%d")
        try:
            resp = (
                supabase.table(TABLE_SELL_LOGS)
                .select("*")
                .eq("decision_date", today)
                .order("created_at", desc=True)
                .execute()
            )
            all_rows = resp.data or []
        except Exception as e:
            logger.warning(f"  오늘자 매도판정 조회 실패: {e}")
            return {}

        latest: Dict[str, dict] = {}
        for r in all_rows:  # created_at desc 라 코드별 첫 등장이 최신
            code = r.get("code")
            if code and code not in latest:
                latest[code] = r
        return latest

    def _pending_rotation_sells(self) -> int:
        """오늘자 LLM 매도검토(코드별 최신 판단 기준)에서 SELL_ALL 이고 아직 집행 전인 종목 수."""
        try:
            latest = self._latest_sell_decisions_today()
            return sum(
                1 for r in latest.values()
                if r.get("decision") == "SELL_ALL" and r.get("status") == "pending"
            )
        except Exception as e:
            logger.warning(f"  교체매매 대기 조회 실패(room 보정 생략): {e}")
            return 0

    def _build_buy_queue(self) -> List[dict]:
        """매수 후보 산출 → LLM 검토 → kr_buy_queue 저장 → Slack 보고."""
        # 매수 원격 스위치 — 꺼져 있으면 LLM 매수검토 자체를 돌리지 않는다(Anthropic 비용
        # 절감, 어차피 집행 단계(execute_buy_queue)에서도 다시 막힌다).
        if not buy_switch_service.is_buy_enabled():
            logger.info("  국내 시장 매수 스위치 꺼짐 — 매수검토 스킵")
            self._artifacts["llm_reasoning"] = "매수 원격 스위치가 꺼져 있어 매수검토를 건너뛰었습니다"
            self._replace_queue([])
            return []
        combined = recommend.get_buy_candidates()
        candidates = combined.get("results", [])
        market = combined.get("market", {})
        self._artifacts["market"] = market
        self._artifacts["all_candidates"] = candidates

        if not candidates:
            logger.info(f"  매수 후보 없음: {combined.get('message')}")
            self._artifacts["llm_reasoning"] = combined.get("message")
            try:
                notify.notify_llm_decisions([], [], combined.get("message", ""), market)
            except Exception as e:
                logger.warning(f"  '후보 없음' 알림 발송 실패: {e}")
            self._replace_queue([])
            return []

        # 이미 보유/주문 중인 종목은 후보에서 제외
        held = self._active_codes()
        if held:
            before = len(candidates)
            candidates = [c for c in candidates if c["code"] not in held]
            if before != len(candidates):
                logger.info(f"  보유/주문 중 {before - len(candidates)}개 종목 제외")

        if not candidates:
            logger.info("  보유 종목 제외 후 남은 후보 없음")
            self._artifacts["llm_reasoning"] = "모든 후보가 이미 보유 중입니다"
            try:
                notify.notify_llm_decisions([], [], "모든 후보가 이미 보유 중입니다", market)
            except Exception as e:
                logger.warning(f"  알림 발송 실패: {e}")
            self._replace_queue([])
            return []

        # 네이버 검색 관심도 — 점수엔 안 넣고 LLM 참고 정보로만 붙인다
        try:
            from app.services.kr import naver_service

            interest = naver_service.get_search_interest_ratios(
                [c["stock_name"] for c in candidates]
            )
            for c in candidates:
                c["search_interest"] = interest.get(c["stock_name"])
        except Exception as e:
            logger.info(f"  검색 관심도 수집 생략: {e}")

        logger.info(f"  매수 후보 {len(candidates)}개 → LLM 최종 검토")
        review = kr_llm_review_service.review_buy_candidates(candidates, market)
        self._artifacts["held"] = list(review["held_candidates"])
        self._artifacts["llm_reasoning"] = review.get("llm_reasoning")

        try:
            notify.notify_llm_decisions(
                review["reviewed_candidates"],
                review["held_candidates"],
                review.get("llm_reasoning", ""),
                market,
            )
        except Exception as e:
            logger.warning(f"  LLM 결정 알림 발송 실패: {e}")

        approved = review["reviewed_candidates"]

        # 슬롯 제한 — 최대 보유 종목 수를 넘지 않게 상위 점수만.
        # 오늘 LLM 매도검토에서 SELL_ALL 판정을 받은 종목(교체매매)은 아직 집행 전이어도
        # room 계산에서 미리 빼준다 — 그래야 같은 날 교체매매 매수가 큐에 들어갈 수 있다.
        # 실집행은 어차피 현금 기준으로 자연 축소되므로(T+2 결제라 당일 매도대금은 못 쓴다)
        # 별도 동기화 장치는 두지 않는다 — 드물게 포지션 수가 일시 초과돼도 다음 매도 감시
        # 사이클에서 자기교정된다.
        current_positions = len(held)
        rotating_out = self._pending_rotation_sells()
        room = max(settings.KR_MAX_POSITIONS - (current_positions - rotating_out), 0)
        if len(approved) > room:
            logger.info(
                f"  보유 한도({settings.KR_MAX_POSITIONS}) 적용: "
                f"현재 {current_positions}종목 → 상위 {room}개만 예약"
            )
            approved = approved[:room]

        self._replace_queue(approved)
        # 슬롯 제한으로 잘린 종목은 '승인됐지만 오늘은 못 사는' 상태 — 리포트에서
        # 보류 목록에 포함시켜야 운용자가 이유를 알 수 있다.
        for c in review["reviewed_candidates"][len(approved):]:
            c["llm_reason"] = (
                f"{c.get('llm_reason', '')} (보유 한도 {settings.KR_MAX_POSITIONS}종목 "
                f"초과로 이번 회차 예약 제외)"
            ).strip()
            self._artifacts.setdefault("held", []).append(c)
        self._artifacts["approved"] = approved

        try:
            notify.notify_buy_queued(approved, settings.KR_EXECUTION_TIME)
        except Exception as e:
            logger.warning(f"  매수 예약 알림 발송 실패: {e}")

        return approved

    def _replace_queue(self, approved: List[dict]):
        """기존 pending 큐를 만료 처리하고 새 예약을 저장한다."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        try:
            supabase.table(TABLE_QUEUE).update({"status": "expired"}).eq(
                "status", "pending"
            ).eq("account_type", kis.current_account_type()).execute()
        except Exception as e:
            logger.warning(f"  기존 매수 큐 만료 처리 실패: {e}")

        if not approved:
            return

        rows = [
            {
                "queued_date": today,
                "code": c["code"],
                "stock_name": c.get("stock_name"),
                "composite_score": c.get("composite_score"),
                "rise_probability": c.get("rise_probability"),
                "llm_reason": c.get("llm_reason"),
                "atr": c.get("atr"),
                "status": "pending",
                "account_type": kis.current_account_type(),
            }
            for c in approved
        ]
        try:
            supabase.table(TABLE_QUEUE).insert(rows).execute()
            logger.info(f"  매수 큐 저장: {len(rows)}건")
        except Exception as e:
            logger.error(f"  매수 큐 저장 실패: {e}", exc_info=True)
            raise

    def _active_codes(self) -> set:
        """보유 중이거나 주문 접수된 종목코드 (KIS 원장 + kr_trade_records 이중 확인)."""
        codes = set()
        try:
            balance = kis.get_balance()
            for h in balance.get("output1", []):
                if h.get("pdno"):
                    codes.add(h["pdno"])
        except Exception as e:
            logger.warning(f"  보유 종목 조회 실패: {e}")

        try:
            resp = (
                supabase.table(TABLE_TRADES)
                .select("code")
                .in_("status", ["buy_ordered", "holding", "sell_ordered"])
                .eq("account_type", kis.current_account_type())
                .execute()
            )
            for r in resp.data or []:
                codes.add(r["code"])
        except Exception as e:
            logger.warning(f"  거래 기록 조회 실패: {e}")

        return codes

    # ══════════════════════════════════════════════════════════
    # Phase B — 매수 집행
    # ══════════════════════════════════════════════════════════

    async def execute_buy_queue(self, force: bool = False) -> dict:
        """
        kr_buy_queue 의 pending 예약을 장 시작 후 집행한다.
        분석 시점 종가가 아니라 집행 시점 현재가로 수량을 다시 계산한다.
        """
        # 매수 원격 스위치 — force 여부와 무관하게 무조건 체크한다("꺼지면 다시 켤 때까지
        # 계속 꺼짐"이 요구사항). 매도 감시/정합성 확인은 이 스위치와 무관하게 별도로 돈다.
        if not buy_switch_service.is_buy_enabled():
            logger.info("국내 시장 매수 스위치 꺼짐 — 매수 집행 스킵")
            return {"success": True, "skipped": "buy_disabled", "ordered": 0}

        now = datetime.now(KST)
        if not force:
            if not kis.is_business_day(now):
                logger.info(f"휴장일({now:%Y-%m-%d}) — 매수 집행 스킵")
                return {"success": True, "skipped": "holiday", "ordered": 0}
            if not kis.is_market_open(now):
                logger.info(f"장 시간 아님({now:%H:%M}) — 매수 집행 스킵")
                return {"success": True, "skipped": "market_closed", "ordered": 0}

        account = kis.current_account_type()

        try:
            resp = (
                supabase.table(TABLE_QUEUE)
                .select("*")
                .eq("status", "pending")
                .eq("account_type", account)
                .order("composite_score", desc=True)
                .execute()
            )
        except Exception as e:
            logger.error(f"매수 큐 조회 실패: {e}", exc_info=True)
            return {"success": False, "error": str(e), "ordered": 0}

        queue = resp.data or []
        if not queue:
            logger.info("집행할 매수 예약이 없습니다.")
            return {"success": True, "ordered": 0, "skipped_items": []}

        # 오래된 예약 만료 (연휴 등으로 밀린 낡은 판단으로 매수하지 않도록)
        cutoff = (now - timedelta(days=QUEUE_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        fresh, stale = [], []
        for q in queue:
            (stale if (q.get("queued_date") or "") < cutoff else fresh).append(q)
        if stale:
            ids = [q["id"] for q in stale]
            try:
                supabase.table(TABLE_QUEUE).update({"status": "expired"}).in_("id", ids).execute()
            except Exception as e:
                logger.warning(f"  낡은 예약 만료 처리 실패: {e}")
            logger.warning(f"  {len(stale)}건의 예약이 {QUEUE_MAX_AGE_DAYS}일 초과로 만료됐습니다")

        if not fresh:
            return {"success": True, "ordered": 0, "skipped_items": ["all_expired"]}

        logger.info(f"매수 집행 시작: 예약 {len(fresh)}건")

        # 총자산 = 주식 평가액 + D+2 예수금 → 종목당 슬롯 고정
        summary = kis.get_account_summary()
        cash = summary.get("d2_deposit", 0.0) or summary.get("deposit", 0.0)
        stock_value = summary.get("stock_eval_amount", 0.0)
        total_assets = cash + stock_value
        if total_assets <= 0:
            logger.error(
                "계좌 요약 조회 실패 또는 총자산 0원 — 매수 집행을 중단합니다 "
                "(전 종목이 '투자금 부족'으로 조용히 스킵되는 것을 방지)"
            )
            return {"success": False, "error": "총자산 조회 실패", "ordered": 0}
        # 확신도 가중 배분 — 종합점수가 높은 종목에 더 많이 넣는다.
        # fresh 는 composite_score 내림차순이라 ratios 도 같은 순서로 대응한다.
        ratios = compute_weighted_slots(
            scores=[q.get("composite_score") for q in fresh],
            base_ratio=settings.KR_SLOT_RATIO,
            tilt=settings.KR_SLOT_TILT,
            min_ratio=settings.KR_MIN_SLOT_RATIO,
            max_ratio=settings.KR_MAX_SLOT_RATIO,
            max_total_exposure=settings.KR_MAX_TOTAL_EXPOSURE,
            method=settings.KR_SLOT_METHOD,
        )
        logger.info(
            f"  총자산 {total_assets:,.0f}원 (예수금 {cash:,.0f} + 평가 {stock_value:,.0f})"
        )
        logger.info(
            f"  배분 방식: {settings.KR_SLOT_METHOD} / tilt={settings.KR_SLOT_TILT} "
            f"(기준 {settings.KR_SLOT_RATIO:.0%}, 종목당 "
            f"{settings.KR_MIN_SLOT_RATIO:.0%}~{settings.KR_MAX_SLOT_RATIO:.0%}, "
            f"총 노출 상한 {settings.KR_MAX_TOTAL_EXPOSURE:.0%}) "
            f"→ 총 {sum(ratios) * 100:.1f}%"
        )
        # 현금 부족 시 비례 축소
        #   슬롯은 총자산 기준이라 보유 비중이 커지면 가용 현금을 넘어설 수 있다.
        #   상위 종목이 현금을 독식해 하위가 0주가 되는 것을 막는다.
        target_total = total_assets * sum(ratios)
        cash_scale = 1.0
        if target_total > cash > 0:
            cash_scale = cash / target_total
            logger.warning(
                f"  현금 부족: 목표 배분 {target_total:,.0f}원 > 가용 현금 {cash:,.0f}원 "
                f"→ 전 종목 {cash_scale * 100:.1f}% 로 비례 축소 (배분 비율 유지)"
            )
            ratios = [r * cash_scale for r in ratios]

        for line in describe_allocation(
            [q.get("stock_name") or q["code"] for q in fresh],
            [q.get("composite_score") for q in fresh],
            ratios,
            total_assets,
        ):
            logger.info(line)

        held = self._active_codes()
        ordered, skipped = 0, []

        for idx, q in enumerate(fresh):
            code = q["code"]
            name = q.get("stock_name") or universe.CODE_TO_NAME.get(code, code)
            slot = total_assets * ratios[idx]   # 이 종목의 확신도 가중 슬롯

            try:
                if code in held:
                    logger.info(f"  {universe.display(code)} 이미 보유/주문 중 → 스킵")
                    self._mark_queue(q["id"], "skipped", "이미 보유/주문 중")
                    skipped.append(f"{code}:held")
                    continue

                current = kis.get_current_price_value(code)
                if current is None:
                    logger.error(f"  {universe.display(code)} 현재가 조회 실패 → 스킵")
                    self._mark_queue(q["id"], "skipped", "현재가 조회 실패")
                    skipped.append(f"{code}:no_price")
                    continue

                # 매수 지정가: 호가단위로 올림 (최대 1틱 양보 ≤0.2%, 체결 확률을 크게 높인다)
                order_price = kis.round_to_tick(current, mode="up")

                # 가용 현금 한도 반영
                invest = min(slot, cash)
                quantity = int(invest // order_price)
                if quantity < 1:
                    logger.info(
                        f"  {universe.display(code)} 슬롯 {invest:,.0f}원"
                        f"({ratios[idx] * 100:.1f}%)으로 "
                        f"1주({order_price:,}원)도 살 수 없음 → 스킵"
                    )
                    self._mark_queue(q["id"], "skipped", "투자금 부족")
                    skipped.append(f"{code}:insufficient")
                    continue

                # 주문가능금액 재확인 (미수 방지)
                orderable = kis.get_orderable_cash(code, order_price)
                if orderable is not None and orderable < quantity * order_price:
                    adjusted = int(orderable // order_price)
                    if adjusted < 1:
                        logger.info(f"  {universe.display(code)} 주문가능금액 부족 → 스킵")
                        self._mark_queue(q["id"], "skipped", "주문가능금액 부족")
                        skipped.append(f"{code}:no_cash")
                        continue
                    logger.warning(
                        f"  {universe.display(code)} 주문가능금액({orderable:,.0f}원) 한도로 "
                        f"{quantity}주 → {adjusted}주 조정"
                    )
                    quantity = adjusted

                # ATR 안전장치 — 익절/손절선 없이는 매수하지 않는다
                atr = self._compute_atr(code, q.get("atr"))
                if atr is None or atr <= 0:
                    logger.warning(
                        f"  ❌ {universe.display(code)} ATR 계산 실패 → 매수 스킵 "
                        f"(자동 익절/손절 안전장치 없이 매수하지 않음)"
                    )
                    self._mark_queue(q["id"], "skipped", "ATR 계산 실패")
                    skipped.append(f"{code}:no_atr")
                    continue

                take_profit = kis.round_to_tick(
                    order_price + atr * recommend.ATR_TAKE_PROFIT_MULT, mode="down"
                )
                stop_loss = kis.round_to_tick(
                    order_price - atr * recommend.ATR_STOP_LOSS_MULT, mode="up"
                )
                logger.info(
                    f"  {universe.display(code)} ATR={atr:,.0f} "
                    f"익절={take_profit:,}원 손절={stop_loss:,}원"
                )

                result = kis.order_stock(code, quantity, order_price, is_buy=True)
                if result.get("rt_cd") != "0":
                    self._mark_queue(q["id"], "failed", result.get("msg1", "주문 실패"))
                    skipped.append(f"{code}:order_failed")
                    continue

                ordered += 1
                held.add(code)
                cash -= quantity * order_price  # 남은 현금 반영
                self._mark_queue(q["id"], "executed", None)

                try:
                    supabase.table(TABLE_TRADES).insert(
                        {
                            "code": code,
                            "stock_name": name,
                            "buy_price": order_price,
                            "buy_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "quantity": quantity,
                            "holding_quantity": 0,
                            "atr": atr,
                            "take_profit_price": take_profit,
                            "stop_loss_price": stop_loss,
                            "status": "buy_ordered",
                            "composite_score": q.get("composite_score"),
                            "account_type": account,
                        }
                    ).execute()
                except Exception as e:
                    logger.error(f"  {universe.display(code)} 거래기록 저장 실패: {e}")

                try:
                    notify.notify_buy_ordered(
                        code, name, quantity, order_price, q.get("composite_score")
                    )
                except Exception as e:
                    logger.warning(f"  매수 주문 알림 발송 실패: {e}")

            except Exception as e:
                logger.error(f"  {universe.display(code)} 매수 처리 중 오류: {e}", exc_info=True)
                skipped.append(f"{code}:exception")

        logger.info(f"매수 집행 완료: 주문 {ordered}건, 스킵 {len(skipped)}건")
        return {"success": True, "ordered": ordered, "skipped_items": skipped}

    def _compute_atr(self, code: str, cached: Optional[float]) -> Optional[float]:
        """예약 시 저장해둔 ATR 을 우선 쓰고, 없으면 일봉으로 재계산."""
        if cached:
            try:
                v = float(cached)
                if v > 0:
                    return v
            except (ValueError, TypeError):
                pass
        return recommend.compute_atr(code)

    def _mark_queue(self, queue_id, status: str, note: Optional[str]):
        try:
            supabase.table(TABLE_QUEUE).update(
                {"status": status, "note": note, "executed_at": datetime.now(KST).isoformat()}
            ).eq("id", queue_id).execute()
        except Exception as e:
            logger.warning(f"  매수 큐 상태 갱신 실패(id={queue_id}): {e}")

    # ══════════════════════════════════════════════════════════
    # 매도 감시 + 정합성
    # ══════════════════════════════════════════════════════════

    async def execute_auto_sell(self) -> dict:
        now = datetime.now(KST)
        if not kis.is_business_day(now):
            return {"skipped": "holiday"}

        # 장 시간 밖이어도 정합성 확인은 돌린다 (마감 후 미체결 정리 필요)
        balance = kis.get_balance()
        self._reconcile_orders(balance)

        if not kis.is_sell_window(now):
            return {"skipped": "outside_sell_window"}

        # ── 공포장 지속 시 장중 추가 매도검토 (조건부 주기체크) ──
        self._maybe_run_intraday_sell_review(now)

        # ── 기계적 규칙 (ATR 전량 익절/손절, 기술신호개수, 공포장) — 항상 LLM 과 무관하게 실행 ──
        result = recommend.get_mechanical_sell_candidates(balance=balance)
        candidates = result.get("sell_candidates", [])

        # 이미 매도 주문이 나간 종목 제외
        try:
            resp = (
                supabase.table(TABLE_TRADES)
                .select("code")
                .eq("status", "sell_ordered")
                .eq("account_type", kis.current_account_type())
                .execute()
            )
            pending = {r["code"] for r in (resp.data or [])}
            if pending:
                before = len(candidates)
                candidates = [c for c in candidates if c["code"] not in pending]
                if before != len(candidates):
                    logger.info(f"  매도 주문 접수 중인 {before - len(candidates)}개 제외")
        except Exception as e:
            logger.warning(f"  매도 중복 확인 실패: {e}")

        sold = 0
        account = kis.current_account_type()

        if candidates:
            logger.info(f"매도 대상 {len(candidates)}개 식별(기계적)")

        for c in candidates:
            code, name = c["code"], c["stock_name"]
            qty = c["quantity"]  # 항상 보유 전량
            reason_code = c.get("reason_code", "signal")
            try:
                logger.info(
                    f"  {name}({code}) 전량매도 근거: {'; '.join(c['sell_reasons'])}"
                )

                current = kis.get_current_price_value(code)
                if current is None:
                    logger.error(f"  {name}({code}) 현재가 조회 실패 → 다음 회차 재시도")
                    continue

                # 매도 지정가: 호가단위로 내림 (체결 확률 우선)
                order_price = kis.round_to_tick(current, mode="down")

                result_order = kis.order_stock(code, qty, order_price, is_buy=False)
                if result_order.get("rt_cd") != "0":
                    continue

                sold += 1
                buy_price = c.get("buy_price") or 0
                trade_id = c.get("trade_id")

                pnl_leg = (order_price - buy_price) * qty if buy_price > 0 else None
                # 과거 부분매도로 이미 실현한 분이 남아 있으면 총손익에 합산한다 (신규 보유분은 0)
                realized_pnl = c.get("realized_partial_pnl") or 0.0
                realized_qty = c.get("realized_partial_qty") or 0
                total_qty = qty + realized_qty
                pnl_total = (pnl_leg or 0) + realized_pnl if buy_price > 0 else None
                pnl_pct_total = (
                    (pnl_total / (buy_price * total_qty) * 100)
                    if buy_price > 0 and total_qty > 0
                    else None
                )

                try:
                    self._update_trade_record(
                        {
                            "status": "sell_ordered",
                            "sell_price": order_price,
                            "sell_date": datetime.now(KST).isoformat(),
                            "sell_reason": reason_code,
                            "profit_loss": round(pnl_total, 0) if pnl_total is not None else None,
                            "profit_loss_pct": (
                                round(pnl_pct_total, 2) if pnl_pct_total is not None else None
                            ),
                        },
                        trade_id=trade_id,
                        code=code,
                        account=account,
                    )
                except Exception as e:
                    logger.error(f"  {name}({code}) 거래기록 갱신 실패: {e}")

                try:
                    notify.notify_sell_ordered(code, name, qty, order_price, reason_code)
                except Exception as e:
                    logger.warning(f"  매도 주문 알림 발송 실패: {e}")

            except Exception as e:
                logger.error(f"  {name}({code}) 매도 처리 중 오류: {e}", exc_info=True)

        # ── LLM 매도검토 판정(SELL_ALL) — 기계적 규칙이 처리하지 않은 종목만 ──
        handled_codes = {c["code"] for c in candidates}
        llm_sold = await self._execute_llm_sell_decisions(balance, handled_codes)

        return {"sold": sold, "llm_sold": llm_sold}

    def _update_trade_record(
        self,
        fields: dict,
        trade_id=None,
        code: Optional[str] = None,
        account: Optional[str] = None,
    ):
        """trade_id 가 있으면 그 행을, 없으면 code+status=holding+account_type 로 매칭해 갱신."""
        q = supabase.table(TABLE_TRADES).update(fields)
        if trade_id is not None:
            q = q.eq("id", trade_id)
        else:
            q = q.eq("code", code).eq("status", "holding").eq("account_type", account)
        q.execute()

    def _should_defer_llm_sell(self, row: dict, code: str, current_price: float) -> Optional[str]:
        """
        LLM 매도판정 집행 직전 재검증. 유예해야 하면 사유 문자열, 아니면 None.

        두 가지를 본다 (하나라도 걸리면 유예):
          1) 가격 — 판단 시점보다 KR_SELL_REVALIDATE_PCT 이상 유리한 방향(상승)으로 이미 움직였는가.
          2) 근거 — 판단 시점 기술신호 개수보다 지금이 적은가(개선 신호).
        가격만으로는 "판단의 실제 근거(신호)가 바뀌었는지"까지는 못 보므로 2)를 더한다.
        """
        if settings.KR_SELL_REVALIDATE_PCT <= 0:
            return None

        judged_price = row.get("price_at_decision")
        if judged_price:
            try:
                judged_price = float(judged_price)
                move_pct = (
                    (current_price - judged_price) / judged_price * 100
                    if judged_price > 0 else 0.0
                )
            except (ValueError, TypeError):
                move_pct = 0.0
            if move_pct >= settings.KR_SELL_REVALIDATE_PCT:
                return (
                    f"판단 시점({judged_price:,.0f}원) 대비 {move_pct:+.2f}% 상승"
                    f"(재검증 임계 {settings.KR_SELL_REVALIDATE_PCT:.1f}%)"
                )

        judged_signals = row.get("signal_count_at_decision")
        if judged_signals is not None:
            current_signals = recommend.get_current_signal_count(code)
            if current_signals is not None and current_signals < judged_signals:
                return f"기술신호 {judged_signals}개 → {current_signals}개로 감소(근거 개선)"

        return None

    def _mark_sell_decision(self, decision_id, status: str):
        try:
            supabase.table(TABLE_SELL_LOGS).update(
                {"status": status, "executed_at": datetime.now(KST).isoformat()}
            ).eq("id", decision_id).execute()
        except Exception as e:
            logger.warning(f"  LLM 매도판정 상태 갱신 실패(id={decision_id}): {e}")

    async def _execute_llm_sell_decisions(self, balance: dict, skip_codes: set) -> int:
        """
        오늘자 LLM 매도검토(kr_llm_sell_decision_logs, 코드별 최신 판단 기준)의
        SELL_ALL 미집행 판정을 집행한다(매도는 언제나 전량이다). 기계적 규칙이 이번 사이클에
        이미 처리한 종목은 건너뛴다(중복 매도 방지, 기계적 처리 우선).

        과거에 쌓인 SELL_PARTIAL 판정이 pending 으로 남아 있으면 집행하지 않고 skipped 로
        닫는다 — 부분매도는 더 이상 하지 않으므로, 그렇다고 전량매도로 승격하면 사람이 내리지
        않은 결정을 대신 내리는 셈이 된다. 다음 검토에서 새 판단으로 자연 대체된다.
        """
        account = kis.current_account_type()

        latest = self._latest_sell_decisions_today()
        rows = []
        for r in latest.values():
            if r.get("status") != "pending":
                continue
            if r.get("decision") == "SELL_ALL":
                rows.append(r)
            elif r.get("decision") == "SELL_PARTIAL":
                # 레거시 판정 — 부분매도 폐지. 영원히 pending 으로 남지 않도록 닫아둔다.
                logger.info(
                    f"  {universe.display(r['code'])} 레거시 SELL_PARTIAL 판정 → 집행하지 않고 종료"
                )
                self._mark_sell_decision(r["id"], "skipped")

        if not rows:
            return 0

        holdings = {h.get("pdno"): h for h in balance.get("output1", []) if h.get("pdno")}

        try:
            tr_resp = (
                supabase.table(TABLE_TRADES)
                .select("*")
                .eq("status", "holding")
                .eq("account_type", account)
                .execute()
            )
            trade_map = {t["code"]: t for t in (tr_resp.data or [])}
        except Exception as e:
            logger.warning(f"  거래기록 조회 실패(LLM 매도판정 집행 생략): {e}")
            return 0

        executed = 0
        for row in rows:
            code = row["code"]
            if code in skip_codes:
                self._mark_sell_decision(row["id"], "skipped")
                continue

            item = holdings.get(code)
            trade = trade_map.get(code)
            if item is None or trade is None:
                self._mark_sell_decision(row["id"], "expired")
                continue

            try:
                quantity = int(item.get("ord_psbl_qty", 0) or 0)
                buy_price = float(item.get("pchs_avg_pric", 0) or 0)
            except (ValueError, TypeError):
                continue
            if quantity <= 0:
                self._mark_sell_decision(row["id"], "expired")
                continue

            sell_qty = quantity  # 항상 전량

            current = kis.get_current_price_value(code)
            if current is None:
                logger.error(f"  {universe.display(code)} 현재가 조회 실패 → LLM 매도판정 다음 회차 재시도")
                continue

            # 재검증: 판단(전날 16:30) 시점 대비 가격이 이미 유리한 방향(상승)으로 크게 움직였거나,
            # 판단 근거였던 기술신호 개수가 그새 줄었다면(근거 개선) 이번 사이클 집행을 보류한다.
            # (취소 아님 — status 를 건드리지 않고 그냥 넘어가 다음 사이클에 다시 검사한다.
            # 마감까지 계속 그렇다면 결국 집행되지 않고 다음날 새 판단으로 자연 대체된다.)
            defer_reason = self._should_defer_llm_sell(row, code, current)
            if defer_reason:
                logger.info(
                    f"  {universe.display(code)} LLM 매도판정 유예: {defer_reason} — "
                    f"이번 사이클 집행 보류"
                )
                continue

            order_price = kis.round_to_tick(current, mode="down")

            result_order = kis.order_stock(code, sell_qty, order_price, is_buy=False)
            if result_order.get("rt_cd") != "0":
                continue

            executed += 1
            reason_code = "llm_sell_all"
            name = trade.get("stock_name") or item.get("prdt_name", code)

            pnl_leg = (order_price - buy_price) * sell_qty if buy_price > 0 else None
            # 과거 부분매도로 이미 실현한 분이 남아 있으면 총손익에 합산 (신규 보유분은 0)
            realized_pnl = float(trade.get("realized_partial_pnl") or 0)
            realized_qty = int(trade.get("realized_partial_qty") or 0)
            total_qty = sell_qty + realized_qty
            pnl_total = (pnl_leg or 0) + realized_pnl if buy_price > 0 else None
            pnl_pct_total = (
                (pnl_total / (buy_price * total_qty) * 100)
                if buy_price > 0 and total_qty > 0
                else None
            )
            try:
                supabase.table(TABLE_TRADES).update(
                    {
                        "status": "sell_ordered",
                        "sell_price": order_price,
                        "sell_date": datetime.now(KST).isoformat(),
                        "sell_reason": reason_code,
                        "profit_loss": round(pnl_total, 0) if pnl_total is not None else None,
                        "profit_loss_pct": (
                            round(pnl_pct_total, 2) if pnl_pct_total is not None else None
                        ),
                    }
                ).eq("id", trade["id"]).execute()
            except Exception as e:
                logger.error(f"  {name}({code}) 거래기록 갱신 실패: {e}")
            try:
                notify.notify_sell_ordered(code, name, sell_qty, order_price, reason_code)
            except Exception as e:
                logger.warning(f"  LLM 매도 알림 발송 실패: {e}")

            self._mark_sell_decision(row["id"], "executed")

        return executed

    def _reconcile_orders(self, balance: Optional[dict] = None):
        """
        KIS 원장 기준으로 kr_trade_records 상태를 맞춘다.

        - buy_ordered  + 보유 O  → holding (체결 확인, 체결 알림 발송)
        - buy_ordered  + 보유 X  + 장 마감 후 → buy_failed (당일 유효 주문 자동 실효)
        - sell_ordered + 보유 X  → sold (체결 확인, 체결 알림 발송)
        - sell_ordered + 보유 O  + 장 마감 후 → holding 복원
        - holding                → 보유수량 동기화
        - KIS 에는 있는데 기록에 없는 종목 → 고아 레코드 자동 생성
        """
        if balance is None:
            balance = kis.get_balance()
        if balance.get("rt_cd") != "0":
            logger.warning(f"정합성 확인용 잔고 조회 실패: {balance.get('msg1')}")
            return

        kis_holdings = {}
        for h in balance.get("output1", []):
            code = h.get("pdno")
            try:
                qty = int(h.get("hldg_qty", 0) or 0)
            except (ValueError, TypeError):
                qty = 0
            if code and qty > 0:
                kis_holdings[code] = {"qty": qty, "item": h}

        now = datetime.now(KST)
        # 15:40 이후를 '마감 정리 시각'으로 본다 (동시호가 15:20~15:30 체결 반영 여유)
        after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 40)
        account = kis.current_account_type()

        try:
            resp = (
                supabase.table(TABLE_TRADES)
                .select("*")
                .in_("status", ["buy_ordered", "holding", "sell_ordered", "partial_sell_ordered"])
                .eq("account_type", account)
                .execute()
            )
            records = resp.data or []
        except Exception as e:
            logger.error(f"정합성 확인용 거래기록 조회 실패: {e}")
            return

        tracked = set()

        for rec in records:
            code = rec["code"]
            status = rec["status"]
            rec_id = rec["id"]
            name = rec.get("stock_name", code)
            kis_qty = kis_holdings.get(code, {}).get("qty", 0)
            tracked.add(code)

            try:
                if status == "buy_ordered":
                    if kis_qty > 0:
                        # 체결가 동기화 — buy_price 는 매수 시점엔 '주문가'로 들어간다.
                        # 지정가 주문이 더 유리한 값에 체결되면 원장(pchs_avg_pric)과 어긋나는데,
                        # 매도 시 손익은 원장 기준으로 계산하므로 그대로 두면 기록상
                        # "22,700 매수 → 22,650 매도, 손익 0원" 같은 모순이 남는다.
                        # 익절/손절선도 주문가 기준으로 잡혀 있으므로 체결가 기준으로 다시 맞춘다
                        # (ATR 배수라는 원래 의도를 체결가에 대해 보존).
                        update_fields = {"status": "holding", "holding_quantity": kis_qty}
                        try:
                            fill_price = float(
                                kis_holdings[code]["item"].get("pchs_avg_pric", 0) or 0
                            )
                        except (ValueError, TypeError, KeyError):
                            fill_price = 0.0

                        ordered_price = float(rec.get("buy_price") or 0)
                        if fill_price > 0 and fill_price != ordered_price:
                            update_fields["buy_price"] = fill_price
                            try:
                                rec_atr = float(rec["atr"]) if rec.get("atr") is not None else None
                            except (ValueError, TypeError):
                                rec_atr = None
                            if rec_atr and rec_atr > 0:
                                update_fields["take_profit_price"] = kis.round_to_tick(
                                    fill_price + rec_atr * recommend.ATR_TAKE_PROFIT_MULT,
                                    mode="down",
                                )
                                update_fields["stop_loss_price"] = kis.round_to_tick(
                                    fill_price - rec_atr * recommend.ATR_STOP_LOSS_MULT,
                                    mode="up",
                                )
                            logger.info(
                                f"  {name}({code}) 체결가 동기화: 주문 {ordered_price:,.0f}원 → "
                                f"체결 {fill_price:,.0f}원"
                                + (
                                    f" (익절 {update_fields['take_profit_price']:,}원 / "
                                    f"손절 {update_fields['stop_loss_price']:,}원 재산출)"
                                    if "take_profit_price" in update_fields
                                    else ""
                                )
                            )

                        supabase.table(TABLE_TRADES).update(update_fields).eq(
                            "id", rec_id
                        ).execute()
                        ordered_qty = rec.get("quantity") or 0
                        if kis_qty < ordered_qty:
                            logger.info(
                                f"  {name}({code}) 부분 체결 → holding "
                                f"(주문 {ordered_qty}주 / 체결 {kis_qty}주)"
                            )
                        else:
                            logger.info(f"  {name}({code}) 매수 체결 확인 → holding ({kis_qty}주)")

                        try:
                            # 알림도 동기화된 값으로 — 재산출됐으면 그쪽이 실제 적용선이다
                            notify.notify_buy_filled(
                                code=code,
                                stock_name=name,
                                qty=kis_qty,
                                fill_price=fill_price or ordered_price,
                                take_profit_price=update_fields.get(
                                    "take_profit_price", rec.get("take_profit_price")
                                ),
                                stop_loss_price=update_fields.get(
                                    "stop_loss_price", rec.get("stop_loss_price")
                                ),
                                composite_score=rec.get("composite_score"),
                            )
                        except Exception as e:
                            logger.warning(f"  {code} 매수 체결 알림 발송 실패: {e}")

                    elif after_close:
                        supabase.table(TABLE_TRADES).update({"status": "buy_failed"}).eq(
                            "id", rec_id
                        ).execute()
                        logger.warning(f"  {name}({code}) 매수 미체결 (장 마감) → buy_failed")

                elif status == "holding":
                    prev = rec.get("holding_quantity") or 0
                    if kis_qty > 0 and kis_qty != prev:
                        supabase.table(TABLE_TRADES).update(
                            {"holding_quantity": kis_qty}
                        ).eq("id", rec_id).execute()
                        logger.info(f"  {name}({code}) 보유수량 동기화: {prev} → {kis_qty}주")

                elif status == "sell_ordered":
                    if kis_qty == 0:
                        supabase.table(TABLE_TRADES).update(
                            {"status": "sold", "holding_quantity": 0}
                        ).eq("id", rec_id).execute()
                        logger.info(f"  {name}({code}) 매도 체결 확인 → sold")

                        try:
                            notify.notify_sell_filled(
                                code=code,
                                stock_name=name,
                                qty=rec.get("quantity") or 0,
                                fill_price=float(rec.get("sell_price") or 0),
                                sell_reason=rec.get("sell_reason") or "signal",
                                profit_loss=float(rec.get("profit_loss") or 0),
                                profit_loss_pct=float(rec.get("profit_loss_pct") or 0),
                                buy_price=rec.get("buy_price"),
                                buy_date=rec.get("buy_date"),
                            )
                        except Exception as e:
                            logger.warning(f"  {code} 매도 체결 알림 발송 실패: {e}")

                    elif after_close:
                        supabase.table(TABLE_TRADES).update(
                            {
                                "status": "holding",
                                "holding_quantity": kis_qty,
                                "sell_price": None,
                                "sell_date": None,
                                "sell_reason": None,
                                "profit_loss": None,
                                "profit_loss_pct": None,
                            }
                        ).eq("id", rec_id).execute()
                        logger.warning(f"  {name}({code}) 매도 미체결 (장 마감) → holding 복원")

                elif status == "partial_sell_ordered":
                    # 레거시 — 부분매도를 폐지하기 전에 나간 주문이 아직 떠 있는 경우.
                    # 새로 이 상태가 만들어지는 경로는 없으므로, KIS 원장을 기준으로 정리만 하고
                    # holding 으로 되돌린다. 이후 사이클부터는 전량 익절/손절 규칙이 적용된다.
                    prev_qty = rec.get("holding_quantity") or 0
                    pending_qty = rec.get("pending_partial_qty") or 0
                    filled_qty = prev_qty - kis_qty if kis_qty < prev_qty else 0

                    update_fields = {
                        "status": "holding",
                        "holding_quantity": kis_qty,
                        "pending_partial_qty": None,
                    }

                    if filled_qty > 0:
                        # 체결된 만큼을 실현분에 누적한다. 이 값은 나중에 잔량을 전량매도할 때
                        # 총손익 계산에 합산된다.
                        try:
                            fill_price = float(
                                kis_holdings.get(code, {}).get("item", {}).get("prpr", 0) or 0
                            )
                        except (ValueError, TypeError):
                            fill_price = 0.0
                        buy_price = float(rec.get("buy_price") or 0)
                        fill_price = fill_price or buy_price
                        pnl = (fill_price - buy_price) * filled_qty if buy_price > 0 else 0.0

                        update_fields["partial_exit_done"] = True
                        update_fields["realized_partial_pnl"] = (
                            float(rec.get("realized_partial_pnl") or 0) + pnl
                        )
                        update_fields["realized_partial_qty"] = (
                            int(rec.get("realized_partial_qty") or 0) + filled_qty
                        )

                        supabase.table(TABLE_TRADES).update(update_fields).eq("id", rec_id).execute()
                        logger.info(
                            f"  {name}({code}) 레거시 부분매도 {filled_qty}주 체결 확인 → "
                            f"holding ({kis_qty}주 잔량, 이후 전량 익절/손절 규칙 적용)"
                        )
                        try:
                            notify.notify_sell_filled(
                                code=code,
                                stock_name=name,
                                qty=filled_qty,
                                fill_price=fill_price,
                                sell_reason="partial_take_profit",
                                profit_loss=pnl,
                                profit_loss_pct=(
                                    (fill_price - buy_price) / buy_price * 100 if buy_price > 0 else 0
                                ),
                                buy_price=rec.get("buy_price"),
                                buy_date=rec.get("buy_date"),
                            )
                        except Exception as e:
                            logger.warning(f"  {code} 레거시 부분매도 체결 알림 발송 실패: {e}")
                    else:
                        supabase.table(TABLE_TRADES).update(update_fields).eq("id", rec_id).execute()
                        logger.warning(
                            f"  {name}({code}) 레거시 부분매도 미체결 (대기수량 {pending_qty}주) "
                            f"→ holding 복원"
                        )

            except Exception as e:
                logger.error(f"  {name}({code}) 정합성 처리 실패: {e}")

        # 고아 감지: KIS 에 있는데 기록에 없는 종목
        for code, info in kis_holdings.items():
            if code in tracked:
                continue
            item = info["item"]
            try:
                supabase.table(TABLE_TRADES).insert(
                    {
                        "code": code,
                        "stock_name": item.get("prdt_name", code),
                        "buy_price": float(item.get("pchs_avg_pric", 0) or 0),
                        "buy_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "quantity": info["qty"],
                        "holding_quantity": info["qty"],
                        "status": "holding",
                        "account_type": account,
                    }
                ).execute()
                logger.warning(
                    f"  고아 감지: {code} KIS 보유 {info['qty']}주 but 기록 없음 "
                    f"→ 레코드 자동 생성 (account={account})"
                )
            except Exception as e:
                logger.error(f"  {code} 고아 레코드 생성 실패: {e}")


# ══════════════════════════════════════════════════════════════════
# 싱글톤 + 외부 진입점
# ══════════════════════════════════════════════════════════════════

kr_scheduler = KrScheduler()


def start_kr_scheduler() -> bool:
    return kr_scheduler.start()


def stop_kr_scheduler() -> bool:
    return kr_scheduler.stop()


def get_kr_scheduler_status() -> dict:
    return kr_scheduler.status()


def _run_in_thread(coro_factory):
    """FastAPI 이벤트 루프 안에서 asyncio.run 을 쓸 수 없어 별도 스레드에서 실행."""

    def _runner():
        try:
            asyncio.run(coro_factory())
        except Exception as e:
            logger.error(f"수동 실행 중 오류: {e}", exc_info=True)

    threading.Thread(target=_runner, daemon=True).start()
    return True


def run_analysis_now() -> bool:
    """분석 파이프라인 즉시 실행 (휴장일 가드 무시)."""
    return _run_in_thread(lambda: kr_scheduler.execute_analysis_pipeline(force=True))


def run_buy_execution_now(force: bool = False) -> bool:
    """매수 큐 즉시 집행. force=True 면 장 시간 가드도 무시."""
    return _run_in_thread(lambda: kr_scheduler.execute_buy_queue(force=force))


def run_auto_sell_now() -> bool:
    """매도 판단 즉시 실행."""
    return _run_in_thread(lambda: kr_scheduler.execute_auto_sell())


def run_report_now() -> bool:
    """
    직전 분석 파이프라인의 원자료로 리포트만 다시 만들어 보낸다 (수동 재발송용).

    LLM 호출 + PDF 생성이라 수십 초가 걸려 별도 스레드에서 돌린다. 서버 재기동 등으로
    원자료가 비어 있으면 매수 예약이 없는 리포트가 나오므로, 그때는 분석 파이프라인을
    다시 돌리는 편이 맞다.
    """

    def _runner():
        try:
            kr_report_service.build_and_send_report(kr_scheduler._artifacts)
        except Exception as e:
            logger.error(f"수동 리포트 생성 실패: {e}", exc_info=True)

    threading.Thread(target=_runner, daemon=True).start()
    return True


def run_sell_review_now() -> bool:
    """보유 종목 LLM 매도검토 즉시 실행 (동기 함수라 별도 스레드에서 직접 실행)."""

    def _runner():
        try:
            kr_scheduler._build_sell_review()
        except Exception as e:
            logger.error(f"수동 매도검토 실행 중 오류: {e}", exc_info=True)

    threading.Thread(target=_runner, daemon=True).start()
    return True
