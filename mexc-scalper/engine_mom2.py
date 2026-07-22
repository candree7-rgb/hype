"""ADDITIVE engine for mom2_* momentum-sleeve experiments.

New file — engine_mom.py is untouched and remains the reference for the frozen
baseline. This engine extends the simulation with exit features the baseline
sweep needs, under STRICTER-than-engine_mom artifact rules:

- PARTIAL TP ladder: `ptp_dists` / `ptp_fracs` (JSON lists on the signal row)
  are reduce-only MAKER limits. They may fill ONLY on bars STRICTLY AFTER the
  entry bar (the entry-candle TP artifact killed the fade family; here we do
  not even allow close-confirmed entry-candle partials — the order is placed
  mid-candle after the fill, so next-bar-earliest is the honest model).
  Fill = strict trade-through (high > level for longs).
- BREAKEVEN move: once the favorable extreme UP TO THE PREVIOUS BAR exceeds
  entry*(1+be_arm), the stop rises to entry*(1+be_offset) (long; mirrored
  short). Uses prior-bar peak only — no same-bar peak-then-stop optimism.
- TRAILING stop: same semantics as engine_mom (peak up to prior bar), but
  trail_dist is per-signal so strategies can set it from ATR at signal time.
- Same-bar stop + TP reachable -> STOP taken on the whole remaining position
  (conservative; no partial credit on an ambiguous bar).
- One position per symbol (busy_until), R = total net frac / initial sl_dist.

Fee model identical to engine_mom (Hyperliquid base tier).
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from engine_mom import (DATA_DIR, HOLDOUT_START, MAKER_FEE, SLIPPAGE,
                        TAKER_FEE, attach_times, list_symbols, load_symbol,
                        load_strategy, stats)


@dataclass
class Trade:
    symbol: str
    side: str
    signal_idx: int
    entry_idx: int
    exit_idx: int
    entry: float
    exit: float          # final-leg exit price
    sl_dist: float
    outcome: str         # outcome of the FINAL leg
    r: float             # whole-trade R (all legs)
    net_frac: float      # position-weighted net fraction
    n_partials: int = 0


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)

    def df(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])


def _getf(sig, name, default=np.nan):
    v = getattr(sig, name, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _getlist(sig, name):
    v = getattr(sig, name, None)
    if v is None:
        return []
    if isinstance(v, str):
        v = json.loads(v)
    if isinstance(v, (list, tuple, np.ndarray)):
        return [float(x) for x in v]
    return []


def simulate(df: pd.DataFrame, signals: pd.DataFrame, symbol: str) -> Result:
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    n = len(df)
    res = Result()

    busy_until = -1
    for sig in signals.sort_values("idx").itertuples(index=False):
        i = int(sig.idx)
        if i <= busy_until:
            continue
        long = sig.side == "long"
        d = 1.0 if long else -1.0
        entry_type = getattr(sig, "entry_type", "taker")
        sl_dist = float(sig.sl_dist)
        trail_dist = _getf(sig, "trail_dist")
        trail_arm = _getf(sig, "trail_arm", 0.0)
        if not np.isfinite(trail_arm):
            trail_arm = 0.0
        be_arm = _getf(sig, "be_arm")
        be_offset = _getf(sig, "be_offset", 0.0)
        if not np.isfinite(be_offset):
            be_offset = 0.0
        max_hold = int(_getf(sig, "max_hold", 240.0))
        ptp_dists = _getlist(sig, "ptp_dists")
        ptp_fracs = _getlist(sig, "ptp_fracs")
        assert len(ptp_dists) == len(ptp_fracs)
        assert sum(ptp_fracs) < 1.0 + 1e-9

        # ---- entry (identical to engine_mom)
        if entry_type == "taker":
            if i + 1 >= n:
                continue
            entry_idx = i + 1
            raw = open_[entry_idx]
            entry = raw * (1 + SLIPPAGE) if long else raw * (1 - SLIPPAGE)
            entry_fee = TAKER_FEE
        else:
            p = float(sig.limit_price)
            ttl = int(_getf(sig, "ttl", 5.0))
            if long and not (p < close[i]):
                continue
            if not long and not (p > close[i]):
                continue
            entry_idx = -1
            for j in range(i + 1, min(i + 1 + ttl, n)):
                if (long and low[j] < p) or (not long and high[j] > p):
                    entry_idx = j
                    break
            if entry_idx < 0:
                continue
            entry = p
            entry_fee = MAKER_FEE

        sl = entry * (1 - d * sl_dist)
        ptps = [entry * (1 + d * dist) for dist in ptp_dists]
        ptp_filled = [False] * len(ptps)
        remaining = 1.0
        legs_net = 0.0  # sum over legs of frac_size * (gross - fees)

        peak = entry
        outcome, exit_price, exit_idx = None, None, None
        end = min(entry_idx + max_hold, n)
        for j in range(entry_idx, end):
            stop_level = sl
            fav = d * (peak / entry - 1.0)  # favorable move up to prev bar
            if np.isfinite(be_arm) and fav >= be_arm:
                be_lvl = entry * (1 + d * be_offset)
                stop_level = max(stop_level, be_lvl) if long else min(stop_level, be_lvl)
            if np.isfinite(trail_dist) and fav >= trail_arm:
                t_lvl = peak * (1 - d * trail_dist)
                stop_level = max(stop_level, t_lvl) if long else min(stop_level, t_lvl)

            hit_stop = low[j] <= stop_level if long else high[j] >= stop_level
            if hit_stop:
                # conservative: whole remaining position stops; no same-bar
                # partial credit even if a TP level was also reachable
                px = stop_level * (1 - d * SLIPPAGE)
                gross = d * (px - entry) / entry
                legs_net += remaining * (gross - entry_fee - TAKER_FEE)
                outcome = "trail" if stop_level != sl and not (
                    np.isfinite(be_arm) and abs(stop_level - entry * (1 + d * be_offset)) < 1e-12 and fav >= be_arm and not np.isfinite(trail_dist)
                ) else "sl"
                # simpler labeling: stop above/at entry -> protected
                if d * (stop_level - entry) >= 0:
                    outcome = "be_or_trail"
                exit_idx, exit_price = j, px
                remaining = 0.0
                break

            # partial TPs: STRICTLY after the entry bar, strict trade-through
            if j > entry_idx:
                for k, lvl in enumerate(ptps):
                    if ptp_filled[k] or remaining <= 0:
                        continue
                    hit = high[j] > lvl if long else low[j] < lvl
                    if hit:
                        frac = min(ptp_fracs[k], remaining)
                        gross = d * (lvl - entry) / entry
                        legs_net += frac * (gross - entry_fee - MAKER_FEE)
                        remaining -= frac
                        ptp_filled[k] = True
                if remaining <= 1e-9:
                    outcome, exit_idx, exit_price = "tp_full", j, ptps[-1]
                    break

            peak = max(peak, high[j]) if long else min(peak, low[j])

        if outcome is None:
            exit_idx = end - 1
            c = close[exit_idx]
            px = c * (1 - d * SLIPPAGE / 2)
            gross = d * (px - entry) / entry
            legs_net += remaining * (gross - entry_fee - TAKER_FEE)
            outcome, exit_price = "time", px
            remaining = 0.0

        res.trades.append(Trade(
            symbol=symbol, side=sig.side, signal_idx=i, entry_idx=entry_idx,
            exit_idx=int(exit_idx), entry=float(entry), exit=float(exit_price),
            sl_dist=float(sl_dist), outcome=outcome,
            r=float(legs_net / sl_dist), net_frac=float(legs_net),
            n_partials=int(sum(ptp_filled)),
        ))
        busy_until = exit_idx
    return res


def run(strategy_path: str, params: dict | None = None,
        end_time: pd.Timestamp | None = None) -> pd.DataFrame:
    strategy = load_strategy(strategy_path)
    params = params or getattr(strategy, "DEFAULT_PARAMS", {})

    def _trunc(df):
        if end_time is None:
            return df
        dt = df["dt"].dt.tz_localize(None)
        return df[dt < end_time].reset_index(drop=True)

    all_t = []
    universe = getattr(strategy, "UNIVERSE", list_symbols())
    dfs = {s: _trunc(load_symbol(s)) for s in universe
           if (DATA_DIR / f"{s}_Min1.parquet").exists()}
    sigmap = strategy.generate_signals_multi(dfs, params)
    for sym, sig in sigmap.items():
        if sig is None or sig.empty:
            continue
        res = simulate(dfs[sym], sig, sym)
        if res.trades:
            all_t.append(attach_times(res.df(), dfs[sym]))
    if not all_t:
        return pd.DataFrame()
    return pd.concat(all_t).sort_values("entry_time").reset_index(drop=True)


def monthly(t: pd.DataFrame) -> pd.DataFrame:
    t = t.copy()
    t["month"] = pd.to_datetime(t["entry_time"]).dt.to_period("M")
    g = t.groupby("month")["r"].agg(["count", "sum", "mean"])
    return g.round(3)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy")
    ap.add_argument("--params", default=None)
    ap.add_argument("--is-only", action="store_true")
    args = ap.parse_args()
    params = json.loads(args.params) if args.params else None
    t = run(args.strategy, params,
            end_time=HOLDOUT_START if args.is_only else None)
    print(json.dumps({"all": stats(t)}, indent=2, default=str))
    print(monthly(t))
