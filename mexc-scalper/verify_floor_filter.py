#!/usr/bin/env python3
"""Adversarial verification of the floor-filter claims (feature-analysis agent).

Claims under test:
 1. Floor-bound trades (3*ATR60/close < 0.003 => sl_dist floored at 0.003)
    have ~zero edge (-0.015R, n=2624) vs non-floor +0.213R (n=2797) on 12mo
    Binance. Stability by month, by coin, within mid-caps; filtered portfolio.
 2. BTC: 93.9% floor-bound; post-filter BTC ~ +0.096R on n~40/yr.
 3. Package = floor filter + liquidity floor + trailing selector N=12
    (drop-worst-6), walk-forward, vs unfiltered baseline; holdout Jan-Jun 26;
    MEXC 38-coin 30d directionally.
 4. Fragility: thresholds 0.25%/0.35%; regime concentration of non-floor edge.

Everything recomputed from candles + trades parquets. ATR is recomputed
independently at each signal candle and cross-checked against the stored
sl_dist.
"""
import glob
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = "/tmp/claude-0/-home-user-hype/c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad"
BIN_DIR = os.path.join(HERE, "data_binance")
MEXC_DIR = os.path.join(HERE, "data")
ATR_N = 60
SL_FLOOR = 0.003
EPS = 1e-9


def frac_atr(candle_file: str) -> np.ndarray:
    df = pd.read_parquet(candle_file, columns=["high", "low", "close"])
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(ATR_N).mean().to_numpy() / c


def attach_recomputed_atr(trades: pd.DataFrame, candle_dir: str) -> pd.DataFrame:
    """Recompute 3*ATR60/close at each signal candle; cross-check vs sl_dist."""
    out = []
    for sym, sub in trades.groupby("symbol"):
        f = os.path.join(candle_dir, f"{sym}_Min1.parquet")
        atr = frac_atr(f)
        sub = sub.copy()
        sub["atr3"] = 3.0 * atr[sub["signal_idx"].to_numpy()]
        out.append(sub)
    t = pd.concat(out, ignore_index=True)
    t["sl_expected"] = np.maximum(t["atr3"], SL_FLOOR)
    t["sl_mismatch"] = np.abs(t["sl_expected"] - t["sl_dist"]) > 1e-9
    t["floor_bound"] = t["atr3"] < SL_FLOOR - EPS          # recomputed definition
    t["floor_bound_sl"] = t["sl_dist"] <= SL_FLOOR + EPS   # from stored sl_dist
    return t


def stats(sub: pd.DataFrame, label: str) -> dict:
    n = len(sub)
    if n == 0:
        return {"label": label, "n": 0, "avg_r": np.nan, "wr": np.nan, "tot": 0.0}
    return {"label": label, "n": n, "avg_r": sub["r"].mean(),
            "wr": (sub["outcome"] == "tp").mean(), "tot": sub["r"].sum()}


def prow(d):
    print(f"  {d['label']:<28} n={d['n']:>5}  avg_r={d['avg_r']:+.4f}  "
          f"wr={d['wr']:.1%}  total_r={d['tot']:+8.1f}")


def month_col(t: pd.DataFrame) -> pd.Series:
    p = t["entry_time"].dt.to_period("M")
    p0 = pd.Period("2025-07", "M")
    return (p - p0).map(lambda x: x.n) + 1


# --------------------------------------------------------------- selector bits
def select_coins(trades, m, lookback_days, shrink_k, top_n, all_syms):
    t1 = (pd.Period("2025-07", "M") + (m - 1)).to_timestamp()
    t0 = t1 - pd.Timedelta(days=lookback_days)
    win = trades[(trades["entry_time"] >= t0) & (trades["entry_time"] < t1)]
    pool = win["r"].mean() if len(win) else 0.0
    g = win.groupby("symbol")["r"].agg(["sum", "count"])
    sc = {}
    for s in all_syms:
        sr, n = (g.loc[s, "sum"], g.loc[s, "count"]) if s in g.index else (0.0, 0)
        sc[s] = (sr + shrink_k * pool) / (n + shrink_k)
    return sorted(all_syms, key=lambda s: -sc[s])[:top_n]


