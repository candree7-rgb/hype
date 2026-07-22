"""Vol-adjusted EMA crossover trend following.

Signal at close t: sign(EMA_fast - EMA_slow), optionally soft-scaled by the
crossover gap in vol units (clipped), so position fades near crossovers
instead of flipping hard. Vol-targeted per asset, 1/N across eligible assets.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "fast": 20,
    "slow": 100,
    "soft": False,             # False = pure sign; True = clipped gap/vol scaling
    "soft_clip": 2.0,          # gap in vol units at which position saturates
    "vol_window": 30,
    "asset_vol_target": 0.20,
    "max_asset_lev": 1.0,
    "warmup": 90,
    "ppy": 365,
}


def target_weights(panel: dict, params: dict) -> pd.DataFrame:
    close = panel["close"]
    ppy = params["ppy"]
    ret = close.pct_change()
    vol = ret.ewm(span=params["vol_window"],
                  min_periods=max(5, params["vol_window"] // 2)).std() * np.sqrt(ppy)

    ema_f = close.ewm(span=params["fast"], min_periods=params["fast"]).mean()
    ema_s = close.ewm(span=params["slow"], min_periods=params["slow"]).mean()
    gap = (ema_f - ema_s) / close

    if params["soft"]:
        # gap in per-period vol units, saturate at soft_clip
        per_vol = vol / np.sqrt(ppy)
        sig = (gap / (per_vol * params["soft_clip"])).clip(-1, 1)
    else:
        sig = np.sign(gap)

    eligible = close.notna().cumsum() >= params["warmup"]
    scale = (params["asset_vol_target"] / vol).clip(upper=params["max_asset_lev"])
    raw = (sig * scale).where(eligible, 0.0).fillna(0.0)
    n_elig = eligible.sum(axis=1).clip(lower=1)
    return raw.div(n_elig, axis=0)
