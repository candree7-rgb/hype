"""Cross-sectional momentum backtest engine — DAILY data, WEEKLY rebalance.

Designed after the 2026-07-22 invalidation of all 1m-candle strategies: every
number here comes from daily closes with execution at the next daily open, so
no intrabar path assumption exists anywhere. Costs are charged on every unit
of traded notional (0.045% fee + 0.03% slippage per side by default).

Accounting model (per day, weights are fractions of current equity):
  - Non-rebalance day t:      r_p = sum(w * (close_t/close_{t-1} - 1))
  - Rebalance day t:          old weights earn the overnight gap
                              close_{t-1} -> open_t, then the NEW target
                              weights (decided on close_{t-1} data only) earn
                              open_t -> close_t, minus cost * |w_new - w_gap|.
  - Delisting: a held symbol with no more data is closed at its last close
    (cost charged), return 0 afterwards.

Universe is point-in-time: at each rebalance a symbol must (a) have a full
formation window of closes, (b) trade on the rebalance day, (c) clear a
trailing-30d median dollar-volume floor; then the top `max_universe` by that
volume are kept. Later-delisted symbols (e.g. MATIC) stay in while listed.

Strategy contract (strategies/xsmom_*.py):
    DEFAULT_PARAMS = {...}
    def score(closes, sig_date, params, aux) -> pd.Series
        closes: full close panel; MUST only use rows <= sig_date.
        aux: dict with 'vol30' (trailing 30d daily-return std panel), 'btc'
        (BTC close series). Return higher = stronger (buy candidates).
An automatic truncation check verifies no lookahead: scores recomputed from a
panel truncated at sig_date must match.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data_xsmom"

FEE_SIDE = 0.00045 + 0.0003          # 0.045% taker fee + 0.03% slippage
VOL_FLOOR = 10_000_000               # trailing 30d MEDIAN daily $ volume
MAX_UNIVERSE = 40                    # top-N by that volume, point-in-time
MIN_LISTED_DAYS = 40                 # must have traded this long

IS_START, IS_END = "2023-07-01", "2025-06-30"
HO_START, HO_END = "2025-07-01", "2026-06-30"


# ---------------------------------------------------------------- data
def load_panels():
    closes, opens, qvol = {}, {}, {}
    for f in sorted(DATA_DIR.glob("*.parquet")):
        sym = f.stem
        df = pd.read_parquet(f)
        df = df.set_index(
            pd.to_datetime(df["dt"]).dt.tz_localize(None).dt.normalize())
        closes[sym] = df["close"]
        opens[sym] = df["open"]
        qvol[sym] = df["quote_vol"]
    C = pd.DataFrame(closes).sort_index()
    O = pd.DataFrame(opens).sort_index()
    V = pd.DataFrame(qvol).sort_index()
    return C, O, V


def build_aux(C: pd.DataFrame):
    rets = C.pct_change(fill_method=None)
    return {
        "vol30": rets.rolling(30, min_periods=20).std(),
        "btc": C["BTCUSDT"],
        "rets": rets,
    }


# ---------------------------------------------------------------- universe
def eligible(C, V, sig_date, formation_days):
    """Point-in-time universe at signal date (a close)."""
    hist = C.loc[:sig_date]
    if len(hist) < formation_days + 1:
        return []
    window = hist.iloc[-(formation_days + 1):]
    has_hist = window.notna().all(axis=0)
    listed_long = hist.notna().sum(axis=0) >= max(MIN_LISTED_DAYS,
                                                  formation_days + 1)
    v30 = V.loc[:sig_date].iloc[-30:].median(axis=0)
    liquid = v30 >= VOL_FLOOR
    ok = has_hist & listed_long & liquid
    syms = v30[ok].sort_values(ascending=False).index[:MAX_UNIVERSE]
    return list(syms)


# ---------------------------------------------------------------- weights
def make_weights(scores: pd.Series, k: int, mode: str, weighting: str,
                 vol30_row: pd.Series) -> pd.Series:
    s = scores.dropna()
    if len(s) < 2 * k and mode == "mn":
        return pd.Series(dtype=float)
    if len(s) < k:
        return pd.Series(dtype=float)
    ranked = s.sort_values(ascending=False)
    longs = ranked.index[:k]
    shorts = ranked.index[-k:] if mode == "mn" else []

    def leg(syms, budget):
        if weighting == "iv":
            iv = 1.0 / vol30_row.reindex(syms).replace(0, np.nan)
            iv = iv.fillna(iv.mean() if iv.notna().any() else 1.0)
            return budget * iv / iv.sum()
        return pd.Series(budget / len(syms), index=syms)

    if mode == "mn":
        w = pd.concat([leg(longs, 0.5), -leg(shorts, 0.5)])
    else:                                   # long-only
        w = leg(longs, 1.0)
    return w


# ---------------------------------------------------------------- backtest
def run_backtest(C, O, V, aux, score_fn, params, k=5, mode="mn",
                 weighting="ew", formation_days=None, start=None, end=None,
                 lookahead_check=False):
    """Returns dict with daily return series, weights history, turnover."""
    dates = C.index
    # weekly rebalance on Mondays; signal = previous available close
    reb_mask = pd.Series(dates.dayofweek == 0, index=dates)
    if formation_days is None:
        formation_days = params.get("formation_days", 28)

    all_syms = C.columns
    w = pd.Series(0.0, index=all_syms)
    rows = []
    la_violations = 0
    checked = 0
    start_ts = pd.Timestamp(start) if start else dates[0]
    end_ts = pd.Timestamp(end) if end else dates[-1]

    C_v, O_v = C.values, O.values
    date_pos = {d: i for i, d in enumerate(dates)}

    for t in dates:
        if t < start_ts or t > end_ts:
            continue
        i = date_pos[t]
        if i == 0:
            continue
        c_prev = pd.Series(C_v[i - 1], index=all_syms)
        c_now = pd.Series(C_v[i], index=all_syms)
        o_now = pd.Series(O_v[i], index=all_syms)

        if reb_mask.loc[t]:
            sig_date = dates[i - 1]
            syms = eligible(C, V, sig_date, formation_days)
            if syms:
                scores = score_fn(C[syms], sig_date, params, aux)
                if lookahead_check and checked < 3 and len(scores.dropna()):
                    trunc = score_fn(C[syms].loc[:sig_date], sig_date,
                                     params, aux)
                    if not np.allclose(scores.dropna(),
                                       trunc.reindex(scores.dropna().index),
                                       equal_nan=True):
                        la_violations += 1
                    checked += 1
                vol_row = aux["vol30"].loc[:sig_date].iloc[-1]
                w_tgt_s = make_weights(scores, k, mode, weighting, vol_row)
            else:
                w_tgt_s = pd.Series(dtype=float)
            w_tgt = pd.Series(0.0, index=all_syms)
            w_tgt[w_tgt_s.index] = w_tgt_s.values
            # drop targets with no open price today (halted/delisted)
            w_tgt[o_now.isna()] = 0.0

            # old weights earn the overnight gap; dead symbols close flat
            gap = (o_now / c_prev - 1.0).where(o_now.notna() & c_prev.notna(),
                                               0.0)
            r_gap = float((w * gap).sum())
            w_gap = w * (1 + gap)
            if abs(1 + r_gap) > 1e-9:
                w_gap = w_gap / (1 + r_gap)
            turnover = float((w_tgt - w_gap).abs().sum())
            cost = FEE_SIDE * turnover
            intr = (c_now / o_now - 1.0).where(c_now.notna() & o_now.notna(),
                                               0.0)
            r_int = float((w_tgt * intr).sum())
            r_p = r_gap + r_int - cost
            w = w_tgt * (1 + intr)
            if abs(1 + r_int) > 1e-9:
                w = w / (1 + r_int)
            rows.append((t, r_p, turnover, cost, int((w != 0).sum())))
        else:
            r = (c_now / c_prev - 1.0)
            dead = w.ne(0) & c_now.isna()
            r = r.where(c_now.notna() & c_prev.notna(), 0.0)
            cost = FEE_SIDE * float(w[dead].abs().sum())  # forced close
            r_p = float((w * r).sum()) - cost
            w = w * (1 + r)
            w[dead] = 0.0
            if abs(1 + r_p) > 1e-9:
                w = w / (1 + r_p)
            rows.append((t, r_p, 0.0, cost, int((w != 0).sum())))

    res = pd.DataFrame(rows, columns=["dt", "ret", "turnover", "cost",
                                      "n_pos"]).set_index("dt")
    res.attrs["lookahead_violations"] = la_violations
    return res


# ---------------------------------------------------------------- metrics
def metrics(res: pd.DataFrame, btc_ret: pd.Series, label=""):
    r = res["ret"]
    if len(r) < 30:
        return {"label": label, "n_days": len(r)}
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    monthly = (1 + r).groupby([r.index.year, r.index.month]).prod() - 1
    b = btc_ret.reindex(r.index)
    corr = float(r.corr(b))
    ann = float(r.mean() / (r.std() + 1e-12) * np.sqrt(365))
    return {
        "label": label,
        "n_days": len(r),
        "total_ret": float(eq.iloc[-1] - 1),
        "cagr": float(eq.iloc[-1] ** (365 / len(r)) - 1),
        "sharpe": ann,
        "maxdd": float(dd),
        "avg_month": float(monthly.mean()),
        "worst_month": float(monthly.min()),
        "best_month": float(monthly.max()),
        "months_pos": f"{int((monthly > 0).sum())}/{len(monthly)}",
        "btc_corr": corr,
        "avg_weekly_turnover": float(
            res.loc[res["turnover"] > 0, "turnover"].mean()),
        "ann_cost": float(res["cost"].sum() * 365 / len(r)),
        "monthly": {f"{y}-{m:02d}": round(v, 4)
                    for (y, m), v in monthly.items()},
    }


def load_strategy(path: str):
    p = Path(path)
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    strat_path = sys.argv[1]
    overrides = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    mod = load_strategy(strat_path)
    params = {**mod.DEFAULT_PARAMS, **overrides}
    C, O, V = load_panels()
    aux = build_aux(C)
    btc_ret = aux["rets"]["BTCUSDT"]
    res = run_backtest(C, O, V, aux, mod.score, params,
                       k=params.get("k", 5), mode=params.get("mode", "mn"),
                       weighting=params.get("weighting", "ew"),
                       start=IS_START, end=IS_END, lookahead_check=True)
    m = metrics(res, btc_ret, label=f"{Path(strat_path).stem} IS")
    m["lookahead_violations"] = res.attrs["lookahead_violations"]
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
