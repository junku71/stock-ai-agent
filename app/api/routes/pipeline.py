"""
일일 매수 파이프라인 통합 API

기존 별도 호출하던 두 API를 하나로 통합:
  - POST /stocks/recommendations/recommended-stocks/generate-technical-recommendations
  - POST /llm/review-buy-candidates

자동화 흐름(documents/07_자동화_방안.md Phase 1) 과 동일하게 감성 분석까지 포함.

추가:
  - Kaggle API 연동 (documents/08_Kaggle_API_연동.md)
    /pipeline/kaggle/auth-check, /pipeline/kaggle/status, /pipeline/kaggle/trigger-ml
"""
import asyncio
import time
import logging
from fastapi import APIRouter, HTTPException
from app.services.stock_recommendation_service import StockRecommendationService
from app.services.llm_review_service import review_buy_candidates
from app.services import ml_trigger_service
from app.utils.scheduler import _execute_daily_pipeline

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/run-buy-pipeline",
    response_model=dict,
    summary="기술지표 + 감성분석 + LLM 검토 통합 실행",
)
async def run_buy_pipeline():
    """
    매수 후보 산출 + LLM 최종 검토를 하나의 API로 순차 실행합니다.

    ## 실행 순서

      1. **기술 지표 생성** (`generate_technical_recommendations`)
         - SMA20/50, RSI, MACD, ADX, 거래량 비율, 당일 변동률 계산
         - `stock_recommendations` 테이블 갱신

      2. **뉴스 감성 분석** (`fetch_and_store_sentiment_for_recommendations`)
         - AlphaVantage NEWS_SENTIMENT API 호출
         - 추천 종목 + 보유 종목 대상
         - `ticker_sentiment_analysis` 테이블 갱신

      3. **통합 매수 후보 추출** (`get_combined_recommendations_with_technical_and_sentiment`)
         - composite_score 계산 (ML 25% / 기술 25% / 감성 20% / 거래량 15% / ADX 10% / VIX 5%)
         - 하드블록: VIX > 35, RSI > 80
         - 통과선: composite_score >= 0.3

      4. **LLM 최종 검토** (`review_buy_candidates`)
         - Claude Opus 4.7 → 실패 시 Sonnet 4.6 폴백
         - 거부권 행사 (BUY → HOLD 변경만 가능)
         - `llm_decision_logs` 테이블에 결과 저장

    ## 예상 소요 시간

    - 약 2~3분 (감성 분석 단계가 종목당 5초 sleep으로 가장 느림)

    ## 참고

    - 자동 매수 주문은 별도 스케줄러(`scheduler._execute_auto_buy`)가 NY 10:30 ET에 실행.
      이 API는 LLM 판정만 갱신함 (실제 매수 주문은 발생하지 않음).
    - 자동화 흐름은 `documents/07_자동화_방안.md` Phase 1 참조.
    """
    pipeline_start = time.time()
    steps_summary = {}

    try:
        service = StockRecommendationService()

        # ──────────────────────────────────────────────────
        # Step 1: 기술 지표 생성
        # ──────────────────────────────────────────────────
        logger.info("[1/4] 기술 지표 생성 시작")
        step_start = time.time()
        tech_results = service.generate_technical_recommendations()
        tech_elapsed = time.time() - step_start
        logger.info(
            f"[1/4] 기술 지표 완료 ({tech_elapsed:.1f}초): {tech_results['message']}"
        )
        steps_summary["1_technical_analysis"] = {
            "message": tech_results["message"],
            "count": len(tech_results.get("data", [])),
            "elapsed_sec": round(tech_elapsed, 1),
        }

        # ──────────────────────────────────────────────────
        # Step 1.5: 실적 캘린더 수집 (전용 키, 1회 호출, best-effort)
        #   수동 실행 시에도 옛 DB 값이 아닌 그 시점 실적을 LLM에 반영하기 위함
        # ──────────────────────────────────────────────────
        try:
            earnings_result = service.fetch_and_store_earnings_calendar()
            logger.info(f"[1.5] 실적 캘린더 수집: {earnings_result.get('message', '')}")
            steps_summary["1.5_earnings_calendar"] = {
                "message": earnings_result.get("message", ""),
                "count": earnings_result.get("count", 0),
            }
        except Exception as e:
            logger.warning(f"[1.5] 실적 캘린더 수집 실패(무시): {e}")
            steps_summary["1.5_earnings_calendar"] = {"message": f"실패(무시): {e}", "count": 0}

        # ──────────────────────────────────────────────────
        # Step 2: 뉴스 감성 분석
        # ──────────────────────────────────────────────────
        logger.info("[2/4] 뉴스 감성 분석 시작")
        step_start = time.time()
        sentiment_results = service.fetch_and_store_sentiment_for_recommendations()
        sentiment_elapsed = time.time() - step_start
        logger.info(
            f"[2/4] 뉴스 감성 분석 완료 ({sentiment_elapsed:.1f}초): "
            f"{sentiment_results['message']}"
        )
        steps_summary["2_sentiment_analysis"] = {
            "message": sentiment_results["message"],
            "count": len(sentiment_results.get("results", [])),
            "elapsed_sec": round(sentiment_elapsed, 1),
        }

        # ──────────────────────────────────────────────────
        # Step 3: 통합 매수 후보 추출 (composite_score 계산)
        # ──────────────────────────────────────────────────
        logger.info("[3/4] 매수 후보 추출 시작")
        step_start = time.time()
        recommendations = service.get_combined_recommendations_with_technical_and_sentiment()
        candidates = recommendations.get("results", [])
        extract_elapsed = time.time() - step_start
        logger.info(
            f"[3/4] 매수 후보 추출 완료 ({extract_elapsed:.1f}초): {len(candidates)}개"
        )
        steps_summary["3_candidate_extraction"] = {
            "message": recommendations.get("message", ""),
            "count": len(candidates),
            "elapsed_sec": round(extract_elapsed, 1),
        }

        # 후보가 없으면 LLM 호출 스킵
        if not candidates:
            total_elapsed = time.time() - pipeline_start
            logger.info(
                f"매수 후보 없음 → LLM 검토 스킵 (총 {total_elapsed:.1f}초)"
            )
            return {
                "message": "매수 후보가 없어 LLM 검토를 건너뜁니다",
                "steps": steps_summary,
                "candidates_before_llm": 0,
                "candidates_after_llm": 0,
                "held": 0,
                "results": [],
                "held_results": [],
                "llm_reasoning": "",
                "total_elapsed_sec": round(total_elapsed, 1),
            }

        # ──────────────────────────────────────────────────
        # Step 4: LLM 최종 검토
        # ──────────────────────────────────────────────────
        logger.info("[4/4] LLM 검토 시작")
        step_start = time.time()
        vix_value = candidates[0].get("vix_value") if candidates else None
        review_result = review_buy_candidates(candidates, vix_value)
        llm_elapsed = time.time() - step_start
        logger.info(
            f"[4/4] LLM 검토 완료 ({llm_elapsed:.1f}초): "
            f"{len(review_result['reviewed_candidates'])} BUY / "
            f"{len(review_result['held_candidates'])} HOLD"
        )
        steps_summary["4_llm_review"] = {
            "buy_count": len(review_result["reviewed_candidates"]),
            "hold_count": len(review_result["held_candidates"]),
            "elapsed_sec": round(llm_elapsed, 1),
        }

        # ──────────────────────────────────────────────────
        # 최종 응답
        # ──────────────────────────────────────────────────
        total_elapsed = time.time() - pipeline_start
        logger.info(f"통합 파이프라인 전체 완료 (총 {total_elapsed:.1f}초)")

        return {
            "message": (
                f"통합 파이프라인 완료 - "
                f"BUY {len(review_result['reviewed_candidates'])}개 / "
                f"HOLD {len(review_result['held_candidates'])}개"
            ),
            "steps": steps_summary,
            "candidates_before_llm": len(candidates),
            "candidates_after_llm": len(review_result["reviewed_candidates"]),
            "held": len(review_result["held_candidates"]),
            "results": review_result["reviewed_candidates"],
            "held_results": review_result["held_candidates"],
            "llm_reasoning": review_result["llm_reasoning"],
            "total_elapsed_sec": round(total_elapsed, 1),
        }

    except Exception as e:
        logger.error(f"통합 파이프라인 중 오류 발생: {e}", exc_info=True)
        import traceback
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"통합 파이프라인 중 오류 발생: {str(e)}",
        )


