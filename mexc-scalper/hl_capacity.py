"""HL CAPACITY / SCALE CEILING analysis (headline deliverable).

For each universe coin, pull ~5000 HL 1m candles (venue-native), find the
SHOCK minutes the strategy actually fires on (range>3.5*ATR60 & ATR gate), and
measure the quote $ volume on exactly those minutes (that's the minute we get
filled). Solve the max equity at which each coin stays tradable given order <=
10% of shock-minute $vol, at the coin's typical sl_dist. Then aggregate:
effective coins & retained expected-R vs equity, at 2% and 1% risk.

Read-only. Uses frozen strategy params. HL fees are irrelevant here (this is a
liquidity/capacity study), but expected-R weights come from the 12mo Binance
per-coin edge passed in via a small cache file if present.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import engine_hl  # noqa: E402

STRAT = engine_hl.load_strategy(str(BASE / "strategies" / "hl_native_shock_freq.py"))
P = STRAT.DEFAULT_PARAMS
DATA = BASE / "data_binance"

# engine name -> HL coin symbol
HL_NAME = {"PEPE": "kPEPE", "PUMPFUN": "PUMP"}

UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "HYPE", "ZEC", "PEPE", "AVAX", "TAO",
            "DOGE", "SUI", "NEAR", "WLD", "PUMPFUN", "LTC", "BNB", "ADA", "LINK"]
# candidates appended at runtime from arg
EXTRA = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] else []

RISK_LEVELS = [0.02, 0.01]
EQUITIES = [1000, 5000, 10000, 25000, 50000, 100000, 250000]
CAP_FRAC = 0.10  # order must be <= 10% of shock-minute $vol


def hl_post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def meta_lev():
    d = hl_post({"type": "meta"})
    return {x["name"]: x["maxLeverage"] for x in d["universe"]}


def hl_candles(coin, n=5000):
    end = int(time.time() * 1000)
    start = end - n * 60 * 1000
    for attempt in range(4):
        try:
            d = hl_post({"type": "candleSnapshot",
                         "req": {"coin": coin, "interval": "1m",
                                 "startTime": start, "endTime": end}})
            break
        except Exception:
            if attempt == 3:
                return None
            time.sleep(2 ** attempt)
    if not d:
        return None
    df = pd.DataFrame(d)
    for c in ("o", "h", "l", "c", "v"):
        df[c] = df[c].astype(float)
    return df.rename(columns={"o": "open", "h": "high", "l": "low",
                              "c": "close", "v": "vol"}).reset_index(drop=True)


def shock_stats(df):
    """Return shock-minute $vol dist + sl_dist on the coin's own HL candles."""
    high, low, close, open_ = (df[c].to_numpy(float) for c in
                               ("high", "low", "close", "open"))
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close),
                                           np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(P["atr_n"]).mean().to_numpy() / close
    with np.errstate(invalid="ignore", divide="ignore"):
        rng_atr = ((high - low) / close) / atr
    idx = np.arange(len(df))
    shock = (rng_atr > P["shock_atr"]) & (idx > P["warmup"]) & np.isfinite(atr) \
        & (atr >= P["atr_min"])
    dvol = df["vol"].to_numpy(float) * close  # quote $ volume per minute
    all_min = float(np.median(dvol[np.isfinite(dvol)]))
    # typical sl_dist from full-window ATR (robust), floored
    sl_all = np.maximum(P["sl_atr_mult"] * atr[np.isfinite(atr)], P["sl_floor"])
    med_sl_all = float(np.median(sl_all))
    days = (df["t"].iloc[-1] - df["t"].iloc[0]) / 1000 / 86400 if "t" in df else \
        len(df) / 1440
    ns = int(shock.sum())
    out = dict(n_shock=ns, med_min_vol=all_min, med_sl=med_sl_all, days=float(days),
               med_vol=None, p25_vol=None)
    if ns > 0:
        shock_vol = dvol[shock]
        out["med_vol"] = float(np.median(shock_vol))
        out["p25_vol"] = float(np.percentile(shock_vol, 25))
        out["med_sl"] = float(np.median(
            np.maximum(P["sl_atr_mult"] * atr[shock], P["sl_floor"])))
    return out


def edge_weights():
    """Per-coin trades/yr and total_r from 12mo Binance (for retained-R weights)."""
    w = {}
    for sym in UNIVERSE + EXTRA:
        f = DATA / f"{sym}_USDT_Min1.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f).reset_index(drop=True)
        sig = STRAT.generate_signals(df, P)
        if sig.empty:
            continue
        t = engine_hl.simulate(df, sig, sym).df()
        if t.empty:
            continue
        w[sym] = dict(n=len(t), total_r=float(t["r"].sum()),
                      avg_r=float(t["r"].mean()))
    return w


def max_equity(shock_vol, sl_dist, risk):
    # notional = equity*risk/sl_dist  <=  CAP_FRAC*shock_vol
    return CAP_FRAC * shock_vol * sl_dist / risk


