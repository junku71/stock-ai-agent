"""
국내주식(KOSPI 100) 자동매매 스케줄러.

미국 트랙과 시간 구조가 다르다. 미국은 KST 21:00 파이프라인이 곧 NY 장 시작 전이라
분석과 매수를 한 번에 끝낼 수 있지만, 한국 증시는 15:30 KST 에 닫히므로
장 마감 후 분석 시점에는 주문을 낼 수 없다. 그래서 2단계로 나눴다.

  Phase A — 분석 (평일 KR_ANALYSIS_TIME, 기본 16:30 KST)
     1) 시장 데이터 수집 (지수/환율/글로벌/30종목 종가/ECOS)
     2) Kaggle ML 예측 (국내 전용 커널)
     3) 기술적 지표 + 네이버 뉴스 감성 + 수급
     4) LLM 최종 검토 → kr_buy_queue 에 '다음 개장일 매수 예약' 저장 + Slack 보고

  Phase B — 집행 (평일 KR_EXECUTION_TIME, 기본 09:05 KST)
     큐를 읽어 현재가 재조회 → 수량 재계산 → 지정가 매수 주문

  매도 감시 — 1분마다, 09:00~15:20 KST
     ATR 익절/손절 + 기술적 매도 신호 + 공포장 조건
     동시에 KIS 원장과 kr_trade_records 정합성을 맞춘다 (체결 확인 / 미체결 정리)

전역 schedule 큐를 미국 트랙과 공유하면 두 스레드가 run_pending() 을 동시에 돌려
같은 잡이 중복 실행될 수 있다. 그래서 KR 전용 Scheduler 인스턴스를 따로 만든다.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

import pytz
import schedule

from app.core.config import settings
from app.db.supabase import supabase
from app.services import ml_trigger_service
from app.services.position_sizing import compute_weighted_slots, describe_allocation
from app.services.kr import (
    kis_domestic_service as kis,
    kr_llm_review_service,
    kr_market_data_service,
    kr_notification_service as notify,
    kr_recommendation_service as recommend,
    kr_sentiment_service,
    universe,
)

logger = logging.getLogger("kr_scheduler")

KST = pytz.timezone("Asia/Seoul")

TABLE_QUEUE = "kr_buy_queue"
TABLE_TRADES = "kr_trade_records"

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
        4단계 순차 실행. 실패 시 즉시 중단하고 Slack 장애 알림.

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
        logger.info(f"[1/4] {name}")
        t0 = time.time()
        try:
            result = kr_market_data_service.collect_market_data()
            if not result.get("success"):
                raise RuntimeError(result.get("message", "수집 실패"))
            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(f"[1/4] 완료 ({completed[key]['elapsed_sec']}초) — {result['message']}")
        except Exception as e:
            logger.error(f"[1/4] 실패: {e}", exc_info=True)
            return _fail(key, name, str(e))

        # ── Step 2: Kaggle ML ────────────────────────────────
        name, key = "Kaggle ML 예측 (국내 커널)", "2_kaggle_ml"
        logger.info(f"[2/4] {name}")
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
            logger.info(f"[2/4] 완료 ({completed[key]['elapsed_sec']}초)")
        except Exception as e:
            logger.error(f"[2/4] 실패: {e}", exc_info=True)
            return _fail(key, name, str(e))

        # ── Step 3: 기술 지표 + 뉴스 감성 ────────────────────
        name, key = "기술적 지표 + 뉴스 감성 분석", "3_tech_sentiment"
        logger.info(f"[3/4] {name}")
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
            logger.info(f"[3/4] 완료 ({completed[key]['elapsed_sec']}초)")
        except Exception as e:
            logger.error(f"[3/4] 실패: {e}", exc_info=True)
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

        # ── Step 4: LLM 검토 → 매수 큐 ───────────────────────
        name, key = "LLM 최종 검토 + 매수 예약", "4_llm_queue"
        logger.info(f"[4/4] {name}")
        t0 = time.time()
        try:
            queued = self._build_buy_queue()
            completed[key] = {"step_name": name, "elapsed_sec": int(time.time() - t0)}
            logger.info(f"[4/4] 완료 ({completed[key]['elapsed_sec']}초) — 예약 {len(queued)}건")
        except Exception as e:
            logger.error(f"[4/4] 실패: {e}", exc_info=True)
            return _fail(key, name, str(e))

        total = int(time.time() - started)
        logger.info(f"===== 국내 분석 파이프라인 완료 (총 {total}초) =====")
        return {
            "success": True,
            "failed_step": None,
            "completed_steps": completed,
            "queued_count": len(queued),
            "total_elapsed_sec": total,
        }

    def _build_buy_queue(self) -> List[dict]:
        """매수 후보 산출 → LLM 검토 → kr_buy_queue 저장 → Slack 보고."""
        combined = recommend.get_buy_candidates()
        candidates = combined.get("results", [])
        market = combined.get("market", {})

        if not candidates:
            logger.info(f"  매수 후보 없음: {combined.get('message')}")
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

        # 슬롯 제한 — 최대 보유 종목 수를 넘지 않게 상위 점수만
        current_positions = len(held)
        room = max(settings.KR_MAX_POSITIONS - current_positions, 0)
        if len(approved) > room:
            logger.info(
                f"  보유 한도({settings.KR_MAX_POSITIONS}) 적용: "
                f"현재 {current_positions}종목 → 상위 {room}개만 예약"
            )
            approved = approved[:room]

        self._replace_queue(approved)

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
        # 현금 부족 시 비례 축소 (미국 트랙과 동일 정책)
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

                # ATR 안전장치 — 익절/손절선 없이는 매수하지 않는다 (미국 트랙과 동일 정책)
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

        result = recommend.get_sell_candidates(balance=balance)
        candidates = result.get("sell_candidates", [])
        if not candidates:
            return {"sold": 0}

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

        if not candidates:
            return {"sold": 0}

        logger.info(f"매도 대상 {len(candidates)}개 식별")
        sold = 0

        for c in candidates:
            code, name, qty = c["code"], c["stock_name"], c["quantity"]
            try:
                logger.info(f"  {name}({code}) 매도 근거: {'; '.join(c['sell_reasons'])}")

                current = kis.get_current_price_value(code)
                if current is None:
                    logger.error(f"  {name}({code}) 현재가 조회 실패 → 다음 회차 재시도")
                    continue

                # 매도 지정가: 호가단위로 내림 (체결 확률 우선)
                order_price = kis.round_to_tick(current, mode="down")

                reason = "signal"
                for r in c["sell_reasons"]:
                    if "익절" in r:
                        reason = "take_profit"
                        break
                    if "손절" in r:
                        reason = "stop_loss"
                        break
                    if "패닉셀" in r:
                        reason = "panic_sell"
                        break
                    if "순매도" in r:
                        reason = "flow_out"
                        break

                result_order = kis.order_stock(code, qty, order_price, is_buy=False)
                if result_order.get("rt_cd") != "0":
                    continue

                sold += 1
                buy_price = c.get("buy_price") or 0
                pnl = (order_price - buy_price) * qty if buy_price > 0 else None
                pnl_pct = (
                    (order_price - buy_price) / buy_price * 100 if buy_price > 0 else None
                )

                try:
                    supabase.table(TABLE_TRADES).update(
                        {
                            "status": "sell_ordered",
                            "sell_price": order_price,
                            "sell_date": datetime.now(KST).isoformat(),
                            "sell_reason": reason,
                            "profit_loss": round(pnl, 0) if pnl is not None else None,
                            "profit_loss_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                        }
                    ).eq("code", code).eq("status", "holding").eq(
                        "account_type", kis.current_account_type()
                    ).execute()
                except Exception as e:
                    logger.error(f"  {name}({code}) 거래기록 갱신 실패: {e}")

                try:
                    notify.notify_sell_ordered(code, name, qty, order_price, reason)
                except Exception as e:
                    logger.warning(f"  매도 주문 알림 발송 실패: {e}")

            except Exception as e:
                logger.error(f"  {name}({code}) 매도 처리 중 오류: {e}", exc_info=True)

        return {"sold": sold}

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
                .in_("status", ["buy_ordered", "holding", "sell_ordered"])
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
                        supabase.table(TABLE_TRADES).update(
                            {"status": "holding", "holding_quantity": kis_qty}
                        ).eq("id", rec_id).execute()
                        ordered_qty = rec.get("quantity") or 0
                        if kis_qty < ordered_qty:
                            logger.info(
                                f"  {name}({code}) 부분 체결 → holding "
                                f"(주문 {ordered_qty}주 / 체결 {kis_qty}주)"
                            )
                        else:
                            logger.info(f"  {name}({code}) 매수 체결 확인 → holding ({kis_qty}주)")

                        try:
                            item = kis_holdings[code]["item"]
                            fill = float(
                                item.get("pchs_avg_pric", 0) or rec.get("buy_price") or 0
                            )
                            notify.notify_buy_filled(
                                code=code,
                                stock_name=name,
                                qty=kis_qty,
                                fill_price=fill,
                                take_profit_price=rec.get("take_profit_price"),
                                stop_loss_price=rec.get("stop_loss_price"),
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
