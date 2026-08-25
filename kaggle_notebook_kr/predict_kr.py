"""
국내주식(KOSPI 100) 주가 예측 모델 (Transformer) — Kaggle 실행용.

2-input Transformer (LOOKBACK 90 / FORECAST_HORIZON 14) 구조:

  - 입력 테이블 : kr_economic_and_stock_data
  - 타깃        : KOSPI 100 종목 (원 단위)
  - 피처        : 한국 지수·환율·거시지표 + 글로벌 지표
  - 출력 테이블 : kr_stock_analysis_results (code / stock_name / rise_probability ...)
  - 학습 구간   : 30개 종목이 모두 상장된 이후로 자동 절단
                  (LG에너지솔루션 2022, SK스퀘어 2021 등 신규 상장 종목이 있어
                   전체 구간을 ffill/bfill 하면 상장 전 구간이 가짜 가격으로 채워진다)

SUPABASE_URL / SUPABASE_KEY 는 ml_trigger_service 가 push 직전에
노트북 첫 셀로 주입한다.
"""
import os
import sys
import time
import subprocess

subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "supabase"])

import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error
from supabase import create_client, Client

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.layers import (
    Input, Dense, Dropout, LayerNormalization, MultiHeadAttention, Add, GlobalAveragePooling1D
)
from tensorflow.keras.optimizers import Adam


# ============================================================
# 환경 / 연결
# ============================================================
def check_environment():
    print("=" * 60)
    print("환경 확인")
    print("=" * 60)
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  TensorFlow: {tf.__version__}")
    gpus = tf.config.list_physical_devices("GPU")
    print(f"  GPU: {len(gpus)}개 감지" if gpus else "  GPU: 없음 (CPU로 실행)")
    print()


SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    try:
        from kaggle_secrets import UserSecretsClient

        user_secrets = UserSecretsClient()
        SUPABASE_URL = SUPABASE_URL or user_secrets.get_secret("SUPABASE_URL")
        for _name in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"):
            if SUPABASE_KEY:
                break
            try:
                SUPABASE_KEY = user_secrets.get_secret(_name)
            except Exception:
                continue
    except Exception as e:
        print(f"  UserSecretsClient 로드 실패: {e}")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL / SUPABASE_(SERVICE_ROLE_)KEY 환경변수가 없습니다. "
        "ml_trigger_service 가 주입하거나 Kaggle Secrets 에 등록해야 합니다."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================================
# 설정
# ============================================================
LOOKBACK = 90
FORECAST_HORIZON = 14
EPOCHS = 50
BATCH_SIZE = 32
LEARNING_RATE = 0.0001

# 학습에 최소한 확보해야 할 행 수 (테이블은 달력일 기준이라 거래일보다 약 1.45배 많다).
# 이보다 짧아지는 원인이 되는 종목은 타깃에서 자동 제외한다.
#
# 값 선정 근거 (2026-08 실측, 유니버스 100종목 / 5년 수집 기준):
#     MIN    채택종목   학습행   샘플
#     900      97       908     804
#    1100      96      1669    1565   ← 종목 1개(2024년 상장) 더 빼고 샘플 2배
#    1500      96      1669    1565
#    1700      95      1728    1624   ← 종목 하나 더 빼도 이득 미미
# 1100~1500 이 안정 구간이라 중앙값을 쓴다. 타깃이 100개인 Transformer 에
# 800 샘플은 과적합 위험이 커서 샘플 수를 우선했다.
MIN_TRAIN_ROWS = 1200

SOURCE_TABLE = "kr_economic_and_stock_data"
RESULT_TABLE = "kr_stock_analysis_results"

