"""SMC Fair-Value-Gap fill — engine-compatible strategy (strict engine_hl).

Definition (causal):
  - Bull FVG at candle i: low[i] > high[i-2] and gap = low[i]-high[i-2]
    >= gap_k * ATR60[i].  Bear FVG mirrored (high[i] < low[i-2]).
  - Signal at idx=i. Entry: maker limit at gap MIDPOINT.
  - SL 'edge':   beyond the gap's distal edge (high[i-2] for bull) - buffer.
    SL 'origin': beyond the origin of the impulse (min(low[i-2], low[i-1])
                 for bull) - buffer.  Fully dynamic per-trade distances.
  - tp_dist = tp_mult * sl_dist.
  - Optional causal HTF filter (4h close > EMA50, completed bars only).
  - Cooldown per side.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "gap_k": 0.5,        # gap size >= k * ATR60
    "sl_mode": "edge",   # 'edge' | 'origin'
    "buf_atr": 0.10,
    "tp_mult": 1.0,
    "ttl": 240,
    "cooldown": 15,
    "htf_filter": False,
    "atr_n": 60,
}


def _atr(df: pd.DataFrame, n: int) -> np.ndarray:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def _htf_trend(df: pd.DataFrame) -> np.ndarray:
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
    c = df["close"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    atr = _atr(df, p["atr_n"])
    n = len(df)
    trend = _htf_trend(df) if p["htf_filter"] else None

    h2 = np.roll(h, 2)
    l2 = np.roll(l, 2)
    bull = np.zeros(n, dtype=bool)
    bear = np.zeros(n, dtype=bool)
    bull[2:] = (l[2:] > h2[2:]) & ((l[2:] - h2[2:]) >= p["gap_k"] * atr[2:])
    bear[2:] = (h[2:] < l2[2:]) & ((l2[2:] - h[2:]) >= p["gap_k"] * atr[2:])
    valid = np.isfinite(atr) & (atr > 0)
    bull &= valid
    bear &= valid

    rows = []
    last_sig = {"long": -10**9, "short": -10**9}
    for i in np.flatnonzero(bull | bear):
        side = "long" if bull[i] else "short"
        if i - last_sig[side] < p["cooldown"]:
            continue
        if trend is not None:
            t = trend[i]
            if (side == "long" and t != 1) or (side == "short" and t != -1):
                continue
        buf = p["buf_atr"] * atr[i]
        if side == "long":
            gap_hi, gap_lo = l[i], h[i - 2]
            limit = (gap_hi + gap_lo) / 2.0
            origin = min(l[i - 2], l[i - 1])
            slp = (gap_lo if p["sl_mode"] == "edge" else origin) - buf
            if not (limit < c[i]) or slp >= limit:
                continue
            sl_dist = (limit - slp) / limit
        else:
            gap_lo, gap_hi = h[i], l[i - 2]
            limit = (gap_hi + gap_lo) / 2.0
            origin = max(h[i - 2], h[i - 1])
            slp = (gap_hi if p["sl_mode"] == "edge" else origin) + buf
            if not (limit > c[i]) or slp <= limit:
                continue
            sl_dist = (slp - limit) / limit
        if sl_dist <= 0:
            continue
        last_sig[side] = i
        rows.append({"idx": int(i), "side": side, "limit_price": float(limit),
                     "tp_dist": float(p["tp_mult"] * sl_dist),
                     "sl_dist": float(sl_dist), "ttl": int(p["ttl"])})
    return pd.DataFrame(rows, columns=["idx", "side", "limit_price",
                                       "tp_dist", "sl_dist", "ttl"])
