"""Backtest engine for MOMENTUM strategies — Hyperliquid fee model.

New file (does not modify engine.py / engine_hl.py). Differences vs engine_hl:

- DATA_DIR points at data_binance (12mo of 1m candles).
- Entries may be TAKER (market at next bar open + slippage + taker fee) or
  MAKER (resting limit, strict trade-through, maker fee) — momentum entries
  are stop/market-shaped, so the taker path must be modeled honestly.
- Exits support a TRAILING stop (fraction from the best favorable extreme,
  optionally armed only after +arm move), a hard SL, an optional maker TP,
  and a time stop. Trailing level at bar j uses extremes up to bar j-1 only
  (no same-bar peak-then-exit optimism). Stop exits pay taker + slippage.
- One position per symbol at a time (long holds => overlap must be blocked,
  a cooldown alone cannot guarantee it).
- If stop and TP are both reachable in one candle -> counted as loss.
- R accounting: risk = initial sl_dist; r = net_frac / sl_dist.

Signal contract (DataFrame from the strategy):
  idx (int)         decision bar; execution starts at idx+1
  side              "long" | "short"
  entry_type        "taker" | "maker"
  limit_price       required for maker (must rest passively vs close[idx])
  ttl (int)         maker fill window in bars (ignored for taker)
  sl_dist (frac)    initial stop distance from entry
  tp_dist (frac)    optional take profit distance (np.nan = none)
  trail_dist (frac) optional trailing stop distance (np.nan = none)
  trail_arm (frac)  favorable move needed before the trail activates (0 = now)
  max_hold (int)    bars until time-stop

Strategies expose DEFAULT_PARAMS and either
  generate_signals(df, params)                       (single symbol) or
  generate_signals_multi(dfs: dict[str, df], params) -> dict[str, signals]
  (cross-symbol information, e.g. shock breadth; still causal per minute).
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data_binance"

TAKER_FEE = 0.00045  # Hyperliquid base tier
MAKER_FEE = 0.00015
SLIPPAGE = 0.0003    # adverse slippage on any market/stop fill

HOLDOUT_START = pd.Timestamp("2026-02-01")


def set_costs(maker: float = 0.00015, taker: float = 0.00045,
              slippage: float = 0.0003) -> None:
    global MAKER_FEE, TAKER_FEE, SLIPPAGE
    MAKER_FEE = maker
    TAKER_FEE = taker
    SLIPPAGE = slippage


@dataclass
class Trade:
    symbol: str
    side: str
    signal_idx: int
    entry_idx: int
    exit_idx: int
    entry: float
    exit: float
    sl_dist: float
    outcome: str          # "tp" | "sl" | "trail" | "ambiguous_sl" | "time"
    r: float
    net_frac: float


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)

    def df(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])


def load_symbol(symbol: str, interval: str = "Min1") -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / f"{symbol}_{interval}.parquet")
    return df.reset_index(drop=True)


def list_symbols(interval: str = "Min1") -> list[str]:
    return sorted(p.stem.replace(f"_{interval}", "")
                  for p in DATA_DIR.glob(f"*_{interval}.parquet"))


def _get(sig, name, default=np.nan):
    v = getattr(sig, name, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def simulate(df: pd.DataFrame, signals: pd.DataFrame, symbol: str) -> Result:
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    n = len(df)
    res = Result()

    busy_until = -1  # no new signal is taken while a position/order is live
    for sig in signals.sort_values("idx").itertuples(index=False):
        i = int(sig.idx)
        if i <= busy_until:
            continue
        long = sig.side == "long"
        entry_type = getattr(sig, "entry_type", "taker")
        sl_dist = float(sig.sl_dist)
        tp_dist = _get(sig, "tp_dist")
        trail_dist = _get(sig, "trail_dist")
        trail_arm = _get(sig, "trail_arm", 0.0)
        if not np.isfinite(trail_arm):
            trail_arm = 0.0
        max_hold = int(_get(sig, "max_hold", 240.0))

        # ---- entry
        if entry_type == "taker":
            if i + 1 >= n:
                continue
            entry_idx = i + 1
            raw = open_[entry_idx]
            entry = raw * (1 + SLIPPAGE) if long else raw * (1 - SLIPPAGE)
            entry_fee = TAKER_FEE
        else:  # maker
            p = float(sig.limit_price)
            ttl = int(_get(sig, "ttl", 5.0))
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

        sl = entry * (1 - sl_dist) if long else entry * (1 + sl_dist)
        tp = None
        if np.isfinite(tp_dist):
            tp = entry * (1 + tp_dist) if long else entry * (1 - tp_dist)

        peak = entry  # best favorable extreme up to previous bar
        outcome, exit_price, exit_idx = None, None, None
        end = min(entry_idx + max_hold, n)
        for j in range(entry_idx, end):
            stop_level = sl
            if np.isfinite(trail_dist):
                armed = (peak >= entry * (1 + trail_arm)) if long else \
                        (peak <= entry * (1 - trail_arm))
                if armed:
                    t_lvl = peak * (1 - trail_dist) if long else peak * (1 + trail_dist)
                    stop_level = max(stop_level, t_lvl) if long else min(stop_level, t_lvl)

            hit_sl = low[j] <= stop_level if long else high[j] >= stop_level
            hit_tp = (tp is not None) and (high[j] > tp if long else low[j] < tp)
            if hit_tp and j == entry_idx and entry_type == "maker":
                # ENTRY-CANDLE TP on a MAKER fill requires CLOSE confirmation:
                # the candle's favorable extreme can occur BEFORE the
                # late-in-candle limit fill (same artifact as the fade engines;
                # tick replay showed only 6-8% of high-based same-candle TP
                # credits were real). A close beyond TP provably happens after
                # the fill. Taker entries fill at the bar OPEN, so their bar
                # extremes are always post-fill — no confirmation needed.
                hit_tp = close[j] > tp if long else close[j] < tp
            if hit_sl:
                # ambiguous if TP also reachable -> loss (conservative)
                outcome = "ambiguous_sl" if hit_tp else \
                          ("trail" if stop_level != sl else "sl")
                exit_idx = j
                exit_price = stop_level * (1 - SLIPPAGE) if long else stop_level * (1 + SLIPPAGE)
                break
            if hit_tp:
                outcome, exit_idx, exit_price = "tp", j, tp
                break
            peak = max(peak, high[j]) if long else min(peak, low[j])
        if outcome is None:
            exit_idx = end - 1
            outcome = "time"
            c = close[exit_idx]
            exit_price = c * (1 - SLIPPAGE / 2) if long else c * (1 + SLIPPAGE / 2)

        gross = (exit_price - entry) / entry if long else (entry - exit_price) / entry
        exit_fee = MAKER_FEE if outcome == "tp" else TAKER_FEE
        net = gross - entry_fee - exit_fee
        res.trades.append(Trade(
            symbol=symbol, side=sig.side, signal_idx=i, entry_idx=entry_idx,
            exit_idx=int(exit_idx), entry=float(entry), exit=float(exit_price),
            sl_dist=float(sl_dist), outcome=outcome,
            r=float(net / sl_dist), net_frac=float(net),
        ))
        busy_until = exit_idx
    return res


def attach_times(trades: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    dt = df["dt"].dt.tz_localize(None) if df["dt"].dt.tz is not None else df["dt"]
    trades = trades.copy()
    trades["entry_time"] = dt.iloc[trades["entry_idx"]].values
    trades["exit_time"] = dt.iloc[trades["exit_idx"]].values
    return trades


def stats(t: pd.DataFrame) -> dict:
    if t is None or len(t) == 0:
        return {"n": 0}
    wins = (t["r"] > 0).sum()
    gp = t.loc[t["r"] > 0, "r"].sum()
    gl = -t.loc[t["r"] <= 0, "r"].sum()
    eq = t.sort_values("exit_time")["r"].cumsum()
    dd = float((eq.cummax() - eq).max())
    aw = float(t.loc[t["r"] > 0, "r"].mean()) if wins else 0.0
    al = float(-t.loc[t["r"] <= 0, "r"].mean()) if wins < len(t) else 0.0
    be = al / (aw + al) if (aw + al) > 0 else None
    return {
        "n": int(len(t)), "wr": round(float(wins / len(t)), 4),
        "avg_win_r": round(aw, 3), "avg_loss_r": round(al, 3),
        "breakeven_wr": round(be, 4) if be is not None else None,
        "avg_r": round(float(t["r"].mean()), 4),
        "total_r": round(float(t["r"].sum()), 2),
        "profit_factor": round(float(gp / gl), 3) if gl > 0 else float("inf"),
        "max_dd_r": round(dd, 2),
        "outcomes": t["outcome"].value_counts().to_dict(),
    }


def load_strategy(path: str):
    spec = importlib.util.spec_from_file_location("strategy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(strategy_path: str, params: dict | None = None,
        symbols: list[str] | None = None,
        end_time: pd.Timestamp | None = None) -> pd.DataFrame:
    """Run a mom strategy; returns the trades DataFrame with times attached.

    end_time truncates the DATA (not just the trades) — use it to keep tuning
    runs blind to the holdout window.
    """
    strategy = load_strategy(strategy_path)
    params = params or getattr(strategy, "DEFAULT_PARAMS", {})

    def _trunc(df):
        if end_time is None:
            return df
        dt = df["dt"].dt.tz_localize(None)
        return df[dt < end_time].reset_index(drop=True)

    all_t = []
    if hasattr(strategy, "generate_signals_multi"):
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
    else:
        for sym in (symbols or list_symbols()):
            df = _trunc(load_symbol(sym))
            sig = strategy.generate_signals(df, params)
            if sig.empty:
                continue
            res = simulate(df, sig, sym)
            if res.trades:
                all_t.append(attach_times(res.df(), df))
    if not all_t:
        return pd.DataFrame()
    return pd.concat(all_t).sort_values("entry_time").reset_index(drop=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy")
    ap.add_argument("--params", default=None)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--is-only", action="store_true")
    args = ap.parse_args()
    params = json.loads(args.params) if args.params else None
    symbols = args.symbols.split(",") if args.symbols else None
    t = run(args.strategy, params, symbols,
            end_time=HOLDOUT_START if args.is_only else None)
    print(json.dumps({"all": stats(t)}, indent=2, default=str))