# KOSPI 100 (app/services/kr/universe.py 와 반드시 일치해야 한다)
TARGET_COLUMNS = [
    "삼성전자", "SK하이닉스", "SK스퀘어", "삼성전기", "현대차",
    "LG에너지솔루션", "삼성바이오로직스", "삼성생명", "삼성물산", "KB금융",
    "한화에어로스페이스", "기아", "신한지주", "HD현대중공업", "두산에너빌리티",
    "현대모비스", "셀트리온", "SK", "삼성SDI", "하나금융지주",
    "NAVER", "LG전자", "삼성화재", "LS ELECTRIC", "HD현대일렉트릭",
    "고려아연", "한화오션", "효성중공업", "HD한국조선해양", "POSCO홀딩스",
    "우리금융지주", "SK텔레콤", "SK이노베이션", "한국전력", "HMM",
    "한미반도체", "메리츠금융지주", "미래에셋증권", "KT&G", "LG화학",
    "삼성중공업", "삼성에스디에스", "두산", "HD현대", "LG",
    "기업은행", "S-Oil", "카카오", "LIG디펜스앤에어로스페이스", "현대글로비스",
    "에이피알", "현대로템", "포스코퓨처엠", "한화시스템", "KT",
    "LG이노텍", "한국항공우주", "현대오토에버", "현대건설", "DB손해보험",
    "GS", "삼양식품", "한국금융지주", "크래프톤", "카카오뱅크",
    "NH투자증권", "대한항공", "LS", "포스코인터내셔널", "HD현대마린솔루션",
    "삼성E&A", "삼성증권", "한국타이어앤테크놀로지", "아모레퍼시픽", "한진칼",
    "이수페타시스", "하이브", "한화솔루션", "키움증권", "LG씨엔에스",
    "코웨이", "유한양행", "SK바이오팜", "대우건설", "LG유플러스",
    "한화", "카카오페이", "두산밥캣", "HD건설기계", "삼성카드",
    "한미약품", "대한전선", "대덕전자", "산일전기", "JB금융지주",
    "오리온", "OCI홀딩스", "NC", "한화생명", "LG디스플레이",
]

STOCK_NAME_TO_CODE = {
    "삼성전자": "005930", "SK하이닉스": "000660", "SK스퀘어": "402340",
    "삼성전기": "009150", "현대차": "005380", "LG에너지솔루션": "373220",
    "삼성바이오로직스": "207940", "삼성생명": "032830", "삼성물산": "028260",
    "KB금융": "105560", "한화에어로스페이스": "012450", "기아": "000270",
    "신한지주": "055550", "HD현대중공업": "329180", "두산에너빌리티": "034020",
    "현대모비스": "012330", "셀트리온": "068270", "SK": "034730",
    "삼성SDI": "006400", "하나금융지주": "086790", "NAVER": "035420",
    "LG전자": "066570", "삼성화재": "000810", "LS ELECTRIC": "010120",
    "HD현대일렉트릭": "267260", "고려아연": "010130", "한화오션": "042660",
    "효성중공업": "298040", "HD한국조선해양": "009540", "POSCO홀딩스": "005490",
    "우리금융지주": "316140", "SK텔레콤": "017670", "SK이노베이션": "096770",
    "한국전력": "015760", "HMM": "011200", "한미반도체": "042700",
    "메리츠금융지주": "138040", "미래에셋증권": "006800", "KT&G": "033780",
    "LG화학": "051910", "삼성중공업": "010140", "삼성에스디에스": "018260",
    "두산": "000150", "HD현대": "267250", "LG": "003550",
    "기업은행": "024110", "S-Oil": "010950", "카카오": "035720",
    "LIG디펜스앤에어로스페이스": "079550", "현대글로비스": "086280", "에이피알": "278470",
    "현대로템": "064350", "포스코퓨처엠": "003670", "한화시스템": "272210",
    "KT": "030200", "LG이노텍": "011070", "한국항공우주": "047810",
    "현대오토에버": "307950", "현대건설": "000720", "DB손해보험": "005830",
    "GS": "078930", "삼양식품": "003230", "한국금융지주": "071050",
    "크래프톤": "259960", "카카오뱅크": "323410", "NH투자증권": "005940",
    "대한항공": "003490", "LS": "006260", "포스코인터내셔널": "047050",
    "HD현대마린솔루션": "443060", "삼성E&A": "028050", "삼성증권": "016360",
    "한국타이어앤테크놀로지": "161390", "아모레퍼시픽": "090430", "한진칼": "180640",
    "이수페타시스": "007660", "하이브": "352820", "한화솔루션": "009830",
    "키움증권": "039490", "LG씨엔에스": "064400", "코웨이": "021240",
    "유한양행": "000100", "SK바이오팜": "326030", "대우건설": "047040",
    "LG유플러스": "032640", "한화": "000880", "카카오페이": "377300",
    "두산밥캣": "241560", "HD건설기계": "267270", "삼성카드": "029780",
    "한미약품": "128940", "대한전선": "001440", "대덕전자": "353200",
    "산일전기": "062040", "JB금융지주": "175330", "오리온": "271560",
    "OCI홀딩스": "010060", "NC": "036570", "한화생명": "088350",
    "LG디스플레이": "034220",
}

