"""SMC Study 2 — Order-block retest & FVG fill (maker limits, STRICT engine_hl)
with distance-matched random-LIMIT control.

For every real signal the control places N_CTRL random limit orders in the same
window: same symbol, same side, same fractional offset from close, same
sl_dist/tp_dist/ttl — only the TIME is random. Both go through
engine_hl.simulate (strict entry-candle TP close-confirmation, HL fees).
The WR delta among FILLED trades isolates the structural component beyond
generic "price dipped into a limit" selection.

Windows: IS = Jul 1 2025 .. Jan 31 2026. Holdout via --window holdout
(pre-registered finalists only, opened once).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import engine_hl  # strict engine, HL fees

DATA = ROOT / "data_binance"
IS_END = pd.Timestamp("2026-02-01", tz="UTC")
HOLDOUT_START = pd.Timestamp("2026-02-01", tz="UTC")
WARMUP_START = pd.Timestamp("2026-01-01", tz="UTC")
N_CTRL = 10
MAX_SIG_PER_SYM = 300  # seeded random subsample per symbol/variant (declared:
                       # keeps engine runtime sane; unbiased w.r.t. outcome)

OB_GRID = [
    {"name": f"ob_k{k}_tp{tp}_htf{int(h)}",
     "strategy": "strategies/smc_ob_retest.py",
     "params": {"disp_k": k, "tp_mult": tp, "htf_filter": h}}
    for k in (2.0, 3.0) for tp in (1.0, 1.1) for h in (False, True)
]
FVG_GRID = [
    {"name": f"fvg_k{k}_{m}_htf{int(h)}",
     "strategy": "strategies/smc_fvg_fill.py",
     "params": {"gap_k": k, "sl_mode": m, "htf_filter": h}}
    for k in (0.5, 1.0) for m in ("edge", "origin") for h in (False, True)
]
GRID = OB_GRID + FVG_GRID


def list_symbols() -> list[str]:
    return sorted(p.stem.replace("_Min1", "") for p in DATA.glob("*_Min1.parquet"))


def load_window(sym: str, window: str) -> tuple[pd.DataFrame, int]:
    df = pd.read_parquet(DATA / f"{sym}_Min1.parquet").reset_index(drop=True)
    if window == "is":
        df = df[df["dt"] < IS_END].reset_index(drop=True)
        return df, 61
    df = df[df["dt"] >= WARMUP_START].reset_index(drop=True)
    mask = df["dt"] >= HOLDOUT_START
    lo = int(mask.idxmax()) if mask.any() else len(df)
    return df, lo


def make_controls(df: pd.DataFrame, signals: pd.DataFrame, lo: int,
                  rng: np.random.Generator) -> pd.DataFrame:
    close = df["close"].to_numpy()
    n = len(df)
    rows = []
    for s in signals.itertuples(index=False):
        i = int(s.idx)
        off = abs(close[i] - s.limit_price) / close[i]
        picks = rng.integers(lo, n - 1, size=N_CTRL)
        for r in picks:
            c = close[int(r)]
            lp = c * (1 - off) if s.side == "long" else c * (1 + off)
            rows.append({"idx": int(r), "side": s.side, "limit_price": float(lp),
                         "tp_dist": float(s.tp_dist), "sl_dist": float(s.sl_dist),
                         "ttl": int(s.ttl)})
    return pd.DataFrame(rows, columns=["idx", "side", "limit_price",
                                       "tp_dist", "sl_dist", "ttl"])


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0}
    wr = float((t["r"] > 0).mean())
    return {"n": int(len(t)), "wr": round(wr, 4),
            "avg_r": round(float(t["r"].mean()), 4),
            "total_r": round(float(t["r"].sum()), 1),
            "outcomes": t["outcome"].value_counts().to_dict(),
            "med_sl_dist": round(float(t["sl_dist"].median()), 5)}


def run_variant(v: dict, window: str, dfs: dict, seed: int = 23,
                dump: str | None = None) -> dict:
    strat = engine_hl.load_strategy(str(ROOT / v["strategy"]))
    rng = np.random.default_rng(seed)
    real_tr, ctrl_tr = [], []
    n_signals = 0
    for sym, (df, lo) in dfs.items():
        sig = strat.generate_signals(df, v["params"])
        if not sig.empty:
            sig = sig[sig["idx"] >= lo].reset_index(drop=True)
        if sig.empty:
            continue
        if len(sig) > MAX_SIG_PER_SYM:
            keep = rng.choice(len(sig), size=MAX_SIG_PER_SYM, replace=False)
            sig = sig.iloc[np.sort(keep)].reset_index(drop=True)
        n_signals += len(sig)
        res = engine_hl.simulate(df, sig, sym)
        if res.trades:
            real_tr.append(res.df())
        ctrl = make_controls(df, sig, lo, rng)
        cres = engine_hl.simulate(df, ctrl, sym)
        if cres.trades:
            ctrl_tr.append(cres.df())
    real = pd.concat(real_tr) if real_tr else pd.DataFrame()
    ctrl = pd.concat(ctrl_tr) if ctrl_tr else pd.DataFrame()
    rs, cs = stats(real), stats(ctrl)
    out = {"variant": v["name"], "window": window, "n_signals": n_signals,
           "fill_rate": round(rs.get("n", 0) / n_signals, 3) if n_signals else None,
           "real": rs, "control": cs}
    if rs.get("n") and cs.get("n"):
        out["delta_wr"] = round(rs["wr"] - cs["wr"], 4)
        p1, n1 = rs["wr"], rs["n"]
        p2 = cs["wr"]
        se = (p1 * (1 - p1) / n1 + p2 * (1 - p2) / n1) ** 0.5
        out["delta_se_conservative"] = round(se, 4)
    if dump and not real.empty:
        real.to_csv(f"{dump.rstrip('/')}/limit_{window}_{v['name']}.csv", index=False)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=["is", "holdout"], default="is")
    ap.add_argument("--variants", default=None)
    ap.add_argument("--dump", default=None)
    ap.add_argument("--check-lookahead", action="store_true")
    args = ap.parse_args()

    dfs = {}
    for sym in list_symbols():
        df, lo = load_window(sym, args.window)
        if len(df) > 5000:
            dfs[sym] = (df, lo)

    if args.check_lookahead:
        sym0 = "BTC_USDT"
        for spath, params in [("strategies/smc_ob_retest.py", {"htf_filter": True}),
                              ("strategies/smc_fvg_fill.py", {"htf_filter": True})]:
            strat = engine_hl.load_strategy(str(ROOT / spath))
            v = engine_hl.verify_no_lookahead(strat, dfs[sym0][0], params)
            print(f"lookahead {spath}: {v or 'CLEAN'}", file=sys.stderr, flush=True)

    results = []
    for v in GRID:
        if args.variants and v["name"] not in args.variants.split(","):
            continue
        r = run_variant(v, args.window, dfs, dump=args.dump)
        results.append(r)
        print(json.dumps(r), flush=True)

    print("\n=== SUMMARY ===", file=sys.stderr)
    for r in results:
        re, ct = r["real"], r["control"]
        if not re.get("n"):
            print(f"{r['variant']}: no fills", file=sys.stderr)
            continue
        print(f"{r['variant']:>24}: sig={r['n_signals']:5d} fill={r['fill_rate']} "
              f"n={re['n']:5d} wr={re['wr']:.3f} avg_r={re['avg_r']:+.4f} | "
              f"ctrl wr={ct.get('wr')} avg_r={ct.get('avg_r')} | "
              f"dWR={r.get('delta_wr'):+.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
