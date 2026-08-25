"""
기술적 지표 계산 (시장 무관 순수 계산 모듈).

가격 시계열 또는 KIS 일봉 배열만 받아서 지표 값을 돌려준다. DB·API·설정에 전혀
의존하지 않으므로 어느 트랙에서든 그대로 쓸 수 있다.

  · calculate_sma / calculate_ema / calculate_rsi / calculate_macd
        pandas Series (종가) 를 받는다.
  · calculate_atr / calculate_adx
        KIS 일봉 dict 리스트를 받는다. **최신일이 index 0** 인 순서를 전제로 하며,
        각 dict 는 "high" / "low" / "clos" 키를 문자열 숫자로 가져야 한다.
        국내 일봉(FHKST03010100)은 stck_hgpr/stck_lwpr/stck_clpr 로 내려오므로
        app/services/kr/kr_recommendation_service._normalize_daily() 가 이 형태로
        변환한 뒤 넘긴다.

RSI 는 Wilder's Smoothing(업계 표준)을 쓰고, ATR/ADX 도 동일한 평활을 쓴다.
"""
import pandas as pd


class TechnicalIndicators:
    """지표 계산 메서드 모음. 상태를 갖지 않으므로 인스턴스 하나를 공유해도 안전하다."""

    def calculate_sma(self, series, period):
        """단순 이동평균(SMA) 계산"""
        return series.rolling(window=period).mean()

    def calculate_ema(self, series, period):
        """지수 이동평균(EMA) 계산"""
        return series.ewm(span=period, adjust=False).mean()

    def calculate_rsi(self, series, period=14):
        """RSI 계산 (Wilder's Smoothing - 업계 표준)"""
        # 비거래일 제거 (ffill로 인한 변동 0인 날 = 가격 변동 없는 중복)
        trading_series = series[series.diff() != 0].copy()
        # 첫 번째 값은 diff가 NaN이므로 포함
        if len(series) > 0:
            trading_series = pd.concat([series.iloc[:1], trading_series]).drop_duplicates()

        if len(trading_series) < period + 1:
            return pd.Series([50] * len(series), index=series.index)

        delta = trading_series.diff().dropna()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        # Wilder's Smoothing (EMA with alpha = 1/period)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # 원본 인덱스에 맞춰 reindex (비거래일은 마지막 거래일 RSI 사용)
        rsi = rsi.reindex(series.index, method='ffill')
        return rsi

    def calculate_macd(self, series, short_period=12, long_period=26, signal_period=9):
        """MACD 및 Signal 라인 계산"""
        short_ema = self.calculate_ema(series, short_period)
        long_ema = self.calculate_ema(series, long_period)
        macd = short_ema - long_ema
        signal = self.calculate_ema(macd, signal_period)
        return macd, signal

    def calculate_atr(self, daily_data, period=14):
        """KIS API 일봉 데이터로 ATR 계산

        Args:
            daily_data: KIS API output2 (최신일이 index 0)
            period: ATR 계산 기간 (기본 14일)

        Returns:
            float: ATR 값, 계산 불가 시 None
        """
        try:
            if len(daily_data) < period + 1:
                return None

            data = list(reversed(daily_data))

            highs = [float(d.get("high", "0") or "0") for d in data]
            lows = [float(d.get("low", "0") or "0") for d in data]
            closes = [float(d.get("clos", "0") or "0") for d in data]

            if any(v == 0 for v in closes[:period + 1]):
                return None

            # True Range 계산
            tr_list = []
            for i in range(1, len(data)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1])
                )
                tr_list.append(tr)

            if len(tr_list) < period:
                return None

            # Wilder's smoothing ATR
            atr = sum(tr_list[:period]) / period
            for i in range(period, len(tr_list)):
                atr = (atr * (period - 1) + tr_list[i]) / period

            return round(atr, 4)
        except Exception as e:
            print(f"  ATR 계산 오류: {e}")
            return None

    def calculate_adx(self, daily_data, period=14):
        """KIS API 일봉 데이터로 ADX 계산

        Args:
            daily_data: KIS API output2 (최신일이 index 0)
            period: ADX 계산 기간 (기본 14일)

        Returns:
            float: ADX 값, 계산 불가 시 None
        """
        try:
            if len(daily_data) < period * 2 + 1:
                return None

            # 최신일이 0번이므로 역순 정렬 (오래된 날짜부터)
            data = list(reversed(daily_data))

            highs = [float(d.get("high", "0") or "0") for d in data]
            lows = [float(d.get("low", "0") or "0") for d in data]
            closes = [float(d.get("clos", "0") or "0") for d in data]

            if any(v == 0 for v in closes[:period * 2]):
                return None

            # True Range, +DM, -DM 계산
            tr_list, plus_dm_list, minus_dm_list = [], [], []
            for i in range(1, len(data)):
                high_diff = highs[i] - highs[i - 1]
                low_diff = lows[i - 1] - lows[i]

                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0
                minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0

                tr_list.append(tr)
                plus_dm_list.append(plus_dm)
                minus_dm_list.append(minus_dm)

            # Smoothed TR, +DM, -DM (Wilder's smoothing)
            atr = sum(tr_list[:period])
            plus_di_smooth = sum(plus_dm_list[:period])
            minus_di_smooth = sum(minus_dm_list[:period])

            dx_list = []
            for i in range(period, len(tr_list)):
                atr = atr - (atr / period) + tr_list[i]
                plus_di_smooth = plus_di_smooth - (plus_di_smooth / period) + plus_dm_list[i]
                minus_di_smooth = minus_di_smooth - (minus_di_smooth / period) + minus_dm_list[i]

                if atr == 0:
                    continue

                plus_di = 100 * plus_di_smooth / atr
                minus_di = 100 * minus_di_smooth / atr
                di_sum = plus_di + minus_di

                if di_sum == 0:
                    dx_list.append(0)
                else:
                    dx_list.append(100 * abs(plus_di - minus_di) / di_sum)

            if len(dx_list) < period:
                return None

            # ADX = DX의 이동평균
            adx = sum(dx_list[:period]) / period
            for i in range(period, len(dx_list)):
                adx = (adx * (period - 1) + dx_list[i]) / period

            return round(adx, 2)
        except Exception as e:
            print(f"  ADX 계산 오류: {e}")
            return None

