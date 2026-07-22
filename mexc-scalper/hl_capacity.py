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
    print("=== HL SHOCK-MINUTE LIQUIDITY (venue-native, ~3.5d of HL 1m) ===")
    print(f"{'coin':7}{'HLname':8}{'maxLev':>7}{'nShock':>7}{'medShk$':>12}"
          f"{'p25Shk$':>12}{'medSL':>8}{'maxEq2%':>12}{'maxEq1%':>12}")
    for sym in coins:
        hln = HL_NAME.get(sym, sym)
        df = hl_candles(hln)
        if df is None or len(df) < 300:
            print(f"{sym:7}{hln:8}  NO HL CANDLES")
            continue
        s = shock_stats(df)
        if s is None:
            print(f"{sym:7}{hln:8} no shock minutes in window")
            continue
        me2 = max_equity(s["med_vol"], s["med_sl"], 0.02)
        me1 = max_equity(s["med_vol"], s["med_sl"], 0.01)
        # conservative: p25 shock vol
        me2_p25 = max_equity(s["p25_vol"], s["med_sl"], 0.02)
        s.update(maxlev=lev.get(hln), maxeq2=me2, maxeq1=me1, maxeq2_p25=me2_p25,
                 hlname=hln)
        rows[sym] = s
        print(f"{sym:7}{hln:8}{str(lev.get(hln)):>7}{s['n_shock']:>7}"
              f"{s['med_vol']:>12,.0f}{s['p25_vol']:>12,.0f}{s['med_sl']:>8.4f}"
              f"{me2:>12,.0f}{me1:>12,.0f}")

    # Aggregate: effective coins & retained R vs equity
    tot_n = sum(weights.get(c, {}).get("n", 0) for c in rows)
    tot_r = sum(weights.get(c, {}).get("total_r", 0) for c in rows)
    print(f"\ntotal 12mo trades across analyzed coins: {tot_n}, total_r {tot_r:.0f}")
    for risk in RISK_LEVELS:
        key = "maxeq2" if risk == 0.02 else "maxeq1"
        print(f"\n=== SCALE @ {int(risk*100)}% risk (order<=10% shock-min $vol, "
              f"median) ===")
        print(f"{'equity':>10}{'effCoins':>10}{'trades/yr%':>12}{'totR%':>10}"
              f"  dropped")
        for eq in EQUITIES:
            ok = [c for c in rows if rows[c][key] >= eq]
            n_ret = sum(weights.get(c, {}).get("n", 0) for c in ok)
            r_ret = sum(weights.get(c, {}).get("total_r", 0) for c in ok)
            dropped = [c for c in rows if c not in ok]
            print(f"{eq:>10,}{len(ok):>10}{100*n_ret/tot_n:>11.0f}%"
                  f"{100*r_ret/tot_r if tot_r else 0:>9.0f}%  "
                  f"{','.join(sorted(dropped, key=lambda c: -rows[c][key]))[:80]}")

    # save for report
    outp = Path("/tmp/claude-0/-home-user-hype/"
                "c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad/hl_capacity.json")
    json.dump({c: {k: v for k, v in r.items() if k != "days"}
               for c, r in rows.items()}, open(outp, "w"), indent=1)
    print(f"\nsaved {outp}")


if __name__ == "__main__":
    main()
