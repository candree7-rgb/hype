"""Funding-extreme mean reversion (time-series, per coin).

When the 8h funding rate printed at boundary t is in its coin-specific rolling
extreme percentile AND economically meaningful (abs floor), position AGAINST
the crowded side at the boundary close: extreme positive funding -> short
(collect funding + fade crowded longs), extreme negative -> long. Hold a fixed
number of 8h periods; re-triggering while in position extends the hold.

Vol-targeted sizing: each position sized to pos_vol (annualized); portfolio
gross capped.

VERDICT (2026-07-22, IS 2023-07..2025-06 only; never taken to holdout):
- short_side (the textbook trade — short extreme-positive funding): -39%/yr,
  Sharpe -0.93. DEAD WRONG in crypto: extreme funding coincides with trends
  whose drift (~+300bp/3d) is 10x the funding collected (~28bp/3d).
- long_side (long extreme-negative funding, squeeze catch): +26-70%/yr IS
  depending on hold, Sharpe up to 1.9 — but BTC-hedged Sharpe only 1.07 and
  2025H1 (most recent IS half) already NEGATIVE hedged (-51bp/3d). Judged
  regime-fragile; not selected as a holdout finalist.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "window": 270,        # rolling pctile window in 8h events (~90d)
    "min_obs": 180,
    "pct": 0.975,         # extreme percentile threshold (two-sided)
    "floor_8h": 0.0005,   # abs funding floor per 8h (5 bp) to be worth trading
    "hold": 9,            # hold periods (9 x 8h = 3 days)
    "pos_vol": 0.20,      # annualized vol target per position
    "vol_win": 90,        # 8h periods for realized-vol estimate (~30d)
    "max_gross": 3.0,     # portfolio gross cap (sum |w|)
    "w_cap": 0.5,         # per-position weight cap
    "long_side": True,    # trade extreme-negative-funding longs
    "short_side": True,   # trade extreme-positive-funding shorts
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
        if p["short_side"]:
            sig[(pct >= p["pct"]) & (f >= p["floor_8h"])] = -1.0
        if p["long_side"]:
            sig[(pct <= 1 - p["pct"]) & (f <= -p["floor_8h"])] = 1.0
        # extend each trigger for `hold` periods (re-trigger refreshes)
        held = sig.replace(0.0, np.nan).ffill(limit=p["hold"] - 1).fillna(0.0)
        w = held * np.minimum(p["w_cap"], p["pos_vol"] / vol.clip(lower=0.05))
        W[sym] = w.fillna(0.0)
    W = pd.DataFrame(W, index=grid)
    gross = W.abs().sum(axis=1)
    scale = np.minimum(1.0, p["max_gross"] / gross.replace(0, np.nan))
    return W.mul(scale.fillna(1.0), axis=0)
