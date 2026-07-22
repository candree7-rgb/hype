#!/usr/bin/env python3
"""8h-grid portfolio engine for funding/carry strategies.

Grid: 8h funding boundaries (00:00/08:00/16:00 UTC). Entries/exits happen at
boundary closes (taker fee both ways). Funding settled at boundary t is paid
on the position held INTO t, i.e. weight set at t-1. Long pays positive
funding, short receives it.

No intrabar anything: fills at 4h-candle closes that coincide with funding
boundaries. Signals at t may use only information with timestamp <= t
(funding rate with calc_time == t is published at t, so it is usable at t).

Strategy contract (strategies/fund_*.py):
    DEFAULT_PARAMS = {...}
    def target_weights(data: dict[str, pd.DataFrame], grid: pd.DatetimeIndex,
                       params: dict) -> pd.DataFrame
        # returns DataFrame indexed by grid, columns = symbols, values =
        # signed portfolio weight (fraction of equity notional) to hold from
        # that boundary close until the next one. NaN treated as 0.

Engine PnL per step t (weights w indexed same as grid):
    ret[t] = sum_s  w[t-1,s] * px_ret[t,s]              (price move t-1 -> t)
           - sum_s  w[t-1,s] * funding[t,s]             (funding settled at t)
           - sum_s |w[t,s] - w[t-1,s] * drift| * fee    (turnover at t)
Equity compounds multiplicatively.
"""
import glob
import importlib.util
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data_funding")

TAKER_FEE = 0.00045  # 0.045% per side (HL deployment target)

IS_START, IS_END = "2023-07-01", "2025-06-30 23:59"
HO_START, HO_END = "2025-07-01", "2026-06-30 23:59"


