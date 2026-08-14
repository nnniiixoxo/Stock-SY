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
