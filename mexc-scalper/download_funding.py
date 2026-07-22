#!/usr/bin/env python3
"""Download Binance USDT-M futures funding rates + 1d/4h klines (monthly files).

Output: data_funding/funding_<SYM>.parquet, klines_1d_<SYM>.parquet, klines_4h_<SYM>.parquet
Period: 2023-07 .. 2026-06 (36 months). Missing months (pre-listing) are skipped.
"""
import io
import os
import sys
import zipfile
import concurrent.futures as cf

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/monthly"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_funding")
os.makedirs(OUT, exist_ok=True)

SYMBOLS = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "NEAR", "SUI",
    "WLD", "ENA", "TAO", "TON", "APT", "ARB", "OP", "INJ", "FET", "LTC",
    "BNB", "XLM", "AAVE", "UNI", "1000PEPE", "1000BONK", "WIF", "JUP", "TIA", "SEI",
]

MONTHS = [f"{y}-{m:02d}" for y in (2023, 2024, 2025, 2026) for m in range(1, 13)
          if f"{y}-{m:02d}" >= "2023-07" and f"{y}-{m:02d}" <= "2026-06"]

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]

session = requests.Session()


def fetch_csv(url):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            name = zf.namelist()[0]
            return zf.read(name)
        except Exception as e:
            if attempt == 2:
                print(f"FAIL {url}: {e}", file=sys.stderr)
                return None
    return None


def parse_kline(raw):
    first = raw.split(b"\n", 1)[0]
    header = 0 if first.startswith(b"open_time") else None
    df = pd.read_csv(io.BytesIO(raw), header=header, names=None if header == 0 else KLINE_COLS)
    df.columns = KLINE_COLS
    return df[["open_time", "open", "high", "low", "close", "volume", "quote_volume",
               "taker_buy_volume"]]


def parse_funding(raw):
    first = raw.split(b"\n", 1)[0]
    header = 0 if b"calc_time" in first or b"fund" in first.lower() else None
    names = ["calc_time", "funding_interval_hours", "last_funding_rate"]
    df = pd.read_csv(io.BytesIO(raw), header=header, names=None if header == 0 else names)
    df.columns = names[: len(df.columns)]
    return df


def do_symbol(sym):
    pair = sym + "USDT"
    results = {}
    # funding
    frames = []
    for m in MONTHS:
        raw = fetch_csv(f"{BASE}/fundingRate/{pair}/{pair}-fundingRate-{m}.zip")
        if raw:
            frames.append(parse_funding(raw))
    if frames:
        df = pd.concat(frames, ignore_index=True).sort_values("calc_time").drop_duplicates("calc_time")
        df.to_parquet(os.path.join(OUT, f"funding_{sym}.parquet"), index=False)
        results["funding"] = len(df)
    for interval in ("1d", "4h"):
        frames = []
        for m in MONTHS:
            raw = fetch_csv(f"{BASE}/klines/{pair}/{interval}/{pair}-{interval}-{m}.zip")
            if raw:
                frames.append(parse_kline(raw))
        if frames:
            df = pd.concat(frames, ignore_index=True).sort_values("open_time").drop_duplicates("open_time")
            df.to_parquet(os.path.join(OUT, f"klines_{interval}_{sym}.parquet"), index=False)
            results[interval] = len(df)
    print(f"{sym}: {results}")
    return sym, results


if __name__ == "__main__":
    syms = sys.argv[1:] or SYMBOLS
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(do_symbol, syms))
    print("done")
