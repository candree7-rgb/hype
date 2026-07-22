"""Capacity-research engine wrapper — HL fees, deep-book coins, date-window splits.

Wraps engine_hl (unchanged) with:
  - DATA_DIR pointed at data_binance (12mo Binance 1m, Jul-2025..Jun-2026)
  - date-window runs: IS = Jul-2025..Jan-2026, HOLDOUT = Feb-2026..Jun-2026
    (windows are cut by slicing the candle df to rows < window end, so a
    window run can never see future data; trades are then filtered to
    entries inside the window)
  - monthly breakdown, trades/day, and FILL-MINUTE $volume ratio capture:
    for every simulated entry we record the Binance quote-$ volume of the
    fill minute relative to the symbol's median minute. The median of that
    ratio, applied to the coin's HL typical-minute (floor) $vol from
    hl_capacity, is the per-strategy capacity basis.

Used by strategies/cap_* research. Does not modify engine.py / engine_hl.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import engine_hl as eh

BASE = Path(__file__).parent
eh.DATA_DIR = BASE / "data_binance"

IS_START, IS_END = "2025-07-01", "2026-02-01"      # 7 months (tune here only)
HOLD_START, HOLD_END = "2026-02-01", "2026-07-01"  # 5 months (open once)

DEEP_COINS = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT",
              "BNB_USDT", "DOGE_USDT"]

# HL typical-minute (floor) median $vol, venue-native ~3.5d snapshot
# (from hl_capacity.py run 2026-07-22; shock-minute vols are much larger).
HL_FLOOR_VOL = {
    "BTC_USDT": 397_156, "ETH_USDT": 119_306, "SOL_USDT": 22_952,
    "XRP_USDT": 2_186, "HYPE_USDT": 71_111, "BNB_USDT": 152, "DOGE_USDT": 458,
}
HL_MAX_LEV = {"BTC_USDT": 40, "ETH_USDT": 25, "SOL_USDT": 20, "XRP_USDT": 20,
              "HYPE_USDT": 10, "BNB_USDT": 10, "DOGE_USDT": 10}

_df_cache: dict[str, pd.DataFrame] = {}


def load(sym: str) -> pd.DataFrame:
    if sym not in _df_cache:
        df = pd.read_parquet(eh.DATA_DIR / f"{sym}_Min1.parquet").reset_index(drop=True)
        df["dt"] = pd.to_datetime(df["dt"], utc=True)
        _df_cache[sym] = df
    return _df_cache[sym]


def run_window(strategy_path: str, params: dict | None = None,
               symbols: list[str] | None = None,
               start: str = IS_START, end: str = IS_END,
               max_hold: int = 240, check_lookahead: bool = False) -> pd.DataFrame:
    """Run one strategy over a date window. Data is truncated at `end` (no
    future rows exist during the run); trades filtered to entry dt >= start.
    Returns pooled trades df with dt / month / fill-minute vol ratio."""
    strat = eh.load_strategy(strategy_path)
    params = {**getattr(strat, "DEFAULT_PARAMS", {}), **(params or {})}
    symbols = symbols or DEEP_COINS
    out = []
    end_ts = pd.Timestamp(end, tz="UTC")
    start_ts = pd.Timestamp(start, tz="UTC")
    for sym in symbols:
        df = load(sym)
        dfw = df[df["dt"] < end_ts].reset_index(drop=True)
        sig = strat.generate_signals(dfw, params)
        if sig.empty:
            continue
        res = eh.simulate(dfw, sig, sym, max_hold=max_hold)
        if not res.trades:
            continue
        t = res.df()
        t["dt"] = dfw["dt"].to_numpy()[t["entry_idx"].to_numpy()]
        t = t[t["dt"] >= start_ts].copy()
        if t.empty:
            continue
        t["month"] = t["dt"].dt.strftime("%Y-%m")
        # fill-minute Binance $vol ratio vs symbol median minute
        dvol = dfw["amount"].to_numpy(float)
        if not np.isfinite(np.nanmedian(dvol)) or np.nanmedian(dvol) <= 0:
            dvol = dfw["vol"].to_numpy(float) * dfw["close"].to_numpy(float)
        med = float(np.nanmedian(dvol))
        t["fill_vol_ratio"] = dvol[t["entry_idx"].to_numpy()] / med
        out.append(t)
        if check_lookahead and sym == symbols[0]:
            v = eh.verify_no_lookahead(strat, dfw, params)
            if v:
                print(f"LOOKAHEAD {strategy_path} {sym}: {v}")
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def summarize(t: pd.DataFrame, days: float, label: str = "") -> dict:
    if t.empty:
        return {"label": label, "n": 0}
    wins = (t["r"] > 0).sum()
    gp = t.loc[t["r"] > 0, "r"].sum()
    gl = -t.loc[t["r"] <= 0, "r"].sum()
    aw = t.loc[t["r"] > 0, "r"].mean() if wins else 0.0
    al = -t.loc[t["r"] <= 0, "r"].mean() if wins < len(t) else 0.0
    eq = t.sort_values("dt")["r"].cumsum()
    dd = float((eq.cummax() - eq).max())
    monthly = t.groupby("month")["r"].sum()
    return {
        "label": label, "n": int(len(t)), "trades_day": round(len(t) / days, 2),
        "wr": round(float(wins / len(t)), 4),
        "breakeven_wr": round(float(al / (aw + al)), 4) if (aw + al) > 0 else None,
        "avg_r": round(float(t["r"].mean()), 4),
        "total_r": round(float(t["r"].sum()), 1),
        "r_per_month": round(float(t["r"].sum()) / (days / 30.44), 1),
        "pf": round(float(gp / gl), 3) if gl > 0 else float("inf"),
        "max_dd_r": round(dd, 1),
        "months_pos": f"{int((monthly > 0).sum())}/{len(monthly)}",
        "monthly_r": {k: round(v, 1) for k, v in monthly.items()},
        "med_sl": round(float(t["sl_dist"].median()), 5),
        "med_fill_volx": round(float(t["fill_vol_ratio"].median()), 1),
        "per_symbol_r": {s: round(float(x["r"].sum()), 1)
                         for s, x in t.groupby("symbol")},
    }


def capacity_table(t: pd.DataFrame, equities=(10_000, 25_000),
                   cap_frac: float = 0.10, max_lev_frac: float = 0.5) -> dict:
    """Per-coin: HL fill-minute capacity = floor_vol * median fill ratio.
    Max risk% at equity E for coin c: r <= cap_frac*fillvol*sl_med / E,
    also r <= max_lev_frac*maxlev*sl_med (margin bound). Returns per-coin
    max risk%, and portfolio math left to the report."""
    out = {}
    for sym, x in t.groupby("symbol"):
        ratio = float(x["fill_vol_ratio"].median())
        slm = float(x["sl_dist"].median())
        fillvol = HL_FLOOR_VOL.get(sym, 0) * ratio
        row = {"fill_volx": round(ratio, 1), "hl_fill_vol": round(fillvol),
               "med_sl": round(slm, 5), "max_order": round(cap_frac * fillvol),
               "share_r": round(float(x["r"].sum()), 1), "n": len(x)}
        for E in equities:
            r_cap = cap_frac * fillvol * slm / E
            r_marg = max_lev_frac * HL_MAX_LEV.get(sym, 10) * slm
            row[f"maxrisk@{E//1000}k"] = round(min(r_cap, r_marg) * 100, 3)
        out[sym] = row
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy")
    ap.add_argument("--params", default=None)
    ap.add_argument("--window", default="is", choices=["is", "holdout", "all"])
    ap.add_argument("--symbols", default=None)
    args = ap.parse_args()
    params = json.loads(args.params) if args.params else None
    syms = args.symbols.split(",") if args.symbols else None
    w = {"is": (IS_START, IS_END), "holdout": (HOLD_START, HOLD_END),
         "all": (IS_START, HOLD_END)}[args.window]
    days = (pd.Timestamp(w[1]) - pd.Timestamp(w[0])).days
    t = run_window(args.strategy, params, syms, *w, check_lookahead=True)
    print(json.dumps(summarize(t, days, args.strategy), indent=2))
    print(json.dumps(capacity_table(t), indent=2))
