"""CAP family G — trend-pullback continuation on deep-book majors.

If the H-hour momentum exceeds a threshold, rest a maker limit a small
pullback below (longs) / above (shorts) the current price, in the TREND
direction; TP = tp_over_sl * sl beyond, stop fixed fraction. Large stops
(>= 0.8%) keep the trade multi-candle, where the 1m engine is honest, and
keep fee drag tiny (~0.08R). Edge-triggered crossing + cooldown.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "mom_n": 240,        # momentum lookback, minutes
    "mom_th": 0.015,     # abs momentum to qualify as trend
    "pullback": 0.004,   # limit distance below/above close
    "sl_dist": 0.010,
    "tp_over_sl": 1.0,
    "ttl": 60,
    "cooldown": 120,
    "warmup": 300,
}
COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    close = df["close"].to_numpy(float)
    N = int(p["mom_n"])
    mom = close / np.concatenate((np.full(N, np.nan), close[:-N])) - 1.0
    idx_arr = np.arange(len(df))
    ok = (idx_arr > max(p["warmup"], N + 1)) & np.isfinite(mom)
    long_sig = ok & (mom > p["mom_th"])
    short_sig = ok & (mom < -p["mom_th"])

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

    is_long = long_sig[keep]
    limit = np.where(is_long,
                     close[keep] * (1.0 - p["pullback"]),
                     close[keep] * (1.0 + p["pullback"]))
    sl = np.full(keep.size, p["sl_dist"])
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
