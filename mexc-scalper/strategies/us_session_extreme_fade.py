"""Session-Gated Extreme-Z Fade, two-sided (us_session_extreme_fade).

Mechanism: mean reversion of extreme 15-min moves is strongly hour-dependent.
During US hours (14-23 UTC; deepest books, two-sided institutional flow),
|z15| >= z_min stretches are stop-cascades that get absorbed and revert on
BOTH sides. The identical setup during Asia/EU-morning hours continues and
loses (control hours 0-13: WR 47.9%, avg_r -0.112 in-sample) — the session
gate does the work.

Entry: at close of bar i (warmup i > 300), if hour gate passes and
  z15[i] < -z_min  -> LONG  limit at close[i] * (1 - off_atr * ATR60[i])
  z15[i] > +z_min  -> SHORT limit at close[i] * (1 + off_atr * ATR60[i])
ttl = 20 bars, 15-bar cooldown per symbol shared across both sides
(tuned in-sample; spec defaults were ttl=15/cooldown=20).

Exit: tp_dist = sl_dist = max(tp_atr_mult * ATR60[i], sl_floor), 1:1 RR,
max_hold = 240 (engine default).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "z_min": 3.0,
    "h_start": 14,
    "h_end": 23,
    "hours": None,        # optional explicit list of UTC hours; overrides h_start/h_end
    "off_atr": 1.0,
    "ttl": 20,
    "cooldown": 15,
    "tp_atr_mult": 3.0,
    "sl_floor": 0.003,
    "warmup": 300,
}

EMPTY = pd.DataFrame(columns=["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"])


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    close = df["close"]
    n = len(df)
    if n <= p["warmup"]:
        return EMPTY.copy()

    # z-score of the 15-min return vs. 1-min vol scaled to 15 min (causal)
    ret1 = close.pct_change()
    sigma15 = ret1.rolling(240).std() * np.sqrt(15.0)
    z15 = close.pct_change(15) / sigma15

    # ATR60 as fraction of price (causal: TR uses previous close)
    prev_close = close.shift(1)
    tr = np.maximum(df["high"], prev_close) - np.minimum(df["low"], prev_close)
    atr60 = (tr.rolling(60).mean() / close)

    hour = df["dt"].dt.hour
    if p["hours"] is not None:
        hour_ok = hour.isin(list(p["hours"]))
    elif p["h_start"] <= p["h_end"]:
        hour_ok = (hour >= p["h_start"]) & (hour <= p["h_end"])
    else:  # wraps midnight
        hour_ok = (hour >= p["h_start"]) | (hour <= p["h_end"])

    z = z15.to_numpy()
    valid = np.zeros(n, dtype=bool)
    valid[p["warmup"] + 1:] = True
    valid &= hour_ok.to_numpy() & np.isfinite(z) & np.isfinite(atr60.to_numpy())

    long_mask = valid & (z < -p["z_min"])
    short_mask = valid & (z > p["z_min"])
    cand = np.flatnonzero(long_mask | short_mask)
    if len(cand) == 0:
        return EMPTY.copy()

    # 20-bar cooldown per symbol, shared across both sides
    kept = []
    last = -10**9
    cd = p["cooldown"]
    for i in cand:
        if i - last >= cd:
            kept.append(i)
            last = i
    kept = np.asarray(kept)

    c = close.to_numpy()[kept]
    a = atr60.to_numpy()[kept]
    is_long = long_mask[kept]
    limit = np.where(is_long, c * (1 - p["off_atr"] * a), c * (1 + p["off_atr"] * a))
    dist = np.maximum(p["tp_atr_mult"] * a, p["sl_floor"])

    return pd.DataFrame({
        "idx": kept,
        "side": np.where(is_long, "long", "short"),
        "limit_price": limit,
        "tp_dist": dist,
        "sl_dist": dist,
        "ttl": p["ttl"],
    })