ECONOMIC_FEATURES = [
    # 한국 시장
    "코스피", "코스피200", "코스닥", "원달러환율", "엔원환율",
    # 변동성 국면 — 같은 지표 값이라도 평온장/폭락장에서 의미가 달라진다
    "코스피 변동성 20일",
    # 한국 거시 (ECOS)
    "한국 기준금리", "국고채 3년", "국고채 10년", "CD 91일",
    "한국 소비자물가지수", "한국 통화량 M2",
    # 글로벌 — 한국 증시는 미국장·반도체 업황·달러에 강하게 연동된다
    "S&P 500 지수", "나스닥 종합지수", "VIX 지수", "필라델피아 반도체 지수",
    "달러 인덱스", "미국 10년 국채금리", "금 가격", "WTI 유가",
    "닛케이 225", "상해종합", "항셍",
]


# ============================================================
# 데이터 로드
# ============================================================
def get_all_data(table_name):
    all_rows, offset, limit = [], 0, 1000
    while True:
        resp = (
            supabase.table(table_name)
            .select("*")
            .order("날짜", desc=False)
            .limit(limit)
            .offset(offset)
            .execute()
        )
        if not resp.data:
            break
        all_rows.extend(resp.data)
        offset += limit
    return all_rows


def get_stock_data_from_db():
    rows = get_all_data(SOURCE_TABLE)
    print(f"  {SOURCE_TABLE}: {len(rows)}개 레코드")
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["날짜"] = pd.to_datetime(df["날짜"])
    df.sort_values("날짜", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # 사용 가능한 컬럼만 남긴다 (ECOS 키 미설정 등으로 일부 피처가 비어 있을 수 있음)
    global ECONOMIC_FEATURES, TARGET_COLUMNS
    missing_targets = [c for c in TARGET_COLUMNS if c not in df.columns]
    if missing_targets:
        raise ValueError(
            f"타깃 종목 컬럼이 테이블에 없습니다: {missing_targets}. "
            "sql/kr/setup_kr.sql 의 유니버스와 일치하는지 확인하세요."
        )

    available_features = [c for c in ECONOMIC_FEATURES if c in df.columns]
    dropped = set(ECONOMIC_FEATURES) - set(available_features)
    if dropped:
        print(f"  ⚠ 테이블에 없는 피처 제외: {sorted(dropped)}")

    numeric_cols = TARGET_COLUMNS + available_features
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")

    # ── 학습 구간 절단 + 늦은 상장 종목 자동 제외 ──────────────
    # 상장 전 구간을 bfill 하면 모델이 가짜 패턴을 배우므로, 전 종목이 값을 가진
    # 구간만 쓴다. 그런데 종목이 100개라 가장 늦게 상장한 한 종목 때문에 학습 구간이
    # 통째로 잘리는 일이 생긴다(예: 2024년 상장 종목 하나가 5년치를 1년치로 만든다).
    #
    # 그래서 'MIN_TRAIN_ROWS 행을 확보할 수 있는 가장 늦은 시작일'을 먼저 정하고,
    # 그 시점에 이미 상장해 있던 종목만 타깃으로 남긴다.
    # 제외된 종목은 ML 예측이 없으므로 매수 후보에서도 빠진다(설계상 안전한 방향).

    first_valid = {}
    for col in TARGET_COLUMNS:
        idx = df[col].first_valid_index()
        if idx is not None:
            first_valid[col] = idx

    if not first_valid:
        raise ValueError("종목 가격 데이터가 전혀 없습니다. 시장 데이터 수집을 먼저 실행하세요.")

    # MIN_TRAIN_ROWS 를 확보할 수 있는 최대 시작 인덱스
    max_start = max(len(df) - MIN_TRAIN_ROWS, 0)

    kept = [c for c, i in first_valid.items() if i <= max_start]
    excluded = [c for c in TARGET_COLUMNS if c not in kept]

    if not kept:
        raise ValueError(
            f"MIN_TRAIN_ROWS({MIN_TRAIN_ROWS})를 확보할 수 있는 종목이 없습니다. "
            f"KR_HISTORY_YEARS 를 늘리거나 MIN_TRAIN_ROWS 를 낮추세요."
        )

    if excluded:
        print(f"  ⚠ 상장이 늦어 학습 구간을 확보하지 못한 {len(excluded)}종목 제외:")
        for c in sorted(excluded, key=lambda x: first_valid.get(x, 10**9), reverse=True):
            fd = df.loc[first_valid[c], "날짜"] if c in first_valid else None
            print(f"      {c} (최초 데이터 {fd:%Y-%m-%d})" if fd is not None else f"      {c} (데이터 없음)")

    TARGET_COLUMNS = [c for c in TARGET_COLUMNS if c in kept]

    first_idx = max(first_valid[c] for c in TARGET_COLUMNS)
    first_date = df.loc[first_idx, "날짜"]
    print(
        f"  학습 대상 {len(TARGET_COLUMNS)}종목 / 시작일 {first_date:%Y-%m-%d} "
        f"(이전 {first_idx}행 제외, 남은 {len(df) - first_idx}행)"
    )
    df = df.loc[first_idx:].reset_index(drop=True)

    # 남은 구간의 산발적 결측(휴장일 등)만 전진/후진 채움
    df[numeric_cols] = df[numeric_cols].ffill().bfill()

    # 그래도 전부 비어 있는 피처는 제외 (상수 컬럼은 스케일링에서 0 division 유발)
    usable_features = [c for c in available_features if df[c].notna().any() and df[c].nunique() > 1]
    if len(usable_features) < len(available_features):
        print(f"  ⚠ 값이 없거나 상수인 피처 제외: {sorted(set(available_features) - set(usable_features))}")
    ECONOMIC_FEATURES = usable_features

    df.dropna(subset=TARGET_COLUMNS + ECONOMIC_FEATURES, inplace=True)
    df.reset_index(drop=True, inplace=True)

    print(f"  전처리 후: {df.shape[0]}일 × (타깃 {len(TARGET_COLUMNS)} + 피처 {len(ECONOMIC_FEATURES)})")
    if df.shape[0] < LOOKBACK + FORECAST_HORIZON + 200:
        print(
            f"  ⚠ 학습 데이터가 적습니다({df.shape[0]}일). "
            "KR_HISTORY_YEARS 를 늘리거나 MIN_TRAIN_ROWS 를 조정하세요."
        )
    return df


# ============================================================
# 모델
# ============================================================
def transformer_encoder(inputs, num_heads, ff_dim, dropout=0.1):
    attn = MultiHeadAttention(num_heads=num_heads, key_dim=inputs.shape[-1])(inputs, inputs)
    attn = Dropout(dropout)(attn)
    attn = Add()([inputs, attn])
    attn = LayerNormalization(epsilon=1e-6)(attn)

    ffn = Dense(ff_dim, activation="relu")(attn)
    ffn = Dense(inputs.shape[-1])(ffn)
    ffn = Dropout(dropout)(ffn)
    ffn = Add()([attn, ffn])
    return LayerNormalization(epsilon=1e-6)(ffn)


def build_model(stock_shape, econ_shape, num_heads, ff_dim, target_size):
    stock_inputs = Input(shape=stock_shape)
    s = stock_inputs
    for _ in range(4):
        s = transformer_encoder(s, num_heads=num_heads, ff_dim=ff_dim)
    s = Dense(64, activation="relu")(s)

    econ_inputs = Input(shape=econ_shape)
    e = econ_inputs
    for _ in range(4):
        e = transformer_encoder(e, num_heads=num_heads, ff_dim=ff_dim)
    e = Dense(64, activation="relu")(e)

    merged = Add()([s, e])
    merged = Dense(128, activation="relu")(merged)
    merged = Dropout(0.2)(merged)
    merged = GlobalAveragePooling1D()(merged)
    outputs = Dense(target_size)(merged)
    return Model(inputs=[stock_inputs, econ_inputs], outputs=outputs)


# ============================================================
# 평가 / 분석
# ============================================================
def evaluate_predictions(data, target_columns, forecast_horizon):
    metrics = []
    for col in target_columns:
        pred_col, act_col = f"{col}_Predicted", f"{col}_Actual"
        if pred_col not in data.columns or act_col not in data.columns:
            continue

        predicted = data[pred_col]
        actual = data[act_col].shift(-forecast_horizon)
        valid = ~predicted.isna() & ~actual.isna() & (actual != 0)
        predicted, actual = predicted[valid], actual[valid]
        if len(predicted) == 0:
            continue

        mae = mean_absolute_error(actual, predicted)
        mse = mean_squared_error(actual, predicted)
        mape = (abs((actual - predicted) / actual).mean()) * 100
        metrics.append(
            {
                "Stock": col,
                "MAE": round(mae, 4),
                "RMSE": round(mse ** 0.5, 4),
                "MAPE (%)": round(mape, 4),
                "Accuracy (%)": round(100 - mape, 4),
            }
        )
    return pd.DataFrame(metrics)


def analyze_rise_predictions(data, target_columns):
    last = data.iloc[-1]
    results = []
    for col in target_columns:
        actual = last.get(f"{col}_Actual", np.nan)
        predicted = last.get(f"{col}_Predicted", np.nan)
        if pd.notna(actual) and pd.notna(predicted) and actual != 0:
            rise = predicted > actual
            rise_pct = (predicted - actual) / actual * 100
        else:
            rise, rise_pct = np.nan, np.nan
        results.append(
            {
                "Stock": col,
                "Last Actual Price": actual,
                "Predicted Future Price": predicted,
                "Predicted Rise": rise,
                "Rise Probability (%)": rise_pct,
            }
        )
    return pd.DataFrame(results)


def make_recommendation(row):
    prob, rise = row.get("Rise Probability (%)", 0), row.get("Predicted Rise", False)
    if pd.isna(prob) or pd.isna(rise):
        return "No Data"
    if rise and prob > 0:
        return "STRONG BUY" if prob > 2 else "BUY"
    return "SELL"


def make_analysis(row):
    name, prob, rise = row["Stock"], row.get("Rise Probability (%)", 0), row.get("Predicted Rise", False)
    if pd.isna(prob) or pd.isna(rise):
        return f"{name}: 데이터 부족"
    if rise:
        return f"{name}은(는) 향후 {FORECAST_HORIZON}거래일간 약 {prob:.2f}% 상승이 예측됩니다."
    return f"{name}은(는) 향후 {FORECAST_HORIZON}거래일간 약 {-prob:.2f}% 하락이 예측됩니다."


# ============================================================
# 저장
# ============================================================
def save_analysis_to_db(final_df):
    """kr_stock_analysis_results 로 저장 (전량 교체)."""
    records = []
    for _, row in final_df.iterrows():
        name = row["Stock"]

        def _num(key):
            v = row.get(key)
            return None if pd.isna(v) else float(v)

        records.append(
            {
                "code": STOCK_NAME_TO_CODE.get(name),
                "stock_name": name,
                "accuracy": _num("Accuracy (%)"),
                "rise_probability": _num("Rise Probability (%)"),
                "last_actual_price": _num("Last Actual Price"),
                "predicted_future_price": _num("Predicted Future Price"),
                "recommendation": row.get("Recommendation"),
                "analysis": row.get("Analysis"),
            }
        )

    try:
        supabase.table(RESULT_TABLE).delete().neq("id", 0).execute()
        for i in range(0, len(records), 100):
            supabase.table(RESULT_TABLE).insert(records[i : i + 100]).execute()
        print(f"  {RESULT_TABLE}: {len(records)}개 저장 완료")
    except Exception as e:
        print(f"  {RESULT_TABLE} 저장 오류: {e}")
        raise


# ============================================================
# 메인
# ============================================================
def main():
    total_start = time.time()
    check_environment()

    print("=" * 60)
    print("PART 1: 데이터 로드 → 전처리")
    print("=" * 60)
    data = get_stock_data_from_db()
    if data is None or data.empty:
        raise ValueError("DB에서 데이터를 가져오지 못했습니다.")

    stock_scaler, econ_scaler = MinMaxScaler(), MinMaxScaler()
    scaled = data.copy()
    scaled[TARGET_COLUMNS] = stock_scaler.fit_transform(data[TARGET_COLUMNS])
    scaled[ECONOMIC_FEATURES] = econ_scaler.fit_transform(data[ECONOMIC_FEATURES])

    print("\n" + "=" * 60)
    print("PART 2: 모델 학습")
    print("=" * 60)

    X_stock, X_econ, y = [], [], []
    for i in range(LOOKBACK, len(scaled) - FORECAST_HORIZON):
        X_stock.append(scaled[TARGET_COLUMNS].iloc[i - LOOKBACK : i].values)
        X_econ.append(scaled[ECONOMIC_FEATURES].iloc[i - LOOKBACK : i].values)
        y.append(scaled[TARGET_COLUMNS].iloc[i + FORECAST_HORIZON - 1].values)

    X_stock, X_econ, y = np.array(X_stock), np.array(X_econ), np.array(y)
    print(f"  학습 샘플: {len(y)}개")
    if len(y) < 100:
        raise ValueError(f"학습 샘플이 너무 적습니다({len(y)}개). 시장 데이터를 더 수집하세요.")

    model = build_model(
        (LOOKBACK, len(TARGET_COLUMNS)),
        (LOOKBACK, len(ECONOMIC_FEATURES)),
        num_heads=8,
        ff_dim=256,
        target_size=len(TARGET_COLUMNS),
    )
    model.compile(optimizer=Adam(learning_rate=LEARNING_RATE), loss="mse", metrics=["mae"])
    model.summary()

    t_train = time.time()
    model.fit([X_stock, X_econ], y, epochs=EPOCHS, batch_size=BATCH_SIZE, verbose=1)
    print(f"\n  학습 완료: {EPOCHS} epochs, {time.time() - t_train:.1f}초")

    print("\n" + "=" * 60)
    print("PART 3: 전체 예측")
    print("=" * 60)

    Xs_full, Xe_full = [], []
    for i in range(LOOKBACK, len(scaled)):
        Xs_full.append(scaled[TARGET_COLUMNS].iloc[i - LOOKBACK : i].to_numpy())
        Xe_full.append(scaled[ECONOMIC_FEATURES].iloc[i - LOOKBACK : i].to_numpy())

    preds = model.predict([np.array(Xs_full), np.array(Xe_full)], verbose=1)
    preds_actual = stock_scaler.inverse_transform(preds)

    pred_len = len(preds_actual)
    dates = data["날짜"].iloc[LOOKBACK : LOOKBACK + pred_len].values
    actual_end = min(LOOKBACK + pred_len, len(data))
    actual_full = data[TARGET_COLUMNS].iloc[LOOKBACK:actual_end].values
    if actual_full.shape[0] < pred_len:
        pad = np.full((pred_len - actual_full.shape[0], len(TARGET_COLUMNS)), np.nan)
        actual_full = np.vstack([actual_full, pad])

    result = pd.DataFrame({"날짜": dates})
    for idx, col in enumerate(TARGET_COLUMNS):
        result[f"{col}_Predicted"] = preds_actual[:, idx]
        result[f"{col}_Actual"] = actual_full[:, idx]

    print(f"  예측 완료: {pred_len}개 샘플")

    print("\n" + "=" * 60)
    print("PART 4: 분석 및 저장")
    print("=" * 60)

    eval_df = evaluate_predictions(result, TARGET_COLUMNS, FORECAST_HORIZON)
    avg_acc = eval_df["Accuracy (%)"].mean() if not eval_df.empty else 0
    print(f"  평균 정확도: {avg_acc:.2f}%")

    rise_df = analyze_rise_predictions(result, TARGET_COLUMNS)
    final = pd.merge(eval_df, rise_df, on="Stock", how="outer")
    final = final.sort_values("Rise Probability (%)", ascending=False)
    final["Recommendation"] = final.apply(make_recommendation, axis=1)
    final["Analysis"] = final.apply(make_analysis, axis=1)

    save_analysis_to_db(final)

    print("\n" + "=" * 60)
    print("FINAL REPORT")
    print("=" * 60)
    print(
        final[
            ["Stock", "Accuracy (%)", "Last Actual Price",
             "Predicted Future Price", "Rise Probability (%)", "Recommendation"]
        ].to_string(index=False)
    )
    print(f"\n  총 소요시간: {time.time() - total_start:.1f}초")


if __name__ == "__main__":
    main()