def main():
    lev = meta_lev()
    weights = edge_weights()
    coins = UNIVERSE + EXTRA
    rows = {}
    MIN_SHOCK = 5  # need >=5 shock minutes to trust a shock-median
    # Pass 1: pull candles + shock stats
    for sym in coins:
        hln = HL_NAME.get(sym, sym)
        df = hl_candles(hln)
        if df is None or len(df) < 300:
            print(f"[skip] {sym} ({hln}): NO HL CANDLES")
            continue
        s = shock_stats(df)
        s.update(hlname=hln, maxlev=lev.get(hln),
                 bn_n=weights.get(sym, {}).get("n", 0),
                 bn_r=weights.get(sym, {}).get("total_r", 0.0))
        rows[sym] = s

    # cross-coin shock/all-minute vol ratio (from well-sampled coins) to
    # ESTIMATE shock-minute vol where the 3.5d window gave <5 shocks
    ratios = [r["med_vol"] / r["med_min_vol"] for r in rows.values()
              if r["n_shock"] >= 10 and r["med_vol"] and r["med_min_vol"] > 0]
    R = float(np.median(ratios)) if ratios else 40.0
    print(f"cross-coin shock/all-min $vol ratio: median={R:.0f} "
          f"(n={len(ratios)} coins, range {min(ratios):.0f}-{max(ratios):.0f})")

    # two capacity bases:
    #  floor  = all-minute median $vol         (assumes we fill in a typical minute)
    #  shock  = measured shock-median (n>=5) OR all-min*R (estimated, flagged)
    for r in rows.values():
        r["floor_vol"] = r["med_min_vol"]
        if r["n_shock"] >= MIN_SHOCK and r["med_vol"]:
            r["shock_vol"], r["shk_est"] = r["med_vol"], False
        else:
            r["shock_vol"], r["shk_est"] = r["med_min_vol"] * R, True
        for basis in ("floor", "shock"):
            v = r[f"{basis}_vol"]
            r[f"{basis}_eq2"] = max_equity(v, r["med_sl"], 0.02)
            r[f"{basis}_eq1"] = max_equity(v, r["med_sl"], 0.01)

    print("\n=== PER-COIN HL LIQUIDITY & CAPACITY (venue-native, ~3.5d HL 1m) ===")
    print("floorVol=all-minute median $vol (robust n~5000); shockVol=shock-min "
          "median (n>=5) or all-min*ratio (est,*); maxEq at 2% risk, order<=10% vol")
    print(f"{'coin':7}{'lev':>4}{'nShk':>5}{'bnTrd':>6}{'bnR':>6}"
          f"{'floorVol$':>11}{'shockVol$':>12}{'medSL':>8}"
          f"{'floorEq2%':>11}{'shockEq2%':>11}")
    for sym in sorted(rows, key=lambda c: -rows[c]["shock_eq2"]):
        r = rows[sym]
        sv = f"{r['shock_vol']:,.0f}" + ("*" if r["shk_est"] else " ")
        print(f"{sym:7}{str(r['maxlev']):>4}{r['n_shock']:>5}{r['bn_n']:>6}"
              f"{r['bn_r']:>6.0f}{r['med_min_vol']:>11,.0f}{sv:>12}"
              f"{r['med_sl']:>8.4f}{r['floor_eq2']:>11,.0f}{r['shock_eq2']:>11,.0f}")

    tot_n = sum(r["bn_n"] for r in rows.values())
    tot_r = sum(r["bn_r"] for r in rows.values())
    print(f"\ntotal 12mo across {len(rows)} coins: trades={tot_n}, total_r={tot_r:.0f}")
    for basis, label in (("shock", "REALISTIC: fills on shock minute"),
                         ("floor", "CONSERVATIVE: fills on a typical minute")):
        for risk in RISK_LEVELS:
            key = f"{basis}_eq{2 if risk == 0.02 else 1}"
            print(f"\n=== SCALE [{label}] @ {int(risk*100)}% risk ===")
            print(f"{'equity':>9}{'effCoins':>9}{'trd%':>6}{'R%':>6}  droppedTopR")
            for eq in EQUITIES:
                ok = [c for c in rows if rows[c][key] >= eq]
                n_ret = sum(rows[c]["bn_n"] for c in ok)
                r_ret = sum(rows[c]["bn_r"] for c in ok)
                drop = sorted([c for c in rows if c not in ok],
                              key=lambda c: -rows[c]["bn_r"])
                print(f"{eq:>9,}{len(ok):>9}{100*n_ret/tot_n:>5.0f}%"
                      f"{100*r_ret/tot_r if tot_r else 0:>5.0f}%  "
                      f"{','.join(drop[:8])}")

    # save for report
    outp = Path("/tmp/claude-0/-home-user-hype/"
                "c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad/hl_capacity.json")
    json.dump({c: {k: v for k, v in r.items() if k != "days"}
               for c, r in rows.items()}, open(outp, "w"), indent=1)
    print(f"\nsaved {outp}")


if __name__ == "__main__":
    main()
