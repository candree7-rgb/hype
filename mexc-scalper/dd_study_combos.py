#!/usr/bin/env python3
"""Combos of best single overlay rules + diagnostics (monthly, trigger counts)."""
import numpy as np
import pandas as pd
from dd_study_overlays import load, stats, simulate, HOLDOUT_START

def show(name, t, m):
    s = stats(t, m)
    print(f"{name:34s} {int(m.sum()):5d} {s['is_total']:8.1f} {s['is_dd']:7.1f} "
          f"{s['ho_avg']:8.4f} {s['ho_total']:8.1f} {s['ho_dd']:7.1f} "
          f"{s['nov_total']:8.1f} {s['nov_n']:6d}")
    return s

def monthly(t, m, label):
    a = t[m].copy()
    a["month"] = a["entry_time"].dt.to_period("M")
    g = a.groupby("month")["r"].agg(["sum", "count"])
    b = t.copy()
    b["month"] = b["entry_time"].dt.to_period("M")
    gb = b.groupby("month")["r"].agg(["sum", "count"])
    print(f"\nMonthly R ({label} vs baseline):")
    for mth in gb.index:
        rs, ns = (g.loc[mth] if mth in g.index else pd.Series({"sum": 0, "count": 0}))
        print(f"  {mth}  overlay {rs:7.1f} (n={int(ns):4d})   base {gb.loc[mth,'sum']:7.1f} (n={int(gb.loc[mth,'count']):4d})   skipped_r {gb.loc[mth,'sum']-rs:+7.1f}")

def main():
    t = load()
    print(f"{'rule':34s} {'n':>5s} {'IS_tot':>8s} {'IS_DD':>7s} {'HO_avg':>8s} "
          f"{'HO_tot':>8s} {'HO_DD':>7s} {'NovR':>8s} {'Nov_n':>6s}")
    combos = [
        ("C1 roll24_X6 + side_cap_N5", dict(roll24_x=6, side_cap=5)),
        ("C2 roll24_X6 + streak_K6", dict(roll24_x=6, streak_k=6)),
        ("C3 roll24_X6 + cap_N5 + streak_K6", dict(roll24_x=6, side_cap=5, streak_k=6)),
    ]
    masks = {}
    for name, kw in combos:
        m = simulate(t, **kw)
        masks[name] = m
        show(name, t, m)

    # diagnostics for the leading single rule: rolling-24h breaker X=6
    m6 = simulate(t, roll24_x=6)
    print()
    show("single roll24_X6 (ref)", t, m6)
    skipped = t[~m6]
    skipped_days = skipped["entry_time"].dt.date.nunique()
    print(f"\nroll24_X6: skipped {len(skipped)} trades on {skipped_days} distinct days "
          f"(of {t['entry_time'].dt.date.nunique()} trading days)")
    sk = skipped.copy()
    sk["month"] = sk["entry_time"].dt.to_period("M")
    print("skipped trades / skipped-R by month:")
    print(sk.groupby("month")["r"].agg(["count", "sum"]).round(1).to_string())
    monthly(t, m6, "roll24_X6")
    monthly(t, masks["C1 roll24_X6 + side_cap_N5"], "C1")

if __name__ == "__main__":
    main()
