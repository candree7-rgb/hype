"""12-month Binance anchor for the HL transfer estimate.

Re-runs the frozen gate config (strategies/shock_hl_variant.py) on
data_binance/ (12 months, 18 coins) under the engine_hl cost model at:
  - HL tier 0: maker 0.015% / taker 0.045%
  - HL tier 1: maker 0.012% / taker 0.040%  (>$5M 14d volume)
Splits trades by SIGNAL DATE at 2026-02-01 (IS Jul25-Jan26 / holdout
Feb-Jun26, matching the frozen-config protocol), reports holdout metrics,
monthly trade counts and monthly emitted-signal counts (post-cooldown,
post-gate) for the trades/month scaling.

Read-only wrt existing modules.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import engine_hl  # noqa: E402

DATA = BASE / "data_binance"
GATE = engine_hl.load_strategy(str(BASE / "strategies" / "shock_hl_variant.py"))
PARAMS = GATE.DEFAULT_PARAMS
HOLDOUT_START = pd.Timestamp("2026-02-01", tz="UTC")

TIERS = {"tier0": (0.00015, 0.00045), "tier1": (0.00012, 0.00040)}


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0}
    wins = (t["r"] > 0).sum()
    aw = float(t.loc[t["r"] > 0, "r"].mean()) if wins else 0.0
    al = float(-t.loc[t["r"] <= 0, "r"].mean()) if wins < len(t) else 0.0
    return {"n": int(len(t)), "wr": round(float(wins / len(t)), 4),
            "breakeven_wr": round(al / (aw + al), 4) if aw + al > 0 else None,
            "avg_r": round(float(t["r"].mean()), 4),
            "sd_r": round(float(t["r"].std(ddof=1)), 3),
            "se_avg_r": round(float(t["r"].std(ddof=1) / np.sqrt(len(t))), 4),
            "total_r": round(float(t["r"].sum()), 1)}


def main():
    syms = sorted(p.stem.replace("_Min1", "") for p in DATA.glob("*_Min1.parquet"))
    print(f"{len(syms)} symbols: {syms}")

    per_tier_trades = {k: [] for k in TIERS}
    sig_months = []
    for sym in syms:
        df = pd.read_parquet(DATA / f"{sym}_Min1.parquet").reset_index(drop=True)
        sig = GATE.generate_signals(df, PARAMS)
        dts = df["dt"]
        sig_months.append(pd.DataFrame({
            "symbol": sym, "month": dts.iloc[sig["idx"].to_numpy()]
            .dt.strftime("%Y-%m").to_numpy()}))
        for tier, (mk, tk) in TIERS.items():
            engine_hl.set_costs(maker=mk, taker=tk, slippage=0.0003)
            res = engine_hl.simulate(df, sig, sym)
            t = res.df()
            if t.empty:
                continue
            t["sig_dt"] = dts.iloc[t["signal_idx"].to_numpy()].to_numpy()
            per_tier_trades[tier].append(t)
        print(f"  {sym}: signals={len(sig)}", flush=True)
    engine_hl.set_costs()  # restore defaults

    sigs = pd.concat(sig_months)
    print("\nemitted signals (post-cooldown, post-gate) per month, all 18 coins:")
    ms = sigs.groupby("month").size()
    print(ms.to_string())
    print(f"12mo mean signals/month: {ms.mean():.1f}")

    for tier in TIERS:
        trades = pd.concat(per_tier_trades[tier])
        trades["sig_dt"] = pd.to_datetime(trades["sig_dt"], utc=True)
        hold = trades[trades["sig_dt"] >= HOLDOUT_START]
        ins = trades[trades["sig_dt"] < HOLDOUT_START]
        print(f"\n=== {tier} (maker {TIERS[tier][0]*100:.3f}% / taker {TIERS[tier][1]*100:.3f}%) ===")
        print("IS  Jul25-Jan26:", stats(ins))
        print("OOS Feb26-Jun26:", stats(hold))
        m = hold.groupby(hold["sig_dt"].dt.strftime("%Y-%m"))["r"].agg(["size", "mean", "sum"])
        print("holdout by month:")
        print(m.round(3).to_string())
        if tier == "tier0":
            fillrate = len(trades) / len(sigs)
            print(f"fill rate (12mo, all): {fillrate:.3f}   "
                  f"trades/month holdout: {len(hold)/5:.1f}")
            # per-symbol holdout
            ps = hold.groupby("symbol")["r"].agg(["size", "mean"]).round(3)
            print("holdout per symbol:")
            print(ps.to_string())
            hold.to_parquet(Path("/tmp/claude-0/-home-user-hype/"
                                 "c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad/"
                                 "bin12mo_holdout_tier0.parquet"))


if __name__ == "__main__":
    main()
