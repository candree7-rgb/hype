"""HL-native shock overshoot fade — high-frequency variant (FROZEN WINNER).

Same cascade-overshoot mechanism as shock_hl_variant (the incumbent), with
three levers moved to trade HL's fee model on frequency rather than depth:

- shock_atr 4.0 -> 3.5: milder cascades qualify (more signals),
- offset_atr 3.0 -> 2.5: shallower resting limit (higher fill rate; the edge
  cliff sits below 2.5 — offsets <= 2.25 collapse into adverse selection),
- cooldown 20 -> 15 bars: more shocks per cluster tradable.

The frozen atr_min=0.001 quality gate (skip when 3*ATR < 0.30%) is kept — it
is what makes shallow/frequent viable at HL's 0.015%/0.045% fees. Rationale:
per-trade edge across offset/shock variants is flat within noise, so at equal
avg_r the config with more fills wins on total R; fee drag is already paid
for by the gate, not by depth.

FROZEN CONFIG (2026-07-21), tuned on IS Jul-2025..Jan-2026 only; 55 variants
tried across 4 families (asymmetric TP: dead, monotone worse for tp_over_sl
>1; two-rung ladders: no improvement over best single rung at equal risk;
5m-detection: signal too rare on 12mo Binance, n~300/7mo; vol-regime gate:
raises avg_r but halves n, loses on total R). Selected as the best IS config
by total_r BEFORE the holdout was opened.

Holdout Feb-Jun-2026, 12mo Binance data, HL base fees (engine_hl):
n=2115, WR 62.6% vs breakeven 56.3%, avg_r +0.1327, total +280.6R, PF 1.29,
5/5 months positive, 18/18 symbols positive, top coin 14.7% of holdout R.
Incumbent same holdout: +0.1386, n=1233, +170.9R (avg_r diff is noise,
Welch t=-0.16); this variant delivers +64% more total R at equal per-trade
edge. Stress (holdout): see RESULTS notes (1.5x slippage, HL fee tier 1).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "shock_atr": 3.5,     # candle range must exceed this multiple of ATR
    "offset_atr": 2.5,    # limit offset beyond shock extreme, in ATR
    "ttl_entry": 5,       # fill only during cascade extension
    "cooldown": 15,       # bars, shared across sides per symbol
    "atr_n": 60,
    "sl_atr_mult": 3.0,   # stop distance in ATR
    "sl_floor": 0.003,    # minimum stop distance (fraction)
    "tp_over_sl": 1.0,    # 1:1
    "atr_min": 0.001,     # skip signal entirely if fractional ATR < atr_min
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
    atr = pd.Series(tr).rolling(p["atr_n"]).mean().to_numpy() / close  # fractional

    with np.errstate(invalid="ignore", divide="ignore"):
        rng_atr = ((high - low) / close) / atr

    idx_arr = np.arange(len(df))
    shock = (rng_atr > p["shock_atr"]) & (idx_arr > p["warmup"]) & np.isfinite(atr)
    down = shock & (close < open_)   # -> long fade below the low
    up = shock & (close > open_)     # -> short fade above the high

    cand = np.flatnonzero(down | up)
    if cand.size == 0:
        return pd.DataFrame(columns=COLS)

    # shared per-symbol cooldown across both sides (all shocks consume
    # cooldown; the ATR gate filters afterwards)
    keep = []
    last = -10**9
    cd = int(p["cooldown"])
    for i in cand:
        if i - last >= cd:
            keep.append(i)
            last = i
    keep = np.asarray(keep)

    a = atr[keep]
    if p["atr_min"] > 0:
        ok = a >= p["atr_min"]
        keep, a = keep[ok], a[ok]
    if keep.size == 0:
        return pd.DataFrame(columns=COLS)

    sl = np.maximum(p["sl_atr_mult"] * a, p["sl_floor"])
    tp = p["tp_over_sl"] * sl
    is_long = down[keep]

    limit = np.where(is_long,
                     low[keep] * (1.0 - p["offset_atr"] * a),
                     high[keep] * (1.0 + p["offset_atr"] * a))

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
