"""국내주식(KOSPI 100) 트랙 API 라우트."""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from app.core.config import settings
from app.services.kr import kr_scoring
from app.services.kr import (
    kis_domestic_service as kis,
    kr_market_data_service,
    kr_override_service,
    kr_recommendation_service as recommend,
    kr_sentiment_service,
    naver_service,
    universe,
)
from app.utils.kr_scheduler import (
    get_kr_scheduler_status,
    run_analysis_now,
    run_auto_sell_now,
    run_buy_execution_now,
    start_kr_scheduler,
    stop_kr_scheduler,
)

router = APIRouter()


# ══════════════════════════════════════════════════════════════════
# 상태 / 유니버스
# ══════════════════════════════════════════════════════════════════

@router.get("/status", summary="국내 트랙 상태 조회")
def status():
    """스케줄러 상태 + 계좌 모드 + 외부 API 설정 여부를 한 번에 확인합니다."""
    return {
        "scheduler": get_kr_scheduler_status(),
        "config": {
            "kr_enabled": settings.KR_ENABLED,
            "dry_run": settings.KR_DRY_RUN,
            "use_mock_account": settings.KIS_USE_MOCK,
            "slot_ratio": settings.KR_SLOT_RATIO,
            "max_positions": settings.KR_MAX_POSITIONS,
            "sentiment_model": settings.KR_SENTIMENT_MODEL,
        },
        "integrations": {
            "naver_api_hub": naver_service.is_configured(),
            "ecos": bool(settings.ECOS_API_KEY),
            "krx_open_api": bool(settings.KRX_AUTH_KEY),
            "anthropic": bool(settings.ANTHROPIC_API_KEY),
            "slack": bool(settings.SLACK_WEBHOOK_URL),
        },
        "market": {
            "is_business_day": kis.is_business_day(),
            "is_market_open": kis.is_market_open(),
            "is_sell_window": kis.is_sell_window(),
        },
    }


@router.get("/universe", summary="KOSPI 100 유니버스 조회")
def get_universe():
    return {"count": len(universe.UNIVERSE), "stocks": universe.UNIVERSE}


@router.post("/scheduler/start", summary="국내 스케줄러 시작")
def scheduler_start():
    return {"started": start_kr_scheduler(), "status": get_kr_scheduler_status()}


@router.post("/scheduler/stop", summary="국내 스케줄러 중지")
def scheduler_stop():
    return {"stopped": stop_kr_scheduler()}


# ══════════════════════════════════════════════════════════════════
# 데이터 수집
# ══════════════════════════════════════════════════════════════════

@router.post("/market-data/collect", summary="시장 데이터 수집 (증분)")
def collect_market_data(
    background_tasks: BackgroundTasks,
    full: bool = Query(False, description="true 면 2006년부터 전체 재수집"),
    wait: bool = Query(False, description="true 면 완료까지 대기하고 결과 반환"),
):
    """
    지수·환율·글로벌 지표·KOSPI100 종가·한국은행 거시지표를 수집해
    kr_economic_and_stock_data 에 저장합니다.

    전체 재수집(full=true)은 수십 분이 걸리므로 기본은 백그라운드 실행입니다.
    """
    if wait:
        try:
            return kr_market_data_service.collect_market_data(force_full=full)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"시장 데이터 수집 오류: {e}")

    background_tasks.add_task(kr_market_data_service.collect_market_data, full)
    return {"message": "백그라운드에서 시장 데이터 수집을 시작했습니다", "force_full": full}


@router.get("/market-data/context", summary="현재 시장 환경 조회")
def market_context():
    """코스피, 20일 실현변동성(한국판 공포지수), 원/달러, VIX."""
    return kr_market_data_service.get_market_context()