# ══════════════════════════════════════════════════════════════════
# 실적 캘린더 (documents/17_실적캘린더_연동_기획.md)
# ══════════════════════════════════════════════════════════════════

@router.get(
    "/earnings/preview",
    response_model=dict,
    summary="실적 캘린더 미리보기 (DB 저장 안 함)",
)
async def earnings_preview():
    """
    ALPHA_VANTAGE_API_KEY_EARNINGS 로 EARNINGS_CALENDAR 를 1회 호출하여
    우리 유니버스(추천 종목 + 보유 종목)로 필터링한 결과만 반환합니다. **DB 미저장**.

    키/응답/필터 동작을 안전하게 점검하기 위한 용도.
    - 키가 없거나 rate-limit 이면 예외 없이 count=0 으로 반환.
    """
    service = StockRecommendationService()
    rows = await asyncio.to_thread(service.preview_earnings_calendar)
    return {"count": len(rows), "results": rows}


@router.post(
    "/earnings/fetch",
    response_model=dict,
    summary="실적 캘린더 수집 + earnings_calendar 테이블 저장",
)
async def earnings_fetch():
    """
    fetch_and_store_earnings_calendar() 를 실행하여 earnings_calendar 테이블을 갱신합니다 (실제 DB 저장).
    전체 삭제 후 삽입 방식. best-effort 라 실패해도 500 대신 count=0 으로 반환.
    """
    service = StockRecommendationService()
    result = await asyncio.to_thread(service.fetch_and_store_earnings_calendar)
    return result


