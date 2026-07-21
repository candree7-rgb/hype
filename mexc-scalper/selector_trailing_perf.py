#!/usr/bin/env python3
"""Trailing-performance coin selector with empirical-Bayes shrinkage.

Idea: each calendar month, rank coins by their OWN recent realized strategy
performance (trailing avg_r of shock_candle_overshoot_catch trades), shrunk
toward the pool mean so low-trade-count coins cannot outrank high-count coins
on noise, then trade only the top-N coins that month.

Score for coin c at month start t:
    score_c = (sum_r_c + K * pool_mean) / (n_c + K)
i.e. the posterior mean under a prior centered on the trailing pool avg_r
with prior strength K pseudo-trades (empirical-Bayes / James-Stein-style
shrinkage). A coin with n=0 trades scores exactly pool_mean; confidence in a
coin's own record is n/(n+K).

Rebalance period: MONTHLY. Justification: the strategy produces ~10-55 trades
per coin per month; a weekly window would leave most coins with <10 trades of
fresh evidence per period, making even shrunk estimates pure prior. Monthly
rebalance + a multi-week trailing lookback gives 30-150 trades per coin, the
minimum for avg_r to carry signal.

Walk-forward: months numbered 1..12 (Jul-2025 .. Jun-2026). For each test
month m >= 3, the selection uses ONLY trades with entry_time strictly before
the first day of month m (and within the trailing lookback). Baselines over
the same test months: (1) trade all 18 coins, (2) static top-10 by total
12-month quote volume from the candle data.

Tuning: lookback / K / N are chosen on the EARLY test months only
(m = 3..6, Sep-Dec 2025) and then frozen; months 7..12 (Jan-Jun 2026) are
untouched holdout. The number of variants tried is printed.

Usage: python3 selector_trailing_perf.py
"""

import glob
import os
import subprocess
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = "/tmp/claude-0/-home-user-hype/c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad"
TRADES_PARQUET = os.path.join(SCRATCH, "binance_trades.parquet")
CANDLE_DIR = os.path.join(HERE, "data_binance")

# ---------------------------------------------------------------- frozen rule
# The rule is frozen as the winner of the tuning grid on months 3..6 ONLY
# (computed in main(); ties in avg_r within EPS are broken toward the LARGEST
# shrinkage K — most conservative — then the LARGEST N). Months 7..12 never
# influence the choice.
TIE_EPS = 1e-9

TEST_MONTH_FIRST = 3          # first walk-forward test month (1-based)
TUNE_MONTHS = (3, 4, 5, 6)    # months used for tuning design choices
HOLDOUT_MONTHS = (7, 8, 9, 10, 11, 12)


def load_trades() -> pd.DataFrame:
    if not os.path.exists(TRADES_PARQUET):
        print(f"{TRADES_PARQUET} missing — regenerating via validate_binance.py")
        subprocess.run([sys.executable, os.path.join(HERE, "validate_binance.py")],
                       check=True, cwd=HERE)
    df = pd.read_parquet(TRADES_PARQUET)
    df = df.sort_values("entry_time").reset_index(drop=True)
    # month index 1..12 starting Jul-2025
    p = df["entry_time"].dt.to_period("M")
    p0 = pd.Period("2025-07", "M")
    df["month"] = (p - p0).map(lambda x: x.n) + 1
    return df


def month_start(m: int) -> pd.Timestamp:
    return (pd.Period("2025-07", "M") + (m - 1)).to_timestamp()


def volume_top10() -> list:
    rows = []
    for f in sorted(glob.glob(os.path.join(CANDLE_DIR, "*_Min1.parquet"))):
        sym = os.path.basename(f).replace("_Min1.parquet", "")
        amt = pd.read_parquet(f, columns=["amount"])["amount"].sum()
        rows.append((sym, amt))
    rows.sort(key=lambda x: -x[1])
    return [s for s, _ in rows[:10]]


def select_coins(trades: pd.DataFrame, m: int, lookback_days: int,
                 shrink_k: float, top_n: int, all_syms: list) -> list:
    """Top-N coins by shrunk trailing avg_r, using only data before month m."""
    t1 = month_start(m)
    t0 = t1 - pd.Timedelta(days=lookback_days)
    win = trades[(trades["entry_time"] >= t0) & (trades["entry_time"] < t1)]
    pool_mean = win["r"].mean() if len(win) else 0.0
    g = win.groupby("symbol")["r"].agg(["sum", "count"])
    scores = {}
    for s in all_syms:
        if s in g.index:
            sr, n = g.loc[s, "sum"], g.loc[s, "count"]
        else:
            sr, n = 0.0, 0
        scores[s] = (sr + shrink_k * pool_mean) / (n + shrink_k)
    ranked = sorted(all_syms, key=lambda s: -scores[s])
    return ranked[:top_n]


def month_metrics(trades: pd.DataFrame, m: int, syms=None) -> dict:
    sub = trades[trades["month"] == m]
    if syms is not None:
        sub = sub[sub["symbol"].isin(syms)]
    n = len(sub)
    if n == 0:
        return {"n": 0, "avg_r": np.nan, "total_r": 0.0, "wr": np.nan}
    return {"n": n,
            "avg_r": sub["r"].mean(),
            "total_r": sub["r"].sum(),
            "wr": (sub["outcome"] == "tp").mean()}


def run_variant(trades, months, lookback, k, top_n, all_syms):
    out = []
    for m in months:
        sel = select_coins(trades, m, lookback, k, top_n, all_syms)
        out.append(month_metrics(trades, m, sel))
    return out


