"""Final venue-native validation: run the frozen Lighter config
(strategies/shock_lighter_v1.py) on LIGHTER'S OWN 12-13 months of 1m candles
(data_lighter/), stop-limit execution, all three slippage regimes.

This is the definitive test: same venue, same fee model, same execution
mechanics as deployment. Usage: python3 validate_lighter.py
"""
import engine_lighter as el

el.DATA_DIR = el.Path(__file__).parent / "data_lighter"

import pandas as pd


def main():
    print(f"Symbols: {el.list_symbols()}")
    for regime, slip in el.REGIMES.items():
        trades, la = el.run("strategies/shock_lighter_v1.py",
                            sl_slippage=slip, sl_mode="stoplimit",
                            check_lookahead=(regime == "base"))
        s = el.stats(trades)
        print(f"\n=== Regime {regime} (SL-slip {slip*100:.2f}%) ===")
        print({k: s[k] for k in ("n", "wr", "avg_r", "total_r", "profit_factor") if k in s})
        if regime == "base":
            print(f"lookahead: {la}")
            print("\nPer month:")
            rows = []
            for m, g in trades.groupby("month"):
                rows.append({"month": m, "n": len(g), "wr": round((g.r > 0).mean(), 3),
                             "avg_r": round(g.r.mean(), 4), "total_r": round(g.r.sum(), 1)})
            print(pd.DataFrame(rows).to_string(index=False))
            print("\nPer symbol:")
            rows = []
            for sym, g in trades.groupby("symbol"):
                rows.append({"symbol": sym, "n": len(g), "wr": round((g.r > 0).mean(), 3),
                             "avg_r": round(g.r.mean(), 4), "total_r": round(g.r.sum(), 1)})
            print(pd.DataFrame(rows).to_string(index=False))
            outcomes = trades.outcome.value_counts()
            print(f"\nOutcomes: {outcomes.to_dict()}")
            n_stops = outcomes.get("sl_limit", 0) + outcomes.get("sl_bail", 0) + outcomes.get("ambiguous_sl", 0)
            if n_stops:
                print(f"Stop-Limit recovery rate: {outcomes.get('sl_limit',0)/n_stops*100:.1f}% "
                      f"(bail: {outcomes.get('sl_bail',0)}, ambiguous: {outcomes.get('ambiguous_sl',0)})")
            trades.to_parquet("/tmp/claude-0/-home-user-hype/c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad/lighter_trades.parquet")


if __name__ == "__main__":
    main()
