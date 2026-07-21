"""Backtest engine for LIGHTER (zk perp DEX) fee model.

Fee model:
- MAKER_FEE = 0, TAKER_FEE = 0 (standard account, 0/0).
- Lighter applies a 200-300ms artificial latency to TAKER orders only
  (makers and cancels are 0ms). Entries and TPs are resting maker limits
  (unaffected). The SL stop-market IS a taker, so the latency manifests as
  extra adverse slippage on stop fills, worst during cascades. We model it as
  a parameterized SL slippage regime:
      0.05% (mild) / 0.10% (base, 300ms inside a cascade) / 0.20% (hostile).
- Time-stop closes are taker too, but occur in calm tape (240 min after
  entry, no trigger chase), so they get half the regime slippage, same shape
  as the original engine.

Optional `sl_mode="stoplimit"`: replaces the stop-market with a stop-LIMIT —
the stop trigger places a LIMIT at the SL price. On Lighter, placing that
limit is a maker action (0ms, 0 fee); once price is beyond SL the limit rests
passively and fills only when price trades back through the level. (A plain
resting limit at SL would cross the book immediately — the stop trigger is
required; we assume trigger monitoring itself adds no cost since the placed
order is passive.) Approximation on 1m candles, careful about intrabar
ambiguity:
  - On the trigger candle j itself, a recovery fill is claimed ONLY if the
    candle CLOSES back through SL (low<=SL then close>SL for longs proves a
    genuine up-through-SL pass after the touch). The candle's high alone
    proves nothing (may predate the SL touch).
  - Otherwise the fill must come from a later candle k in (j, j+sl_grace]
    whose extreme trades strictly through SL by at least `sl_limit_eps`
    (eps=0 default; set >0 to stress queue/latency on the recovery fill).
  - No recovery within the grace window -> taker bail at the grace-window
    close with FULL regime slippage (this is the tail: cascades that keep
    going). Pessimistic: bail at close, not best price in window.

Everything else (fill model, ambiguous-candle-counts-as-loss, lookahead
check) is identical to engine.py, which is left untouched.
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data_binance"

TAKER_FEE = 0.0     # Lighter standard account
MAKER_FEE = 0.0
SL_SLIPPAGE_BASE = 0.0010   # 0.10% base regime for taker-latency during cascades

REGIMES = {"mild": 0.0005, "base": 0.0010, "hostile": 0.0020}


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
    outcome: str         # "tp" | "sl" | "ambiguous_sl" | "sl_limit" | "sl_bail" | "time"
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
             max_hold: int = 240, sl_slippage: float = SL_SLIPPAGE_BASE,
             sl_mode: str = "market", sl_grace: int = 10,
             sl_limit_eps: float = 0.0) -> Result:
    """Simulate signals on one symbol under the Lighter cost model.

    signals columns: idx (int, signal candle), side ("long"/"short"),
    limit_price (float), tp_dist, sl_dist (fractions), ttl (int candles).

    sl_mode:
      "market"    — stop-market with `sl_slippage` adverse fill (taker latency).
      "stoplimit" — resting limit at SL price; fills at SL with 0 slippage only
                    if price trades back through the level within `sl_grace`
                    candles, else taker bail at grace-window close with full
                    `sl_slippage`.
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
                break
            if hit_sl:
                outcome, exit_idx = "sl", j
                break
            if hit_tp:
                outcome, exit_idx = "tp", j
                exit_price = tp
                break

        if outcome in ("sl", "ambiguous_sl"):
            if sl_mode == "stoplimit":
                # limit placed at SL on trigger: fills only on genuine
                # trade-back-through. Trigger candle counts only via its close
                # (close beyond-recovery proves the pass happened after the
                # touch); later candles via strict extreme trade-through.
                fill_idx = -1
                thresh = sl * (1 + sl_limit_eps) if long else sl * (1 - sl_limit_eps)
                if (long and close[exit_idx] > thresh) or \
                   (not long and close[exit_idx] < thresh):
                    fill_idx = exit_idx
                else:
                    for k in range(exit_idx + 1, min(exit_idx + 1 + sl_grace, n)):
                        if (long and high[k] > thresh) or (not long and low[k] < thresh):
                            fill_idx = k
                            break
                if fill_idx >= 0:
                    outcome = "sl_limit" if outcome == "sl" else "ambiguous_sl"
                    exit_idx = fill_idx
                    exit_price = sl
                else:
                    outcome = "sl_bail"
                    exit_idx = min(exit_idx + sl_grace, n - 1)
                    c = close[exit_idx]
                    exit_price = c * (1 - sl_slippage) if long else c * (1 + sl_slippage)
            else:
                exit_price = sl * (1 - sl_slippage) if long else sl * (1 + sl_slippage)

        if outcome is None:
            exit_idx = min(entry_idx + max_hold, n) - 1
            outcome = "time"
            c = close[exit_idx]
            exit_price = c * (1 - sl_slippage / 2) if long else c * (1 + sl_slippage / 2)

        gross = (exit_price - p) / p if long else (p - exit_price) / p
        entry_fee = MAKER_FEE
        exit_fee = MAKER_FEE if outcome in ("tp", "sl_limit") else TAKER_FEE
        net = gross - entry_fee - exit_fee
        res.trades.append(Trade(
            symbol=symbol, side=sig.side, signal_idx=i, entry_idx=entry_idx,
            exit_idx=exit_idx, entry=p, exit=float(exit_price),
            tp_dist=float(sig.tp_dist), sl_dist=float(sig.sl_dist),
            outcome=outcome, r=float(net / risk), net_frac=float(net),
        ))
    return res


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0}
    wins = (t["r"] > 0).sum()
    gp = t.loc[t["r"] > 0, "r"].sum()
    gl = -t.loc[t["r"] <= 0, "r"].sum()
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
        "outcomes": t["outcome"].value_counts().to_dict(),
    }


