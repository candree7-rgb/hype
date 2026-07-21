"""Same-window triple-venue equivalence test (Hyperliquid transfer study).

On the identical ~3-day window (intersection of data_hl/, data_bin3d/ and
data/ per coin), run BOTH configs:
  - RAW  = strategies/shock_candle_overshoot_catch.py (no ATR gate)
  - GATE = strategies/shock_hl_variant.py (atr_min=0.001 gate, frozen)
on all three venues' candles with the SAME cost model (engine_hl fees:
maker 0.015% / taker 0.045% / slip 0.03%).

Outputs per-coin and pooled: signal counts, fills, fill rate, WR, avg_r,
stop-distance (3*ATR at signal) distribution, plus the HL/Binance
microstructure map (ATR ratio on matched minutes, shock rates, gate
survival). Saves trades to the scratchpad for downstream use.

Read-only wrt existing modules; engine_hl is imported, not modified.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import engine_hl  # noqa: E402  (HL cost model; simulate() used for all venues)

SCRATCH = Path("/tmp/claude-0/-home-user-hype/c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad")

COINS = ["ADA", "AVAX", "BNB", "BTC", "DOGE", "ETH", "HYPE", "LINK", "LTC",
         "NEAR", "PEPE", "PUMPFUN", "SOL", "SUI", "TAO", "WLD", "XRP", "ZEC"]

VENUES = {
    "HL": lambda s: BASE / "data_hl" / f"{s}_Min1.parquet",
    "BIN": lambda s: BASE / "data_bin3d" / f"{s}_Min1.parquet",
    "MEXC": lambda s: BASE / "data" / f"{s}_USDT_Min1.parquet",
}

RAW = engine_hl.load_strategy(str(BASE / "strategies" / "shock_candle_overshoot_catch.py"))
GATE = engine_hl.load_strategy(str(BASE / "strategies" / "shock_hl_variant.py"))
CONFIGS = {"raw": (RAW, RAW.DEFAULT_PARAMS), "gate": (GATE, GATE.DEFAULT_PARAMS)}

ATR_N, WARMUP, SHOCK_ATR, ATR_MIN = 60, 150, 4.0, 0.001


def frac_atr(df: pd.DataFrame) -> np.ndarray:
    high, low, close = (df[c].to_numpy(float) for c in ("high", "low", "close"))
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(ATR_N).mean().to_numpy() / close


def shock_mask(df: pd.DataFrame, atr: np.ndarray) -> np.ndarray:
    rng = ((df["high"] - df["low"]) / df["close"]).to_numpy(float)
    m = np.zeros(len(df), dtype=bool)
    ok = np.isfinite(atr)
    m[ok] = rng[ok] / atr[ok] > SHOCK_ATR
    m[: WARMUP + 1] = False
    return m


def stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n": 0, "wr": None, "avg_r": None, "total_r": 0.0}
    wr = float((trades["r"] > 0).mean())
    return {"n": int(len(trades)), "wr": round(wr, 4),
            "avg_r": round(float(trades["r"].mean()), 4),
            "total_r": round(float(trades["r"].sum()), 2),
            "sd_r": round(float(trades["r"].std(ddof=1)), 3) if len(trades) > 1 else None}


def main():
    # ---- load + intersect windows per coin -------------------------------
    data = {}          # (venue, coin) -> sliced df
    windows = {}
    for coin in COINS:
        dfs = {}
        for v, pathfn in VENUES.items():
            df = pd.read_parquet(pathfn(coin))
            dfs[v] = df
        t0 = max(df["time"].min() for df in dfs.values())
        t1 = min(df["time"].max() for df in dfs.values())
        windows[coin] = (t0, t1)
        for v, df in dfs.items():
            sl = df[(df["time"] >= t0) & (df["time"] <= t1)].reset_index(drop=True)
            data[(v, coin)] = sl

    days = np.mean([(t1 - t0) / 86400 for t0, t1 in windows.values()])
    eff_days = days - (WARMUP + 1) / 1440  # warmup consumes the window start
    print(f"common window: mean {days:.3f} days raw, {eff_days:.3f} days post-warmup")
    for coin in COINS[:1]:
        t0, t1 = windows[coin]
        print(f"  e.g. {coin}: {pd.to_datetime(t0, unit='s', utc=True)} -> "
              f"{pd.to_datetime(t1, unit='s', utc=True)}")
    for v in VENUES:
        lens = {coin: len(data[(v, coin)]) for coin in COINS}
        miss = {c: (windows[c][1] - windows[c][0]) // 60 + 1 - n
                for c, n in lens.items() if (windows[c][1] - windows[c][0]) // 60 + 1 - n > 0}
        print(f"  {v}: missing-minute coins: {miss if miss else 'none'}")

    # ---- run both configs on all venues ----------------------------------
    all_trades = {}
    rows = []
    for cfg_name, (strat, params) in CONFIGS.items():
        for v in VENUES:
            tr_frames, sigs, sig_sl = [], 0, []
            per_coin = {}
            for coin in COINS:
                df = data[(v, coin)]
                sig = strat.generate_signals(df, params)
                res = engine_hl.simulate(df, sig, coin)
                t = res.df()
                if not t.empty:
                    tr_frames.append(t)
                sigs += len(sig)
                sig_sl.extend(sig["sl_dist"].tolist())
                per_coin[coin] = {"signals": len(sig),
                                  "fills": 0 if t.empty else len(t),
                                  **({} if t.empty else
                                     {"wr": round(float((t["r"] > 0).mean()), 3),
                                      "avg_r": round(float(t["r"].mean()), 3)})}
            trades = pd.concat(tr_frames) if tr_frames else pd.DataFrame(columns=["r"])
            all_trades[(cfg_name, v)] = trades
            s = stats(trades)
            sl = np.array(sig_sl)
            rows.append({
                "config": cfg_name, "venue": v, "signals": sigs,
                "signals_per_day_per_coin": round(sigs / eff_days / len(COINS), 3),
                "fills": s["n"],
                "fill_rate": round(s["n"] / sigs, 3) if sigs else None,
                "wr": s["wr"], "avg_r": s["avg_r"], "total_r": s["total_r"],
                "sd_r": s.get("sd_r"),
                "sl_med_pct": round(float(np.median(sl)) * 100, 3) if len(sl) else None,
                "sl_q25_pct": round(float(np.quantile(sl, .25)) * 100, 3) if len(sl) else None,
                "sl_q75_pct": round(float(np.quantile(sl, .75)) * 100, 3) if len(sl) else None,
            })
            print(f"[{cfg_name}/{v}] per-coin:",
                  json.dumps(per_coin, separators=(",", ":")))

    print("\n=== POOLED (same window, same coins, HL fee model everywhere) ===")
    print(pd.DataFrame(rows).to_string(index=False))

    # ---- microstructure map: matched-minute ATR + shock rates ------------
    print("\n=== microstructure map (matched minutes) ===")
    map_rows = []
    for coin in COINS:
        merged = {}
        atrs, shocks, gates = {}, {}, {}
        for v in VENUES:
            df = data[(v, coin)]
            a = frac_atr(df)
            sm = shock_mask(df, a)
            atrs[v] = pd.Series(a, index=df["time"].values)
            shocks[v] = int(sm.sum())
            gates[v] = int((sm & (a >= ATR_MIN)).sum())
        common = atrs["HL"].index.intersection(atrs["BIN"].index).intersection(
            atrs["MEXC"].index)
        hb = (atrs["HL"].loc[common] / atrs["BIN"].loc[common]).dropna()
        mb = (atrs["MEXC"].loc[common] / atrs["BIN"].loc[common]).dropna()
        map_rows.append({
            "coin": coin,
            "atr_HL/BIN": round(float(hb.median()), 3),
            "atr_MEXC/BIN": round(float(mb.median()), 3),
            "shocks_HL": shocks["HL"], "shocks_BIN": shocks["BIN"],
            "shocks_MEXC": shocks["MEXC"],
            "gate_shocks_HL": gates["HL"], "gate_shocks_BIN": gates["BIN"],
            "gate_shocks_MEXC": gates["MEXC"],
        })
    mdf = pd.DataFrame(map_rows)
    print(mdf.to_string(index=False))
    tot = mdf[[c for c in mdf.columns if c.startswith(("shocks", "gate"))]].sum()
    print("\npooled shock (pre-cooldown) counts:", tot.to_dict())
    print("ATR ratio HL/BIN: median-of-coin-medians =",
          round(float(mdf["atr_HL/BIN"].median()), 3))
    print("shock rate ratio HL/BIN (all)  =",
          round(tot["shocks_HL"] / tot["shocks_BIN"], 3))
    print("shock rate ratio HL/BIN (gate) =",
          round(tot["gate_shocks_HL"] / tot["gate_shocks_BIN"], 3))

    # ---- save trades for downstream transfer math ------------------------
    for (cfg, v), t in all_trades.items():
        if not t.empty:
            t.to_parquet(SCRATCH / f"eq_trades_{cfg}_{v}.parquet")
    print("\ntrades saved to scratchpad (eq_trades_<cfg>_<venue>.parquet)")


if __name__ == "__main__":
    main()
