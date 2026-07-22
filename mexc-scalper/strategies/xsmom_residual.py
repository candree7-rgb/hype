"""Residual momentum: rank on BTC-beta-adjusted trailing returns.

For each coin, estimate beta vs BTC on trailing `beta_days` daily returns,
then the score is the cumulative residual (coin return minus beta * BTC
return) over the formation window. Removes the shared market factor so ranks
reflect idiosyncratic strength, not just high-beta exposure.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "j_weeks": 4,
    "skip_week": False,
    "beta_days": 60,     # beta estimation window
    "k": 5,
    "mode": "mn",
    "weighting": "ew",
}
DEFAULT_PARAMS["formation_days"] = max(
    DEFAULT_PARAMS["j_weeks"] * 7, DEFAULT_PARAMS["beta_days"])


def score(closes: pd.DataFrame, sig_date, params, aux) -> pd.Series:
    j = params["j_weeks"] * 7
    bdays = params["beta_days"]
    hist = closes.loc[:sig_date]
    need = max(j, bdays) + 1
    if len(hist) < need:
        return pd.Series(dtype=float)
    rets = hist.iloc[-(need):].pct_change(fill_method=None).iloc[1:]
    btc = aux["btc"].loc[:sig_date].iloc[-(need):].pct_change(
        fill_method=None).iloc[1:]
    btc = btc.reindex(rets.index)

    bwin = rets.iloc[-bdays:]
    bb = btc.iloc[-bdays:]
    bvar = bb.var()
    if not np.isfinite(bvar) or bvar == 0:
        return pd.Series(dtype=float)
    beta = bwin.apply(lambda col: col.cov(bb)) / bvar

    fwin = rets.iloc[-j:]
    fb = btc.iloc[-j:]
    if params["skip_week"]:
        fwin, fb = fwin.iloc[:-7], fb.iloc[:-7]
    resid = fwin.sub(np.outer(fb.values, beta.values), axis=0)
    return resid.sum(axis=0)
