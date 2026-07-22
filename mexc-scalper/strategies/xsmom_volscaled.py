"""Vol-scaled momentum: formation return divided by realized vol over the
same window (a Sharpe-like score). Prefers steady trends over one-pump coins;
a form of quality tilt on top of raw momentum.
"""
import pandas as pd

DEFAULT_PARAMS = {
    "j_weeks": 4,
    "skip_week": False,
    "k": 5,
    "mode": "mn",
    "weighting": "ew",
}
DEFAULT_PARAMS["formation_days"] = DEFAULT_PARAMS["j_weeks"] * 7


def score(closes: pd.DataFrame, sig_date, params, aux) -> pd.Series:
    j = params["j_weeks"] * 7
    hist = closes.loc[:sig_date]
    if len(hist) < j + 1:
        return pd.Series(dtype=float)
    win = hist.iloc[-(j + 1):]
    rets = win.pct_change(fill_method=None).iloc[1:]
    if params["skip_week"]:
        rets = rets.iloc[:-7]
    mu = rets.sum(axis=0)
    sd = rets.std(axis=0)
    return mu / (sd + 1e-9)