# ══════════════════════════════════════════════════════════════════
# Kaggle API 연동 (documents/08_Kaggle_API_연동.md)
# ══════════════════════════════════════════════════════════════════

@router.get(
    "/kaggle/auth-check",
    response_model=dict,
    summary="Kaggle 인증 확인",
)
async def kaggle_auth_check():
    """
    .env 의 KAGGLE_USERNAME / KAGGLE_KEY 로 Kaggle CLI 인증이 정상 작동하는지 확인.
    내부적으로 `kaggle kernels list -m --page-size 1` 호출.
    """
    try:
        ok, msg = await asyncio.to_thread(ml_trigger_service.check_auth)
        return {"ok": ok, "message": msg}
    except RuntimeError as e:
        # KAGGLE_USERNAME/KEY 미설정 등
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Kaggle 인증 확인 중 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/kaggle/status",
    response_model=dict,
    summary="Kaggle 노트북 실행 상태 조회",
)
async def kaggle_status():
    """
    현재 Kaggle 노트북의 실행 상태 조회.
    Returns: queued / running / complete / error / cancel_* / unknown
    """
    try:
        status = await asyncio.to_thread(ml_trigger_service.get_status)
        return {
            "kernel": f"{ml_trigger_service.settings.KAGGLE_USERNAME}/"
                      f"{ml_trigger_service.settings.KAGGLE_KERNEL_SLUG}",
            "status": status,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Kaggle 상태 조회 중 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/kaggle/trigger-ml",
    response_model=dict,
    summary="Kaggle ML 노트북 트리거 + 완료 대기 (최대 15분)",
)
async def kaggle_trigger_ml(max_wait_sec: int = 900):
    """
    Kaggle 노트북을 push 하여 학습 + 예측을 트리거하고, 완료될 때까지 대기.

    ## 동작
      1. `kaggle kernels push -p kaggle_notebook` 실행 → 새 버전 생성 + 자동 실행
      2. 10초 간격으로 `kaggle kernels status` 폴링
      3. 'complete' / 'error' / timeout 도달 시 결과 반환

    ## 주의
      - 보통 5~10분 소요 (큐 대기 30초~2분 + GPU 학습 5~7분)
      - 클라이언트 timeout 을 충분히(>15분) 잡아야 함
      - 비동기 백그라운드 실행을 원하면 별도 BackgroundTasks 패턴으로 분리 가능

    ## 사전 조건
      - .env: KAGGLE_USERNAME, KAGGLE_KEY 설정
      - 프로젝트 루트에 `kaggle_notebook/` 폴더 + `kernel-metadata.json` + `predict.py` 존재
      - 노트북은 한 번 수동으로 push 되어 Kaggle에 등록되어 있어야 함

    ## 참고
      - documents/08_Kaggle_API_연동.md 의 "5. FastAPI 통합" 섹션
    """
    try:
        success, message, meta = await asyncio.to_thread(
            ml_trigger_service.trigger_and_wait,
            ml_trigger_service.POLL_INTERVAL_SEC,
            max_wait_sec,
        )

        if not success:
            # 실패도 200으로 반환 (운영자가 meta 보고 판단)
            return {
                "success": False,
                "message": message,
                "meta": meta,
            }

        return {
            "success": True,
            "message": message,
            "meta": meta,
        }
    except RuntimeError as e:
        # 환경변수 누락 등
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Kaggle 트리거 중 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════
# 일일 통합 파이프라인 (4단계 순차 실행)
# ══════════════════════════════════════════════════════════════════