@router.get("/economic/ecos-check", summary="ECOS 통계코드 유효성 진단")
def ecos_check():
    """
    한국은행이 통계항목코드를 개편하면 특정 거시지표만 조용히 비게 됩니다.
    어떤 시리즈가 살아있는지 확인하고, 실패한 코드를
    app/services/kr/kr_market_data_service.py 의 ECOS_SERIES 에서 고치세요.
    """
    if not settings.ECOS_API_KEY:
        raise HTTPException(status_code=400, detail="ECOS_API_KEY 가 설정되지 않았습니다")
    return {"series": kr_market_data_service.check_ecos_series()}


# ══════════════════════════════════════════════════════════════════
# 분석
# ══════════════════════════════════════════════════════════════════

@router.post("/technical/generate", summary="기술적 지표 생성")
def generate_technical():
    """KOSPI100 각 종목의 SMA/RSI/MACD/ADX/ATR/거래량비율/수급을 계산해 저장합니다."""
    try:
        return recommend.generate_technical_recommendations()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"기술적 지표 생성 오류: {e}")


@router.post("/sentiment/collect", summary="네이버 뉴스 감성 분석 수집")
def collect_sentiment():
    """
    NAVER API Hub 로 종목별 최근 뉴스를 모으고 Claude 로 -1~+1 감성 점수를 산출합니다.
    (네이버 검색 API 는 감성 점수를 제공하지 않아 직접 스코어링합니다)
    """
    try:
        return kr_sentiment_service.fetch_and_store_sentiment()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"감성 분석 오류: {e}")


@router.get("/sentiment", summary="저장된 감성 점수 조회")
def get_sentiment():
    data = kr_sentiment_service.get_sentiment_map()
    return {"count": len(data), "results": list(data.values())}


@router.get("/news/{code}", summary="종목 뉴스 검색 (원문 확인용)")
def get_news(
    code: str,
    days: int = Query(3, ge=1, le=30, description="최근 N일"),
):
    """감성 점수의 근거가 된 기사를 직접 확인할 때 씁니다."""
    resolved = universe.resolve(code)
    query = universe.news_query(resolved) if resolved else code
    articles = naver_service.search_recent_news(query, days=days)
    return {
        "code": resolved or code,
        "query": query,
        "count": len(articles),
        "articles": [
            {
                "title": a["title"],
                "description": a["description"],
                "link": a["link"],
                "published_at": a["pub_date"].isoformat() if a["pub_date"] else None,
            }
            for a in articles
        ],
    }


@router.get("/candidates/buy", summary="매수 후보 조회 (LLM 검토 전)")
def buy_candidates():
    """ML + 기술 + 감성 + 수급을 통합해 채점한 매수 후보를 반환합니다."""
    try:
        return recommend.get_buy_candidates()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"매수 후보 조회 오류: {e}")


@router.get("/candidates/sell", summary="매도 후보 조회")
def sell_candidates():
    try:
        return recommend.get_sell_candidates()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"매도 후보 조회 오류: {e}")


# ══════════════════════════════════════════════════════════════════
# 변동성 게이트 수동 오버라이드
# ══════════════════════════════════════════════════════════════════

@router.get("/fear-gate", summary="변동성 게이트 상태 조회")
def fear_gate_status():
    """현재 변동성, 적용 임계값, 수동 해제 여부를 함께 보여줍니다."""
    market = kr_market_data_service.get_market_context()
    fear = market.get("kospi_vol_20d")
    override = kr_override_service.describe()
    blocked = (
        fear is not None
        and fear > kr_scoring.FEAR_HARD_BLOCK
        and not override["active"]
    )
    return {
        "kospi_vol_20d": fear,
        "hard_block_at": kr_scoring.FEAR_HARD_BLOCK,
        "threshold": kr_scoring.get_threshold(
            None if override.get("relax_threshold") else fear
        ),
        "buy_blocked": blocked,
        "override": override,
    }


