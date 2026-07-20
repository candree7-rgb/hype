"""Volume-Climax Blowoff Ladder Fade — SHORT ONLY (slug: pump_fade_core).

Mechanism: volume-confirmed parabolic multi-minute up-impulses in alt perps are
terminal FOMO/short-squeeze climaxes. If the pump extends *after* the signal,
that extension is exhaustion and mean-reverts. Strictly short-only — the long
mirror (fading volume-confirmed dumps) failed in every tested config.

Emission: at each signal bar, TWO resting short limit rungs above close
(kA and kB ATRs up), each an independent 1:1 bracket, ttl=10. Limits above
close -> maker by construction; they fill only if the pump extends.

Merged from pump_fade_deep_ladder + blowoff_pump_ladder_fade research.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "z_min": 3.0,        # impulse strictness: r5 / rolling-720 std of r5
    "vr_min": 2.0,       # LOAD-BEARING volume confirmation: 5m vol vs 240m avg
    "kA": 1.5,           # rung A depth in ATRs above signal close
    "kB": 2.5,           # rung B depth in ATRs above signal close
    "d_atr": 4.0,        # tp/sl distance in atr_pct units
    "sl_floor": 0.003,   # min stop distance (cost trap guard)
    "ttl": 10,           # stale climax orders must die fast
    "cooldown": 45,      # bars between emitted signals per symbol
    "z_window": 720,
    "vol_fast": 5,
    "vol_slow": 240,
    "warmup": 750,
}

_COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    close = df["close"]
    vol = df["vol"]

    # 5-minute return z-scored by its own rolling std (720 min lookback)
    r5 = close.pct_change(5)
    z = r5 / r5.rolling(p["z_window"]).std()

    # Wilder ATR14 as fraction of price
    prev_close = close.shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_pct = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean() / close

    # volume confirmation: last 5 minutes vs 240-min average volume
    vol_ratio = vol.rolling(p["vol_fast"]).sum() / (
        vol.rolling(p["vol_slow"]).mean() * p["vol_fast"])

    idx_arr = np.arange(len(df))
    mask = (
        (z.to_numpy() > p["z_min"])
        & (vol_ratio.to_numpy() > p["vr_min"])
        & (idx_arr > p["warmup"])
        & np.isfinite(atr_pct.to_numpy())
    )
    cand = idx_arr[mask]

    # per-symbol cooldown: suppress signals within `cooldown` bars of the last
    # *emitted* one (causal — depends only on earlier emitted signals)
    kept = []
    last = -10**9
    for i in cand:
        if i - last >= p["cooldown"]:
            kept.append(i)
            last = i
    if not kept:
        return pd.DataFrame(columns=_COLS)

    kept = np.asarray(kept)
    c = close.to_numpy()[kept]
    a = atr_pct.to_numpy()[kept]
    dist = np.maximum(p["d_atr"] * a, p["sl_floor"])

    # Rung A rests from the signal bar. Rung B (deeper) is registered one bar
    # later but is priced/sized entirely from signal-bar (i) features — causal,
    # and gives each output row a unique idx (one order per row index).
    rung_a = pd.DataFrame({
        "idx": kept,
        "side": "short",
        "limit_price": c * (1.0 + p["kA"] * a),
        "tp_dist": dist,
        "sl_dist": dist,
        "ttl": p["ttl"],
    })
    b_ok = kept + 1 < len(df)
    rung_b = pd.DataFrame({
        "idx": kept[b_ok] + 1,
        "side": "short",
        "limit_price": (c * (1.0 + p["kB"] * a))[b_ok],
        "tp_dist": dist[b_ok],
        "sl_dist": dist[b_ok],
        "ttl": p["ttl"],
    })
    out = pd.concat([rung_a, rung_b], ignore_index=True)
    return out.sort_values(["idx", "limit_price"]).reset_index(drop=True)[_COLS]
