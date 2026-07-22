"""Edge validation of candidate expansion coins on 12mo Binance @ HL fees.

Runs the FROZEN strategies/hl_native_shock_freq.py config over each coin's full
12mo via engine_hl.simulate (HL fees). Reports n, WR, avg_r, PF, months-positive,
median 1-min $vol. Also runs the full blended portfolio (existing 18 + passing)
to check the edge holds. Read-only wrt live executor / strategies.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import engine_hl  # noqa: E402

DATA = BASE / "data_binance"
STRAT = engine_hl.load_strategy(str(BASE / "strategies" / "hl_native_shock_freq.py"))
PARAMS = STRAT.DEFAULT_PARAMS

EXISTING = ["BTC", "ETH", "SOL", "XRP", "HYPE", "ZEC", "PEPE", "AVAX", "TAO",
            "DOGE", "SUI", "NEAR", "WLD", "PUMPFUN", "LTC", "BNB", "ADA", "LINK"]
CANDIDATES = ["ENA", "ONDO", "AAVE", "ARB", "INJ", "OP", "FET", "LDO", "UNI",
              "WIF", "TIA", "JUP", "XMR", "POL", "PENGU", "ENS", "SEI", "TON", "APT"]

PASS = dict(avg_r=0.08, wr=0.58, months_pos=8, n=150)


def run_coin(sym):
    f = DATA / f"{sym}_USDT_Min1.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f).reset_index(drop=True)
    months = df["dt"].dt.strftime("%Y-%m").nunique()
    med_vol = float((df["vol"] * df["close"]).median())  # 1-min $vol
    sig = STRAT.generate_signals(df, PARAMS)
    if sig.empty:
        return dict(sym=sym, months=months, n=0, med_vol=med_vol)
    res = engine_hl.simulate(df, sig, sym)
    t = res.df()
    if t.empty:
        return dict(sym=sym, months=months, n=0, med_vol=med_vol)
    t["sig_dt"] = df["dt"].iloc[t["signal_idx"].to_numpy()].to_numpy()
    return dict(sym=sym, months=months, med_vol=med_vol, trades=t)


def stats(t):
    n = len(t)
    wins = (t["r"] > 0).sum()
    gp = t.loc[t["r"] > 0, "r"].sum()
    gl = -t.loc[t["r"] <= 0, "r"].sum()
    by_m = t.groupby(pd.to_datetime(t["sig_dt"], utc=True).dt.strftime("%Y-%m"))["r"].sum()
    return dict(n=n, wr=round(wins / n, 4), avg_r=round(t["r"].mean(), 4),
                total_r=round(t["r"].sum(), 1),
                pf=round(gp / gl, 3) if gl > 0 else float("inf"),
                months_pos=int((by_m > 0).sum()), months_tot=int(len(by_m)),
                med_sl=round(float(t["sl_dist"].median()), 5))


def main():
    rows, cand_trades = [], {}
    print("=== PER-CANDIDATE (frozen hl_native_shock_freq, 12mo Binance, HL fees) ===")
    for sym in CANDIDATES:
        r = run_coin(sym)
        if r is None:
            print(f"{sym:7} NO DATA")
            rows.append((sym, "nodata"))
            continue
        if r.get("n") == 0 or "trades" not in r:
            print(f"{sym:7} months={r['months']:2} n=0 (no fills)  medvol=${r['med_vol']:,.0f}")
            rows.append((sym, "nofills"))
            continue
        s = stats(r["trades"])
        cand_trades[sym] = r["trades"]
        passed = (s["avg_r"] >= PASS["avg_r"] and s["wr"] >= PASS["wr"]
                  and s["months_pos"] >= PASS["months_pos"] and s["n"] >= PASS["n"]
                  and r["months"] >= 12)
        tag = "PASS" if passed else "----"
        rows.append((sym, "pass" if passed else "fail", s, r))
        print(f"{sym:7} mo={r['months']:2} n={s['n']:4} WR={s['wr']:.3f} "
              f"avg_r={s['avg_r']:+.4f} PF={s['pf']:.2f} m+={s['months_pos']}/{s['months_tot']} "
              f"totR={s['total_r']:+.0f} medSL={s['med_sl']:.4f} "
              f"medVol=${r['med_vol']:,.0f}  [{tag}]")

    passers = [sym for sym, *rest in rows if rest and rest[0] == "pass"]
    print(f"\nPASS list (edge): {passers}")

    # Blended portfolio before/after
    def portfolio(syms):
        parts = []
        for sym in syms:
            f = DATA / f"{sym}_USDT_Min1.parquet"
            if not f.exists():
                continue
            df = pd.read_parquet(f).reset_index(drop=True)
            sig = STRAT.generate_signals(df, PARAMS)
            if sig.empty:
                continue
            t = engine_hl.simulate(df, sig, sym).df()
            if t.empty:
                continue
            t["sig_dt"] = df["dt"].iloc[t["signal_idx"].to_numpy()].to_numpy()
            t["symbol"] = sym
            parts.append(t)
        return pd.concat(parts) if parts else pd.DataFrame()

    base = portfolio(EXISTING)
    withnew = portfolio(EXISTING + passers)

    def pstats(t):
        t = t.sort_values("signal_idx")
        by_m = t.groupby(pd.to_datetime(t["sig_dt"], utc=True).dt.strftime("%Y-%m"))["r"].sum()
        # global equity DD by chronological signal time
        ts = t.sort_values("sig_dt")["r"]
        eq = ts.cumsum()
        dd = float((eq.cummax() - eq).max())
        wins = (t["r"] > 0).sum()
        gp = t.loc[t["r"] > 0, "r"].sum(); gl = -t.loc[t["r"] <= 0, "r"].sum()
        return dict(n=len(t), wr=round(wins/len(t), 4), avg_r=round(t["r"].mean(), 4),
                    total_r=round(t["r"].sum(), 1), pf=round(gp/gl, 3),
                    max_dd_r=round(dd, 1), months_pos=int((by_m > 0).sum()),
                    months_tot=int(len(by_m)))

    print("\n=== BLENDED PORTFOLIO ===")
    print(f"BEFORE (18 existing): {pstats(base)}")
    print(f"AFTER  (18 + {len(passers)} new={passers}): {pstats(withnew)}")


if __name__ == "__main__":
    main()
