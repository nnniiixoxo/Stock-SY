# -*- coding: utf-8 -*-
"""
기술적 지표 계산 모듈
- RSI (Relative Strength Index)
- 이격도 (Disparity Index)
- 이동평균선 (Moving Average)
"""
import pandas as pd


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    일반적인 RSI(14) 계산 (Wilder's smoothing 방식).
    close: 날짜 오름차순으로 정렬된 종가 시리즈
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_disparity(close: pd.Series, ma_period: int = 20) -> pd.Series:
    """
    이격도 = (현재가 / 이동평균) * 100
    ma_period: 기준 이동평균선 기간 (기본 20일선)
    """
    ma = close.rolling(window=ma_period).mean()
    disparity = (close / ma) * 100
    return disparity


def calc_ma(close: pd.Series, period: int) -> pd.Series:
    """단순 이동평균선."""
    return close.rolling(window=period).mean()


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD (기본 12/26/9).
    반환: (macd_line, signal_line, histogram) 세 개의 Series.
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
                     k_period: int = 14, k_slow: int = 3, d_period: int = 3):
    """
    슬로우 스토캐스틱 (기본 14/3/3).
    Fast %K = (종가 - 최근 k_period 최저) / (최근 k_period 최고 - 최저) * 100
    Slow %K = Fast %K의 k_slow 이동평균
    %D = Slow %K의 d_period 이동평균
    반환: (slow_k, d)
    """
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    fast_k = (close - lowest_low) / (highest_high - lowest_low).replace(0, 1e-10) * 100
    slow_k = fast_k.rolling(window=k_slow).mean()
    d = slow_k.rolling(window=d_period).mean()
    return slow_k, d


def crossed_above_series(series_a: pd.Series, series_b: pd.Series, lookback: int = 2) -> pd.Series:
    """
    series_a가 series_b를 각 시점 기준 최근 lookback일 이내에 아래->위로 돌파했는지를
    시계열 전체(불리언 Series)로 반환. (백테스트처럼 여러 과거 시점을 한 번에 평가할 때 사용)
    """
    diff = series_a - series_b
    crossed_today = (diff.shift(1) <= 0) & (diff > 0)
    return crossed_today.rolling(window=lookback, min_periods=1).max().astype(bool)


def crossed_above(series_a: pd.Series, series_b: pd.Series, lookback: int = 2) -> bool:
    """
    series_a가 series_b를 최근 lookback일 이내에 아래->위로 돌파(골든크로스)했는지 확인.
    (가장 최근 시점 하나만 필요할 때 사용. 여러 시점이 필요하면 crossed_above_series 사용)
    """
    result = crossed_above_series(series_a, series_b, lookback=lookback)
    if result.empty:
        return False
    return bool(result.iloc[-1])


def is_disparity_decreasing(disparity: pd.Series, lookback: int = 3) -> bool:
    """
    최근 lookback 개 값이 연속으로 감소(수렴)하고 있는지 확인.
    (현재 main.py에서는 사용하지 않지만, 다른 조건 실험 시 참고용으로 남겨둠)
    """
    recent = disparity.dropna().tail(lookback)
    if len(recent) < lookback:
        return False
    values = recent.tolist()
    return all(values[i] > values[i + 1] for i in range(len(values) - 1))
