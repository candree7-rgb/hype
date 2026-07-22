"""Funding-spike event strategy.

Event: the 8h-to-8h CHANGE in funding at boundary t exceeds k sigma of its
trailing distribution (63-period std). A sudden funding jump marks a fresh
crowding impulse. Direction set by `mode`:
  - "fade":   trade against the spike side (spike up in funding -> short)
  - "follow": trade with it (spike up -> long)
Hold fixed periods. Sized like the other sleeves.

VERDICT (2026-07-22, IS 2023-07..2025-06 only; never taken to holdout):
fade -17%/yr (Sharpe -0.56) — fading funding spikes loses, same reason the
short-crowd trades die (spikes mark trend ignition, not exhaustion).
follow +16%/yr (Sharpe 0.44) — positive but strictly dominated by fund_mom
(same mechanism, weaker expression). Neither selected as finalist.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "k": 4.0,
    "sig_win": 63,
    "min_obs": 40,
    "hold": 6,
    "mode": "fade",
    "floor_8h": 0.0003,   # spike must land at an economically big abs level
    "pos_vol": 0.20,
    "vol_win": 90,
    "w_cap": 0.5,
    "max_gross": 3.0,
}


def target_weights(data, grid, params):
    p = params
    W = {}
    for sym, df in data.items():
        f = df["funding"].reindex(grid)
        c = df["close"].reindex(grid)
        dz = f.diff() / f.diff().rolling(p["sig_win"], min_periods=p["min_obs"]).std()
        ret = c.pct_change(fill_method=None)
        vol = ret.rolling(p["vol_win"], min_periods=30).std() * np.sqrt(3 * 365)
        sig = pd.Series(0.0, index=grid)
        up = (dz >= p["k"]) & (f >= p["floor_8h"])
        dn = (dz <= -p["k"]) & (f <= -p["floor_8h"])
        s = -1.0 if p["mode"] == "fade" else 1.0
        sig[up] = s
        sig[dn] = -s
        held = sig.replace(0.0, np.nan).ffill(limit=p["hold"] - 1).fillna(0.0)
        w = held * np.minimum(p["w_cap"], p["pos_vol"] / vol.clip(lower=0.05))
        W[sym] = w.fillna(0.0)
    W = pd.DataFrame(W, index=grid)
    gross = W.abs().sum(axis=1)
    scale = np.minimum(1.0, p["max_gross"] / gross.replace(0, np.nan))
    return W.mul(scale.fillna(1.0), axis=0)
