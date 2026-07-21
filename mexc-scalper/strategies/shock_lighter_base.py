"""Shock-candle overshoot catch — Lighter-venue variant.

Same mechanism as strategies/shock_candle_overshoot_catch.py (see that file's
docstring), with the TP and SL decoupled so the stop can be sized
independently of the target. Rationale on Lighter: fees are 0/0, so the fee
penalty on tight stops is gone — but the taker-latency slippage on the SL is
NOT, and it is paid per stop-out. A wider SL pays the slippage less often and
amortizes it over more risk; a tighter SL pays it more often over less risk.

Params beyond the original:
- sl_atr_mult: SL distance in ATR (original hard-tied sl = tp).
- sl_floor / tp_floor: absolute floors for each distance.

r is still measured against sl_dist (risk normalization unchanged by engine).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "shock_atr": 4.0,     # candle range must exceed this multiple of ATR
    "offset_atr": 3.0,    # limit offset beyond shock extreme, in ATR
    "ttl_entry": 5,       # very short TTL: fill only during cascade extension
    "cooldown": 20,       # bars, shared across sides per symbol
    "atr_n": 60,
    "tp_atr_mult": 3.0,
    "sl_atr_mult": 3.0,
    "tp_floor": 0.003,
    "sl_floor": 0.003,
    "warmup": 150,
}

COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}

    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)

    # True range (causal): uses previous close only
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / close  # fractional

    rng_atr = ((high - low) / close) / atr

    idx_arr = np.arange(len(df))
    shock = (rng_atr > p["shock_atr"]) & (idx_arr > p["warmup"]) & np.isfinite(atr)
    down = shock & (close < open_)   # -> long fade below the low
    up = shock & (close > open_)     # -> short fade above the high

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)

    # shared per-symbol cooldown across both sides
    keep = []
    last = -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep)

    a = atr[keep]
    tp = np.maximum(p["tp_atr_mult"] * a, p["tp_floor"])
    sl = np.maximum(p["sl_atr_mult"] * a, p["sl_floor"])
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
