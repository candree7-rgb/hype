"""HL-native shock overshoot fade — two-rung entry ladder.

Same 1m cascade-overshoot mechanism as shock_hl_variant, but the single limit
at offset_atr*ATR is split into two half-size rungs at rung1_atr / rung2_atr
beyond the shock extreme. Deep cascades fill both rungs for a deeper average
entry; shallow extensions fill only the near rung. Each rung carries its own
TP/SL sized from its own entry (two independent bracket orders — directly
implementable live on Hyperliquid), with a `weight` column (0.5 per rung) for
size-correct aggregation. Both rungs derive from the same shock and share the
per-symbol cooldown by construction.

Includes the frozen atr_min=0.001 quality gate from shock_hl_variant.

NEGATIVE RESULT (2026-07-21): 10 IS variants (rung pairs 2.0-5.0, cooldown
10/15/20, shock_atr 3.5/4.0). Best (2.5+4.5 cd=15) IS avg_r +0.1585 at
eff_n 1461 — but a plain single rung at offset 2.5 matches/beats it at
higher n (the ladder's far rung fills too rarely to earn its half of the
size). Never advanced to the holdout; superseded by hl_native_shock_freq.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "shock_atr": 4.0,
    "rung1_atr": 2.5,     # near rung offset beyond shock extreme, in ATR
    "rung2_atr": 4.0,     # far rung offset
    "ttl_entry": 5,
    "cooldown": 20,
    "atr_n": 60,
    "sl_atr_mult": 3.0,
    "sl_floor": 0.003,
    "tp_over_sl": 1.0,
    "atr_min": 0.001,
    "warmup": 150,
}

COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl", "weight"]


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

    idx_arr = np.arange(len(df))
    shock = (rng_atr > p["shock_atr"]) & (idx_arr > p["warmup"]) & np.isfinite(atr)
    down = shock & (close < open_)
    up = shock & (close > open_)

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)

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

    frames = []
    for off in (p["rung1_atr"], p["rung2_atr"]):
        limit = np.where(is_long,
                         low[keep] * (1.0 - off * a),
                         high[keep] * (1.0 + off * a))
        frames.append(pd.DataFrame({
            "idx": keep,
            "side": np.where(is_long, "long", "short"),
            "limit_price": limit,
            "tp_dist": tp,
            "sl_dist": sl,
            "ttl": int(p["ttl_entry"]),
            "weight": 0.5,
        }))
    out = pd.concat(frames, ignore_index=True)
    cl = close[out["idx"].to_numpy()]
    long_mask = out["side"].to_numpy() == "long"
    passive = np.where(long_mask, out["limit_price"].to_numpy() < cl,
                       out["limit_price"].to_numpy() > cl)
    return out[passive].sort_values("idx").reset_index(drop=True)