def agg_months(t, months, syms_by_month=None):
    rows = []
    for m in months:
        sub = t[t["month"] == m]
        if syms_by_month is not None:
            sub = sub[sub["symbol"].isin(syms_by_month[m])]
        rows.append((m, len(sub), sub["r"].sum(),
                     (sub["outcome"] == "tp").mean() if len(sub) else np.nan))
    n = sum(r[1] for r in rows)
    tot = sum(r[2] for r in rows)
    wr = sum((r[3] if r[3] == r[3] else 0) * r[1] for r in rows) / n if n else np.nan
    pos = sum(1 for r in rows if r[2] > 0)
    return {"n": n, "avg_r": tot / n if n else np.nan, "tot": tot, "wr": wr,
            "months_pos": pos, "months": len(rows), "rows": rows}


def show_agg(name, a):
    print(f"  {name:<34} n={a['n']:>5} avg_r={a['avg_r']:+.4f} "
          f"total_r={a['tot']:+8.1f} wr={a['wr']:.1%} "
          f"months+={a['months_pos']}/{a['months']}")


def main():
    # ================================================================ load
    bt = pd.read_parquet(os.path.join(SCRATCH, "binance_trades.parquet"))
    bt = attach_recomputed_atr(bt, BIN_DIR)
    bt["month"] = month_col(bt)

    print("=" * 78)
    print("SANITY: recomputed max(3*ATR60,0.003) vs stored sl_dist (Binance 12mo)")
    print(f"  trades={len(bt)}  sl mismatches(>1e-9)={int(bt['sl_mismatch'].sum())}")
    agree = (bt["floor_bound"] == bt["floor_bound_sl"]).mean()
    print(f"  floor-bound via recomputed ATR vs via sl_dist agree: {agree:.4%}")
    fb = bt["floor_bound"]

    # ================================================================ claim 1
    print("\n" + "=" * 78)
    print("CLAIM 1 — floor vs non-floor split (Binance 12mo)")
    prow(stats(bt[fb], "floor-bound (3ATR<0.3%)"))
    prow(stats(bt[~fb], "non-floor"))

    print("\n  Month-by-month (avg_r | n)  floor vs non-floor:")
    print(f"  {'month':>7} | {'floor avg_r':>11} {'n':>5} | {'nonfl avg_r':>11} {'n':>5} | nonfl tot_r")
    worse = 0
    for m in range(1, 13):
        sm = bt[bt["month"] == m]
        f, nf = sm[sm["floor_bound"]], sm[~sm["floor_bound"]]
        lbl = (pd.Period("2025-07", "M") + (m - 1)).strftime("%b-%y")
        print(f"  {lbl:>7} | {f['r'].mean():>+11.4f} {len(f):>5} | "
              f"{nf['r'].mean():>+11.4f} {len(nf):>5} | {nf['r'].sum():>+8.1f}")
        if nf["r"].mean() > f["r"].mean():
            worse += 1
    print(f"  months where non-floor beats floor: {worse}/12")

    # per-coin
    print("\n  Per-coin split (sorted by median 1-min $vol):")
    dvol = {}
    for sym in bt["symbol"].unique():
        f = os.path.join(BIN_DIR, f"{sym}_Min1.parquet")
        dvol[sym] = pd.read_parquet(f, columns=["amount"])["amount"].median()
    order = sorted(dvol, key=lambda s: -dvol[s])
    print(f"  {'symbol':<14} {'med$vol/1m':>11} | {'floor avg_r':>11} {'n':>5} | "
          f"{'nonfl avg_r':>11} {'n':>5} | floor%")
    helped_within = []
    for sym in order:
        s = bt[bt["symbol"] == sym]
        f, nf = s[s["floor_bound"]], s[~s["floor_bound"]]
        print(f"  {sym:<14} {dvol[sym]:>11,.0f} | {f['r'].mean():>+11.4f} {len(f):>5} | "
              f"{nf['r'].mean():>+11.4f} {len(nf):>5} | {len(f)/len(s):.0%}")
        if len(f) >= 20 and len(nf) >= 20:
            helped_within.append((sym, nf["r"].mean() - f["r"].mean()))
    npos = sum(1 for _, d in helped_within if d > 0)
    print(f"  coins with >=20 trades on both sides where non-floor > floor: "
          f"{npos}/{len(helped_within)}")

    # mid-caps: exclude the top-3 books (BTC/ETH/SOL by $vol) entirely
    mid = [s for s in order[3:]]
    sm = bt[bt["symbol"].isin(mid)]
    print(f"\n  WITHIN mid-caps (excluding top-3 books {order[:3]}):")
    prow(stats(sm[sm["floor_bound"]], "mid-cap floor-bound"))
    prow(stats(sm[~sm["floor_bound"]], "mid-cap non-floor"))

    # filtered portfolio
    print("\n  Floor-filtered portfolio (non-floor only), all 12 months:")
    a = agg_months(bt[~fb].assign(), range(1, 13))
    show_agg("floor-filtered", a)
    a0 = agg_months(bt, range(1, 13))
    show_agg("unfiltered baseline", a0)

    # ================================================================ claim 2
    print("\n" + "=" * 78)
    print("CLAIM 2 — BTC")
    b = bt[bt["symbol"] == "BTC_USDT"]
    print(f"  BTC floor-bound fraction: {b['floor_bound'].mean():.1%}  (claimed 93.9%)")
    prow(stats(b[b["floor_bound"]], "BTC floor-bound"))
    prow(stats(b[~b["floor_bound"]], "BTC non-floor (survives filter)"))

    # ================================================================ claim 3
    print("\n" + "=" * 78)
    print("CLAIM 3 — deployment package, walk-forward on Binance 12mo")
    all_syms = sorted(bt["symbol"].unique())

    # liquidity floor: median 1-min $vol thresholds
    liq_full = [s for s in all_syms if dvol[s] >= 27_000]
    liq_excl = [s for s in all_syms if dvol[s] < 2_600]
    print(f"  liquidity floor on the 18 Binance coins: full-size ok "
          f"{len(liq_full)}/18; excluded (<$2.6k med): {liq_excl or 'none'}")

    ft = bt[~fb].reset_index(drop=True)  # floor filter applied
    test_months = list(range(3, 13))
    LB, K, N = 60, 50, 12  # frozen lookback/K from selector; N=12 = drop worst 6

    sel_by_month = {}
    for m in test_months:
        sel_by_month[m] = select_coins(ft, m, LB, K, N, all_syms)

    pkg = agg_months(ft, test_months, sel_by_month)
    base = agg_months(bt, test_months)
    filt_only = agg_months(ft, test_months)
    print("\n  months 3..12 (Sep-25 .. Jun-26):")
    show_agg("PACKAGE (floor+liq+selN12)", pkg)
    show_agg("floor filter only", filt_only)
    show_agg("unfiltered all-coin baseline", base)
    print("\n  per-month package rows (m, n, total_r):")
    for m, n, tot, wr in pkg["rows"]:
        lbl = (pd.Period("2025-07", "M") + (m - 1)).strftime("%b-%y")
        drop = [s.replace("_USDT", "") for s in all_syms if s not in sel_by_month[m]]
        print(f"    {lbl}: n={n:>4} tot_r={tot:+7.1f} wr={wr:.1%} dropped={','.join(drop)}")

    hold = [m for m in test_months if m >= 7]
    print("\n  HOLDOUT (Jan-Jun 26):")
    show_agg("PACKAGE", agg_months(ft, hold, sel_by_month))
    show_agg("floor filter only", agg_months(ft, hold))
    show_agg("unfiltered baseline", agg_months(bt, hold))

    # also: original frozen selector (N=6) on filtered trades, for reference
    sel6 = {m: select_coins(ft, m, LB, K, 6, all_syms) for m in test_months}
    show_agg("\n  ref: floor + selector N=6", agg_months(ft, test_months, sel6))

    # ---------------- MEXC 38-coin 30d
    print("\n  MEXC 38-coin 30d (winner_trades + newcoin_trades):")
    mx = pd.concat([pd.read_parquet(os.path.join(SCRATCH, "winner_trades.parquet")),
                    pd.read_parquet(os.path.join(SCRATCH, "newcoin_trades.parquet"))],
                   ignore_index=True)[["symbol", "side", "signal_idx", "entry_time",
                                       "sl_dist", "outcome", "r"]]
    mx = attach_recomputed_atr(mx, MEXC_DIR)
    print(f"    trades={len(mx)} syms={mx['symbol'].nunique()} "
          f"sl mismatches={int(mx['sl_mismatch'].sum())} "
          f"floor-def agree={(mx['floor_bound'] == mx['floor_bound_sl']).mean():.2%}")
    mfb = mx["floor_bound"]
    prow(stats(mx[mfb], "MEXC floor-bound"))
    prow(stats(mx[~mfb], "MEXC non-floor"))
    # within liquidity-passing MEXC coins (med 1-min $vol >= $2.6k)
    mdvol = {s: pd.read_parquet(os.path.join(MEXC_DIR, f"{s}_Min1.parquet"),
                                columns=["amount"])["amount"].median()
             for s in mx["symbol"].unique()}
    liq_ok = [s for s, v in mdvol.items() if v >= 2_600]
    ml = mx[mx["symbol"].isin(liq_ok)]
    print(f"    liquidity-passing MEXC coins (med>=?$2.6k): {len(liq_ok)}/{len(mdvol)}")
    prow(stats(ml[ml["floor_bound"]], "MEXC liq-ok floor-bound"))
    prow(stats(ml[~ml["floor_bound"]], "MEXC liq-ok non-floor"))

    # ================================================================ claim 4
    print("\n" + "=" * 78)
    print("CLAIM 4 — fragility")
    for thr in (0.0025, 0.003, 0.0035):
        lo = bt[bt["atr3"] < thr]
        hi = bt[bt["atr3"] >= thr]
        print(f"  thr={thr:.4f}: below n={len(lo):>4} avg_r={lo['r'].mean():+.4f} | "
              f"above n={len(hi):>4} avg_r={hi['r'].mean():+.4f}")

    # decile curve of avg_r vs atr3 (is it a monotone effect or a cliff?)
    q = pd.qcut(bt["atr3"], 10, duplicates="drop")
    dec = bt.groupby(q, observed=True)["r"].agg(["mean", "count"])
    print("\n  avg_r by decile of 3*ATR60/close:")
    for iv, row in dec.iterrows():
        print(f"    [{iv.left:.5f},{iv.right:.5f}] n={int(row['count']):>4} "
              f"avg_r={row['mean']:+.4f}")

    # regime concentration of non-floor edge
    nf = bt[~fb]
    monthly = nf.groupby("month")["r"].sum().sort_values(ascending=False)
    tot = nf["r"].sum()
    print(f"\n  non-floor total R={tot:+.1f}; best month share="
          f"{monthly.iloc[0]/tot:.1%}, top-3 share={monthly.iloc[:3].sum()/tot:.1%}")
    best_m = monthly.index[0]
    ex = nf[nf["month"] != best_m]
    print(f"  excluding best month ({best_m}): avg_r={ex['r'].mean():+.4f} n={len(ex)}")
    # same for the floor-bound side: is its ~0 an average of huge swings?
    fmon = bt[fb].groupby("month")["r"].sum()
    print(f"  floor-bound monthly total R: min={fmon.min():+.1f} max={fmon.max():+.1f}")


if __name__ == "__main__":
    main()