def load_strategy(path: str):
    spec = importlib.util.spec_from_file_location("strategy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def verify_no_lookahead(strategy, df: pd.DataFrame, params: dict,
                        sample: int = 8, seed: int = 7) -> list[str]:
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
        max_hold: int = 240, sl_slippage: float = SL_SLIPPAGE_BASE,
        sl_mode: str = "market", sl_grace: int = 10, sl_limit_eps: float = 0.0,
        check_lookahead: bool = True) -> tuple[pd.DataFrame, list[str]]:
    """Returns (trades_df with entry_time/month columns, lookahead_violations)."""
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
        res = simulate(df, signals, sym, max_hold=max_hold, sl_slippage=sl_slippage,
                       sl_mode=sl_mode, sl_grace=sl_grace, sl_limit_eps=sl_limit_eps)
        if res.trades:
            t = res.df()
            t["entry_time"] = df["dt"].iloc[t["entry_idx"]].values
            all_trades.append(t)
        if check_lookahead and sym == symbols[0]:
            lookahead = verify_no_lookahead(strategy, df, params)
    trades = pd.concat(all_trades).sort_values("entry_time").reset_index(drop=True) \
        if all_trades else pd.DataFrame()
    if not trades.empty:
        trades["month"] = trades["entry_time"].dt.to_period("M").astype(str)
    return trades, lookahead


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy")
    ap.add_argument("--params", default=None)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--max-hold", type=int, default=240)
    ap.add_argument("--slip", type=float, default=SL_SLIPPAGE_BASE)
    ap.add_argument("--sl-mode", default="market")
    args = ap.parse_args()
    params = json.loads(args.params) if args.params else None
    symbols = args.symbols.split(",") if args.symbols else None
    t, la = run(args.strategy, params, symbols, args.max_hold, args.slip, args.sl_mode)
    out = {"all": stats(t), "lookahead_violations": la,
           "per_month": {m: stats(g) for m, g in t.groupby("month")}}
    print(json.dumps(out, indent=2, default=str))
