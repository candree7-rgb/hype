"""Backtest engine variant — HYPERLIQUID fee model.

Copy of engine.py with the cost constants changed to Hyperliquid base tier:
maker 0.015%, taker 0.045%, slippage kept at 0.03%. Entry limit and TP limit
pay MAKER_FEE; SL stop-market pays TAKER_FEE + SLIPPAGE. Everything else is
identical to engine.py (do not modify engine.py — shared with other agents).

`set_costs()` lets stress runs override fees/slippage (HYPE staking fee
discounts, 1.5x slippage) without editing the module.
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

TAKER_FEE = 0.00045  # 0.045% Hyperliquid base tier
MAKER_FEE = 0.00015  # 0.015% Hyperliquid base tier
SLIPPAGE = 0.0003    # 0.03% assumed adverse slippage on stop-market fills


def set_costs(maker: float = 0.00015, taker: float = 0.00045,
              slippage: float = 0.0003) -> None:
    """Override cost model globals (for fee-discount / slippage stress runs)."""
    global MAKER_FEE, TAKER_FEE, SLIPPAGE
    MAKER_FEE = maker
    TAKER_FEE = taker
    SLIPPAGE = slippage


@dataclass
class Trade:
    symbol: str
    side: str            # "long" | "short"
    signal_idx: int
    entry_idx: int
    exit_idx: int
    entry: float
    exit: float
    tp_dist: float
    sl_dist: float
    outcome: str         # "tp" | "sl" | "ambiguous_sl" | "time"
    r: float             # net result in risk units
    net_frac: float      # net result as fraction of entry price


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)

    def df(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])


def load_symbol(symbol: str, interval: str = "Min1") -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / f"{symbol}_{interval}.parquet")
    return df.reset_index(drop=True)


def list_symbols(interval: str = "Min1") -> list[str]:
    return sorted(p.stem.replace(f"_{interval}", "") for p in DATA_DIR.glob(f"*_{interval}.parquet"))


def simulate(df: pd.DataFrame, signals: pd.DataFrame, symbol: str,
             max_hold: int = 240) -> Result:
    """Simulate signals on one symbol.

    signals columns: idx (int, signal candle), side ("long"/"short"),
    limit_price (float), tp_dist, sl_dist (fractions), ttl (int candles).
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    res = Result()

    for sig in signals.itertuples(index=False):
        i = int(sig.idx)
        p = float(sig.limit_price)
        ttl = int(sig.ttl)
        long = sig.side == "long"
        # a maker limit must rest on the passive side of the book
        if long and not (p < close[i]):
            continue
        if not long and not (p > close[i]):
            continue

        # find fill
        entry_idx = -1
        for j in range(i + 1, min(i + 1 + ttl, n)):
            if (long and low[j] < p) or (not long and high[j] > p):
                entry_idx = j
                break
        if entry_idx < 0:
            continue

        tp = p * (1 + sig.tp_dist) if long else p * (1 - sig.tp_dist)
        sl = p * (1 - sig.sl_dist) if long else p * (1 + sig.sl_dist)
        risk = sig.sl_dist

        outcome, exit_price, exit_idx = None, None, None
        for j in range(entry_idx, min(entry_idx + max_hold, n)):
            hit_tp = high[j] > tp if long else low[j] < tp
            hit_sl = low[j] <= sl if long else high[j] >= sl
            if hit_tp and hit_sl:
                outcome, exit_idx = "ambiguous_sl", j
                exit_price = sl * (1 - SLIPPAGE) if long else sl * (1 + SLIPPAGE)
                break
            if hit_sl:
                outcome, exit_idx = "sl", j
                exit_price = sl * (1 - SLIPPAGE) if long else sl * (1 + SLIPPAGE)
                break
            if hit_tp:
                outcome, exit_idx = "tp", j
                exit_price = tp
                break
        if outcome is None:
            exit_idx = min(entry_idx + max_hold, n) - 1
            outcome = "time"
            c = close[exit_idx]
            exit_price = c * (1 - SLIPPAGE / 2) if long else c * (1 + SLIPPAGE / 2)

        gross = (exit_price - p) / p if long else (p - exit_price) / p
        entry_fee = MAKER_FEE
        exit_fee = MAKER_FEE if outcome == "tp" else TAKER_FEE
        net = gross - entry_fee - exit_fee
        res.trades.append(Trade(
            symbol=symbol, side=sig.side, signal_idx=i, entry_idx=entry_idx,
            exit_idx=exit_idx, entry=p, exit=float(exit_price),
            tp_dist=float(sig.tp_dist), sl_dist=float(sig.sl_dist),
            outcome=outcome, r=float(net / risk), net_frac=float(net),
        ))
    return res


