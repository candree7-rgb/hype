"""HL-native shock overshoot fade — 5-minute shock detection, 1m execution.

Motivation (Hyperliquid microstructure): HL's 1m ATR runs ~0.74x Binance's,
which pushes ~81% of raw 1m shock signals under the 0.30% fee floor — the
1m design starves on HL. Aggregating shock detection to 5m candles multiplies
the working range/ATR scale ~2x: stops sized from 5m ATR clear the fee floor
naturally instead of being gated away, so the same cascade-overshoot edge
survives HL's nonzero maker fee without discarding most of the tape.

Mechanism: causal 5m resample of the 1m stream (bucket = epoch//300; a bucket
is complete at the 1m row with time % 300 == 240). A 5m candle whose range
exceeds shock_atr * ATR(5m) marks a cascade; a limit rests offset_atr * ATR5
beyond the 5m extreme with a TTL counted in 1m bars. TP/SL are fractions of
entry from the 5m ATR. Entries/exits are simulated on 1m for fill precision.

The signal at 1m row i uses only rows <= i (the completed bucket ends at i);
verified by the engine's truncation check.

FROZEN CONFIG (2026-07-21): tuned on IS Jul-2025..Jan-2026 only (28 IS
variants across shock_atr {3,3.5,4}, offset_atr {1.5,2,2.5,3}, atr_n {12,36},
ttl {10,25}, sl_atr_mult {2,3}, atr_min5 {0,0.002}, cooldown {60,100}).
Holdout Feb-Jun-2026 evaluated once at the end; see RESULTS notes.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "shock_atr": 4.0,     # 5m candle range must exceed this multiple of 5m ATR
    "offset_atr": 3.0,    # limit offset beyond 5m shock extreme, in 5m ATR
    "ttl_entry": 25,      # TTL in 1m bars (5 x 5m candles)
    "cooldown": 100,      # 1m bars, shared across sides per symbol
    "atr_n": 12,          # 5m bars for ATR (12 x 5m = 1h, mirrors 1m atr_n=60)
    "sl_atr_mult": 3.0,   # stop distance in 5m ATR
    "sl_floor": 0.003,    # minimum stop distance (fraction)
    "tp_over_sl": 1.0,    # tp_dist = tp_over_sl * sl_dist
    "atr_min": 0.0,       # skip signal if fractional 5m ATR < atr_min
    "warmup": 30,         # 5m bars of warmup
}

COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}

    time = df["time"].to_numpy(np.int64)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)

    # --- causal 5m resample -------------------------------------------------
    # bucket id per 1m row; a bucket is complete at its time%300==240 row.
    bucket = time // 300
    # groupby preserves order (buckets are increasing)
    g = pd.DataFrame({"b": bucket, "o": open_, "h": high, "l": low, "c": close})
    agg = g.groupby("b", sort=True).agg(
        o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))
    b_ids = agg.index.to_numpy()
    o5, h5, l5, c5 = (agg["o"].to_numpy(), agg["h"].to_numpy(),
                      agg["l"].to_numpy(), agg["c"].to_numpy())

    # completion row: the 1m index whose time % 300 == 240 within each bucket.
    # (gaps: buckets missing that row never fire — causal either way)
    is_completion = (time % 300) == 240
    comp_rows = np.flatnonzero(is_completion)
    comp_bucket = bucket[comp_rows]
    # map bucket id -> completion 1m row (only buckets that have one)
    bmap = dict(zip(comp_bucket.tolist(), comp_rows.tolist()))
    sig_row = np.array([bmap.get(b, -1) for b in b_ids])

    # NOTE on causality with gaps: agg for bucket k may include 1m rows AFTER
    # the completion row only if timestamps repeat/disorder — data is sorted
    # and gapless, and rows after time%300==240 in the same bucket cannot
    # exist (240 is the last minute of the bucket).

    prev_c5 = np.concatenate(([np.nan], c5[:-1]))
    tr5 = np.maximum(h5 - l5, np.maximum(np.abs(h5 - prev_c5), np.abs(l5 - prev_c5)))
    atr5 = pd.Series(tr5).rolling(int(p["atr_n"])).mean().to_numpy() / c5  # fractional

    with np.errstate(invalid="ignore", divide="ignore"):
        rng_atr = ((h5 - l5) / c5) / atr5

    k_arr = np.arange(len(b_ids))
    shock = (rng_atr > p["shock_atr"]) & (k_arr > p["warmup"]) & np.isfinite(atr5) \
        & (sig_row >= 0)
    down = shock & (c5 < o5)   # -> long fade below the 5m low
    up = shock & (c5 > o5)     # -> short fade above the 5m high

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)

    # shared per-symbol cooldown in 1m bars across both sides
    keep = []
    last = -10**9
    cd = int(p["cooldown"])
    for k in cand:
        r = sig_row[k]
        if r - last >= cd:
            keep.append(k)
            last = r
    keep = np.asarray(keep)

    a = atr5[keep]
    if p["atr_min"] > 0:
        ok = a >= p["atr_min"]
        keep, a = keep[ok], a[ok]
    if keep.size == 0:
        return pd.DataFrame(columns=COLS)

    sl = np.maximum(p["sl_atr_mult"] * a, p["sl_floor"])
    tp = p["tp_over_sl"] * sl
    is_long = down[keep]

    limit = np.where(is_long,
                     l5[keep] * (1.0 - p["offset_atr"] * a),
                     h5[keep] * (1.0 + p["offset_atr"] * a))
    rows = sig_row[keep]

    out = pd.DataFrame({
        "idx": rows,
        "side": np.where(is_long, "long", "short"),
        "limit_price": limit,
        "tp_dist": tp,
        "sl_dist": sl,
        "ttl": int(p["ttl_entry"]),
    })
    passive = np.where(is_long, limit < close[rows], limit > close[rows])
    return out[passive].reset_index(drop=True)
