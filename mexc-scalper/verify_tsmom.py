"""Adversarial verification of the TSMOM finalists (2026-07-22, second agent).

Jobs:
  repro    — reproduce F1/F2 IS + holdout metrics from engine + data
  funding  — join real Binance funding (data_funding) onto the position series,
             report actual funding paid/received and funding-adjusted metrics
  exec     — integrity: 4h bar continuity, missing bars, delistings, held
             positions with unpriced forward returns, port-vol causality,
             lookahead truncation check (8 cuts)
  robust   — k-cliff for F1 {63,84,105}, F2 plateau sweep, quarterly Sharpe,
             day-of-week, 2x costs
  spot     — 10 random 4h candles vs aggregated 1m Binance data

Run:  python3 verify_tsmom.py [repro|funding|exec|robust|spot|all]
New file — does not modify any frozen code.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import engine_tsmom as E  # noqa: E402

HERE = Path(__file__).parent
FUND_DIR = HERE / "data_funding"
BIN_DIR = HERE / "data_binance"

# frozen finalist configs (from scratchpad/tsmom_holdout.py, frozen pre-holdout)
F1P = {"lookbacks": [84], "vol_window": 180, "asset_vol_target": 0.20,
       "max_asset_lev": 1.0, "warmup": 540, "ppy": 2190, "rebalance_band": 0.0}
F2P = {"entry_n": 330, "exit_n": 120, "atr_window": 120, "atr_mult": 3.0,
       "vol_window": 180, "asset_vol_target": 0.20, "max_asset_lev": 1.0,
       "warmup": 540, "ppy": 2190}
PORT_VOL = 0.20


def load_mod(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, HERE / "strategies" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build(panel):
    classic, donch = load_mod("tsmom_classic"), load_mod("tsmom_donchian")
    out = {}
    for name, mod, P in [("F1", classic, F1P), ("F2", donch, F2P)]:
        w0 = mod.target_weights(panel, P)
        w = E.apply_port_vol_target(panel, w0, "4h", PORT_VOL)
        out[name] = (mod, P, w0, w)
    return out


def fmt(m):
    return (f"sharpe {m['sharpe']:+.2f}  avg_mo {m['avg_month']*100:+.2f}%  "
            f"worst_mo {m['worst_month']*100:+.1f}%  maxdd {m['max_dd']*100:.1f}%  "
            f"mo+ {int(m['pct_months_pos']*m['n_months'])}/{m['n_months']}  "
            f"lev {m['avg_gross_lev']:.2f}  turn {m['ann_turnover']:.0f}x")


# ------------------------------------------------------------------ funding

def load_funding(symbols, index):
    """Return DataFrame of funding rates aligned to the 4h panel index.
    Rate r at timestamp T means: longs pay r (shorts receive) at instant T."""
    rates = pd.DataFrame(0.0, index=index, columns=symbols)
    missing = []
    for sym in symbols:
        base = sym.replace("USDT", "")
        f = FUND_DIR / f"funding_{base}.parquet"
        if not f.exists():
            missing.append(sym)
            continue
        df = pd.read_parquet(f)
        # Binance stamps occasionally carry a +1ms remainder -> round to hour
        ts = pd.to_datetime(df["calc_time"], unit="ms", utc=True).dt.round("h")
        s = pd.Series(df["last_funding_rate"].values, index=ts)
        s = s[~s.index.duplicated(keep="last")]
        aligned = s.reindex(index)          # funding stamps sit on the 4h grid
        off_grid = s.index.difference(index)
        if len(off_grid) > 0:
            print(f"  WARN {sym}: {len(off_grid)} funding stamps off the 4h grid")
        rates[sym] = aligned.fillna(0.0)
    if missing:
        print("  WARN no funding file for:", missing)
    return rates


def funding_pnl(w, rates):
    """Funding attributed to signal date t: the position w_t is held during
    [open_{t+1}, open_{t+2}); a funding event at instant t+1 hits it.
    fund_t = -(w_t * rate_{t+1}) (long pays positive funding)."""
    return -(w * rates.shift(-1)).fillna(0.0)


def metrics_with(pnl, tf="4h"):
    bt = {"pnl": pnl, "pnl_by_asset": pd.DataFrame(pnl), "tf": tf,
          "lev": pnl * 0, "turnover": pnl * 0}
    return E.metrics(bt)


def slice_pnl(pnl, s, e):
    m = (pnl.index >= pd.Timestamp(s, tz="UTC")) & \
        (pnl.index <= pd.Timestamp(e, tz="UTC") + pd.Timedelta(days=1))
    return pnl[m]


def job_repro(panel, built):
    print("=" * 72)
    print("REPRO — frozen configs, engine as committed")
    for name, (mod, P, w0, w) in built.items():
        viol = E.lookahead_check(mod, panel, P, w0)
        print(f"\n{name} (lookahead violations {viol})")
        for tag, s, e in [("IS  ", E.IS_START, E.IS_END),
                          ("HOLD", E.HOLDOUT_START, E.HOLDOUT_END)]:
            bt = E.run_backtest(panel, w, "4h", s, e)
            m = E.metrics(bt)
            print(f"  [{tag}] {fmt(m)}")


def job_funding(panel, built):
    print("=" * 72)
    print("FUNDING — real Binance rates joined onto held positions")
    idx = panel["close"].index
    rates = load_funding(list(panel["close"].columns), idx)
    n_ev = (rates != 0).sum().sum()
    print(f"  funding events on grid: {n_ev}, mean |rate| "
          f"{rates[rates != 0].abs().stack().mean():.5f}")
    for name, (mod, P, w0, w) in built.items():
        fp = funding_pnl(w, rates)
        fp_sens = -(w.shift(1) * rates.shift(-1)).fillna(0.0)  # pre-trade position
        bt_full = E.run_backtest(panel, w, "4h")
        for tag, s, e in [("IS  ", E.IS_START, E.IS_END),
                          ("HOLD", E.HOLDOUT_START, E.HOLDOUT_END)]:
            pnl = slice_pnl(bt_full["pnl"], s, e)
            f = slice_pnl(fp.sum(axis=1), s, e).reindex(pnl.index).fillna(0.0)
            f2 = slice_pnl(fp_sens.sum(axis=1), s, e).reindex(pnl.index).fillna(0.0)
            m0 = metrics_with(pnl)
            m1 = metrics_with(pnl + f)
            yrs = len(pnl) / 2190
            print(f"\n{name} [{tag}] funding net {f.sum()*100:+.2f}% "
                  f"({f.sum()/yrs*100:+.2f}%/yr; sensitivity alt-attrib "
                  f"{f2.sum()*100:+.2f}%)")
            print(f"    ex-funding : {fmt(m0)}")
            print(f"    incl fundg : {fmt(m1)}")
            # split: funding received while short vs paid while long
            wl = w.clip(lower=0)
            ws = w.clip(upper=0)
            fl = slice_pnl((-(wl * rates.shift(-1))).sum(axis=1).fillna(0), s, e)
            fs = slice_pnl((-(ws * rates.shift(-1))).sum(axis=1).fillna(0), s, e)
            print(f"    long-side funding {fl.sum()*100:+.2f}%  "
                  f"short-side funding {fs.sum()*100:+.2f}%")


def job_exec(panel, built):
    print("=" * 72)
    print("EXECUTION INTEGRITY")
    idx = panel["close"].index
    diffs = pd.Series(idx).diff().dropna()
    bad = diffs[diffs != pd.Timedelta(hours=4)]
    print(f"  union index: {len(idx)} bars {idx[0]} .. {idx[-1]}; "
          f"non-4h gaps in union index: {len(bad)}")
    if len(bad):
        print(bad.head())
    c = panel["close"]
    print("\n  per-symbol: first bar, last bar, missing-inside count")
    for sym in c.columns:
        s = c[sym]
        fv, lv = s.first_valid_index(), s.last_valid_index()
        inside = s.loc[fv:lv]
        miss = inside.isna().sum()
        end_early = " <-- ENDS EARLY" if lv < idx[-1] else ""
        if miss > 0 or end_early or fv > idx[0]:
            print(f"    {sym:16s} {fv.date()} .. {lv.date()}  missing {miss}{end_early}")
    # held positions with unpriced forward return (open[t+1] ok, open[t+2] NaN)
    o = panel["open"]
    fwd = o.shift(-2) / o.shift(-1) - 1.0
    for name, (_, _, _, w) in built.items():
        wt = w.where(o.shift(-1).notna(), 0.0)
        unpriced = ((wt != 0) & fwd.isna() & o.shift(-1).notna()).iloc[:-2].sum().sum()
        print(f"  {name}: held-position periods with NaN forward return: {unpriced}")
    # port-vol causality: recompute multiplier on truncated panel
    print("\n  port-vol targeting causality (truncated-panel recompute):")
    classic = load_mod("tsmom_classic")
    w0 = built["F1"][2]
    w_full = built["F1"][3]
    rng = np.random.default_rng(11)
    cuts = sorted(rng.choice(np.arange(len(idx) // 2, len(idx) - 3), 5, replace=False))
    viol = 0
    for ci in cuts:
        sub = {k: v.iloc[: ci + 1] for k, v in panel.items()}
        w0s = classic.target_weights(sub, F1P)
        ws = E.apply_port_vol_target(sub, w0s, "4h", PORT_VOL)
        t = idx[ci]
        # NOTE: run_backtest inside apply_port_vol_target loses the last 2 rows'
        # pnl (no fwd return) — the vol estimate at the cut uses shift(2), so the
        # truncated estimate at t must equal the full-panel one if causal.
        a, b = w_full.loc[t].fillna(0), ws.loc[t].reindex(w_full.columns).fillna(0)
        if not np.allclose(a.values, b.values, atol=1e-9):
            viol += 1
            print(f"    VIOLATION at {t}: max diff {(a-b).abs().max():.2e}")
    print(f"    violations: {viol}/5")
    # strategy lookahead with more cuts
    for name, (mod, P, w0n, _) in built.items():
        v = E.lookahead_check(mod, panel, P, w0n, n_cuts=8)
        print(f"  {name}: strategy lookahead check (8 cuts): {v} violations")


def job_robust(panel, built):
    print("=" * 72)
    print("ROBUSTNESS")
    classic, donch = load_mod("tsmom_classic"), load_mod("tsmom_donchian")
    print("\nF1 k-sweep (holdout, frozen everything else):")
    sharpes = {}
    for k in (63, 84, 105):
        P = dict(F1P, lookbacks=[k])
        w = E.apply_port_vol_target(panel, classic.target_weights(panel, P), "4h", PORT_VOL)
        mi = E.metrics(E.run_backtest(panel, w, "4h", E.IS_START, E.IS_END))
        mh = E.metrics(E.run_backtest(panel, w, "4h", E.HOLDOUT_START, E.HOLDOUT_END))
        sharpes[k] = mh["sharpe"]
        print(f"  k={k:3d}  IS sharpe {mi['sharpe']:+.2f}  |  HOLD {fmt(mh)}")
    print(f"  -> honest-parameter holdout Sharpe (avg of 3): "
          f"{np.mean(list(sharpes.values())):+.2f} vs cherry k=84 {sharpes[84]:+.2f}")
    print("\nF2 plateau (holdout):")
    s_list = []
    for en in (270, 330, 390):
        for ex in (90, 120, 150):
            P = dict(F2P, entry_n=en, exit_n=ex)
            w = E.apply_port_vol_target(panel, donch.target_weights(panel, P), "4h", PORT_VOL)
            mh = E.metrics(E.run_backtest(panel, w, "4h", E.HOLDOUT_START, E.HOLDOUT_END))
            s_list.append(mh["sharpe"])
            print(f"  entry {en} exit {ex}: HOLD sharpe {mh['sharpe']:+.2f} "
                  f"avg_mo {mh['avg_month']*100:+.2f}%")
    print(f"  -> plateau avg holdout Sharpe: {np.mean(s_list):+.2f} "
          f"(min {np.min(s_list):+.2f}, max {np.max(s_list):+.2f})")
    print("\nQuarterly Sharpe (frozen configs, full 3y):")
    for name, (_, _, _, w) in built.items():
        bt = E.run_backtest(panel, w, "4h")
        pnl = bt["pnl"]
        q = pnl.groupby(pnl.index.to_period("Q"))
        qs = q.apply(lambda x: x.mean() / x.std() * np.sqrt(2190) if x.std() > 0 else 0)
        print(f"  {name}: " + "  ".join(f"{str(k)}:{v:+.1f}" for k, v in qs.items()))
    print("\nDay-of-week mean pnl (bp/4h-bar, holdout):")
    for name, (_, _, _, w) in built.items():
        pnl = E.run_backtest(panel, w, "4h", E.HOLDOUT_START, E.HOLDOUT_END)["pnl"]
        dw = pnl.groupby(pnl.index.dayofweek).mean() * 1e4
        print(f"  {name}: " + "  ".join(f"{d}:{v:+.1f}" for d, v in dw.items()))
    print("\n2x total costs (0.15%/side), holdout:")
    E.COST_PER_SIDE = 0.0015
    for name, (_, _, _, w) in built.items():
        mh = E.metrics(E.run_backtest(panel, w, "4h", E.HOLDOUT_START, E.HOLDOUT_END))
        print(f"  {name}: {fmt(mh)}")
    E.COST_PER_SIDE = 0.00075


def job_spot(panel, built):
    print("=" * 72)
    print("SPOT-CHECK — 10 random 4h candles vs 1m Binance aggregation")
    rng = np.random.default_rng(42)
    syms = [s for s in panel["close"].columns
            if (BIN_DIR / f"{s.replace('USDT','_USDT')}_Min1.parquet").exists()]
    checks = 0
    worst = 0.0
    while checks < 10:
        sym = syms[rng.integers(len(syms))]
        m1 = pd.read_parquet(BIN_DIR / f"{sym.replace('USDT','_USDT')}_Min1.parquet")
        m1 = m1.set_index("dt")
        t = panel["close"].index[rng.integers(len(panel["close"].index))]
        if t < m1.index[0] or t + pd.Timedelta(hours=4) > m1.index[-1]:
            continue
        win = m1.loc[t: t + pd.Timedelta(minutes=239)]
        if len(win) < 200 or pd.isna(panel["open"].loc[t, sym]):
            continue
        agg = {"open": win["open"].iloc[0], "high": win["high"].max(),
               "low": win["low"].min(), "close": win["close"].iloc[-1]}
        errs = {f: abs(agg[f] / panel[f].loc[t, sym] - 1) for f in agg}
        worst = max(worst, max(errs.values()))
        print(f"  {sym:14s} {t}  " +
              "  ".join(f"{f}:{e*1e4:.2f}bp" for f, e in errs.items()) +
              f"  (n_1m={len(win)})")
        checks += 1
    print(f"  worst field error: {worst*1e4:.2f} bp")


def main():
    jobs = sys.argv[1:] or ["all"]
    panel = E.load_panel("4h")
    built = build(panel)
    all_jobs = {"repro": job_repro, "funding": job_funding, "exec": job_exec,
                "robust": job_robust, "spot": job_spot}
    for j in (all_jobs if "all" in jobs else jobs):
        all_jobs[j](panel, built)


if __name__ == "__main__":
    main()
