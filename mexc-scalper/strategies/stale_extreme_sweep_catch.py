"""Stop-Cascade Overshoot Ladder at Aged Fresh Extremes.

Mechanism: stops cluster beyond multi-hour swing extremes. When price breaks a
fresh (single-touch) 8-hour high/low in an elevated-vol regime, the stop-market
cascade overshoots; maker limits resting DEEP beyond the level are filled by
the cascade itself and mean-revert. A 3-rung depth ladder harvests the whole
overshoot distribution and de-risks the offset parameter.

Contract: generate_signals(df, params) -> DataFrame[idx, side, limit_price,
tp_dist, sl_dist, ttl]. Signal at row i uses only rows <= i (all features are
trailing rolling / shifted — causal by construction).
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "atr_n": 60,             # ATR window (minutes)
    "lookback_n": 600,       # swing-extreme lookback (10h; IS edge rises with level age)
    "rungs": [3.0, 4.0, 5.0],  # ladder depths in ATR beyond the level
    "approach_atr": 2.0,     # max distance of close beyond the level to trigger
    "regime_on": True,       # require ATR above its long median
    "regime_med_n": 10080,   # 7d median window for regime
    "regime_min_periods": 1440,
    "fresh_only": True,      # require exactly one touch-group in the window
    "touch_tol": 0.0012,     # touch tolerance (fraction of level)
    "group_gap": 10,         # bars separating touch groups
    "tp_atr_mult": 3.0,      # tp = sl = max(tp_atr_mult*ATR, sl_floor), 1:1
    "sl_floor": 0.003,       # 0.3% floor keeps losses <= ~1.17R
    "ttl": 30,
    "cooldown": 30,          # bars, per side
    "warmup": 540,
}


def _apply_cooldown(idxs: np.ndarray, cooldown: int) -> list:
    kept, last = [], -10**9
    for i in idxs:
        if i - last > cooldown:
            kept.append(int(i))
            last = i
    return kept


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    n = len(df)
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    # --- ATR as fraction of price -------------------------------------------
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(p["atr_n"]).mean() / close

    # --- prior extremes over [i-lookback, i-1] ------------------------------
    lb = p["lookback_n"]
    prior_low = low.shift(1).rolling(lb).min()
    prior_high = high.shift(1).rolling(lb).max()

    # --- vol regime ---------------------------------------------------------
    if p["regime_on"]:
        atr_med = atr.rolling(p["regime_med_n"],
                              min_periods=p["regime_min_periods"]).median()
        regime = atr > atr_med
    else:
        regime = pd.Series(True, index=df.index)

    # --- freshness: exactly one touch-group in the trailing window ----------
    tol, gap = p["touch_tol"], p["group_gap"]
    # touch[j]: bar j probes its own trailing extreme (near-new low/high)
    touch_lo = low <= prior_low * (1 + tol)
    touch_hi = high >= prior_high * (1 - tol)
    # group start: touch with no touch in the prior `gap` bars
    gs_lo = touch_lo & (touch_lo.shift(1).rolling(gap).sum().fillna(0) == 0)
    gs_hi = touch_hi & (touch_hi.shift(1).rolling(gap).sum().fillna(0) == 0)
    n_groups_lo = gs_lo.shift(1).rolling(lb).sum()
    n_groups_hi = gs_hi.shift(1).rolling(lb).sum()
    if p["fresh_only"]:
        fresh_lo = n_groups_lo == 1
        fresh_hi = n_groups_hi == 1
    else:
        fresh_lo = n_groups_lo >= 1
        fresh_hi = n_groups_hi >= 1

    # --- triggers -----------------------------------------------------------
    warm = np.arange(n) > max(p["warmup"], p["atr_n"], lb + 1)
    appr = p["approach_atr"]
    long_trig = (
        (close > prior_low)
        & (close < prior_low * (1 + appr * atr))
        & regime & fresh_lo & warm & atr.notna() & prior_low.notna()
    )
    short_trig = (
        (close < prior_high)
        & (close > prior_high * (1 - appr * atr))
        & regime & fresh_hi & warm & atr.notna() & prior_high.notna()
    )

    long_idx = _apply_cooldown(np.flatnonzero(long_trig.to_numpy()), p["cooldown"])
    short_idx = _apply_cooldown(np.flatnonzero(short_trig.to_numpy()), p["cooldown"])

    # --- ladder rows --------------------------------------------------------
    atr_np = atr.to_numpy()
    close_np = close.to_numpy()
    pl_np = prior_low.to_numpy()
    ph_np = prior_high.to_numpy()
    # Rung j is emitted at idx i+j (features all from bar i — causal; staggering
    # keeps (idx, side) unique so the engine's lookahead verifier can match rows).
    rows = []
    for i in long_idx:
        dist = max(p["tp_atr_mult"] * atr_np[i], p["sl_floor"])
        for j, m in enumerate(p["rungs"]):
            ei = i + j
            if ei >= n:
                continue
            lp = pl_np[i] * (1 - m * atr_np[i])
            if lp < close_np[ei]:  # maker guard at emission bar
                rows.append({"idx": ei, "side": "long", "limit_price": lp,
                             "tp_dist": dist, "sl_dist": dist, "ttl": p["ttl"]})
    for i in short_idx:
        dist = max(p["tp_atr_mult"] * atr_np[i], p["sl_floor"])
        for j, m in enumerate(p["rungs"]):
            ei = i + j
            if ei >= n:
                continue
            lp = ph_np[i] * (1 + m * atr_np[i])
            if lp > close_np[ei]:
                rows.append({"idx": ei, "side": "short", "limit_price": lp,
                             "tp_dist": dist, "sl_dist": dist, "ttl": p["ttl"]})

    cols = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols).sort_values("idx").reset_index(drop=True)
