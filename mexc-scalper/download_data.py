"""Download MEXC USDT-perpetual Min1 klines and cache as parquet.

MEXC contract kline API: max 2000 candles per request, Min1 history ~30 days.
Usage: python3 download_data.py [--days 30] [--interval Min1]
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "https://contract.mexc.com/api/v1/contract"
DATA_DIR = Path(__file__).parent / "data"

# Liquid crypto perps (stocks/commodities/indices excluded), 0% maker fee verified per-contract on download
SYMBOLS = [
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "HYPE_USDT",
    "ZEC_USDT", "PEPE_USDT", "AVAX_USDT", "TAO_USDT", "DOGE_USDT",
    "SUI_USDT", "NEAR_USDT", "WLD_USDT", "PUMPFUN_USDT", "LTC_USDT",
    "BNB_USDT", "ADA_USDT", "LINK_USDT",
]

INTERVAL_SECONDS = {"Min1": 60, "Min5": 300, "Min15": 900, "Min60": 3600}


def get(url: str, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                out = json.load(r)
            if out.get("success"):
                return out
            raise RuntimeError(f"API error: {out}")
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def contract_detail(symbol: str) -> dict:
    return get(f"{BASE}/detail?symbol={symbol}")["data"]


def download_symbol(symbol: str, interval: str, days: int) -> pd.DataFrame:
    step = INTERVAL_SECONDS[interval]
    now = int(time.time())
    start = now - days * 86400
    frames = []
    cursor = start
    while cursor < now:
        end = min(cursor + 2000 * step, now)
        url = f"{BASE}/kline/{symbol}?interval={interval}&start={cursor}&end={end}"
        d = get(url)["data"]
        if d["time"]:
            frames.append(pd.DataFrame({
                "time": d["time"], "open": d["open"], "high": d["high"],
                "low": d["low"], "close": d["close"], "vol": d["vol"],
                "amount": d["amount"],
            }))
            cursor = d["time"][-1] + step
        else:
            cursor = end
        time.sleep(0.25)  # stay well under rate limits
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "vol", "amount"):
        df[c] = df[c].astype(float)
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="Min1")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    meta = {}
    for sym in SYMBOLS:
        out = DATA_DIR / f"{sym}_{args.interval}.parquet"
        detail = contract_detail(sym)
        meta[sym] = {
            "makerFeeRate": detail["makerFeeRate"],
            "takerFeeRate": detail["takerFeeRate"],
            "maxLeverage": detail["maxLeverage"],
            "priceUnit": detail["priceUnit"],
            "contractSize": detail["contractSize"],
        }
        df = download_symbol(sym, args.interval, args.days)
        df.to_parquet(out)
        print(f"{sym}: {len(df)} candles "
              f"({df['dt'].iloc[0]} .. {df['dt'].iloc[-1]}) "
              f"maker={detail['makerFeeRate']} taker={detail['takerFeeRate']} "
              f"maxLev={detail['maxLeverage']}", flush=True)
    (DATA_DIR / "contracts.json").write_text(json.dumps(meta, indent=2))
    print("done")


if __name__ == "__main__":
    main()
