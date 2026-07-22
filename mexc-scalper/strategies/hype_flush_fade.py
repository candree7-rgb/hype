"""HYPE drift-flush reversion (second, low-confidence HYPE strategy).

Catches multi-candle flushes the 1-min shock detector misses: when the
60-minute move exceeds 10 ATR-units (per-minute fractional ATR60) and NO
1-min shock candle (range > 3 ATR) fired in the last 15 bars (that regime
belongs to hype_shock_fade), rest a limit 3.5 ATR beyond the rolling 60m
extreme, TTL 5, 1:1 at 3 ATR (0.3% floor), cooldown 60.

HONESTY FLAG — low confidence: selected as the best corner of an 18-variant
IS grid where 13/18 variants were flat-to-negative. It survived a
pre-registered holdout bar (Feb-Jun26: n=25, avg_r +0.347, +8.7R, PF 2.0;
bar was n>=25, avg_r>=0.10, tot>0), and IS corner was n=54 avg +0.306 — but
total evidence is n=79 trades. Deploy small, kill if realized WR < breakeven
after ~50 fills. Frequency ~80 trades/yr on HYPE.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "win": 60,            # flush lookback, minutes
    "flush_atr": 10.0,    # W-min move must exceed this many (1-min) ATR units
    "offset_atr": 3.5,    # limit offset beyond rolling W-min extreme
    "no_shock_win": 15,   # skip if a 1-min shock candle fired in last N bars
    "shock_atr": 3.0,     # ... "shock" = range > this x ATR (primary's regime)
    "ttl_entry": 5,
    "cooldown": 60,
    "atr_n": 60,
    "sl_atr_mult": 3.0,
    "sl_floor": 0.003,
    "atr_min": 0.001,
    "warmup": 150,
}

COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    W = int(p["win"])

    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)

    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / close
    with np.errstate(invalid="ignore", divide="ignore"):
        rng_atr = ((high - low) / close) / atr

    move = close / np.concatenate((np.full(W, np.nan), close[:-W])) - 1.0
    roll_low = pd.Series(low).rolling(W).min().to_numpy()
    roll_high = pd.Series(high).rolling(W).max().to_numpy()
    recent_shock = (pd.Series(rng_atr > p["shock_atr"])
                    .rolling(p["no_shock_win"], min_periods=1).max().to_numpy() > 0)

    idx_arr = np.arange(len(df))
    ok = (np.isfinite(move) & np.isfinite(atr) & (idx_arr > p["warmup"])
          & (atr >= p["atr_min"]) & ~recent_shock)
    dn = ok & (move < -p["flush_atr"] * atr)   # flush down -> long
    up = ok & (move > p["flush_atr"] * atr)    # melt up -> short

    cand = np.flatnonzero(dn | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)
    keep, last = [], -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep, int)

    a = atr[keep]
    sl = np.maximum(p["sl_atr_mult"] * a, p["sl_floor"])
    is_long = dn[keep]
    limit = np.where(is_long,
                     roll_low[keep] * (1.0 - p["offset_atr"] * a),
                     roll_high[keep] * (1.0 + p["offset_atr"] * a))

    out = pd.DataFrame({
        "idx": keep,
        "side": np.where(is_long, "long", "short"),
        "limit_price": limit,
        "tp_dist": p.get("tp_over_sl", 1.0) * sl,
        "sl_dist": sl,
        "ttl": int(p["ttl_entry"]),
    })
    passive = np.where(is_long, limit < close[keep], limit > close[keep])
    return out[passive].reset_index(drop=True)
