"""Shock-candle overshoot catch — Hyperliquid fee-model variant.

Same mechanism as strategies/shock_candle_overshoot_catch.py (liquidation
cascade overshoot fade: range > shock_atr * ATR marks a cascade, a deep limit
rests offset_atr * ATR beyond the extreme with short TTL). Differences, all
motivated by HL's nonzero maker fee (fee drag scales as 1/sl_dist):

- `atr_min`: ATR-quality gate. Skip signals where fractional ATR60 < atr_min
  entirely (the floor filter generalized: instead of flooring a noise-level
  stop at sl_floor, don't trade it). Applied after the cooldown pass so it
  matches the verified trade-level floor-filter semantics.
- `sl_atr_mult` / `sl_floor`: stop distance = max(sl_atr_mult*ATR, sl_floor).
- `tp_over_sl`: TP distance = tp_over_sl * SL distance (1.0 = 1:1).

With atr_min=0, sl_atr_mult=3, tp_over_sl=1, sl_floor=0.003 this reproduces
the original strategy's signals exactly (verified identical on ADA 12mo).

FROZEN CONFIG (2026-07-21): defaults below = original params + atr_min=0.001
(the pre-registered floor filter from RESULTS.md: skip when 3*ATR < 0.30%).
Tuned on IS Jul-2025..Jan-2026 only (15 variants tried: gate levels, sl_floor
0.4-0.6%, tp_over_sl 0.9/1.1/1.2, offset_atr 3.5/4.0, sl_atr_mult 3.5/4.0 —
nothing beat the plain gate beyond noise). Holdout Feb-Jun-2026, HL base fees
(engine_hl.py): n=1233, WR 62.9% vs breakeven 56.5%, avg_r +0.139, PF 1.31,
5/5 months positive, 14/18 symbols positive, top coin 17% / top month 28% of
holdout R, 0 lookahead violations. Stress (holdout avg_r): 1.5x slippage
+0.126; 10%/20% HL fee discount +0.148/+0.158. Note: with the gate the
sl_floor never binds (3*ATR >= 0.003 by construction).
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
    "sl_atr_mult": 3.0,   # stop distance in ATR
    "sl_floor": 0.003,    # minimum stop distance (fraction)
    "tp_over_sl": 1.0,    # tp_dist = tp_over_sl * sl_dist
    "atr_min": 0.001,     # skip signal entirely if fractional ATR < atr_min (FROZEN)
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
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / close  # fractional ATR

    rng_atr = ((high - low) / close) / atr

    idx_arr = np.arange(len(df))
    shock = (rng_atr > p["shock_atr"]) & (idx_arr > p["warmup"]) & np.isfinite(atr)
    down = shock & (close < open_)   # -> long fade below the low
    up = shock & (close > open_)     # -> short fade above the high

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)

    # shared per-symbol cooldown across both sides (all shocks consume cooldown,
    # matching the original strategy; the ATR gate filters afterwards)
    keep = []
    last = -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep)

    a = atr[keep]
    if p["atr_min"] > 0:
        ok = a >= p["atr_min"]
        keep, a = keep[ok], a[ok]
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
