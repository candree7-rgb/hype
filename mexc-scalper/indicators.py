"""Shared indicator helpers for strategies. All functions are causal: the value
at row i uses only rows <= i (shift where needed is the caller's job for
signal generation — every rolling op here uses trailing windows only)."""
import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rolling_vwap(df: pd.DataFrame, n: int = 60) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["vol"]).rolling(n).sum()
    v = df["vol"].rolling(n).sum()
    return pv / v


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def swing_low(df: pd.DataFrame, left: int, right: int) -> pd.Series:
    """True at i if low[i-right] is the minimum of the window ending at i.
    The swing is only *known* at row i (right bars after the pivot)."""
    lo = df["low"]
    win = lo.rolling(left + right + 1)
    pivot = lo.shift(right)
    return (win.min() == pivot) & pivot.notna()


def swing_high(df: pd.DataFrame, left: int, right: int) -> pd.Series:
    hi = df["high"]
    win = hi.rolling(left + right + 1)
    pivot = hi.shift(right)
    return (win.max() == pivot) & pivot.notna()


def bullish_fvg(df: pd.DataFrame) -> pd.Series:
    """3-candle bullish fair value gap known at row i: low[i] > high[i-2]."""
    return df["low"] > df["high"].shift(2)


def bearish_fvg(df: pd.DataFrame) -> pd.Series:
    return df["high"] < df["low"].shift(2)


def realized_vol(df: pd.DataFrame, n: int = 60) -> pd.Series:
    r = np.log(df["close"] / df["close"].shift(1))
    return r.rolling(n).std()


def volume_zscore(df: pd.DataFrame, n: int = 120) -> pd.Series:
    v = df["vol"]
    return (v - v.rolling(n).mean()) / v.rolling(n).std()
