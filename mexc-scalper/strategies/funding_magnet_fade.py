"""Funding-Clock Stretch Fade with Settlement-Burst Fill (slug: funding_magnet_fade).

Mechanism: moves in the final minutes before the funding settlement print
(00/08/16 UTC, plus 04/12/20 on 4h-funding contracts) are partly
funding-avoidance positioning that expires at the print and reverts. The
settlement minute itself carries ~1.3-1.8x normal volatility, supplying
trade-through fills. We rest a limit AGAINST a pre-funding stretch during the
last `w` minutes of the cycle, offset by 0.5*S so the settlement burst fills
it at the extreme. Order lives THROUGH the print (ttl=20). Hard rule: never
initiate on minutes after the print (post-print entries tested negative).

contracts.json carries no funding-interval field; an IS-only vol study showed
the settlement-minute burst is equally present at 04/12/20 UTC across all 18
symbols, so the cycle length is a uniform rule-based parameter (cycle_min),
not a per-symbol pick.

Secondary variant (straddle at the :59 bar when no directional stretch
exists) is gated by `straddle_on` and must earn its place on IS.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_PARAMS = {
    "cycle_min": 480,      # funding cycle length in minutes (480=8h, 240=4h)
    "z_min": 2.0,          # stretch threshold in units of sd30
    "w": 15,               # pre-settlement window length (minutes)
    "rest_mult": 0.5,      # limit offset = rest_mult * S
    "ttl": 20,             # order lives through the settlement print
    "tp_mult": 1.2,        # tp_dist = sl_dist = max(tp_floor, tp_mult*S)
    "tp_floor": 0.004,
    "warmup": 1500,
    "straddle_on": False,
    "straddle_off": 0.0025,
    "straddle_ttl": 3,
    "straddle_tpsl": 0.003,
}

COLS = ["idx", "side", "limit_price", "tp_dist", "sl_dist", "ttl"]


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **params}
    close = df["close"]
    high = df["high"]
    low = df["low"]
    prev_close = close.shift(1)

    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr60 = tr.rolling(60).mean() / close
    S = (atr60 * np.sqrt(10)).to_numpy()

    r30 = close.pct_change(30)
    sd30 = r30.rolling(1440).std()
    r30 = r30.to_numpy()
    sd30 = sd30.to_numpy()

    # minute position within the funding cycle; epoch minute 0 = midnight UTC
    cyc = int(p["cycle_min"])
    epoch_min = (df["time"].to_numpy() // 60).astype(np.int64)
    m = epoch_min % cyc
    window_id = epoch_min // cyc

    idx = np.arange(len(df))
    valid = (idx > p["warmup"]) & np.isfinite(S) & np.isfinite(sd30) & (sd30 > 0)
    in_pre = m >= cyc - int(p["w"])

    stretch_up = valid & in_pre & (r30 > p["z_min"] * sd30)
    stretch_dn = valid & in_pre & (r30 < -p["z_min"] * sd30)

    closev = close.to_numpy()
    tp = np.maximum(p["tp_floor"], p["tp_mult"] * S)

    cand = []
    if stretch_up.any():
        i = idx[stretch_up]
        cand.append(pd.DataFrame({
            "idx": i, "side": "short",
            "limit_price": closev[i] * (1 + p["rest_mult"] * S[i]),
            "tp_dist": tp[i], "sl_dist": tp[i], "ttl": int(p["ttl"]),
            "window": window_id[i]}))
    if stretch_dn.any():
        i = idx[stretch_dn]
        cand.append(pd.DataFrame({
            "idx": i, "side": "long",
            "limit_price": closev[i] * (1 - p["rest_mult"] * S[i]),
            "tp_dist": tp[i], "sl_dist": tp[i], "ttl": int(p["ttl"]),
            "window": window_id[i]}))

    if cand:
        directional = (pd.concat(cand).sort_values("idx")
                       .groupby("window", as_index=False).first())
    else:
        directional = pd.DataFrame(columns=COLS + ["window"])

    out = directional
    if p["straddle_on"]:
        used = set(directional["window"].tolist())
        last_bar = valid & (m == cyc - 1)
        i = idx[last_bar]
        i = i[[window_id[j] not in used for j in i]]
        if len(i):
            off = p["straddle_off"]
            legs = pd.concat([
                pd.DataFrame({"idx": i, "side": "short",
                              "limit_price": closev[i] * (1 + off),
                              "tp_dist": p["straddle_tpsl"],
                              "sl_dist": p["straddle_tpsl"],
                              "ttl": int(p["straddle_ttl"]),
                              "window": window_id[i]}),
                pd.DataFrame({"idx": i, "side": "long",
                              "limit_price": closev[i] * (1 - off),
                              "tp_dist": p["straddle_tpsl"],
                              "sl_dist": p["straddle_tpsl"],
                              "ttl": int(p["straddle_ttl"]),
                              "window": window_id[i]}),
            ])
            out = pd.concat([directional, legs])

    if out.empty:
        return pd.DataFrame(columns=COLS)
    return (out.sort_values("idx")[COLS]
            .astype({"idx": int, "ttl": int})
            .reset_index(drop=True))
