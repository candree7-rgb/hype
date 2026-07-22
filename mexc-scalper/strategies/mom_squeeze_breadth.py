"""MOMENTUM: cross-coin cascade breadth -> squeeze/trend continuation on majors.

VERDICT (2026-07-22, IS Jul25-Jan26): FAILED — kept as a negative result.
Entering majors immediately on a breadth trigger (any mode: taker now, maker
retracement; any side) is -0.04..-0.08 avg_r across 6 variants; even the
Nov 17 - Dec 4 squeeze window loses (-12R). Hourly breadth >= 5 is true on
~10% of ALL minutes — far too common to be a trade signal by itself. The
breadth works only as a CONFIRMATION of a price breakout, not as an entry:
see mom_cascade_breakout.py (the adopted strategy).

The cascade-fade's poison as food: when many coins print same-direction shock
candles (the fade's own detector: range > 3.5*ATR60, ATR gate) within one
hour, that is a market-wide squeeze/cascade — exactly the regime where fading
bleeds (Nov 17 - Dec 4 2025). This strategy trades WITH the cascade direction
on deep-book majors, entering on the retracement (the same snapback the fade
harvests), holding hours with a trailing exit.

Breadth is computed causally: at minute t, count distinct universe coins with
a same-direction shock in the trailing `window` minutes, using rows <= t only.
Edge-trigger: breadth crosses >= breadth_n from below.

Entry modes:
  maker_retr : limit resting `retr_atr`*ATR60 against the cascade direction
               (maker fee; fills on the fade's snapback) — default
  taker_now  : market at next bar open (taker fee + slippage, math in engine)
  taker_delay: market `delay_min` minutes after the trigger

Exits: hard SL sl_pct, trailing stop trail_pct armed after +arm_pct, time stop
max_hold minutes. All stop exits taker + slippage (engine_mom).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

# fade universe = shock detector universe (frozen hl_native_shock_freq coins)
UNIVERSE = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT",
            "ZEC_USDT", "PEPE_USDT", "AVAX_USDT", "TAO_USDT", "DOGE_USDT",
            "SUI_USDT", "NEAR_USDT", "WLD_USDT", "PUMPFUN_USDT", "LTC_USDT",
            "BNB_USDT", "ADA_USDT", "LINK_USDT"]

DEFAULT_PARAMS = {
    "majors": ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT"],
    "shock_atr": 3.5,      # frozen fade detector
    "atr_n": 60,
    "atr_min": 0.001,
    "window": 60,          # breadth lookback minutes
    "breadth_n": 5,        # distinct coins with same-direction shock
    "sides": "both",       # "up" (longs only) | "down" | "both"
    "entry_mode": "maker_retr",
    "retr_atr": 2.0,       # limit offset against cascade dir, in ATR60 of major
    "ttl": 240,            # maker fill window (minutes)
    "delay_min": 0,        # for taker_delay
    "sl_pct": 0.02,        # hard stop
    "trail_pct": 0.02,     # trailing stop distance
    "arm_pct": 0.01,       # favorable move before trail arms
    "max_hold": 1440,      # minutes
    "warmup": 150,
}

SIG_COLS = ["idx", "side", "entry_type", "limit_price", "ttl", "sl_dist",
            "tp_dist", "trail_dist", "trail_arm", "max_hold"]


def _shock_flags(df: pd.DataFrame, p: dict):
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / c
    with np.errstate(invalid="ignore", divide="ignore"):
        ra = ((h - l) / c) / atr
    shock = (ra > p["shock_atr"]) & (atr >= p["atr_min"]) & np.isfinite(atr)
    return shock & (c > o), shock & (c < o), atr


def generate_signals_multi(dfs: dict, params: dict) -> dict:
    p = {**DEFAULT_PARAMS, **params}
    majors = [m for m in p["majors"] if m in dfs]

    # common minute grid from the first major present
    ref = dfs[majors[0]]
    tidx = pd.Index(ref["time"].to_numpy())
    nT = len(tidx)

    up_mat = np.zeros((nT, len(UNIVERSE)), dtype=bool)
    dn_mat = np.zeros_like(up_mat)
    atrs = {}
    for k, coin in enumerate(UNIVERSE):
        if coin not in dfs:
            continue
        up, dn, atr = _shock_flags(dfs[coin], p)
        pos = tidx.get_indexer(dfs[coin]["time"].to_numpy())
        ok = pos >= 0
        up_mat[pos[ok], k] = up[ok]
        dn_mat[pos[ok], k] = dn[ok]
        if coin in majors:
            atrs[coin] = (pos, ok, atr)

    win = int(p["window"])
    b_up = pd.DataFrame(up_mat).rolling(win, min_periods=1).max().sum(axis=1).to_numpy()
    b_dn = pd.DataFrame(dn_mat).rolling(win, min_periods=1).max().sum(axis=1).to_numpy()

    out = {}
    for coin in majors:
        df = dfs[coin]
        close = df["close"].to_numpy(float)
        # ATR of this major on its own index
        _, _, atr = _shock_flags(df, p)  # atr fractional
        # map grid position -> this major's row index
        gpos = tidx.get_indexer(df["time"].to_numpy())  # grid slot per row
        row_of_slot = np.full(nT, -1, dtype=int)
        ok = gpos >= 0
        row_of_slot[gpos[ok]] = np.flatnonzero(ok)

        sigs = []
        for side_name, b in (("long", b_up), ("short", b_dn)):
            if p["sides"] == "up" and side_name == "short":
                continue
            if p["sides"] == "down" and side_name == "long":
                continue
            m = b >= p["breadth_n"]
            trig = np.flatnonzero(m & ~np.concatenate(([False], m[:-1])))
            for t in trig:
                slot = t + int(p["delay_min"]) if p["entry_mode"] == "taker_delay" else t
                if slot >= nT:
                    continue
                i = row_of_slot[slot]
                if i < 0 or i <= p["warmup"] or not np.isfinite(atr[i]):
                    continue
                a = atr[i]
                if p["entry_mode"] == "maker_retr":
                    off = p["retr_atr"] * a
                    lp = close[i] * (1 - off) if side_name == "long" else close[i] * (1 + off)
                    et, ttl = "maker", int(p["ttl"])
                else:
                    et, lp, ttl = "taker", np.nan, 1
                sigs.append((i, side_name, et, lp, ttl, p["sl_pct"], np.nan,
                             p["trail_pct"], p["arm_pct"], int(p["max_hold"])))
        if sigs:
            out[coin] = pd.DataFrame(sigs, columns=SIG_COLS).sort_values("idx")
    return out
