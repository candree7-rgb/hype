"""CAP family D — 1m cascade fade re-geared for deep-book majors.

Same mechanism as the validated hl_native_shock_freq (1m candle range >
shock_atr * ATR60 -> maker limit offset beyond the extreme), but instead of
the atr_min quality gate (which kills ~94% of BTC signals because majors'
ATR is tiny), BOTH the offset and the stop get absolute floors, preserving
the entry geometry at major-coin volatility: offset >= off_floor, stop >=
sl_floor. This tests idea 1 directly: 0.5-1% stops make the fee drag ~0.06-
0.12R and orders (notional = eq*risk/sl) small enough for deep books.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "shock_atr": 3.5,
    "offset_atr": 2.5,
    "off_floor": 0.004,   # min limit offset beyond the shock extreme
    "sl_atr_mult": 3.0,
    "sl_floor": 0.005,    # min stop distance
    "tp_over_sl": 1.0,
    "ttl_entry": 5,
    "cooldown": 15,
    "atr_n": 60,
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
    idx_arr = np.arange(len(df))
    shock = (rng_atr > p["shock_atr"]) & (idx_arr > p["warmup"]) & np.isfinite(atr)
    down = shock & (close < open_)
    up = shock & (close > open_)

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

    a = atr[keep]
    off = np.maximum(p["offset_atr"] * a, p["off_floor"])
    sl = np.maximum(p["sl_atr_mult"] * a, p["sl_floor"])
    tp = p["tp_over_sl"] * sl
    is_long = down[keep]
    limit = np.where(is_long,
                     low[keep] * (1.0 - off),
                     high[keep] * (1.0 + off))

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
