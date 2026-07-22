"""Donchian / turtle-style breakout trend following (long AND short).

Entry signal at close t: close breaks above the prior N-period high -> long;
below the prior N-period low -> short. Exit when close crosses the ATR
trailing stop (chandelier) or the opposite M-period extreme. All decisions on
CLOSE, executed by the engine at next OPEN. Position sized to a per-asset vol
target, divided by eligible-asset count.
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "entry_n": 55,             # breakout lookback
    "exit_n": 20,              # opposite-extreme exit lookback
    "atr_window": 20,
    "atr_mult": 3.0,           # chandelier trail distance
    "vol_window": 30,
    "asset_vol_target": 0.20,
    "max_asset_lev": 1.0,
    "warmup": 90,
    "ppy": 365,
}


def target_weights(panel: dict, params: dict) -> pd.DataFrame:
    close, high, low = panel["close"], panel["high"], panel["low"]
    ppy = params["ppy"]
    ret = close.pct_change()
    vol = ret.ewm(span=params["vol_window"],
                  min_periods=max(5, params["vol_window"] // 2)).std() * np.sqrt(ppy)

    pc = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - pc).abs(), (low - pc).abs()))
    atr = tr.ewm(span=params["atr_window"],
                 min_periods=params["atr_window"] // 2).mean()

    n = params["entry_n"]
    m = params["exit_n"]
    hi_n = high.rolling(n).max().shift(1)
    lo_n = low.rolling(n).min().shift(1)
    hi_m = high.rolling(m).max().shift(1)
    lo_m = low.rolling(m).min().shift(1)

    eligible = (close.notna().cumsum() >= params["warmup"]).values
    C, H, L = close.values, high.values, low.values
    HN, LN, HM, LM = hi_n.values, lo_n.values, hi_m.values, lo_m.values
    A = atr.values
    T, S = C.shape

    pos = np.zeros((T, S))
    state = np.zeros(S)            # -1 / 0 / +1
    trail = np.full(S, np.nan)     # trailing stop level
    for t in range(T):
        for s in range(S):
            c = C[t, s]
            if not eligible[t, s] or np.isnan(c) or np.isnan(HN[t, s]) or np.isnan(A[t, s]):
                state[s] = 0.0
                trail[s] = np.nan
                continue
            st = state[s]
            if st > 0:
                trail[s] = max(trail[s], c - params["atr_mult"] * A[t, s])
                if c < trail[s] or c < LM[t, s]:
                    st = 0.0
            elif st < 0:
                trail[s] = min(trail[s], c + params["atr_mult"] * A[t, s])
                if c > trail[s] or c > HM[t, s]:
                    st = 0.0
            if st == 0.0:
                if c > HN[t, s]:
                    st = 1.0
                    trail[s] = c - params["atr_mult"] * A[t, s]
                elif c < LN[t, s]:
                    st = -1.0
                    trail[s] = c + params["atr_mult"] * A[t, s]
            state[s] = st
            pos[t, s] = st

    sig = pd.DataFrame(pos, index=close.index, columns=close.columns)
    scale = (params["asset_vol_target"] / vol).clip(upper=params["max_asset_lev"])
    elig_df = pd.DataFrame(eligible, index=close.index, columns=close.columns)
    raw = (sig * scale).where(elig_df, 0.0).fillna(0.0)
    n_elig = elig_df.sum(axis=1).clip(lower=1)
    return raw.div(n_elig, axis=0)