def agg(rows):
    ns = np.array([r["n"] for r in rows])
    tot = np.array([r["total_r"] for r in rows])
    n = ns.sum()
    avg = tot.sum() / n if n else np.nan
    wr = np.nansum([r["wr"] * r["n"] for r in rows]) / n if n else np.nan
    return {"n": int(n), "avg_r": avg, "total_r": tot.sum(), "wr": wr,
            "months_pos": int((tot > 0).sum()), "months": len(rows)}


def main():
    trades = load_trades()
    all_syms = sorted(trades["symbol"].unique())
    test_months = list(range(TEST_MONTH_FIRST, 13))

    # ------------------------------------------------ tuning (early months only)
    grid_lb = [30, 60, 90]
    grid_k = [10, 25, 50]
    grid_n = [6, 9, 12]
    print(f"TUNING on months {TUNE_MONTHS} (Sep-Dec 2025) only; "
          f"{len(grid_lb) * len(grid_k) * len(grid_n)} variants tried")
    results = []
    for lb in grid_lb:
        for k in grid_k:
            for tn in grid_n:
                rows = run_variant(trades, TUNE_MONTHS, lb, k, tn, all_syms)
                a = agg(rows)
                results.append(((lb, k, tn), a))
    # sort: best avg_r first; ties (within EPS) -> largest K, then largest N
    results.sort(key=lambda x: (-round(x[1]["avg_r"] / TIE_EPS) * TIE_EPS,
                                -x[0][1], -x[0][2]))
    print(f"{'lb':>4} {'K':>4} {'N':>3} {'avg_r':>8} {'total_r':>8} {'n':>5} {'m+':>3}")
    for (lb, k, tn), a in results[:8]:
        print(f"{lb:>4} {k:>4} {tn:>3} {a['avg_r']:>8.4f} {a['total_r']:>8.1f} "
              f"{a['n']:>5} {a['months_pos']:>3}")
    lb, k, tn = results[0][0]
    print(f"FROZEN rule (tune-window winner): lookback={lb}d K={k} N={tn}")

    # ------------------------------------------------ baselines
    vol10 = volume_top10()
    print(f"\nstatic top-10 by 12mo quote volume: {vol10}")

    # ------------------------------------------------ walk-forward table
    print(f"\nWALK-FORWARD (test months {test_months[0]}..12, "
          f"selection uses only trades before each month)")
    hdr = (f"{'month':>8} | {'sel n':>5} {'avg_r':>8} {'tot_r':>7} {'wr':>5} | "
           f"{'all n':>5} {'avg_r':>8} {'tot_r':>7} {'wr':>5} | "
           f"{'v10 n':>5} {'avg_r':>8} {'tot_r':>7} {'wr':>5} | dropped")
    print(hdr)
    print("-" * len(hdr))
    sel_rows, all_rows, vol_rows, diffs_all, diffs_vol = [], [], [], [], []
    for m in test_months:
        sel = select_coins(trades, m, lb, k, tn, all_syms)
        dropped = [s.replace("_USDT", "") for s in all_syms if s not in sel]
        rs = month_metrics(trades, m, sel)
        ra = month_metrics(trades, m, None)
        rv = month_metrics(trades, m, vol10)
        sel_rows.append(rs); all_rows.append(ra); vol_rows.append(rv)
        diffs_all.append(rs["avg_r"] - ra["avg_r"])
        diffs_vol.append(rs["avg_r"] - rv["avg_r"])
        label = month_start(m).strftime("%b-%y")
        print(f"{label:>8} | {rs['n']:>5} {rs['avg_r']:>8.4f} {rs['total_r']:>7.1f} "
              f"{rs['wr']:>5.1%} | {ra['n']:>5} {ra['avg_r']:>8.4f} "
              f"{ra['total_r']:>7.1f} {ra['wr']:>5.1%} | {rv['n']:>5} "
              f"{rv['avg_r']:>8.4f} {rv['total_r']:>7.1f} {rv['wr']:>5.1%} | "
              f"{','.join(dropped)}")

    def show(name, rows):
        a = agg(rows)
        print(f"{name:>22}: n={a['n']:>5} avg_r={a['avg_r']:+.4f} "
              f"total_r={a['total_r']:+8.1f} wr={a['wr']:.1%} "
              f"months_positive={a['months_pos']}/{a['months']}")
        return a

    print("\nAGGREGATE over all test months (3..12):")
    show("selector top-N", sel_rows)
    show("baseline all coins", all_rows)
    show("baseline vol top-10", vol_rows)

    hs = [r for m, r in zip(test_months, sel_rows) if m in HOLDOUT_MONTHS]
    ha = [r for m, r in zip(test_months, all_rows) if m in HOLDOUT_MONTHS]
    hv = [r for m, r in zip(test_months, vol_rows) if m in HOLDOUT_MONTHS]
    print("\nHOLDOUT ONLY (months 7..12, Jan-Jun 2026 — untouched by tuning):")
    show("selector top-N", hs)
    show("baseline all coins", ha)
    show("baseline vol top-10", hv)

    def tstat(d):
        d = np.array(d, dtype=float)
        return d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))

    d_all = np.array(diffs_all)
    d_vol = np.array(diffs_vol)
    hd_all = [x for m, x in zip(test_months, diffs_all) if m in HOLDOUT_MONTHS]
    print("\nMONTHLY avg_r DIFFERENCES (selector minus baseline):")
    print(f"  vs all-coins : mean={d_all.mean():+.4f}  t={tstat(d_all):+.2f} "
          f"(n={len(d_all)} months)   holdout-only t={tstat(hd_all):+.2f}")
    print(f"  vs vol-top10 : mean={d_vol.mean():+.4f}  t={tstat(d_vol):+.2f}")


if __name__ == "__main__":
    main()
