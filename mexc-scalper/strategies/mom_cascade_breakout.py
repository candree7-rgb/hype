"""MOMENTUM: cascade-confirmed 24h-high breakout on majors (LONG).

Mechanism (the fade's poison as this strategy's food): a 1m close above the
prior 24h high is only worth chasing when the tape shows a MARKET-WIDE
up-cascade — many fade-universe coins printing up-shock candles (the frozen
fade detector: range > 3.5*ATR60, ATR gate) within the last hour. That is the
exact signature of the squeeze/trend days that bleed the cascade-fade
(Nov 17 - Dec 4 2025). Unconfirmed breakouts on majors mean-revert (IS study);
cascade-confirmed ones continue for 24-48h.

Long-only by measurement: downside breakouts show NO continuation on majors in
IS (down-cascades V-bottom) — shorts are not traded, honestly, not by hope.

Entry: taker at next 1m open (breakouts run; fee math via engine_mom:
0.045% taker + 0.03% slippage entry, stop exits same, trail exit taker).
Exits: hard SL below entry, trailing stop armed after +arm, time stop.
One position per symbol at a time (engine enforces).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

UNIVERSE = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT",
            "ZEC_USDT", "PEPE_USDT", "AVAX_USDT", "TAO_USDT", "DOGE_USDT",
            "SUI_USDT", "NEAR_USDT", "WLD_USDT", "PUMPFUN_USDT", "LTC_USDT",
            "BNB_USDT", "ADA_USDT", "LINK_USDT"]

DEFAULT_PARAMS = {
    "majors": ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT"],
    # frozen fade shock detector (not tuned here)
    "shock_atr": 3.5, "atr_n": 60, "atr_min": 0.001,
    "window": 60,          # breadth lookback (minutes)
    "breadth_n": 5,        # distinct coins with up-shock in window
    "lookback": 1440,      # breakout lookback (minutes, prior high)
    "entry_mode": "maker_retest",  # "taker" (market next open) | "maker_retest"
    "ttl": 240,            # maker_retest: fill window (minutes)
    "sl_pct": 0.03,
    "trail_pct": float("nan"),   # no trail: plain time exit captures the drift
    "arm_pct": 0.0,
    "max_hold": 2160,      # 36h time exit
    "warmup": 1500,
}

# FROZEN 2026-07-22 after IS-only search (Jul25-Jan26), 40 engine variants
# across 3 families (squeeze-breadth immediate entry: negative; trend-day ORB:
# negative; cascade-confirmed breakout: broad positive plateau N 4-6,
# sl 2.5-3.5%, hold 24-48h, entry taker or maker-retest). This config is the
# plateau CENTER, not the best IS cell. IS: n=229, WR 45.4%, avg_r +0.217,
# +49.7R/7mo, PF 1.51, maxDD 27.2R, 5/7 months positive, Nov17-Dec4 +4.6R
# (the fade's -64.5R window). Lookahead check: 0 violations.

SIG_COLS = ["idx", "side", "entry_type", "limit_price", "ttl", "sl_dist",
            "tp_dist", "trail_dist", "trail_arm", "max_hold"]


def _up_shock(df: pd.DataFrame, p: dict):
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
    return shock & (c > o)


def generate_signals_multi(dfs: dict, params: dict) -> dict:
    p = {**DEFAULT_PARAMS, **params}
    majors = [m for m in p["majors"] if m in dfs]
    ref = dfs[majors[0]]
    tidx = pd.Index(ref["time"].to_numpy())
    nT = len(tidx)

    up_mat = np.zeros((nT, len(UNIVERSE)), dtype=bool)
    for k, coin in enumerate(UNIVERSE):
        if coin not in dfs:
            continue
        up = _up_shock(dfs[coin], p)
        pos = tidx.get_indexer(dfs[coin]["time"].to_numpy())
        ok = pos >= 0
        up_mat[pos[ok], k] = up[ok]
    b_up = pd.DataFrame(up_mat).rolling(
        int(p["window"]), min_periods=1).max().sum(axis=1).to_numpy()

    out = {}
    for coin in majors:
        df = dfs[coin]
        cl = df["close"].to_numpy(float)
        hi = df["high"].to_numpy(float)
        prevhi = pd.Series(hi).shift(1).rolling(int(p["lookback"])).max().to_numpy()
        gpos = tidx.get_indexer(df["time"].to_numpy())
        bu = np.where(gpos >= 0, b_up[np.clip(gpos, 0, nT - 1)], 0.0)

        cond = (cl > prevhi) & (bu >= p["breadth_n"]) & \
               (np.arange(len(df)) > p["warmup"])
        trig = np.flatnonzero(cond & ~np.concatenate(([False], cond[:-1])))
        if trig.size == 0:
            continue
        if p["entry_mode"] == "maker_retest":
            # rest a limit at the broken 24h-high level (below current close
            # -> passive); fills only on the retest of the breakout level
            out[coin] = pd.DataFrame({
                "idx": trig, "side": "long", "entry_type": "maker",
                "limit_price": prevhi[trig], "ttl": int(p["ttl"]),
                "sl_dist": p["sl_pct"], "tp_dist": np.nan,
                "trail_dist": p["trail_pct"], "trail_arm": p["arm_pct"],
                "max_hold": int(p["max_hold"]),
            })[SIG_COLS]
        else:
            out[coin] = pd.DataFrame({
                "idx": trig, "side": "long", "entry_type": "taker",
                "limit_price": np.nan, "ttl": 1, "sl_dist": p["sl_pct"],
                "tp_dist": np.nan, "trail_dist": p["trail_pct"],
                "trail_arm": p["arm_pct"], "max_hold": int(p["max_hold"]),
            })[SIG_COLS]
    return out