def metrics(trades: pd.DataFrame, split_frac: float = 0.65) -> dict:
    """Metrics with in-sample / out-of-sample split by time (per symbol)."""
    if trades.empty:
        return {"n": 0}

    def _stats(t: pd.DataFrame) -> dict:
        if t.empty:
            return {"n": 0}
        wins = (t["r"] > 0).sum()
        gp = t.loc[t["r"] > 0, "r"].sum()
        gl = -t.loc[t["r"] <= 0, "r"].sum()
        eq = t.sort_values(["entry_idx"])["r"].cumsum()
        dd = float((eq.cummax() - eq).max())
        aw = float(t.loc[t["r"] > 0, "r"].mean()) if wins else 0.0
        al = float(-t.loc[t["r"] <= 0, "r"].mean()) if wins < len(t) else 0.0
        be_wr = al / (aw + al) if (aw + al) > 0 else None
        return {
            "n": int(len(t)),
            "wr": round(float(wins / len(t)), 4),
            "avg_win_r": round(aw, 3),
            "avg_loss_r": round(al, 3),
            "breakeven_wr": round(be_wr, 4) if be_wr is not None else None,
            "avg_r": round(float(t["r"].mean()), 4),
            "total_r": round(float(t["r"].sum()), 2),
            "profit_factor": round(float(gp / gl), 3) if gl > 0 else float("inf"),
            "max_dd_r": round(dd, 2),
            "outcomes": t["outcome"].value_counts().to_dict(),
        }

    out = {"all": _stats(trades)}
    is_parts, oos_parts = [], []
    for sym, t in trades.groupby("symbol"):
        cut = t["entry_idx"].quantile(split_frac)
        is_parts.append(t[t["entry_idx"] <= cut])
        oos_parts.append(t[t["entry_idx"] > cut])
    out["in_sample"] = _stats(pd.concat(is_parts)) if is_parts else {"n": 0}
    out["out_of_sample"] = _stats(pd.concat(oos_parts)) if oos_parts else {"n": 0}
    out["per_symbol"] = {s: _stats(t) for s, t in trades.groupby("symbol")}
    return out


def load_strategy(path: str):
    spec = importlib.util.spec_from_file_location("strategy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def verify_no_lookahead(strategy, df: pd.DataFrame, params: dict,
                        sample: int = 8, seed: int = 7) -> list[str]:
    """Recompute signals on truncated data; a signal at idx i must be identical
    when the strategy only sees rows 0..i. Returns list of violation messages."""
    full = strategy.generate_signals(df, params)
    if full.empty:
        return []
    rng = np.random.default_rng(seed)
    picks = rng.choice(len(full), size=min(sample, len(full)), replace=False)
    violations = []
    for k in picks:
        row = full.iloc[int(k)]
        i = int(row["idx"])
        trunc = strategy.generate_signals(df.iloc[: i + 1].reset_index(drop=True), params)
        match = trunc[(trunc["idx"] == i) & (trunc["side"] == row["side"])]
        if match.empty:
            violations.append(f"signal at idx {i} disappears on truncated data (lookahead!)")
        else:
            m = match.iloc[0]
            if abs(m["limit_price"] - row["limit_price"]) > 1e-9 * row["limit_price"]:
                violations.append(f"signal at idx {i} changes limit_price on truncated data")
    return violations


def run(strategy_path: str, params: dict | None = None, symbols: list[str] | None = None,
        max_hold: int = 240, check_lookahead: bool = True) -> dict:
    strategy = load_strategy(strategy_path)
    params = params or getattr(strategy, "DEFAULT_PARAMS", {})
    symbols = symbols or list_symbols()
    all_trades = []
    lookahead = []
    for sym in symbols:
        df = load_symbol(sym)
        signals = strategy.generate_signals(df, params)
        if signals.empty:
            continue
        res = simulate(df, signals, sym, max_hold=max_hold)
        if res.trades:
            all_trades.append(res.df())
        if check_lookahead and sym == symbols[0]:
            lookahead = verify_no_lookahead(strategy, df, params)
    trades = pd.concat(all_trades) if all_trades else pd.DataFrame()
    out = metrics(trades)
    out["params"] = params
    out["lookahead_violations"] = lookahead
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy")
    ap.add_argument("--params", default=None, help="JSON string of params")
    ap.add_argument("--symbols", default=None, help="comma-separated")
    ap.add_argument("--max-hold", type=int, default=240)
    args = ap.parse_args()
    params = json.loads(args.params) if args.params else None
    symbols = args.symbols.split(",") if args.symbols else None
    print(json.dumps(run(args.strategy, params, symbols, args.max_hold), indent=2, default=str))
