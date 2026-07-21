#!/usr/bin/env python3
"""Robustness neighborhood + full profile of the adopted overlay (C2)."""
import numpy as np
import pandas as pd
from dd_study_overlays import load, stats, simulate, dd_of, HOLDOUT_START, NOV_START, NOV_END

def line(name, t, m):
    s = stats(t, m)
    print(f"{name:30s} {int(m.sum()):5d} {s['is_total']:8.1f} {s['is_dd']:7.1f} "
          f"{s['ho_avg']:8.4f} {s['ho_total']:8.1f} {s['ho_dd']:7.1f} {s['nov_total']:8.1f}")
    return s

def main():
    t = load()
    print("SENSITIVITY (robustness only, not selection)")
    print(f"{'variant':30s} {'n':>5s} {'IS_tot':>8s} {'IS_DD':>7s} {'HO_avg':>8s} "
          f"{'HO_tot':>8s} {'HO_DD':>7s} {'NovR':>8s}")
    for x in (4, 5, 6, 7, 8):
        line(f"roll24_X{x} + streak_K6", t, simulate(t, roll24_x=x, streak_k=6))
    for k in (5, 6, 7, 8):
        line(f"roll24_X6 + streak_K{k}", t, simulate(t, roll24_x=6, streak_k=k))

    m = simulate(t, roll24_x=6, streak_k=6)
    a, b = t[m], t
    print("\nFULL PROFILE: adopted = roll24_breaker X=6 + streak_brake K=6")
    for label, d in (("baseline", b), ("adopted", a)):
        is_t = d[d["entry_time"] < HOLDOUT_START]
        ho_t = d[d["entry_time"] >= HOLDOUT_START]
        nov = d[(d["entry_time"] >= NOV_START) & (d["entry_time"] < NOV_END)]
        yr_dd = dd_of(d["r"].values, d["entry_time"].values)
        nov_dd = dd_of(nov["r"].values, nov["entry_time"].values)
        print(f"  {label}: n={len(d)} total={d['r'].sum():.1f} wr={(d['r']>0).mean():.3f} "
              f"avg={d['r'].mean():.4f} yearDD={yr_dd:.1f}")
        print(f"    IS: n={len(is_t)} total={is_t['r'].sum():.1f} wr={(is_t['r']>0).mean():.3f} DD={dd_of(is_t['r'].values, is_t['entry_time'].values):.1f}")
        print(f"    HO: n={len(ho_t)} total={ho_t['r'].sum():.1f} wr={(ho_t['r']>0).mean():.3f} avg={ho_t['r'].mean():.4f} DD={dd_of(ho_t['r'].values, ho_t['entry_time'].values):.1f}")
        print(f"    Nov17-Dec4: n={len(nov)} total={nov['r'].sum():.1f} winDD={nov_dd:.1f}")

    # monthly for adopted
    am = a.copy(); am["month"] = am["entry_time"].dt.to_period("M")
    bm = b.copy(); bm["month"] = bm["entry_time"].dt.to_period("M")
    ga = am.groupby("month")["r"].sum(); gb = bm.groupby("month")["r"].sum()
    print("\n  monthly R adopted vs baseline:")
    for mth in gb.index:
        print(f"    {mth}  {ga.get(mth,0):7.1f}  vs {gb[mth]:7.1f}")

    # how often does each leg actually bind?
    m_r = simulate(t, roll24_x=6)
    m_s = simulate(t, streak_k=6)
    print(f"\n  skipped by roll24 alone: {(~m_r).sum()}, by streak alone: {(~m_s).sum()}, "
          f"by combo: {(~m).sum()} (overlap of skip sets: {((~m_r)&(~m_s)).sum()})")
    days_hit = t.loc[~m, "entry_time"].dt.date.nunique()
    print(f"  combo skips hit {days_hit} distinct days; skipped trades avg_r = {t.loc[~m,'r'].mean():.3f}")

if __name__ == "__main__":
    main()
