"""Cross-sectional carry harvest.

Rank coins by trailing mean funding (annualized). SHORT the top-N highest-
funding coins (collect positive funding), LONG the bottom-N lowest-funding
coins (collect negative funding) — dollar-balanced long/short so the book is
~delta-neutral. Rank-buffer hysteresis to limit turnover: exit only when the
coin leaves the top/bottom `exit_rank`. Rebalance daily at the 00:00 boundary.

Optional `require_sign`: only short if trailing funding actually > +floor,
only long if < -floor (pure carry, not just relative value).

VERDICT (2026-07-22, IS 2023-07..2025-06 only; never taken to holdout):
CLEAN NEGATIVE. Classic carry harvest is -26%/yr (Sharpe -0.45); pure
relative-value variant -22%/yr. The ~10-15% ann funding collected on the
short side is overwhelmed by adverse price drift: high-funding coins keep
rallying (funding momentum), low-funding coins keep bleeding. The textbook
delta-neutral carry basket does NOT work intra-crypto perps at 3d-lookback
rebalancing on 2023-2025 data.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "lookback": 9,        # 8h periods for trailing mean funding (3d)
    "top_n": 4,
    "exit_rank": 7,       # hysteresis: stay until rank worse than this
    "require_sign": True,
    "floor_8h": 0.0001,   # 1 bp per 8h (~11%/yr) min carry to enter
    "pos_vol": 0.20,
    "vol_win": 90,
    "w_cap": 0.4,
    "max_gross": 3.0,
    "rebalance_hour": 0,  # daily at 00:00 UTC
}


def target_weights(data, grid, params):
    p = params
    closes = pd.DataFrame({s: d["close"].reindex(grid) for s, d in data.items()})
    funding = pd.DataFrame({s: d["funding"].reindex(grid) for s, d in data.items()})
    carry = funding.rolling(p["lookback"], min_periods=p["lookback"]).mean()
    carry = carry.where(closes.notna())
    ret = closes.pct_change(fill_method=None)
    vol = ret.rolling(p["vol_win"], min_periods=30).std() * np.sqrt(3 * 365)

    # ranks at each boundary: 1 = highest carry (short candidate)
    rk_hi = carry.rank(axis=1, ascending=False)
    rk_lo = carry.rank(axis=1, ascending=True)

    idx = grid
    reb = idx.hour == p["rebalance_hour"]
    short_open = (rk_hi <= p["top_n"])
    long_open = (rk_lo <= p["top_n"])
    if p["require_sign"]:
        short_open &= carry >= p["floor_8h"]
        long_open &= carry <= -p["floor_8h"]
    short_keep = (rk_hi <= p["exit_rank"])
    long_keep = (rk_lo <= p["exit_rank"])
    if p["require_sign"]:
        short_keep &= carry > 0
        long_keep &= carry < 0

    cols = closes.columns
    n = len(idx)
    in_short = np.zeros((n, len(cols)), dtype=bool)
    in_long = np.zeros((n, len(cols)), dtype=bool)
    so, lo = short_open.values, long_open.values
    sk, lk = short_keep.values, long_keep.values
    for i in range(1, n):
        if reb[i]:
            in_short[i] = so[i] | (in_short[i - 1] & sk[i])
            in_long[i] = lo[i] | (in_long[i - 1] & lk[i])
        else:
            in_short[i] = in_short[i - 1]
            in_long[i] = in_long[i - 1]
    sgn = pd.DataFrame(in_long.astype(float) - in_short.astype(float), index=idx, columns=cols)
    w = sgn * np.minimum(p["w_cap"], p["pos_vol"] / vol.clip(lower=0.05))
    w = w.fillna(0.0)
    gross = w.abs().sum(axis=1)
    scale = np.minimum(1.0, p["max_gross"] / gross.replace(0, np.nan))
    return w.mul(scale.fillna(1.0), axis=0)
