"""Example strategy (engine smoke test): mean-reversion limit below fast dips.

Contract for all strategies in this folder:
  generate_signals(df, params) -> DataFrame[idx, side, limit_price, tp_dist, sl_dist, ttl]
  - df has columns time, open, high, low, close, vol, amount, dt (UTC)
  - the signal at row i may only use information from rows <= i
  - limit_price must be below close[idx] for longs, above for shorts
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from indicators import atr, ema  # noqa: E402

DEFAULT_PARAMS = {"atr_n": 30, "entry_atr": 1.5, "risk_atr": 1.0, "ttl": 15, "trend_ema": 240}


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    a = atr(df, p["atr_n"])
    trend = ema(df["close"], p["trend_ema"])
    rows = []
    close = df["close"]
    for i in range(p["trend_ema"], len(df)):
        if close.iloc[i] > trend.iloc[i]:
            dist = a.iloc[i] * p["entry_atr"]
            limit = close.iloc[i] - dist
            r = a.iloc[i] * p["risk_atr"] / limit
            rows.append({"idx": i, "side": "long", "limit_price": limit,
                         "tp_dist": r, "sl_dist": r, "ttl": p["ttl"]})
    if not rows:
        return pd.DataFrame(columns=["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"])
    out = pd.DataFrame(rows)
    # avoid stacking a new order every single minute: keep every 10th
    return out.iloc[::10].reset_index(drop=True)
