"""BTC-Shock Beta-Residual Overshoot Fade (cross-coin relative value).

Mechanism: when BTC prints a 1-min shock, alts that moved MORE than their
beta-implied amount in that same minute (positive residual on an up-shock,
negative residual on a down-shock) are overshoots driven by correlated
liquidations and revert toward beta*BTC over the next minutes. Fade the
overshooters with a deep maker offset at 1:1 RR.

Causality / truncation safety: BTC data is loaded from the cached parquet at
module level and left-joined onto the alt frame by exact 'time'. All BTC
features (bret, bstd) are computed on BTC's own full timeline but only ever
referenced at the alt row's own timestamp; rolling windows look strictly
backward. Truncating the alt frame therefore leaves all earlier joined rows
and signals identical.

BTC itself is excluded (close series identical to the joined BTC close).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"

_EMPTY = pd.DataFrame(columns=["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"])

_CACHE: dict = {}


def _btc_features() -> pd.DataFrame:
    """BTC close/return/vol features on BTC's own timeline (module-level cache)."""
    if "btc" not in _CACHE:
        b = pd.read_parquet(DATA_DIR / "BTC_USDT_Min1.parquet")
        b = b[["time", "close"]].rename(columns={"close": "btc_close"})
        b = b.sort_values("time").reset_index(drop=True)
        b["bret"] = b["btc_close"].ffill().pct_change()
        b["bstd"] = b["bret"].rolling(240).std()
        _CACHE["btc"] = b[["time", "btc_close", "bret", "bstd"]]
    return _CACHE["btc"]


DEFAULT_PARAMS = {
    "shock_sigma": 2.5,   # BTC 1-min |return| must exceed sigma * rolling std
    "shock_min": 0.0015,  # ... and this absolute floor
    "resid_min": 0.0010,  # alt residual beyond beta-implied move
    "off": 0.003,         # maker limit offset from signal close (fade direction)
    "tp": 0.004,
    "sl": 0.004,
    "ttl": 10,
    "cooldown": 10,       # per-symbol bars between signals
    "beta_win": 1440,     # rolling beta window (1 day of minutes)
    # optional filters (0/None disables) — must earn their place on IS
    "hour_lo": None,      # e.g. 13 -> only signal when UTC hour in [hour_lo, hour_hi]
    "hour_hi": None,
    "min_natr": 0.0,      # rolling mean |1-min return| of the alt must exceed this
    "natr_win": 60,
    "funding_blackout": 0,  # skip signals within +/- this many minutes of 00/08/16 UTC
}


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    btc = _btc_features()

    d = df[["time", "close"]].merge(btc, on="time", how="left")

    # Exclude BTC itself: joined close identical to own close.
    both = d["close"].notna() & d["btc_close"].notna()
    if both.any() and np.allclose(d.loc[both, "close"], d.loc[both, "btc_close"],
                                  rtol=1e-9):
        return _EMPTY.copy()

    close = d["close"]
    ret = close.pct_change()
    bret = d["bret"]
    bstd = d["bstd"]

    # Rolling beta of alt on BTC (backward-looking), clipped to sane range.
    cov = ret.rolling(p["beta_win"]).cov(bret)
    var = bret.rolling(p["beta_win"]).var()
    beta = (cov / var).clip(0.2, 4.0)
    resid = ret - beta * bret

    valid = beta.notna() & bret.notna() & bstd.notna()
    shock_up = valid & (bret > p["shock_sigma"] * bstd) & (bret > p["shock_min"])
    shock_dn = valid & (bret < -p["shock_sigma"] * bstd) & (bret < -p["shock_min"])

    filt = pd.Series(True, index=d.index)
    if p["hour_lo"] is not None and p["hour_hi"] is not None:
        hour = (d["time"] % 86400) // 3600
        filt &= (hour >= p["hour_lo"]) & (hour <= p["hour_hi"])
    if p["min_natr"] and p["min_natr"] > 0:
        natr = ret.abs().rolling(p["natr_win"]).mean()
        filt &= natr > p["min_natr"]
    if p["funding_blackout"] and p["funding_blackout"] > 0:
        sec_in_8h = d["time"] % 28800
        dist_min = np.minimum(sec_in_8h, 28800 - sec_in_8h) / 60.0
        filt &= pd.Series(dist_min > p["funding_blackout"], index=d.index)

    short_mask = (shock_up & (resid > p["resid_min"]) & filt).to_numpy()
    long_mask = (shock_dn & (resid < -p["resid_min"]) & filt).to_numpy()

    cand = np.flatnonzero(short_mask | long_mask)
    if cand.size == 0:
        return _EMPTY.copy()

    # Per-symbol cooldown across both sides.
    kept = []
    last = -10**9
    for i in cand:
        if i - last >= p["cooldown"]:
            kept.append(int(i))
            last = i
    kept = np.asarray(kept, dtype=int)

    c = close.to_numpy()[kept]
    is_short = short_mask[kept]
    limit = np.where(is_short, c * (1 + p["off"]), c * (1 - p["off"]))

    return pd.DataFrame({
        "idx": kept,
        "side": np.where(is_short, "short", "long"),
        "limit_price": limit,
        "tp_dist": p["tp"],
        "sl_dist": p["sl"],
        "ttl": p["ttl"],
    })
