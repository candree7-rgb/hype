"""mom2: parameterized extension of the frozen mom_cascade_breakout entry.

Entry logic is byte-equivalent to mom_cascade_breakout when params match its
DEFAULT_PARAMS (verified: identical trade list under engine_mom2 with no new
exit features enabled). Extensions, all controlled by params:

- exits: max_hold sweep, per-signal ATR-scaled or pct trailing stop,
  breakeven move (be_arm/be_offset), partial-TP ladder (ptp_dists/ptp_fracs,
  reduce-only maker, next-bar-earliest — see engine_mom2).
- signal: breadth_n, breakout lookback, optional DOWN-side symmetric mode
  (side="short": breakdown of prior 24h low + down-shock breadth).
- universe: `majors` (traded list) is a parameter; breadth universe stays the
  frozen 18-coin fade universe unless `breadth_universe` overrides it.

Nothing here modifies the frozen strategy file.

VERDICT (2026-07-22, 43 IS variants, holdout opened once for 2 finalists):
- Exits: NOTHING beats the frozen 36h time exit on IS. Trails (pct or ATR)
  cut avg_r by 2-4x (they amputate the drift the sleeve lives on); partial
  TPs (next-bar-earliest maker, honest) give up more upside than they lock;
  BE-move at +3% is roughly total-R-neutral with ~12% lower maxDD (IS-only
  evidence, optional). Time-exit sweep: 36h is the IS optimum (24h close).
- Down-side symmetric (short 24h-low breakdown + down-shock breadth): ~zero
  to negative on IS. Long-only is measured, confirmed.
- FINALIST 1 universe expansion (frozen config, 23 coins): IS +0.21/trade,
  +218R — HOLDOUT +0.012, PF 1.02, Apr26 -67R. The 18 expansion coins score
  -0.00 OOS (flat, not negative); only the 5 majors kept edge (+0.066).
- FINALIST 2 7d-lookback (23 coins): IS +0.42 — HOLDOUT -0.18. Dead. The
  IS gain was the Jul25-Jan26 alt-runs regime, not signal.
- 3y reconstruction (exact code, real 1m data Jul23-Jun25, 16-coin breadth
  universe): +12R over 2 YEARS (PF 1.05), and what little there is sits in
  Nov24+Jan25 bull impulses. breadth>=4 sensitivity: Y2 +35R, still impulse-
  concentrated, Y1 negative. The mechanism did NOT exist as a steady edge
  before Jul 2025 — this sleeve is a long-momentum REGIME harvester.
- 12mo baseline composition: HYPE +30.9R of +62.6R (49%), ETH +15.8,
  SOL +15.0, XRP +2.6, BTC -1.6.
=> DEPLOYABLE SLEEVE remains the FROZEN mom_cascade_breakout (majors5),
   unchanged. Everything tested here failed to improve it out-of-sample.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

UNIVERSE = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT",
            "ZEC_USDT", "PEPE_USDT", "AVAX_USDT", "TAO_USDT", "DOGE_USDT",
            "SUI_USDT", "NEAR_USDT", "WLD_USDT", "PUMPFUN_USDT", "LTC_USDT",
            "BNB_USDT", "ADA_USDT", "LINK_USDT",
            # extra tradables (NOT in the frozen breadth universe by default)
            "ENA_USDT", "XMR_USDT", "APT_USDT", "JUP_USDT", "PENGU_USDT",
            "AAVE_USDT", "ARB_USDT", "ENS_USDT", "FET_USDT", "INJ_USDT",
            "LDO_USDT", "ONDO_USDT", "OP_USDT", "POL_USDT", "SEI_USDT",
            "TIA_USDT", "TON_USDT", "UNI_USDT", "WIF_USDT"]

FROZEN_BREADTH_UNIVERSE = UNIVERSE[:18]

DEFAULT_PARAMS = {
    "majors": ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT"],
    "breadth_universe": None,      # None -> frozen 18-coin universe
    "shock_atr": 3.5, "atr_n": 60, "atr_min": 0.001,
    "window": 60,
    "breadth_n": 5,
    "lookback": 1440,
    "side": "long",                # "long" | "short" (symmetric breakdown)
    "entry_mode": "maker_retest",
    "ttl": 240,
    "sl_pct": 0.03,
    "sl_atr_mult": float("nan"),   # per-signal SL = clip(mult*ATR60/close, lo, hi)
    "sl_atr_lo": 0.015, "sl_atr_hi": 0.05,
    # exit extensions (all off by default -> byte-equivalent to frozen)
    "trail_pct": float("nan"),
    "trail_atr_mult": float("nan"),   # per-signal trail = mult * ATR60/close
    "trail_atr_floor": 0.0,           # min trail dist when ATR-scaled
    "arm_pct": 0.0,
    "be_arm": float("nan"),
    "be_offset": 0.0,
    "ptp_dists": None,             # e.g. [0.02] fractions from entry
    "ptp_fracs": None,             # e.g. [0.5]
    "max_hold": 2160,
    "warmup": 1500,
}

SIG_COLS = ["idx", "side", "entry_type", "limit_price", "ttl", "sl_dist",
            "tp_dist", "trail_dist", "trail_arm", "be_arm", "be_offset",
            "ptp_dists", "ptp_fracs", "max_hold"]


def _atr_frac(df: pd.DataFrame, n: int):
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(n).mean().to_numpy() / c


def _shock(df: pd.DataFrame, p: dict, up: bool):
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    atr = _atr_frac(df, p["atr_n"])
    with np.errstate(invalid="ignore", divide="ignore"):
        ra = ((h - l) / c) / atr
    shock = (ra > p["shock_atr"]) & (atr >= p["atr_min"]) & np.isfinite(atr)
    return shock & ((c > o) if up else (c < o))


def generate_signals_multi(dfs: dict, params: dict) -> dict:
    p = {**DEFAULT_PARAMS, **params}
    long = p["side"] == "long"
    majors = [m for m in p["majors"] if m in dfs]
    bu_universe = p["breadth_universe"] or FROZEN_BREADTH_UNIVERSE
    ref = dfs[majors[0]]
    tidx = pd.Index(ref["time"].to_numpy())
    nT = len(tidx)

    mat = np.zeros((nT, len(bu_universe)), dtype=bool)
    for k, coin in enumerate(bu_universe):
        if coin not in dfs:
            continue
        s = _shock(dfs[coin], p, up=long)
        pos = tidx.get_indexer(dfs[coin]["time"].to_numpy())
        ok = pos >= 0
        mat[pos[ok], k] = s[ok]
    breadth = pd.DataFrame(mat).rolling(
        int(p["window"]), min_periods=1).max().sum(axis=1).to_numpy()

    ptp_d = json.dumps(p["ptp_dists"]) if p["ptp_dists"] else None
    ptp_f = json.dumps(p["ptp_fracs"]) if p["ptp_fracs"] else None

    out = {}
    for coin in majors:
        df = dfs[coin]
        cl = df["close"].to_numpy(float)
        hi = df["high"].to_numpy(float)
        lo = df["low"].to_numpy(float)
        if long:
            lvl = pd.Series(hi).shift(1).rolling(int(p["lookback"])).max().to_numpy()
            brk = cl > lvl
        else:
            lvl = pd.Series(lo).shift(1).rolling(int(p["lookback"])).min().to_numpy()
            brk = cl < lvl
        gpos = tidx.get_indexer(df["time"].to_numpy())
        bu = np.where(gpos >= 0, breadth[np.clip(gpos, 0, nT - 1)], 0.0)

        cond = brk & (bu >= p["breadth_n"]) & (np.arange(len(df)) > p["warmup"])
        trig = np.flatnonzero(cond & ~np.concatenate(([False], cond[:-1])))
        if trig.size == 0:
            continue

        atr = _atr_frac(df, p["atr_n"])
        if np.isfinite(p["trail_atr_mult"]):
            td = np.maximum(p["trail_atr_mult"] * atr[trig], p["trail_atr_floor"])
        else:
            td = np.full(trig.size, p["trail_pct"])
        if np.isfinite(p["sl_atr_mult"]):
            sld = np.clip(p["sl_atr_mult"] * atr[trig],
                          p["sl_atr_lo"], p["sl_atr_hi"])
        else:
            sld = np.full(trig.size, p["sl_pct"])

        base = {
            "idx": trig, "side": p["side"], "sl_dist": sld,
            "tp_dist": np.nan, "trail_dist": td, "trail_arm": p["arm_pct"],
            "be_arm": p["be_arm"], "be_offset": p["be_offset"],
            "ptp_dists": ptp_d, "ptp_fracs": ptp_f,
            "max_hold": int(p["max_hold"]),
        }
        if p["entry_mode"] == "maker_retest":
            out[coin] = pd.DataFrame({**base, "entry_type": "maker",
                                      "limit_price": lvl[trig],
                                      "ttl": int(p["ttl"])})[SIG_COLS]
        else:
            out[coin] = pd.DataFrame({**base, "entry_type": "taker",
                                      "limit_price": np.nan,
                                      "ttl": 1})[SIG_COLS]
    return out