def load_symbol(sym):
    """Return 8h-grid DataFrame: close, funding, quote_vol_24h (rolling $ volume)."""
    fpath = os.path.join(DATA, f"funding_{sym}.parquet")
    kpath = os.path.join(DATA, f"klines_4h_{sym}.parquet")
    if not (os.path.exists(fpath) and os.path.exists(kpath)):
        return None
    fund = pd.read_parquet(fpath)
    kl = pd.read_parquet(kpath)
    # timestamps: ms epoch (some 2025+ files use microseconds — normalize)
    for df, col in ((fund, "calc_time"), (kl, "open_time")):
        v = df[col].astype("int64")
        v = np.where(v > 100_000_000_000_000, v // 1000, v)  # us -> ms (1e14 cutoff)
        df[col] = pd.to_datetime(v, unit="ms", utc=True).tz_localize(None)
    kl = kl.set_index("open_time").sort_index()
    kl["close_time"] = kl.index + pd.Timedelta(hours=4)
    px = kl.set_index("close_time")["close"].astype(float)
    qv = kl.set_index("close_time")["quote_volume"].astype(float)
    # 8h grid = 4h closes at 00/08/16 UTC
    grid_mask = px.index.hour.isin([0, 8, 16])
    px8 = px[grid_mask]
    # funding: snap calc_time to nearest boundary (files are exactly on it)
    fund = fund.set_index("calc_time").sort_index()
    fr = fund["last_funding_rate"].astype(float)
    fr.index = fr.index.round("h")
    fr = fr[~fr.index.duplicated(keep="last")]
    df = pd.DataFrame({"close": px8})
    df["funding"] = fr.reindex(df.index).fillna(0.0)
    # 24h rolling dollar volume (sum of six 4h quote_volumes), aligned to grid
    qv24 = qv.rolling(6, min_periods=3).sum()
    df["qvol_24h"] = qv24[grid_mask].reindex(df.index)
    return df


def load_all(symbols=None):
    if symbols is None:
        symbols = sorted(
            os.path.basename(p)[len("funding_"):-len(".parquet")]
            for p in glob.glob(os.path.join(DATA, "funding_*.parquet"))
        )
    data = {}
    for s in symbols:
        df = load_symbol(s)
        if df is not None and len(df) > 90:
            data[s] = df
    return data


def build_grid(data):
    idx = None
    for df in data.values():
        idx = df.index if idx is None else idx.union(df.index)
    return idx.sort_values()


def panel(data, grid, col):
    return pd.DataFrame({s: df[col].reindex(grid) for s, df in data.items()})


def run(weights, data, grid=None, fee=TAKER_FEE, start=None, end=None):
    """Backtest a weights panel. Returns dict of results."""
    if grid is None:
        grid = weights.index
    closes = panel(data, grid, "close").reindex(weights.index)
    funding = panel(data, grid, "funding").reindex(weights.index)[weights.columns]
    closes = closes[weights.columns]
    w = weights.fillna(0.0)
    # can't hold what has no price: zero weights where price missing
    w = w.where(closes.notna(), 0.0)
    px_ret = closes.pct_change(fill_method=None).fillna(0.0)
    w_prev = w.shift(1).fillna(0.0)
    price_pnl = (w_prev * px_ret).sum(axis=1)
    fund_pnl = (-w_prev * funding.fillna(0.0)).sum(axis=1)
    # turnover: weight drifts with the asset's return before rebalance
    w_drift = w_prev * (1.0 + px_ret)
    turnover = (w - w_drift).abs().sum(axis=1)
    cost = turnover * fee
    ret = price_pnl + fund_pnl - cost
    out = pd.DataFrame({
        "ret": ret, "price_pnl": price_pnl, "fund_pnl": fund_pnl,
        "cost": cost, "turnover": turnover,
        "gross": w.abs().sum(axis=1), "net": w.sum(axis=1),
    })
    if start:
        out = out[out.index >= pd.Timestamp(start)]
    if end:
        out = out[out.index <= pd.Timestamp(end)]
    return out


def metrics(out, label=""):
    ret = out["ret"]
    if len(ret) < 10:
        return {"label": label, "n": len(ret)}
    eq = (1 + ret).cumprod()
    peak = eq.cummax()
    dd = (eq / peak - 1).min()
    monthly = eq.resample("ME").last().pct_change(fill_method=None).dropna()
    # first month: use eq change from 1.0
    if len(eq) and eq.index[0].day > 1:
        pass
    first = eq.resample("ME").last().iloc[0] - 1
    monthly = pd.concat([pd.Series([first], index=[eq.resample("ME").last().index[0]]), monthly])
    monthly = monthly[~monthly.index.duplicated(keep="first")]
    per_year = 3 * 365.25
    sharpe = ret.mean() / (ret.std() + 1e-12) * np.sqrt(per_year)
    ann_ret = (1 + ret.mean()) ** per_year - 1
    ann_vol = ret.std() * np.sqrt(per_year)
    return {
        "label": label,
        "n_periods": len(ret),
        "total_ret": float(eq.iloc[-1] - 1),
        "ann_ret": float(ann_ret),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "max_dd": float(dd),
        "monthly_mean": float(monthly.mean()),
        "monthly_median": float(monthly.median()),
        "monthly_worst": float(monthly.min()),
        "monthly_best": float(monthly.max()),
        "months_pos": f"{int((monthly > 0).sum())}/{len(monthly)}",
        "avg_gross": float(out["gross"].mean()),
        "ann_turnover": float(out["turnover"].mean() * per_year),
        "fund_share": float(out["fund_pnl"].sum() / (abs(out["price_pnl"].sum()) + abs(out["fund_pnl"].sum()) + 1e-12)),
        "monthly": monthly,
    }


def fmt(m):
    if "total_ret" not in m:
        return f"{m.get('label','')}: n={m.get('n')} (too few periods)"
    return (f"{m['label']:<40s} tot {m['total_ret']*100:+7.1f}%  ann {m['ann_ret']*100:+6.1f}%  "
            f"vol {m['ann_vol']*100:5.1f}%  shp {m['sharpe']:+5.2f}  dd {m['max_dd']*100:6.1f}%  "
            f"mo m/w {m['monthly_mean']*100:+5.2f}/{m['monthly_worst']*100:+6.2f}%  "
            f"pos {m['months_pos']:>5s}  gross {m['avg_gross']:.2f}  fundshare {m['fund_share']:+.2f}")


def load_strategy(path):
    spec = importlib.util.spec_from_file_location("strat", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    path = args[0]
    params = json.loads(args[1]) if len(args) > 1 else {}
    mod = load_strategy(path)
    p = dict(mod.DEFAULT_PARAMS)
    p.update(params)
    data = load_all()
    grid = build_grid(data)
    w = mod.target_weights(data, grid, p)
    out = run(w, data)
    windows = [("IS 2023-07..2025-06", IS_START, IS_END)]
    if "--holdout" in flags:  # open ONCE for finalists only
        windows.append(("HOLDOUT 2025-07..2026-06", HO_START, HO_END))
    for label, s, e in windows:
        seg = out[(out.index >= s) & (out.index <= e)]
        m = metrics(seg, label)
        print(fmt(m))
        if "--monthly" in flags and "monthly" in m:
            print((m["monthly"] * 100).round(2).to_string())


if __name__ == "__main__":
    main()
