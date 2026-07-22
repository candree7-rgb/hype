"""SMC Order-Block retest — engine-compatible strategy (strict engine_hl).

Definition (causal, all rows <= idx):
  - ATR60 (1m true-range SMA).
  - Displacement candle i: |close-open| >= disp_k * ATR60[i].
  - Order block = most recent OPPOSITE-body candle within lookback_ob candles
    before i (down candle before an up displacement, and vice versa).
  - Signal at idx=i. Long entry: maker limit at OB HIGH (proximal edge);
    SL beyond OB LOW minus buf_atr*ATR (structural far edge). Shorts mirrored.
  - tp_dist = tp_mult * sl_dist  (fully dynamic, per-trade, no floors).
  - Optional causal HTF filter: 4h close > EMA50 (completed bars only) for
    longs, < for shorts.
  - Cooldown per side, and one signal per distinct OB candle.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "disp_k": 2.0,       # displacement body >= k * ATR60
    "lookback_ob": 10,   # how far back to find the opposite candle
    "buf_atr": 0.10,     # SL buffer beyond OB far edge, in ATR units
    "tp_mult": 1.0,      # TP distance = tp_mult * SL distance
    "ttl": 240,          # limit order lifetime (minutes)
    "cooldown": 30,      # per-side cooldown (minutes)
    "htf_filter": False, # 4h EMA50 trend filter
    "atr_n": 60,
}


def _atr(df: pd.DataFrame, n: int) -> np.ndarray:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def _htf_trend(df: pd.DataFrame) -> np.ndarray:
    """1=up, -1=down, 0=unknown; uses only COMPLETED 4h bars (causal)."""
    bars = (df.set_index("dt").resample("4h", label="left", closed="left")
              .agg(close=("close", "last")).dropna())
    ema = bars["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    trend = np.where(bars["close"] > ema, 1, -1)
    trend = np.where(np.isnan(ema), 0, trend)
    usable_from = bars.index + pd.Timedelta(hours=4)
    idx = np.searchsorted(usable_from.values, df["dt"].values, side="right") - 1
    out = np.zeros(len(df), dtype=int)
    ok = idx >= 0
    out[ok] = trend[idx[ok]]
    return out


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **(params or {})}
    o = df["open"].to_numpy()
    c = df["close"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    atr = _atr(df, p["atr_n"])
    body = c - o
    trend = _htf_trend(df) if p["htf_filter"] else None

    disp = np.abs(body) >= p["disp_k"] * atr
    disp &= np.isfinite(atr) & (atr > 0)
    idxs = np.flatnonzero(disp)

    rows = []
    last_sig = {"long": -10**9, "short": -10**9}
    used_ob = set()
    for i in idxs:
        side = "long" if body[i] > 0 else "short"
        if i - last_sig[side] < p["cooldown"]:
            continue
        if trend is not None:
            t = trend[i]
            if (side == "long" and t != 1) or (side == "short" and t != -1):
                continue
        # find last opposite candle before i
        ob = -1
        for k in range(i - 1, max(i - 1 - p["lookback_ob"], 0), -1):
            if (side == "long" and body[k] < 0) or (side == "short" and body[k] > 0):
                ob = k
                break
        if ob < 0 or (side, ob) in used_ob:
            continue
        buf = p["buf_atr"] * atr[i]
        if side == "long":
            limit = h[ob]
            slp = l[ob] - buf
            if not (limit < c[i]) or slp >= limit:
                continue
            sl_dist = (limit - slp) / limit
        else:
            limit = l[ob]
            slp = h[ob] + buf
            if not (limit > c[i]) or slp <= limit:
                continue
            sl_dist = (slp - limit) / limit
        if sl_dist <= 0:
            continue
        used_ob.add((side, ob))
        last_sig[side] = i
        rows.append({"idx": int(i), "side": side, "limit_price": float(limit),
                     "tp_dist": float(p["tp_mult"] * sl_dist),
                     "sl_dist": float(sl_dist), "ttl": int(p["ttl"])})
    return pd.DataFrame(rows, columns=["idx", "side", "limit_price",
                                       "tp_dist", "sl_dist", "ttl"])
