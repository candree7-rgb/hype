"""SMC Study 3 — pre-registered confluence: sweep + displacement reclaim
(order-block quality) + HTF trend, with distance-matched random control.

Builds on smc_sweep_study: same sweep detection/execution, but the reclaim
candle j must be a DISPLACEMENT candle (|close-open| >= reclaim_k * ATR60),
i.e. the sweep is immediately answered by an impulsive move whose origin
candle is the order block. HTF 4h EMA50 filter always ON.

Pre-registered variants (2): W=60, depth=0.25 ATR, reclaim_k=1.0,
tp_mult in {1.0, 1.1}.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

import smc_sweep_study as S


def run_confluence(dfs, W, depth_atr, reclaim_k, tp_mult, entry_lo, entry_hi,
                   seed=11):
    rng = np.random.default_rng(seed)
    real_rows, ctrl_rows = [], []
    for sym, pack in dfs.items():
        df, a, trend = pack["df"], pack["atr"], pack["trend"]
        o = df["open"].to_numpy()
        h = df["high"].to_numpy()
        l = df["low"].to_numpy()
        c = df["close"].to_numpy()
        lo_i, hi_i = entry_lo[sym], entry_hi[sym]
        n = len(df)
        for side in ("long", "short"):
            for ev in S.find_sweeps(df, W, side, depth_atr, a):
                j = ev["j"]
                e = j + 1
                if e >= n or not (lo_i <= e < hi_i):
                    continue
                t = trend[j]
                if (side == "long" and t != 1) or (side == "short" and t != -1):
                    continue
                body = c[j] - o[j]
                if side == "long" and body < reclaim_k * ev["atr"]:
                    continue
                if side == "short" and -body < reclaim_k * ev["atr"]:
                    continue
                entry = o[e]
                buf = S.BUF_ATR * ev["atr"]
                if side == "long":
                    sl_frac = (entry - (ev["extreme"] - buf)) / entry
                else:
                    sl_frac = ((ev["extreme"] + buf) - entry) / entry
                if sl_frac <= 0:
                    continue
                tp_frac = tp_mult * sl_frac
                oc, r = S.sim_market(o, h, l, c, e, side, sl_frac, tp_frac)
                real_rows.append({"symbol": sym, "e": e, "side": side,
                                  "sl_frac": sl_frac, "outcome": oc, "r": r,
                                  "dt": str(df["dt"].iloc[e])})
                for pe in rng.integers(lo_i, hi_i, size=S.N_CONTROL):
                    oc2, r2 = S.sim_market(o, h, l, c, int(pe), side, sl_frac, tp_frac)
                    ctrl_rows.append({"symbol": sym, "e": int(pe), "side": side,
                                      "sl_frac": sl_frac, "outcome": oc2, "r": r2})
    real, ctrl = S.stats(real_rows), S.stats(ctrl_rows)
    out = {"variant": f"confl_W{W}_d{depth_atr}_rk{reclaim_k}_tp{tp_mult}",
           "real": real, "control": ctrl}
    if real.get("n") and ctrl.get("n"):
        out["delta_wr"] = round(real["wr"] - ctrl["wr"], 4)
    out["_rows"] = real_rows
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=["is", "holdout"], default="is")
    ap.add_argument("--dump", default=None)
    args = ap.parse_args()

    dfs, entry_lo, entry_hi = {}, {}, {}
    for sym in S.list_symbols():
        df = S.load(sym)
        if args.window == "is":
            df = df[df["dt"] < S.IS_END].reset_index(drop=True)
            lo_i, hi_i = S.ATR_N + 1, len(df)
        else:
            df = df[df["dt"] >= S.WARMUP_START].reset_index(drop=True)
            mask = df["dt"] >= S.HOLDOUT_START
            lo_i = int(mask.idxmax()) if mask.any() else len(df)
            hi_i = len(df)
        if len(df) < 5000:
            continue
        dfs[sym] = {"df": df, "atr": S.atr(df), "trend": S.htf_trend_up(df)}
        entry_lo[sym], entry_hi[sym] = lo_i, hi_i

    for tpm in (1.0, 1.1):
        r = run_confluence(dfs, 60, 0.25, 1.0, tpm, entry_lo, entry_hi)
        rows = r.pop("_rows")
        if args.dump:
            pd.DataFrame(rows).to_csv(
                f"{args.dump.rstrip('/')}/confl_{args.window}_tp{tpm}.csv", index=False)
        print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
