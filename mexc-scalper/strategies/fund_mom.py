"""Funding momentum (measured, not assumed).

IS measurement (2023-07..2025-06) showed extreme-HIGH funding predicts price
CONTINUATION at 1-7d horizons — consistently across half-years and with a
BTC hedge — not reversion. High funding = aggressive perp longs paying up in
a coin that is running; the run persists longer than the funding cost.

Rule: at boundary t, if coin's funding pctile (rolling `window`) >= `pct` and
funding >= floor, go LONG for `hold` periods (re-trigger extends). Optional
BTC short hedge of `hedge` x the alt gross (crash protection / beta removal).
We PAY funding while holding — engine debits it; the edge must survive that.

FINAL VERDICT (2026-07-22): DEFAULT_PARAMS below = finalist A, sized to ~20%
realized vol on IS. IS 2023-07..2025-06: +43.7%/yr, Sharpe 1.81, maxDD -12.8%
(profits concentrated in 5 alt-season months; active 21% of periods).
HOLDOUT 2025-07..2026-06 (opened once): -1.1%, avg gross 0.01 — the signal
fired 28 coin-events vs 2569 in IS (cross-coin mean funding -0.08bp/8h all
year: bear, no funding manias). Hedged variant (hedge=1.0, pos_vol=0.105,
w_cap=0.25, max_gross=1.5): IS +32.1%/Sharpe 1.43, holdout +0.7%.
NOT validated OOS — the holdout contained no instances of the regime this
harvests. Structurally it stayed out of the bear (worst holdout month -1.7%)
rather than blowing up. Deploy only as a dormant regime-conditional sleeve,
if at all.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "window": 270,
    "min_obs": 180,
    "pct": 0.95,
    "floor_8h": 0.0002,   # funding must be >= 2bp/8h (economically hot)
    "hold": 9,
    "pos_vol": 0.045,     # sized so IS realized portfolio vol ~ 20% ann
    "vol_win": 90,
    "w_cap": 0.112,
    "max_gross": 0.67,
    "hedge": 0.0,         # BTC short = hedge * sum(alt weights)
}


def target_weights(data, grid, params):
    p = params
    W = {}
    for sym, df in data.items():
        f = df["funding"].reindex(grid)
        c = df["close"].reindex(grid)
        pct = f.rolling(p["window"], min_periods=p["min_obs"]).rank(pct=True)
        ret = c.pct_change(fill_method=None)
        vol = ret.rolling(p["vol_win"], min_periods=30).std() * np.sqrt(3 * 365)
        sig = pd.Series(0.0, index=grid)
        sig[(pct >= p["pct"]) & (f >= p["floor_8h"])] = 1.0
        held = sig.replace(0.0, np.nan).ffill(limit=p["hold"] - 1).fillna(0.0)
        w = held * np.minimum(p["w_cap"], p["pos_vol"] / vol.clip(lower=0.05))
        W[sym] = w.fillna(0.0)
    W = pd.DataFrame(W, index=grid)
    if p["hedge"] > 0 and "BTC" in W.columns:
        alt_gross = W.drop(columns=["BTC"]).clip(lower=0).sum(axis=1)
        W["BTC"] = W["BTC"] - p["hedge"] * alt_gross
    gross = W.abs().sum(axis=1)
    scale = np.minimum(1.0, p["max_gross"] / gross.replace(0, np.nan))
    return W.mul(scale.fillna(1.0), axis=0)
