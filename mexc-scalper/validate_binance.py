"""Cross-venue / long-horizon validation: run a strategy over 12 months of
Binance 1m data with the MEXC cost model, report per-month regime stability.

Usage: python3 validate_binance.py strategies/shock_candle_overshoot_catch.py
"""
import sys
from pathlib import Path

import pandas as pd

import engine
from engine import load_strategy, simulate

BDATA = Path(__file__).parent / "data_binance"


def run(strategy_path: str) -> pd.DataFrame:
    strat = load_strategy(strategy_path)
    params = strat.DEFAULT_PARAMS
    all_t = []
    for pq in sorted(BDATA.glob("*_Min1.parquet")):
        sym = pq.stem.replace("_Min1", "")
        df = pd.read_parquet(pq).reset_index(drop=True)
        sig = strat.generate_signals(df, params)
        if sig.empty:
            continue
        res = simulate(df, sig, sym)
        if not res.trades:
            continue
        t = res.df()
        t["entry_time"] = df["dt"].iloc[t["entry_idx"]].values
        all_t.append(t)
        print(f"{sym}: {len(t)} trades, total {t.r.sum():+.1f}R", flush=True)
    return pd.concat(all_t).sort_values("entry_time").reset_index(drop=True)


def report(t: pd.DataFrame):
    t["month"] = t["entry_time"].dt.to_period("M")
    print(f"\n=== 12-MONTH TOTAL: {len(t)} trades ===")
    wr = (t.r > 0).mean()
    wins, losses = t[t.r > 0], t[t.r <= 0]
    aw, al = wins.r.mean(), -losses.r.mean()
    be = al / (aw + al)
    print(f"WR {wr*100:.1f}% (breakeven {be*100:.1f}%) | avg_r {t.r.mean():+.4f} | "
          f"total {t.r.sum():+.0f}R | PF {wins.r.sum()/-losses.r.sum():.3f}")
    print("\nPer month:")
    rows = []
    for m, g in t.groupby("month"):
        rows.append({
            "month": str(m), "n": len(g), "wr": round((g.r > 0).mean(), 3),
            "avg_r": round(g.r.mean(), 3), "total_r": round(g.r.sum(), 1),
            "syms_pos": f"{sum(gg.r.sum() > 0 for _, gg in g.groupby('symbol'))}/{g.symbol.nunique()}",
        })
    print(pd.DataFrame(rows).to_string(index=False))
    print("\nPer symbol (12mo):")
    rows = []
    for s, g in t.groupby("symbol"):
        rows.append({"symbol": s, "n": len(g), "wr": round((g.r > 0).mean(), 3),
                     "avg_r": round(g.r.mean(), 3), "total_r": round(g.r.sum(), 1),
                     "months_pos": f"{sum(gg.r.sum() > 0 for _, gg in g.groupby('month'))}/{g.month.nunique()}"})
    print(pd.DataFrame(rows).to_string(index=False))
    # equity sim, 1% risk sequential compounding
    eq, peak, maxdd = 1000.0, 1000.0, 0.0
    for r in t.r:
        eq *= 1 + 0.01 * r
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    print(f"\nEquity 1% risk/trade compounding: 1000 -> {eq:,.0f} USDT, maxDD {maxdd*100:.1f}%")


if __name__ == "__main__":
    trades = run(sys.argv[1] if len(sys.argv) > 1 else "strategies/shock_candle_overshoot_catch.py")
    trades.to_parquet("/tmp/claude-0/-home-user-hype/c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad/binance_trades.parquet")
    report(trades)
