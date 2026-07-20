"""Failed-Pump Second Leg — trapped-longs bounce short.

MECHANISM: when a volume-climax pump is FULLY retraced within `retrace_window`
minutes (close back below the pre-pump price), late longs are trapped
underwater; the first ~1 ATR bounce gets sold into and price makes a second
leg down. This is reversal-momentum CONTINUATION, not a raw climax fade:
entry is a bounce high inside an established down-leg.

The trapped-inventory condition (close < pre-pump level L) is load-bearing:
without it (short any deep drop from post-pump peak) the edge is breakeven-
minus (IS WR .515, avg_r -0.02). With it, IS avg_r flips to +0.08..+0.10.
Set params["require_retrace"] = False to reproduce that falsification control.

Signal state machine (vectorized with ffill):
- pump bar p: z[p] > z_min AND vol_ratio[p] > vr_min; record L = close[p-5]
- signal at i: a pump occurred within the last `retrace_window` bars AND
  close[i] < L AND i is not itself a pump bar; cooldown 90 bars per symbol.

Entry: SHORT limit at close*(1 + k_bounce*atr_pct) (maker, fills only on a
bounce into the trapped-long overhang), ttl=10.
Exit: tp_dist = sl_dist = max(d_atr*atr_pct, sl_floor), 1:1 RR.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from indicators import atr  # noqa: E402

DEFAULT_PARAMS = {
    "z_min": 3.0,          # climax threshold on 5-min return z-score
    "vr_min": 1.5,         # 5-min volume vs 240-min baseline
    "z_window": 720,       # rolling std window for the z-score
    "retrace_window": 90,  # bars after pump in which full retrace must occur
    "k_bounce": 1.5,       # limit offset above close, in ATRs
    "d_atr": 4.0,          # tp/sl distance in ATRs
    "sl_floor": 0.003,     # min stop distance (cost trap: keep >= 0.3%)
    "ttl": 15,
    "cooldown": 90,        # one entry per episode
    "require_retrace": True,  # False = falsification control (no close<L)
}


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    cols = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]
    close = df["close"]
    vol = df["vol"]
    n = len(df)

    # --- causal features -------------------------------------------------
    r5 = close.pct_change(5)
    z = r5 / r5.rolling(p["z_window"]).std()
    vol_ratio = vol.rolling(5).sum() / (vol.rolling(240).mean() * 5)
    atr_pct = (atr(df, 14) / close).to_numpy()

    # --- pump bars and forward-filled episode state ----------------------
    pump = (z > p["z_min"]) & (vol_ratio > p["vr_min"])
    pump = pump.fillna(False).to_numpy()

    idx_arr = np.arange(n, dtype=float)
    last_pump = pd.Series(np.where(pump, idx_arr, np.nan)).ffill().to_numpy()
    age = idx_arr - last_pump                        # NaN before first pump
    L = pd.Series(np.where(pump, close.shift(5), np.nan)).ffill().to_numpy()

    with np.errstate(invalid="ignore"):
        in_window = (age >= 1) & (age <= p["retrace_window"])
        retraced = close.to_numpy() < L
    cand = in_window & ~pump & ~np.isnan(atr_pct)
    if p["require_retrace"]:
        cand &= retraced

    cand_idx = np.flatnonzero(cand)
    if cand_idx.size == 0:
        return pd.DataFrame(columns=cols)

    # --- cooldown: one entry per episode ---------------------------------
    kept = []
    last = -10**9
    for i in cand_idx:
        if i - last >= p["cooldown"]:
            kept.append(i)
            last = i
    kept = np.asarray(kept)

    c = close.to_numpy()[kept]
    ap = atr_pct[kept]
    dist = np.maximum(p["d_atr"] * ap, p["sl_floor"])
    out = pd.DataFrame({
        "idx": kept,
        "side": "short",
        "limit_price": c * (1 + p["k_bounce"] * ap),
        "tp_dist": dist,
        "sl_dist": dist,
        "ttl": p["ttl"],
    })
    return out[cols]
