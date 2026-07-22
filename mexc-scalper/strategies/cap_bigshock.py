"""CAP family A — larger-distance shock fade on deep-book majors.

Detect a fast W-minute directional move (window range > move_min, absolute %,
suited to majors where 0.5-1.5% stops shrink fee drag to ~0.06-0.12R per
loss), rest a maker limit beyond the move extreme by ext_frac of the move,
stop = sl_frac of the move (floored). 1:1 RR default. Edge-triggered with
per-symbol cooldown. All signals causal (rolling windows, prior rows only).
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "window": 15,        # detection window, minutes
    "move_min": 0.010,   # min window range (fraction) to call it a shock
    "ext_frac": 0.25,    # limit rests beyond extreme by this frac of the move
    "sl_frac": 0.60,     # stop distance as frac of the move
    "sl_floor": 0.005,   # min stop distance
    "tp_over_sl": 1.0,
    "ttl": 10,           # entry TTL, minutes
    "cooldown": 30,      # bars between signals per symbol
    "warmup": 100,
}
COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    W = int(p["window"])
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)

    roll_hi = pd.Series(high).rolling(W).max().to_numpy()
    roll_lo = pd.Series(low).rolling(W).min().to_numpy()
    rng = (roll_hi - roll_lo) / close
    ret_w = close / np.concatenate((np.full(W, np.nan), close[:-W])) - 1.0

    idx_arr = np.arange(len(df))
    with np.errstate(invalid="ignore"):
        shock = (rng > p["move_min"]) & (idx_arr > max(p["warmup"], W + 1)) \
            & np.isfinite(rng) & np.isfinite(ret_w)
    down = shock & (ret_w < 0)
    up = shock & (ret_w > 0)

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)
    keep, last = [], -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep)

    mv = rng[keep]
    is_long = down[keep]
    limit = np.where(is_long,
                     roll_lo[keep] * (1.0 - p["ext_frac"] * mv),
                     roll_hi[keep] * (1.0 + p["ext_frac"] * mv))
    sl = np.maximum(p["sl_frac"] * mv, p["sl_floor"])
    tp = p["tp_over_sl"] * sl

    out = pd.DataFrame({
        "idx": keep,
        "side": np.where(is_long, "long", "short"),
        "limit_price": limit,
        "tp_dist": tp,
        "sl_dist": sl,
        "ttl": int(p["ttl"]),
    })
    passive = np.where(is_long, limit < close[keep], limit > close[keep])
    return out[passive].reset_index(drop=True)
