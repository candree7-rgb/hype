"""CAP family C — rolling-range extreme fade on majors (session-range scalp).

Rolling R-hour high/low defines the range. When price reaches the top/bottom
q of a wide-enough range, rest a maker limit slightly beyond the extreme;
TP back toward the range interior, stop beyond. Stops scale with range width
(floored) so majors get 0.5-1.5% stops. Edge-triggered, per-symbol cooldown.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "range_min": 480,    # rolling range window, minutes (8h)
    "width_min": 0.010,  # min range width (fraction of price)
    "width_max": 0.06,   # skip blown-out regimes
    "pos_q": 0.90,       # price position in range to trigger (and 1-q)
    "ext_frac": 0.10,    # limit beyond the extreme, frac of width
    "sl_frac": 0.35,     # stop distance as frac of width
    "sl_floor": 0.005,
    "tp_over_sl": 1.0,
    "ttl": 30,
    "cooldown": 60,
    "warmup": 100,
}
COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    R = int(p["range_min"])
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)

    roll_hi = pd.Series(high).rolling(R).max().to_numpy()
    roll_lo = pd.Series(low).rolling(R).min().to_numpy()
    width = (roll_hi - roll_lo) / close
    with np.errstate(invalid="ignore", divide="ignore"):
        pos = (close - roll_lo) / (roll_hi - roll_lo)

    idx_arr = np.arange(len(df))
    ok = (idx_arr > max(p["warmup"], R + 1)) & np.isfinite(pos) \
        & (width >= p["width_min"]) & (width <= p["width_max"])
    short_sig = ok & (pos > p["pos_q"])
    long_sig = ok & (pos < 1.0 - p["pos_q"])

    cand = np.flatnonzero(long_sig | short_sig)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)
    keep, last = [], -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep)

    w = width[keep]
    is_long = long_sig[keep]
    limit = np.where(is_long,
                     roll_lo[keep] * (1.0 - p["ext_frac"] * w),
                     roll_hi[keep] * (1.0 + p["ext_frac"] * w))
    sl = np.maximum(p["sl_frac"] * w, p["sl_floor"])
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
