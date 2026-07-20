"""Velocity-Dip Snapback — long-only, trend-OR-vol-expansion gated.

Mechanism: fast 15-minute down-moves (high downward velocity vs recent vol)
are panic flushes dominated by forced sellers and revert — but only when a
regime gate says the dip is flow, not information:
  (a) trend gate: close > EMA(720)  — dip is against prevailing drift
  (b) vol-expansion gate: TR(30)/TR(720) > vr_expand — stops already firing
The merged spec traded the UNION of the two gates. The spec pre-registered
a per-gate dead-gate check on in-sample data; that check showed trend-ONLY
signals are dead weight on IS at every admissible threshold
(z_min_trend=2.5: n=54, avg_r +0.005; z_min_trend=2.0: n=217, avg_r +0.022)
while vol-only (+0.083..+0.113) and both-gates (+0.289..+0.419) carry all
the edge. Default is therefore vol-gate-required (use_trend_gate=False);
the trend flag is kept as a diagnostic tag. Long-only (short mirror tested
negative). The limit entry off_atr*ATR60 below close is itself a second
filter — it only fills if the flush extends.

Falsified controls (do not re-run): unfiltered version negative; slow
displacement variants (VWAP deviation, consecutive-down runs) WR 44-46%;
short mirror negative everywhere.

Contract: generate_signals(df, params) ->
  DataFrame[idx, side, limit_price, tp_dist, sl_dist, ttl]
Signal at row i uses only rows <= i (all features are causal rolling/ewm).
"""
import numpy as np
import pandas as pd

DEFAULT_PARAMS = {
    "z_min": 2.0,        # velocity threshold for vol-expansion gate (PINNED —
                         # z<-2.5 with the vol gate went negative in-sample)
    "use_trend_gate": False,  # trend-only signals dead on IS (see docstring);
                              # vol-expansion gate is required for entry
    "z_min_trend": 2.5,  # velocity threshold for trend gate if enabled
                         # (admissible 2.0-2.5; only used when use_trend_gate)
    "ema_span": 720,     # 12h trend EMA (insensitive 480-1440, do not tune)
    "vr_expand": 1.5,    # ATR30/ATR720 expansion gate (fragile >1.8, keep 1.5-1.8)
    "off_atr": 1.0,      # limit offset in ATR60 units below signal close
    "tp_atr_mult": 3.0,  # tp = sl = max(tp_atr_mult*ATR60, sl_floor)
    "sl_floor": 0.003,   # keeps losses <= ~1.17R (cost trap)
    "ttl": 15,
    "cooldown": 20,      # bars; keep first signal, drop any within cooldown
    "warmup": 1500,
}

_COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl", "gate"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    close = df["close"]
    high = df["high"]
    low = df["low"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    ret1 = close.pct_change()
    z15 = close.pct_change(15) / (ret1.rolling(240).std() * np.sqrt(15))
    ema = close.ewm(span=p["ema_span"]).mean()
    atr60 = tr.rolling(60).mean() / close
    vol_ratio = tr.rolling(30).mean() / tr.rolling(720).mean()

    trend_gate = (z15 < -p["z_min_trend"]) & (close > ema)
    vol_gate = (z15 < -p["z_min"]) & (vol_ratio > p["vr_expand"])

    entry_gate = (trend_gate | vol_gate) if p["use_trend_gate"] else vol_gate

    n = len(df)
    valid = (
        entry_gate
        & atr60.notna()
        & (np.arange(n) > p["warmup"])
    )
    idxs = np.flatnonzero(valid.to_numpy())
    if len(idxs) == 0:
        return pd.DataFrame(columns=_COLS)

    # cooldown: keep first signal, drop any within `cooldown` bars of a kept one
    kept = []
    last = -10 ** 9
    for i in idxs:
        if i - last >= p["cooldown"]:
            kept.append(int(i))
            last = i
    kept = np.array(kept)

    c = close.to_numpy()[kept]
    a = atr60.to_numpy()[kept]
    tg = trend_gate.to_numpy()[kept]
    vg = vol_gate.to_numpy()[kept]
    dist = np.maximum(p["tp_atr_mult"] * a, p["sl_floor"])
    gate = np.where(tg & vg, "both", np.where(tg, "trend", "vol"))

    return pd.DataFrame({
        "idx": kept,
        "side": "long",
        "limit_price": c * (1.0 - p["off_atr"] * a),
        "tp_dist": dist,
        "sl_dist": dist,
        "ttl": p["ttl"],
        "gate": gate,  # diagnostic only; engine ignores extra columns
    })
