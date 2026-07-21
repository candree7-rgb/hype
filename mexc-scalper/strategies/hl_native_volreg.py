"""HL-native shock overshoot fade — volatility-regime gate variant.

Same 1m mechanism as shock_hl_variant, but instead of (or on top of) the
static atr_min gate, trade only when the coin's trailing 24h realized vol is
in the top vol_q quantile of its own trailing 30d distribution (causal
rolling quantile, shifted one bar). Rationale: vol begets cascades; HL fee
drag is fatal exactly in quiet tape.

24h realized vol proxy: rolling mean of |1m log return| over 1440 bars.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "shock_atr": 4.0,
    "offset_atr": 3.0,
    "ttl_entry": 5,
    "cooldown": 20,
    "atr_n": 60,
    "sl_atr_mult": 3.0,
    "sl_floor": 0.003,
    "tp_over_sl": 1.0,
    "atr_min": 0.0,       # optional static gate on top (0 = off)
    "vol_q": 0.667,       # trade only if 24h RV >= this trailing-30d quantile
    "rv_n": 1440,         # 24h in 1m bars
    "rv_window": 43200,   # 30d in 1m bars for the quantile baseline
    "warmup": 150,
}

COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}

    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)

    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / close

    with np.errstate(invalid="ignore", divide="ignore"):
        rng_atr = ((high - low) / close) / atr

    # 24h realized vol and its causal trailing-30d quantile threshold
    ret = pd.Series(close).pct_change().abs()
    rv = ret.rolling(int(p["rv_n"])).mean()
    thr = rv.rolling(int(p["rv_window"]), min_periods=int(p["rv_n"]) * 3) \
            .quantile(float(p["vol_q"])).shift(1)
    vol_ok = (rv >= thr).to_numpy() & np.isfinite(thr.to_numpy())

    idx_arr = np.arange(len(df))
    shock = (rng_atr > p["shock_atr"]) & (idx_arr > p["warmup"]) & np.isfinite(atr)
    down = shock & (close < open_)
    up = shock & (close > open_)

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)

    # cooldown consumed by all shocks (matching incumbent), gates applied after
    keep = []
    last = -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep)

    ok = vol_ok[keep]
    keep = keep[ok]
    a = atr[keep]
    if p["atr_min"] > 0:
        m = a >= p["atr_min"]
        keep, a = keep[m], a[m]
    if keep.size == 0:
        return pd.DataFrame(columns=COLS)

    sl = np.maximum(p["sl_atr_mult"] * a, p["sl_floor"])
    tp = p["tp_over_sl"] * sl
    is_long = down[keep]

    limit = np.where(is_long,
                     low[keep] * (1.0 - p["offset_atr"] * a),
                     high[keep] * (1.0 + p["offset_atr"] * a))

    out = pd.DataFrame({
        "idx": keep,
        "side": np.where(is_long, "long", "short"),
        "limit_price": limit,
        "tp_dist": tp,
        "sl_dist": sl,
        "ttl": int(p["ttl_entry"]),
    })
    passive = np.where(is_long, limit < close[keep], limit > close[keep])
    return out[passive].reset_index(drop=True)
