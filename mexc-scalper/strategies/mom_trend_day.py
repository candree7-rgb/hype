"""MOMENTUM: trend-day capture via opening-range breakout (per symbol).

VERDICT (2026-07-22, IS Jul25-Jan26): FAILED — kept as a negative result.
All 6 variants (OR 2h/4h, long/short/both, compression filter) are clearly
negative (avg_r -0.08..-0.12, PF 0.82-0.89) on the 5 majors. Naked intraday
breakouts lose to mean reversion + taker costs on this data; day-type by
opening range has no edge here. Breakouts only work cascade-confirmed
(mom_cascade_breakout.py).

Day-type detection: the first `or_hours` hours of the UTC day define the
opening range. A 1m close beyond the range (first crossing of the day) tags a
potential trend day; ride it to the day's end (time exit) with a hard stop at
the opposite side of the range (capped). Optional compression filter: only
trade when the opening range is narrow vs 24h ATR (compression -> expansion).

Entries are taker (stop-entry converted to market at next 1m open; fee math in
engine_mom). Long/short configurable — measured on IS, not assumed.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "or_hours": 2,
    "sides": "both",       # "long" | "short" | "both"
    "sl_cap": 0.02,        # stop = min(range width, cap)
    "sl_floor": 0.008,
    "compress_max": None,  # if set: OR width / ATR24h(width) must be below
    "max_hold_to_eod": True,
    "warmup": 1500,
}

SIG_COLS = ["idx", "side", "entry_type", "limit_price", "ttl", "sl_dist",
            "tp_dist", "trail_dist", "trail_arm", "max_hold"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    dtn = df["dt"].dt.tz_localize(None) if df["dt"].dt.tz is not None else df["dt"]
    day = dtn.dt.floor("D")
    minute_of_day = (dtn - day).dt.total_seconds().to_numpy() // 60
    cl = df["close"].to_numpy(float)
    hi = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)

    or_end = p["or_hours"] * 60
    # opening-range high/low per day (first or_hours), assigned causally:
    # only usable at minutes >= or_end of the same day
    g = pd.DataFrame({"day": day, "m": minute_of_day, "hi": hi, "lo": lo})
    orng = g[g["m"] < or_end].groupby("day").agg(orh=("hi", "max"),
                                                 orl=("lo", "min"))
    orh = day.map(orng["orh"]).to_numpy()
    orl = day.map(orng["orl"]).to_numpy()

    # 24h trailing range scale for compression filter
    atr24 = (pd.Series(hi).rolling(1440).max().to_numpy()
             - pd.Series(lo).rolling(1440).min().to_numpy()) / cl

    after_or = minute_of_day >= or_end
    valid = after_or & (np.arange(len(df)) > p["warmup"]) & np.isfinite(orh)
    width = (orh - orl) / cl
    if p["compress_max"] is not None:
        valid &= (width / np.maximum(atr24, 1e-9)) < p["compress_max"]

    sigs = []
    for side, cond in (("long", cl > orh), ("short", cl < orl)):
        if p["sides"] != "both" and p["sides"] != side:
            continue
        c = cond & valid
        trig = np.flatnonzero(c & ~np.concatenate(([False], c[:-1])))
        # first trigger per day only
        d = day.iloc[trig].to_numpy()
        first = np.concatenate(([True], d[1:] != d[:-1]))
        trig = trig[first]
        for i in trig:
            sl = float(np.clip(width[i], p["sl_floor"], p["sl_cap"]))
            hold = int(1440 - minute_of_day[i]) if p["max_hold_to_eod"] else 1440
            if hold < 30:
                continue
            sigs.append((i, side, "taker", np.nan, 1, sl, np.nan,
                         np.nan, 0.0, hold))
    if not sigs:
        return pd.DataFrame(columns=SIG_COLS)
    return pd.DataFrame(sigs, columns=SIG_COLS).sort_values("idx").reset_index(drop=True)
