"""Displacement/Volume-Climax Candle Overshoot Catch.

Mechanism: a single 1-min candle whose range is a large multiple of its own
ATR (optionally confirmed by extreme volume-z) marks a liquidation/stop
cascade in progress. The cascade typically extends a bit further within
minutes, then snaps back. A limit resting deep beyond the shock extreme with
a very short TTL fills only if the cascade continues (against forced taker
flow) and then mean-reverts.

Time-anchored, not level-anchored. Edge is monotone in offset depth (shallow
offsets lose to adverse selection); edge dies if TTL widens.

Hardening notes (2026-07-20, IS-only evaluation, OOS untouched):
- Stress test with doubled stop slippage (0.06%): OOS avg_r +0.117, WR 60.3%
  vs breakeven 55.1% (+5.2pp), PF 1.24, 14/18 symbols positive — passes.
- Per-symbol concentration is noise, not structure: IS-vs-OOS per-symbol
  avg_r Spearman = 0.13; the OOS-negative symbols (BNB/LINK/NEAR/TAO) do not
  match the IS-negative set, and no volatility/price/volume feature separates
  them (|rho| <= 0.33). No rule-based symbol filter is justified.
- Filters tested on IS only and REJECTED (all degrade or are no-ops):
  ATR-window blend [30,60,120]/[45,60,90] (avg_r 0.176/0.191 vs 0.192),
  close-position confirmation <=0.3/0.4/0.5 (0.171-0.176), mega-shock cap
  rng_atr<8/10 (0.187/0.166), risk cap 0.8-1.2% (no-op), tiny-ATR gate
  (sl_floor-bound trades are IS-profitable at +0.18), sl_floor 0.0035/0.004
  (0.138/0.096). Volz gate already rejected in spec. Params stay at defaults.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "shock_atr": 4.0,     # candle range must exceed this multiple of ATR
    "offset_atr": 3.0,    # limit offset beyond shock extreme, in ATR
    "ttl_entry": 5,       # very short TTL: fill only during cascade extension
    "use_volz": False,    # optional volume-z confirmation gate
    "volz_min": 3.0,
    "cooldown": 20,       # bars, shared across sides per symbol
    "atr_n": 60,
    "volz_n": 120,
    "tp_atr_mult": 3.0,
    "sl_floor": 0.003,
    "warmup": 150,
}

COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}

    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)

    # True range (causal): uses previous close only
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / close  # fractional ATR

    rng_atr = ((high - low) / close) / atr

    if p["use_volz"]:
        amt = df["amount"].astype(float)
        mu = amt.rolling(p["volz_n"]).mean()
        sd = amt.rolling(p["volz_n"]).std()
        volz = ((amt - mu) / sd).to_numpy()
        vol_ok = volz > p["volz_min"]
    else:
        vol_ok = np.ones(len(df), dtype=bool)

    idx_arr = np.arange(len(df))
    shock = (rng_atr > p["shock_atr"]) & vol_ok & (idx_arr > p["warmup"]) & np.isfinite(atr)
    down = shock & (close < open_)   # -> long fade below the low
    up = shock & (close > open_)     # -> short fade above the high

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)

    # shared per-symbol cooldown across both sides
    keep = []
    last = -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep)

    a = atr[keep]
    risk = np.maximum(p["tp_atr_mult"] * a, p["sl_floor"])
    is_long = down[keep]

    limit = np.where(is_long,
                     low[keep] * (1.0 - p["offset_atr"] * a),
                     high[keep] * (1.0 + p["offset_atr"] * a))

    out = pd.DataFrame({
        "idx": keep,
        "side": np.where(is_long, "long", "short"),
        "limit_price": limit,
        "tp_dist": risk,
        "sl_dist": risk,
        "ttl": int(p["ttl_entry"]),
    })
    # guard: limit must be strictly passive vs close[i] (true by construction,
    # but enforce anyway)
    passive = np.where(is_long, limit < close[keep], limit > close[keep])
    return out[passive].reset_index(drop=True)