@router.post(
    "/run-full-daily",
    response_model=dict,
    summary="4단계 일일 파이프라인 통합 실행 (경제데이터 → Kaggle ML → 기술지표+감성 → LLM+매수)",
)
async def run_full_daily():
    """
    매일 한 번 실행되는 전체 파이프라인.

    ## 실행 순서

      Step 1) 경제 데이터 + 주가 수집
              update_economic_data_in_background(force=True)
              → economic_and_stock_data 테이블 갱신

      Step 2) Kaggle ML 예측 (5~10분)
              ml_trigger_service.trigger_and_wait()
              → predicted_stocks + stock_analysis_results 테이블 갱신

      Step 3) 기술 지표 + 뉴스 감성 분석
              service.generate_technical_recommendations()
              service.fetch_and_store_sentiment_for_recommendations()
              → stock_recommendations + ticker_sentiment_analysis 테이블 갱신

      Step 4) LLM 검토 + KIS 매수 주문
              stock_scheduler._execute_auto_buy(force=True)
              → trade_records 테이블 갱신 + KIS 모의/실투자 매수 주문

    ## 에러 정책

      - 단계 중 하나라도 실패하면 **HTTPException 500 으로 즉시 중단**
      - 응답 detail 에 어느 단계에서 실패했는지 명시 (`failed_step`, `step_name`, `error`)
      - 이미 성공한 단계는 `completed_steps` 에 기록되어 함께 반환

    ## 자동 스케줄

      - 매일 KST 21:00 자동 실행 (`scheduler._run_daily_pipeline`)
      - 이 API 는 그 동일 로직을 즉시 호출하는 수동 트리거

    ## 예상 소요 시간

      약 8~13분 (Step 2 가 가장 느림)

    ## 구현 노트

      4단계 로직은 `app/utils/scheduler.py:_execute_daily_pipeline()` 에 단일하게 정의되어 있고,
      이 엔드포인트 + KST 21:00 자동 스케줄러 둘 다 같은 함수를 호출. (중복 제거)
    """
    result = await _execute_daily_pipeline()

    if not result["success"]:
        raise HTTPException(
            status_code=500,
            detail={
                "failed_step": result["failed_step"],
                "step_name": result["step_name"],
                "error": result["error"],
                "elapsed_sec": result["total_elapsed_sec"],
                "completed_steps": result["completed_steps"],
            },
        )

    return {
        "success": True,
        "message": "전체 파이프라인 4단계 모두 성공",
        "total_elapsed_sec": result["total_elapsed_sec"],
        "steps": result["completed_steps"],
    }
