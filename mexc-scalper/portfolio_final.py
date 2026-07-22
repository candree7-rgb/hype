"""Final portfolio construction (2026-07-22): TSMOM blend + momentum sleeve.

Builds overlapping DAILY pnl series (Jul 2025 - Jun 2026, the TSMOM holdout =
the sleeve's 12mo window) for:
  F1  tsmom_classic 4h k=84          (frozen finalist, funding included)
  F1e tsmom_classic 4h k=[63,84,105] (honest-parameter ensemble, funding incl)
  F2  tsmom_donchian 330/120 3xATR   (frozen finalist, funding included)
  MOM mom_cascade_breakout at 1% risk per R (trades from cached frozen run,
      reproduced bit-exact by rerunning engine_mom before use)

Reports: correlation matrix, monthly distribution, maxDD, worst month for
  A) frozen book:  50/50 F1+F2 @ 20% vol + MOM 1%
  B) honest book:  50/50 F1e+F2 @ 20% vol + MOM 1%   <- base case
  each also at 10% TSMOM vol (=0.5x the TSMOM daily pnl; pnl is linear in w),
  and $/month at $1k and $10k equity.

Run: python3 portfolio_final.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import engine_tsmom as E  # noqa: E402
import verify_tsmom as V  # noqa: E402

MOM_TRADES = Path("/tmp/claude-0/-home-user-hype/c47c9f5c-c8dc-549c-b753-d71584fd5d9c/"
                  "scratchpad/mom_final_F1.parquet")
HS, HE = E.HOLDOUT_START, E.HOLDOUT_END


def daily(pnl4h: pd.Series) -> pd.Series:
    return pnl4h.groupby(pnl4h.index.floor("D")).sum()


def tsmom_daily(panel, rates, mod, P) -> pd.Series:
    w0 = mod.target_weights(panel, P)
    w = E.apply_port_vol_target(panel, w0, "4h", 0.20)
    bt = E.run_backtest(panel, w, "4h")
    fund = V.funding_pnl(w, rates).sum(axis=1).reindex(bt["pnl"].index).fillna(0.0)
    pnl = V.slice_pnl(bt["pnl"] + fund, HS, HE)
    return daily(pnl)


def mom_daily_series(index) -> pd.Series:
    t = pd.read_parquet(MOM_TRADES)
    t["exit_day"] = pd.to_datetime(t["exit_time"], utc=True).dt.floor("D")
    r = t.groupby("exit_day")["r"].sum()
    return (r * 0.01).reindex(index).fillna(0.0)  # 1% equity risk per R


def stats(d: pd.Series, label: str, eq1=1_000, eq2=10_000):
    eqc = (1 + d).cumprod()
    dd = float((eqc / eqc.cummax() - 1).min())
    mo = d.groupby(d.index.to_period("M")).apply(lambda x: float((1 + x).prod() - 1))
    sharpe = float(d.mean() / d.std() * np.sqrt(365)) if d.std() > 0 else 0.0
    print(f"\n{label}")
    print(f"  sharpe {sharpe:+.2f}  avg_mo {mo.mean()*100:+.2f}%  med_mo "
          f"{mo.median()*100:+.2f}%  worst_mo {mo.min()*100:+.2f}%  best_mo "
          f"{mo.max()*100:+.2f}%  mo+ {(mo>0).sum()}/{len(mo)}  maxDD {dd*100:.1f}%")
    print(f"  12mo total {float(eqc.iloc[-1]-1)*100:+.1f}%  "
          f"$/mo @$1k ${mo.mean()*eq1:+.0f}  @$10k ${mo.mean()*eq2:+.0f}  "
          f"worst-mo $ @$1k {mo.min()*eq1:+.0f}  @$10k {mo.min()*eq2:+.0f}")
    print("  monthly%:", " ".join(f"{str(k)[2:]}:{v*100:+.1f}" for k, v in mo.items()))
    return {"sharpe": sharpe, "avg_mo": mo.mean(), "worst_mo": mo.min(),
            "maxdd": dd, "monthly": mo}


def main():
    panel = E.load_panel("4h")
    rates = V.load_funding(list(panel["close"].columns), panel["close"].index)
    classic, donch = V.load_mod("tsmom_classic"), V.load_mod("tsmom_donchian")

    print("building daily series (holdout Jul25-Jun26, funding included)...")
    f1 = tsmom_daily(panel, rates, classic, V.F1P)
    f1e = tsmom_daily(panel, rates, classic, dict(V.F1P, lookbacks=[63, 84, 105]))
    f2 = tsmom_daily(panel, rates, donch, V.F2P)
    idx = f1.index
    mom = mom_daily_series(idx)

    comp = pd.DataFrame({"F1_k84": f1, "F1_ens": f1e, "F2_donch": f2,
                         "MOM_1pct": mom}).fillna(0.0)
    print("\nDaily correlation matrix (holdout):")
    print(comp.corr().round(2).to_string())
    momo = comp.apply(lambda c: c.groupby(c.index.to_period("M"))
                      .apply(lambda x: (1 + x).prod() - 1))
    print("\nMonthly correlation matrix (holdout):")
    print(momo.corr().round(2).to_string())

    for nm, s in comp.items():
        stats(s, f"component {nm}")

    blend_frozen = 0.5 * f1 + 0.5 * f2
    blend_honest = 0.5 * f1e + 0.5 * f2
    for tag, blend in [("FROZEN (F1 k=84 + F2)", blend_frozen),
                       ("HONEST (F1 ensemble + F2)  <- base case", blend_honest)]:
        print("\n" + "=" * 72)
        print(f"TSMOM blend: {tag}")
        stats(blend, "  TSMOM @20% vol, alone")
        stats(0.5 * blend, "  TSMOM @10% vol, alone")
        stats(blend + mom, "  BOOK: TSMOM@20% + MOM@1%")
        stats(0.5 * blend + mom, "  BOOK: TSMOM@10% + MOM@1%")


if __name__ == "__main__":
    main()
