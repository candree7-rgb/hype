"""SMC Study 1 — Liquidity-sweep entries with STRUCTURAL stops, vs
distance-matched random control.

Core claim under test: an SL placed just beyond a swept swing low/high is hit
LESS often than a random equidistant barrier (asymmetric barrier probability
at symmetric distance). The measured quantity is DELTA = WR(real) - WR(control)
where the control uses the SAME symbol, SAME side, SAME fractional barrier
distances, at RANDOM times in the SAME window.

Execution model (artifact-safe, mirrors strict engine_hl):
  - Market entry at NEXT candle open (taker fee 0.045%).
  - TP is a resting maker limit: strict trade-through (high > tp), and on the
    ENTRY candle requires CLOSE confirmation (no same-candle exit credits).
  - SL stop-market: taker 0.045% + 0.03% slippage. TP&SL same candle -> loss.
  - Unresolved after max_hold -> time exit at close (taker, half slippage).

Sweep definition (causal):
  - Swing low = pivot: low[p] == min(low[p-W..p+W]); CONFIRMED at p+W.
  - Sweep event at candle j: first j in (p+W, p+W+STALE] with low[j] < level;
    valid only if close[j] > level (reclaim) and depth (level - low[j])
    passes the depth filter. Level consumed at first touch either way.
  - Entry long at open[j+1]. SL = low[j] - 0.1*ATR60 (beyond the sweep low).
  - TP = entry + tp_mult * (entry - SL).  Shorts fully mirrored.

Windows: IS = Jul 1 2025 .. Jan 31 2026.  Holdout Feb-Jun 26 only via
--window holdout (opened once, for pre-registered finalists only).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data_binance"

TAKER = 0.00045
MAKER = 0.00015
SLIP = 0.0003

IS_END = pd.Timestamp("2026-02-01", tz="UTC")
HOLDOUT_START = pd.Timestamp("2026-02-01", tz="UTC")
WARMUP_START = pd.Timestamp("2026-01-01", tz="UTC")  # warmup for holdout run

MAX_HOLD = 240
STALE = 1440          # sweep must occur within 24h of pivot confirmation
BUF_ATR = 0.10        # SL buffer beyond structure, in ATR60 units
N_CONTROL = 20        # random control entries per real trade
ATR_N = 60


def list_symbols() -> list[str]:
    return sorted(p.stem.replace("_Min1", "") for p in DATA.glob("*_Min1.parquet"))


def load(sym: str) -> pd.DataFrame:
    df = pd.read_parquet(DATA / f"{sym}_Min1.parquet").reset_index(drop=True)
    return df


def atr(df: pd.DataFrame, n: int = ATR_N) -> np.ndarray:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def htf_trend_up(df: pd.DataFrame) -> np.ndarray:
    """Causal 4h trend: for each 1m row, close of the LAST COMPLETED 4h bar
    vs EMA50 of completed 4h bars. Returns bool array aligned to df rows.
    NaN-period (warmup) -> False for up and False for down (no trades pass
    the filter until EMA is warm); we return an int array: 1 up, -1 down,
    0 unknown."""
    g = df.set_index("dt").resample("4h", label="left", closed="left")
    bars = g.agg(close=("close", "last")).dropna()
    ema = bars["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    trend = np.where(bars["close"] > ema, 1, -1)
    trend = np.where(np.isnan(ema), 0, trend)
    # bar starting at b completes at b+4h; usable for any minute >= b+4h
    usable_from = bars.index + pd.Timedelta(hours=4)
    idx = np.searchsorted(usable_from.values, df["dt"].values, side="right") - 1
    out = np.zeros(len(df), dtype=int)
    ok = idx >= 0
    out[ok] = trend[idx[ok]]
    return out


def pivot_mask(x: np.ndarray, W: int, kind: str) -> np.ndarray:
    s = pd.Series(x)
    if kind == "low":
        roll = s.rolling(2 * W + 1, center=True, min_periods=2 * W + 1).min()
        return (s == roll).to_numpy()
    roll = s.rolling(2 * W + 1, center=True, min_periods=2 * W + 1).max()
    return (s == roll).to_numpy()


def find_sweeps(df: pd.DataFrame, W: int, side: str, depth_atr: float,
                atr_arr: np.ndarray) -> list[dict]:
    """Return sweep events: dicts with j (sweep candle), level, extreme."""
    low = df["low"].to_numpy()
    high = df["high"].to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    if side == "long":
        piv = np.flatnonzero(pivot_mask(low, W, "low"))
    else:
        piv = np.flatnonzero(pivot_mask(high, W, "high"))
    events = []
    for p in piv:
        c = p + W  # confirmation index
        if c + 1 >= n:
            continue
        level = low[p] if side == "long" else high[p]
        end = min(c + 1 + STALE, n)
        seg = low[c + 1:end] if side == "long" else high[c + 1:end]
        hit = (seg < level) if side == "long" else (seg > level)
        if not hit.any():
            continue
        j = c + 1 + int(hit.argmax())
        a = atr_arr[j]
        if not np.isfinite(a) or a <= 0:
            continue
        if side == "long":
            depth = level - low[j]
            reclaim = close[j] > level
            extreme = low[j]
        else:
            depth = high[j] - level
            reclaim = close[j] < level
            extreme = high[j]
        if reclaim and depth >= depth_atr * a:
            events.append({"j": j, "level": level, "extreme": extreme, "atr": a})
    # dedupe: one trade per entry candle
    seen, out = set(), []
    for e in sorted(events, key=lambda d: d["j"]):
        if e["j"] not in seen:
            seen.add(e["j"])
            out.append(e)
    return out


def sim_market(open_: np.ndarray, high: np.ndarray, low: np.ndarray,
               close: np.ndarray, e: int, side: str, sl_frac: float,
               tp_frac: float, max_hold: int = MAX_HOLD) -> tuple[str, float]:
    """Simulate a market entry at open[e]. Returns (outcome, r).
    Strict: entry-candle TP needs close confirmation; TP&SL same candle = loss."""
    n = len(open_)
    end = min(e + max_hold, n)
    entry = open_[e]
    long = side == "long"
    if long:
        tp = entry * (1 + tp_frac)
        sl = entry * (1 - sl_frac)
        hi, lo, cl = high[e:end], low[e:end], close[e:end]
        tp_hit = hi > tp
        tp_hit[0] = cl[0] > tp
        sl_hit = lo <= sl
    else:
        tp = entry * (1 - tp_frac)
        sl = entry * (1 + sl_frac)
        hi, lo, cl = high[e:end], low[e:end], close[e:end]
        tp_hit = lo < tp
        tp_hit[0] = cl[0] < tp
        sl_hit = hi >= sl
    any_hit = tp_hit | sl_hit
    if any_hit.any():
        k = int(any_hit.argmax())
        if sl_hit[k]:
            outcome = "ambiguous_sl" if tp_hit[k] else "sl"
            exitp = sl * (1 - SLIP) if long else sl * (1 + SLIP)
            exit_fee = TAKER
        else:
            outcome = "tp"
            exitp = tp
            exit_fee = MAKER
    else:
        outcome = "time"
        c = cl[-1]
        exitp = c * (1 - SLIP / 2) if long else c * (1 + SLIP / 2)
        exit_fee = TAKER
    gross = (exitp - entry) / entry if long else (entry - exitp) / entry
    net = gross - TAKER - exit_fee
    return outcome, float(net / sl_frac)


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    r = np.array([x["r"] for x in rows])
    wr = float((r > 0).mean())
    oc = pd.Series([x["outcome"] for x in rows]).value_counts().to_dict()
    return {"n": len(r), "wr": round(wr, 4), "avg_r": round(float(r.mean()), 4),
            "total_r": round(float(r.sum()), 1), "outcomes": oc,
            "med_sl_frac": round(float(np.median([x["sl_frac"] for x in rows])), 5)}


def run_variant(dfs: dict, W: int, depth_atr: float, tp_mult: float,
                htf: bool, entry_lo: dict, entry_hi: dict,
                seed: int = 11) -> dict:
    """entry_lo/entry_hi: per-symbol allowed entry index range [lo, hi)."""
    rng = np.random.default_rng(seed)
    real_rows, ctrl_rows = [], []
    for sym, pack in dfs.items():
        df, a, trend = pack["df"], pack["atr"], pack["trend"]
        o = df["open"].to_numpy()
        h = df["high"].to_numpy()
        l = df["low"].to_numpy()
        c = df["close"].to_numpy()
        lo_i, hi_i = entry_lo[sym], entry_hi[sym]
        n = len(df)
        for side in ("long", "short"):
            for ev in find_sweeps(df, W, side, depth_atr, a):
                e = ev["j"] + 1
                if e >= n or not (lo_i <= e < hi_i):
                    continue
                if htf:
                    t = trend[ev["j"]]
                    if (side == "long" and t != 1) or (side == "short" and t != -1):
                        continue
                entry = o[e]
                buf = BUF_ATR * ev["atr"]
                if side == "long":
                    sl_price = ev["extreme"] - buf
                    sl_frac = (entry - sl_price) / entry
                else:
                    sl_price = ev["extreme"] + buf
                    sl_frac = (sl_price - entry) / entry
                if sl_frac <= 0:
                    continue
                tp_frac = tp_mult * sl_frac
                outcome, r = sim_market(o, h, l, c, e, side, sl_frac, tp_frac)
                real_rows.append({"symbol": sym, "e": e, "side": side,
                                  "sl_frac": sl_frac, "outcome": outcome, "r": r,
                                  "dt": str(df["dt"].iloc[e])})
                # distance-matched random control
                picks = rng.integers(lo_i, hi_i, size=N_CONTROL)
                for pe in picks:
                    oc2, r2 = sim_market(o, h, l, c, int(pe), side, sl_frac, tp_frac)
                    ctrl_rows.append({"symbol": sym, "e": int(pe), "side": side,
                                      "sl_frac": sl_frac, "outcome": oc2, "r": r2})
    real, ctrl = stats(real_rows), stats(ctrl_rows)
    delta = None
    if real.get("n") and ctrl.get("n"):
        delta = round(real["wr"] - ctrl["wr"], 4)
        # naive SE on the delta (controls clustered; SE understated for ctrl)
        p1, n1 = real["wr"], real["n"]
        p2, n2 = ctrl["wr"], real["n"]  # conservative: cluster count, not 20x
        se = (p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2) ** 0.5
        delta_se = round(se, 4)
    else:
        delta_se = None
    return {"variant": f"W{W}_d{depth_atr}_tp{tp_mult}_htf{int(htf)}",
            "real": real, "control": ctrl, "delta_wr": delta,
            "delta_se_conservative": delta_se,
            "_real_rows": real_rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=["is", "holdout"], default="is")
    ap.add_argument("--variants", default=None,
                    help="comma list of variant names to run (holdout gating)")
    ap.add_argument("--dump", default=None, help="csv path to dump real trades")
    args = ap.parse_args()

    dfs, entry_lo, entry_hi = {}, {}, {}
    for sym in list_symbols():
        df = load(sym)
        if args.window == "is":
            df = df[df["dt"] < IS_END].reset_index(drop=True)
            lo_i, hi_i = ATR_N + 1, len(df)
        else:
            df = df[df["dt"] >= WARMUP_START].reset_index(drop=True)
            mask = df["dt"] >= HOLDOUT_START
            lo_i = int(mask.idxmax()) if mask.any() else len(df)
            hi_i = len(df)
        if len(df) < 5000:
            continue
        dfs[sym] = {"df": df, "atr": atr(df), "trend": htf_trend_up(df)}
        entry_lo[sym], entry_hi[sym] = lo_i, hi_i

    grid = []
    for W in (15, 60):
        for depth in (0.0, 0.25):
            for tpm in (1.0, 1.1):
                for htf in (False, True):
                    grid.append((W, depth, tpm, htf))

    results = []
    for W, depth, tpm, htf in grid:
        name = f"W{W}_d{depth}_tp{tpm}_htf{int(htf)}"
        if args.variants and name not in args.variants.split(","):
            continue
        r = run_variant(dfs, W, depth, tpm, htf, entry_lo, entry_hi)
        rows = r.pop("_real_rows")
        if args.dump and (not args.variants or name in args.variants.split(",")):
            pd.DataFrame(rows).to_csv(
                f"{args.dump.rstrip('/')}/sweep_{args.window}_{name}.csv", index=False)
        results.append(r)
        print(json.dumps(r), flush=True)

    print("\n=== SUMMARY ===", file=sys.stderr)
    for r in results:
        re, ct = r["real"], r["control"]
        if not re.get("n"):
            print(f"{r['variant']}: n=0", file=sys.stderr)
            continue
        print(f"{r['variant']:>26}: n={re['n']:5d} wr={re['wr']:.3f} "
              f"avg_r={re['avg_r']:+.4f} | ctrl wr={ct['wr']:.3f} "
              f"avg_r={ct['avg_r']:+.4f} | dWR={r['delta_wr']:+.4f} "
              f"(se~{r['delta_se_conservative']})", file=sys.stderr)


if __name__ == "__main__":
    main()