@router.post("/fear-gate/override", summary="변동성 게이트 수동 해제 (운영자 개입)")
def fear_gate_override(
    confirm: str = Query(..., description='확인 문구. 정확히 "OVERRIDE" 를 입력'),
    reason: str = Query(..., description="개입 사유 (5자 이상, 감사 로그에 기록됨)"),
    minutes: int = Query(120, ge=1, le=1440, description="유효 시간(분). 최대 24시간"),
    relax_threshold: bool = Query(
        False,
        description="true 면 변동성 기반 임계값 상향까지 끄고 평온장 기준(0.40)으로 채점",
    ),
):
    """
    공포장 매수 차단(하드블록)을 한시적으로 해제합니다.

    ### 안전장치
    - `confirm="OVERRIDE"` 정확 일치 필수
    - 사유 5자 이상 필수 (감사 로그)
    - 지정 시간이 지나면 **자동 복구** (기본 120분, 최대 24시간)
    - 발급/해제 시 Slack 즉시 알림
    - 새로 발급하면 기존 오버라이드는 자동 만료

    ### 기본 동작
    하드블록만 해제하고 **변동성 기반 선별도는 유지**합니다. 즉 폭락장에서는
    여전히 임계값이 높아 상위 종목만 통과합니다.
    `relax_threshold=true` 는 그 선별도까지 끄는 옵션으로, 파이프라인 전 구간을
    관통시켜 확인할 때만 쓰고 실매매에서는 권장하지 않습니다.
    """
    try:
        record = kr_override_service.create_override(
            confirm=confirm,
            reason=reason,
            minutes=minutes,
            relax_threshold=relax_threshold,
        )
    except kr_override_service.OverrideError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "변동성 게이트를 한시적으로 해제했습니다", "override": record}


@router.post("/fear-gate/revoke", summary="변동성 게이트 수동 해제 취소")
def fear_gate_revoke(
    note: str = Query("수동 해제", description="해제 사유 (감사 로그)"),
):
    """활성 오버라이드를 즉시 종료하고 게이트를 복구합니다."""
    return kr_override_service.revoke_override(note=note)


# ══════════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════════

@router.post("/pipeline/analysis", summary="분석 파이프라인 즉시 실행")
def run_analysis():
    """
    Step 1 시장데이터 → 2 Kaggle ML → 3 기술지표+감성 → 4 LLM 검토 → 매수 큐 저장.
    백그라운드 실행이며 진행 상황은 Slack 과 로그로 확인합니다. (수 분~15분 소요)
    """
    run_analysis_now()
    return {"message": "분석 파이프라인을 백그라운드에서 시작했습니다"}


@router.post("/pipeline/execute-buy", summary="매수 큐 즉시 집행")
def execute_buy(
    force: bool = Query(False, description="true 면 장 시간/휴장일 가드를 무시"),
):
    """kr_buy_queue 의 pending 예약을 현재가 기준으로 집행합니다."""
    run_buy_execution_now(force=force)
    return {"message": "매수 집행을 백그라운드에서 시작했습니다", "force": force}


@router.post("/pipeline/execute-sell", summary="매도 판단 즉시 실행")
def execute_sell():
    run_auto_sell_now()
    return {"message": "매도 판단을 백그라운드에서 시작했습니다"}


# ══════════════════════════════════════════════════════════════════
# 계좌
# ══════════════════════════════════════════════════════════════════

@router.get("/balance", summary="국내주식 잔고 조회")
def get_balance():
    result = kis.get_balance()
    if result.get("rt_cd") != "0":
        raise HTTPException(status_code=400, detail=result.get("msg1", "잔고 조회 실패"))
    return {
        "account_type": kis.current_account_type(),
        "holdings": result["output1"],
        "summary": kis.get_account_summary(),
    }


@router.get("/price/{code}", summary="종목 현재가 조회")
def get_price(code: str):
    resolved = universe.resolve(code) or code
    result = kis.get_price(resolved)
    if result.get("rt_cd") != "0":
        raise HTTPException(status_code=400, detail=result.get("msg1", "현재가 조회 실패"))
    return {"code": resolved, "name": universe.CODE_TO_NAME.get(resolved), **result}
