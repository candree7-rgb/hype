"""Daily/4h panel backtest engine for the TSMOM / trend-following round.

Design (honesty-first, per RESULTS.md invalidation banner):
- Signals at candle CLOSE of period t may use only data <= t.
- Execution at the NEXT candle OPEN (t+1). The engine itself applies the
  one-period shift, so a strategy cannot accidentally trade on the signal
  candle. No intrabar fills anywhere: PnL accrues open-to-open. Stops /
  trailing exits in strategies must be evaluated on CLOSE and exited next
  open (structurally impossible to hit the entry-candle-TP artifact class).
- Costs: taker 0.045% + slippage 0.03% = 0.075% per side, charged on
  turnover |w_t - w_{t-1}| (weights are signed fractions of equity).
- Automatic lookahead check: weights are recomputed on truncated panels and
  compared.

Strategy contract — strategies/tsmom_<name>.py exposes:
    DEFAULT_PARAMS = {...}
    def target_weights(panel, params) -> pd.DataFrame
        # index = panel dates, columns = symbols
        # value at date t = desired signed exposure (fraction of equity)
        # decided at close of t, applied at open of t+1. NaN = flat.

Run:
    python3 engine_tsmom.py strategies/tsmom_classic.py [--tf 1d]
        [--params '{...}'] [--start 2023-07-01] [--end 2025-06-30]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data_tsmom"
COST_PER_SIDE = 0.00045 + 0.0003  # taker + slippage
IS_START, IS_END = "2023-07-01", "2025-06-30"          # in-sample (tune here)
HOLDOUT_START, HOLDOUT_END = "2025-07-01", "2026-06-30"  # open ONCE, <=2 finalists

PERIODS_PER_YEAR = {"1d": 365, "4h": 365 * 6}


# ---------------------------------------------------------------- data panel

def load_panel(tf: str = "1d", symbols: list[str] | None = None) -> dict:
    """Load aligned OHLCV panel. Returns dict of DataFrames keyed by field."""
    files = sorted(DATA_DIR.glob(f"*_{tf}.parquet"))
    frames = {}
    for f in files:
        sym = f.name.replace(f"_{tf}.parquet", "")
        if symbols and sym not in symbols:
            continue
        df = pd.read_parquet(f).set_index("dt")
        frames[sym] = df
    panel = {}
    for field in ("open", "high", "low", "close", "vol", "amount"):
        panel[field] = pd.DataFrame({s: d[field] for s, d in frames.items()}).sort_index()
    return panel


# ------------------------------------------------------------ shared helpers

def realized_vol(close: pd.DataFrame, window: int, ppy: int) -> pd.DataFrame:
    """Annualized EW vol of close-to-close returns (uses data <= t only)."""
    ret = close.pct_change()
    return ret.ewm(span=window, min_periods=max(5, window // 2)).std() * np.sqrt(ppy)


def atr(panel: dict, window: int) -> pd.DataFrame:
    h, l, c = panel["high"], panel["low"], panel["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()]).groupby(level=0).max()
    return tr.ewm(span=window, min_periods=window // 2).mean()


# ---------------------------------------------------------------- backtest

def run_backtest(panel: dict, weights: pd.DataFrame, tf: str,
                 start: str | None = None, end: str | None = None) -> dict:
    """weights[t] decided at close t -> position during open[t+1]..open[t+2]."""
    o = panel["open"]
    # return earned by weight decided at close t: open[t+1] -> open[t+2]
    fwd_oo = o.shift(-2) / o.shift(-1) - 1.0          # aligned to signal date t
    w = weights.reindex(o.index).fillna(0.0).clip(-3, 3)
    # a symbol with no next-open price cannot be traded
    w = w.where(o.shift(-1).notna(), 0.0)
    gross = (w * fwd_oo).fillna(0.0)
    turnover = (w - w.shift(1).fillna(0.0)).abs()
    costs = turnover * COST_PER_SIDE
    pnl_by_asset = gross - costs
    pnl = pnl_by_asset.sum(axis=1)
    lev = w.abs().sum(axis=1)

    if start:
        m = pnl.index >= pd.Timestamp(start, tz="UTC")
        pnl, pnl_by_asset, lev, turnover = pnl[m], pnl_by_asset[m], lev[m], turnover[m]
    if end:
        m = pnl.index <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        pnl, pnl_by_asset, lev, turnover = pnl[m], pnl_by_asset[m], lev[m], turnover[m]
    # drop the last 2 periods (no forward return available)
    pnl, pnl_by_asset = pnl.iloc[:-2], pnl_by_asset.iloc[:-2]
    lev, turnover = lev.iloc[:-2], turnover.iloc[:-2]
    return {"pnl": pnl, "pnl_by_asset": pnl_by_asset, "lev": lev,
            "turnover": turnover.sum(axis=1), "tf": tf}


def metrics(bt: dict) -> dict:
    pnl = bt["pnl"]
    ppy = PERIODS_PER_YEAR[bt["tf"]]
    eq = (1 + pnl).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    monthly = pnl.groupby(pnl.index.to_period("M")).apply(
        lambda x: (1 + x).prod() - 1)
    sd = pnl.std()
    sharpe = float(pnl.mean() / sd * np.sqrt(ppy)) if sd > 0 else 0.0
    yrs = len(pnl) / ppy
    cagr = float(eq.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 and eq.iloc[-1] > 0 else -1.0
    return {
        "period": f"{pnl.index[0].date()} .. {pnl.index[-1].date()}",
        "n_periods": len(pnl),
        "total_return": float(eq.iloc[-1] - 1),
        "cagr": cagr,
        "sharpe": sharpe,
        "max_dd": float(dd),
        "avg_month": float(monthly.mean()),
        "median_month": float(monthly.median()),
        "worst_month": float(monthly.min()),
        "best_month": float(monthly.max()),
        "pct_months_pos": float((monthly > 0).mean()),
        "n_months": len(monthly),
        "avg_gross_lev": float(bt["lev"].mean()),
        "ann_turnover": float(bt["turnover"].mean() * ppy),
        "ann_cost_drag": float(bt["turnover"].mean() * ppy * COST_PER_SIDE),
        "monthly": {str(k): round(float(v), 4) for k, v in monthly.items()},
    }


def per_asset(bt: dict) -> pd.DataFrame:
    pa = bt["pnl_by_asset"]
    ppy = PERIODS_PER_YEAR[bt["tf"]]
    out = pd.DataFrame({
        "total": pa.sum(),
        "sharpe": pa.mean() / pa.std().replace(0, np.nan) * np.sqrt(ppy),
        "n_active": (pa != 0).sum(),
    })
    return out.sort_values("total", ascending=False)


# ----------------------------------------------------- portfolio vol target

def apply_port_vol_target(panel: dict, weights: pd.DataFrame, tf: str,
                          target: float = 0.20, window: int = 60,
                          max_mult: float = 10.0) -> pd.DataFrame:
    """Causal ex-ante portfolio vol targeting. Estimates portfolio vol from
    the UNscaled strategy pnl using only information available at decision
    time: pnl attributed to signal date s is realized over open[s+1]..open[s+2],
    so the estimate at decision date t uses pnl up to t-2 (shift(2))."""
    ppy = PERIODS_PER_YEAR[tf]
    bt0 = run_backtest(panel, weights, tf)
    pnl0 = bt0["pnl"].reindex(weights.index)
    vol_est = (pnl0.shift(2).ewm(span=window, min_periods=window // 2).std()
               * np.sqrt(ppy))
    mult = (target / vol_est).clip(upper=max_mult).fillna(0.0)
    return weights.mul(mult, axis=0)


# ------------------------------------------------------------ lookahead check

def lookahead_check(mod, panel: dict, params: dict, weights: pd.DataFrame,
                    n_cuts: int = 4) -> int:
    """Recompute weights on truncated panels; the weight at the cut date must
    match the full-panel weight (signal at t uses only data <= t)."""
    idx = panel["close"].index
    rng = np.random.default_rng(7)
    cuts = sorted(rng.choice(np.arange(len(idx) // 2, len(idx) - 3), n_cuts,
                             replace=False))
    violations = 0
    for ci in cuts:
        sub = {k: v.iloc[: ci + 1] for k, v in panel.items()}
        w_sub = mod.target_weights(sub, params)
        t = idx[ci]
        a = weights.loc[t].fillna(0.0)
        b = w_sub.loc[t].reindex(a.index).fillna(0.0)
        if not np.allclose(a.values, b.values, atol=1e-9):
            violations += 1
    return violations


# ----------------------------------------------------------------- CLI

def load_strategy(path: str):
    spec = importlib.util.spec_from_file_location("strategy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy")
    ap.add_argument("--tf", default="1d", choices=["1d", "4h"])
    ap.add_argument("--params", default=None)
    ap.add_argument("--start", default=IS_START)
    ap.add_argument("--end", default=IS_END)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--no-lookahead-check", action="store_true")
    ap.add_argument("--per-asset", action="store_true")
    ap.add_argument("--port-vol", type=float, default=None,
                    help="ex-ante portfolio vol target (e.g. 0.20)")
    args = ap.parse_args()

    mod = load_strategy(args.strategy)
    params = dict(mod.DEFAULT_PARAMS)
    if args.params:
        params.update(json.loads(args.params))
    syms = args.symbols.split(",") if args.symbols else None
    panel = load_panel(args.tf, syms)
    weights = mod.target_weights(panel, params)

    viol = 0
    if not args.no_lookahead_check:
        viol = lookahead_check(mod, panel, params, weights)

    if args.port_vol:
        weights = apply_port_vol_target(panel, weights, args.tf, args.port_vol)
    bt = run_backtest(panel, weights, args.tf, args.start, args.end)
    m = metrics(bt)
    m["lookahead_violations"] = viol
    m["params"] = params
    monthly = m.pop("monthly")
    print(json.dumps(m, indent=1, default=str))
    print("monthly:", json.dumps(monthly))
    if args.per_asset:
        print(per_asset(bt).to_string())


if __name__ == "__main__":
    main()
