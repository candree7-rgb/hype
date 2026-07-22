"""Robustness check — VOLATILITY-MATCHED distance-matched random control.

Rebuttal being tested: sweep entries occur in high-vol moments; a control at
uniformly random times sits in calmer conditions, which could mask (or fake)
the structural delta. Here each control entry is drawn only from candles whose
relative ATR (ATR60/close) is within [0.75, 1.33]x of the real trade's, same
symbol, same side, same fractional distances, same strict rules.

Run for: (a) confluence W60_d0.25_rk1.0 (tp 1.0 and 1.1) — best IS delta;
         (b) plain sweep W60_d0.25_tp1.1_htf1 — best big-n sweep variant.
IS window only.
"""
from __future__ import annotations

import json

import numpy as np

import smc_sweep_study as S


def volmatched_controls(pack, lo_i, hi_i, e_real, side, sl_frac, tp_frac, rng):
    df, a = pack["df"], pack["atr"]
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    relvol = pack["relvol"]
    rv0 = relvol[e_real]
    pool = pack["pool"]  # sorted indices with finite relvol in [lo,hi)
    rvs = relvol[pool]
    mask = (rvs >= 0.75 * rv0) & (rvs <= 1.33 * rv0)
    cand = pool[mask]
    if len(cand) < 5:
        return []
    picks = rng.choice(cand, size=min(S.N_CONTROL, len(cand)), replace=True)
    out = []
    for pe in picks:
        oc, r = S.sim_market(o, h, l, c, int(pe), side, sl_frac, tp_frac)
        out.append({"outcome": oc, "r": r, "sl_frac": sl_frac})
    return out


def run(dfs, entry_lo, entry_hi, W, depth, tp_mult, htf, reclaim_k, name,
        seed=31):
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
            for ev in S.find_sweeps(df, W, side, depth, a):
                j = ev["j"]
                e = j + 1
                if e >= n or not (lo_i <= e < hi_i):
                    continue
                if htf:
                    t = trend[j]
                    if (side == "long" and t != 1) or (side == "short" and t != -1):
                        continue
                if reclaim_k > 0:
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
                real_rows.append({"outcome": oc, "r": r, "sl_frac": sl_frac})
                ctrl_rows += volmatched_controls(pack, lo_i, hi_i, e, side,
                                                 sl_frac, tp_frac, rng)
    real, ctrl = S.stats(real_rows), S.stats(ctrl_rows)
    out = {"variant": name, "real": real, "control_volmatched": ctrl}
    if real.get("n") and ctrl.get("n"):
        out["delta_wr"] = round(real["wr"] - ctrl["wr"], 4)
        def slr(s):
            oc = s["outcomes"]
            return (oc.get("sl", 0) + oc.get("ambiguous_sl", 0)) / s["n"]
        out["delta_sl_rate"] = round(slr(real) - slr(ctrl), 4)
    return out


def main():
    dfs, entry_lo, entry_hi = {}, {}, {}
    for sym in S.list_symbols():
        df = S.load(sym)
        df = df[df["dt"] < S.IS_END].reset_index(drop=True)
        if len(df) < 5000:
            continue
        a = S.atr(df)
        close = df["close"].to_numpy()
        relvol = np.where(close > 0, a / close, np.nan)
        lo_i, hi_i = S.ATR_N + 1, len(df)
        pool = np.arange(lo_i, hi_i)
        pool = pool[np.isfinite(relvol[pool])]
        dfs[sym] = {"df": df, "atr": a, "trend": S.htf_trend_up(df),
                    "relvol": relvol, "pool": pool}
        entry_lo[sym], entry_hi[sym] = lo_i, hi_i

    jobs = [
        (60, 0.25, 1.0, True, 1.0, "confl_tp1.0_volmatch"),
        (60, 0.25, 1.1, True, 1.0, "confl_tp1.1_volmatch"),
        (60, 0.25, 1.1, True, 0.0, "sweep_W60_d0.25_tp1.1_htf1_volmatch"),
    ]
    for W, depth, tpm, htf, rk, name in jobs:
        print(json.dumps(run(dfs, entry_lo, entry_hi, W, depth, tpm, htf, rk,
                             name)), flush=True)


if __name__ == "__main__":
    main()
