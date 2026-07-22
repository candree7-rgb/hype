"""Classic time-series momentum (Moskowitz/Ooi/Pedersen style).

Signal at close t: sign of trailing k-period return (or blend of several
lookbacks). Position per asset = sign * (per-asset vol target / realized vol),
capped, divided by number of eligible assets. Rebalanced every period.
An asset is eligible only after `warmup` periods of history (post-listing).
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "lookbacks": [28],        # in periods (days for 1d)
    "vol_window": 30,          # EW span for realized vol
    "asset_vol_target": 0.20,  # annualized per-asset
    "max_asset_lev": 1.0,      # cap on per-asset gross exposure (pre 1/N)
    "warmup": 90,              # periods of history before an asset is eligible
    "ppy": 365,                # periods per year (365 for 1d, 2190 for 4h)
    "rebalance_band": 0.0,     # no-trade band on |target - current| (0 = off)
}


def target_weights(panel: dict, params: dict) -> pd.DataFrame:
    close = panel["close"]
    ppy = params["ppy"]
    ret = close.pct_change()
    vol = ret.ewm(span=params["vol_window"],
                  min_periods=max(5, params["vol_window"] // 2)).std() * np.sqrt(ppy)

    sig = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for k in params["lookbacks"]:
        trail = close / close.shift(k) - 1.0
        sig = sig + np.sign(trail).fillna(0.0)
    sig = sig / len(params["lookbacks"])  # in [-1, 1]

    eligible = close.notna().cumsum() >= params["warmup"]
    scale = (params["asset_vol_target"] / vol).clip(upper=params["max_asset_lev"])
    raw = (sig * scale).where(eligible, 0.0).fillna(0.0)
    n_elig = eligible.sum(axis=1).clip(lower=1)
    w = raw.div(n_elig, axis=0)

    band = params.get("rebalance_band", 0.0)
    if band > 0:
        w_np = w.values
        out = np.zeros_like(w_np)
        prev = np.zeros(w_np.shape[1])
        for i in range(w_np.shape[0]):
            tgt = w_np[i]
            move = np.abs(tgt - prev) > band * np.maximum(np.abs(tgt), 1e-12)
            newly_flat = (tgt == 0) & (prev != 0)
            prev = np.where(move | newly_flat, tgt, prev)
            out[i] = prev
        w = pd.DataFrame(out, index=w.index, columns=w.columns)
    return w
