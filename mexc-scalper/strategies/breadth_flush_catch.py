"""Market-Wide Flush Breadth Catch (cross-symbol systemic cascade fade).

Mechanism: when many perps simultaneously press into their 8-hour extremes,
the move is a correlated liquidation cascade (forced flow) that overshoots and
snaps back. We fade it with a deep maker limit below the prior extreme, sized
1:1 RR.

Breadth is computed once from ALL cached parquets (module-level cache); the
TARGET symbol's own trigger features are re-derived from the df the engine
passes in, so the engine's truncation check only exercises own-symbol features.
Breadth at minute t uses only candles that CLOSED at t across all symbols, so
it is causal by construction.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_DIR = Path(__file__).parent.parent / "data"

DEFAULT_PARAMS = {
    "atr_n": 60,          # rolling TR window for normalized ATR
    "lookback_n": 480,    # 8h prior-extreme window
    "near_atr": 1.0,      # breadth flag: |close - prior_ext| <= near_atr*ATR
    "approach_atr": 2.0,  # own-symbol trigger band above prior_low / below prior_high
    "offset_atr": 3.5,    # limit depth beyond the prior extreme
    "bmin": 4,            # min symbols near same-side 8h extreme
    "tp_atr_mult": 3.0,   # tp = sl = max(tp_atr_mult*ATR, sl_floor)
    "sl_floor": 0.003,
    "ttl": 20,
    "cooldown": 30,       # bars per side per symbol
    "warmup": 540,
}

# ---------------------------------------------------------------------------
# shared cross-symbol breadth (module-level cache)
# ---------------------------------------------------------------------------
_BREADTH_CACHE: dict = {}


def _features(close: pd.Series, high: pd.Series, low: pd.Series, p: dict):
    """Causal per-symbol features: fractional ATR and prior 8h extremes."""
    prev_close = close.shift(1)
    tr = np.maximum(high - low,
                    np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
    atr = tr.rolling(p["atr_n"]).mean() / close
    prior_low = low.shift(1).rolling(p["lookback_n"]).min()
    prior_high = high.shift(1).rolling(p["lookback_n"]).max()
    return atr, prior_low, prior_high


def _breadth_series(p: dict):
    """(breadth_lo, breadth_hi) Series indexed by candle-close minute (time//60),
    counting symbols whose close sits within +/- near_atr*ATR of their prior
    8h low/high. All inputs are at-close values of candles closed at t."""
    files = sorted(DATA_DIR.glob("*_Min1.parquet"))
    key = (tuple((f.name, f.stat().st_mtime) for f in files),
           p["atr_n"], p["lookback_n"], p["near_atr"])
    if key in _BREADTH_CACHE:
        return _BREADTH_CACHE[key]

    lo_flags, hi_flags = [], []
    for f in files:
        d = pd.read_parquet(f, columns=["time", "high", "low", "close"])
        atr, prior_low, prior_high = _features(d["close"], d["high"], d["low"], p)
        rel_lo = (d["close"] / prior_low - 1).abs()
        rel_hi = (d["close"] / prior_high - 1).abs()
        band = p["near_atr"] * atr
        minute = d["time"] // 60
        lo_flags.append(pd.Series((rel_lo <= band).to_numpy(), index=minute, name=f.stem))
        hi_flags.append(pd.Series((rel_hi <= band).to_numpy(), index=minute, name=f.stem))

    breadth_lo = pd.concat(lo_flags, axis=1, join="outer").fillna(False).sum(axis=1)
    breadth_hi = pd.concat(hi_flags, axis=1, join="outer").fillna(False).sum(axis=1)
    _BREADTH_CACHE[key] = (breadth_lo, breadth_hi)
    return _BREADTH_CACHE[key]


# ---------------------------------------------------------------------------
# strategy contract
# ---------------------------------------------------------------------------

def _apply_cooldown(idx: np.ndarray, cooldown: int) -> np.ndarray:
    """Keep first of any cluster: drop indices within `cooldown` bars of the
    last kept index (per side, called per side)."""
    kept = []
    last = -10**9
    for i in idx:
        if i - last >= cooldown:
            kept.append(i)
            last = i
    return np.asarray(kept, dtype=int)


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    breadth_lo, breadth_hi = _breadth_series(p)

    close = df["close"]
    # own-symbol features re-derived from the passed df (truncation-safe)
    atr, prior_low, prior_high = _features(close, df["high"], df["low"], p)

    minute = (df["time"] // 60).to_numpy()
    b_lo = breadth_lo.reindex(minute).fillna(0).to_numpy()
    b_hi = breadth_hi.reindex(minute).fillna(0).to_numpy()

    n = len(df)
    warm = np.zeros(n, dtype=bool)
    warm[min(p["warmup"] + 1, n):] = True

    long_mask = (
        warm
        & (close > prior_low).to_numpy()
        & (close < prior_low * (1 + p["approach_atr"] * atr)).to_numpy()
        & (b_lo >= p["bmin"])
        & atr.notna().to_numpy() & prior_low.notna().to_numpy()
    )
    short_mask = (
        warm
        & (close < prior_high).to_numpy()
        & (close > prior_high * (1 - p["approach_atr"] * atr)).to_numpy()
        & (b_hi >= p["bmin"])
        & atr.notna().to_numpy() & prior_high.notna().to_numpy()
    )

    atr_v = atr.to_numpy()
    plo = prior_low.to_numpy()
    phi = prior_high.to_numpy()
    close_v = close.to_numpy()

    rows = []
    for side, mask in (("long", long_mask), ("short", short_mask)):
        idx = _apply_cooldown(np.flatnonzero(mask), p["cooldown"])
        if len(idx) == 0:
            continue
        a = atr_v[idx]
        dist = np.maximum(p["tp_atr_mult"] * a, p["sl_floor"])
        if side == "long":
            limit = plo[idx] * (1 - p["offset_atr"] * a)
            ok = limit < close_v[idx]
        else:
            limit = phi[idx] * (1 + p["offset_atr"] * a)
            ok = limit > close_v[idx]
        rows.append(pd.DataFrame({
            "idx": idx[ok], "side": side, "limit_price": limit[ok],
            "tp_dist": dist[ok], "sl_dist": dist[ok], "ttl": p["ttl"],
        }))

    if not rows:
        return pd.DataFrame(columns=["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"])
    return (pd.concat(rows, ignore_index=True)
            .sort_values("idx").reset_index(drop=True))
