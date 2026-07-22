"""CAP family B — high-frequency VWAP-deviation mean reversion on majors.

When close deviates from the rolling N-min VWAP by more than a threshold
(ATR-scaled with an absolute floor so the stop stays fee-viable), rest a
maker limit further along the deviation by ext_atr*ATR; TP back toward VWAP
(1:1 by default), stop ATR-scaled and floored. Edge-triggered per crossing,
per-symbol cooldown. This is the "many smaller trades on deep books" test.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "vwap_n": 20,        # rolling VWAP window, minutes
    "dev_atr": 4.0,      # deviation threshold in ATR60 units
    "dev_floor": 0.004,  # absolute min deviation (fraction)
    "ext_atr": 1.0,      # limit offset beyond current close, in ATR
    "sl_atr": 4.0,       # stop distance in ATR
    "sl_floor": 0.004,   # min stop distance
    "tp_over_sl": 1.0,
    "ttl": 5,
    "cooldown": 20,
    "atr_n": 60,
    "warmup": 100,
}
COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    vol = df["vol"].to_numpy(float)

    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / close

    tpx = (high + low + close) / 3.0
    N = int(p["vwap_n"])
    pv = pd.Series(tpx * vol).rolling(N).sum().to_numpy()
    vv = pd.Series(vol).rolling(N).sum().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = pv / vv
        dev = (close - vwap) / vwap
    th = np.maximum(p["dev_atr"] * atr, p["dev_floor"])

    idx_arr = np.arange(len(df))
    ok = (idx_arr > max(p["warmup"], N, p["atr_n"])) & np.isfinite(dev) \
        & np.isfinite(atr) & (atr > 0)
    long_sig = ok & (dev < -th)   # stretched below VWAP -> fade long
    short_sig = ok & (dev > th)

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

    a = atr[keep]
    is_long = long_sig[keep]
    limit = np.where(is_long,
                     close[keep] * (1.0 - p["ext_atr"] * a),
                     close[keep] * (1.0 + p["ext_atr"] * a))
    sl = np.maximum(p["sl_atr"] * a, p["sl_floor"])
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
