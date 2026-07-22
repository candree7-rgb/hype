"""Classic cross-sectional momentum score: trailing J-week return, optional
skip-week (rank on return from t-J*7d to t-7d, excluding the most recent week
to sidestep short-term reversal).

Score at sig_date uses only closes <= sig_date.
"""
import pandas as pd

DEFAULT_PARAMS = {
    "j_weeks": 4,        # formation window in weeks
    "skip_week": False,  # exclude most recent week from formation
    # engine-level (read by runner): k, mode, weighting
    "k": 5,
    "mode": "mn",
    "weighting": "ew",
}


def _formation_days(params):
    return params["j_weeks"] * 7


DEFAULT_PARAMS["formation_days"] = _formation_days(DEFAULT_PARAMS)


def score(closes: pd.DataFrame, sig_date, params, aux) -> pd.Series:
    j = params["j_weeks"] * 7
    hist = closes.loc[:sig_date]
    end = hist.iloc[-8] if params["skip_week"] else hist.iloc[-1]
    if len(hist) < j + 1:
        return pd.Series(dtype=float)
    start = hist.iloc[-(j + 1)]
    return end / start - 1.0
