"""Download 3 years (2023-07 .. 2026-06) of Binance USDT-M futures daily AND 4h
klines from data.binance.vision monthly zips for the TSMOM/trend research round.

Output: data_tsmom/<BSYM>_1d.parquet and <BSYM>_4h.parquet
Columns: time (s), open, high, low, close, vol (base), amount (quote), dt (UTC).
Missing months (pre-listing) are skipped silently; listing date reported.
"""
import io
import json
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent / "data_tsmom"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "LINKUSDT", "AVAXUSDT", "NEARUSDT", "SUIUSDT", "WLDUSDT", "TAOUSDT",
    "TONUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "FETUSDT",
    "LTCUSDT", "BNBUSDT", "XLMUSDT", "AAVEUSDT", "UNIUSDT", "1000PEPEUSDT",
    "1000BONKUSDT", "WIFUSDT", "JUPUSDT", "TIAUSDT", "SEIUSDT", "ENAUSDT",
]

MONTHS = [f"{y}-{m:02d}" for y in (2023, 2024, 2025, 2026)
          for m in range(1, 13)
          if (y, m) >= (2023, 7) and (y, m) <= (2026, 6)]

CSV_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "count", "taker_buy_vol", "taker_buy_quote", "ignore"]


def fetch_month(bsym: str, tf: str, month: str) -> pd.DataFrame | None:
    url = (f"https://data.binance.vision/data/futures/um/monthly/klines/"
           f"{bsym}/{tf}/{bsym}-{tf}-{month}.zip")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # not listed yet / no data
            if attempt == 3:
                print(f"  {bsym} {tf} {month}: FAILED {e}", flush=True)
                return None
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == 3:
                print(f"  {bsym} {tf} {month}: FAILED {e}", flush=True)
                return None
            time.sleep(2 ** attempt)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    with zf.open(zf.namelist()[0]) as f:
        first = f.readline().decode()
        header = 0 if first.startswith("open_time") else None
    with zf.open(zf.namelist()[0]) as f:
        df = pd.read_csv(f, header=header, names=None if header == 0 else CSV_COLS)
    df.columns = CSV_COLS[: len(df.columns)]
    return df


def build(bsym: str, tf: str) -> str:
    out = OUT / f"{bsym}_{tf}.parquet"
    if out.exists():
        return f"{bsym} {tf}: exists, skip"
    frames = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for df in ex.map(lambda m: fetch_month(bsym, tf, m), MONTHS):
            if df is not None:
                frames.append(df)
    if not frames:
        return f"{bsym} {tf}: NO DATA"
    df = pd.concat(frames)
    ot = df["open_time"].astype("int64")
    # normalize ms/us to seconds per-row (dumps switched units in 2025)
    secs = ot.where(ot < 10**14, ot // 1000) // 1000
    res = pd.DataFrame({
        "time": secs.astype("int64"),
        "open": df["open"].astype(float), "high": df["high"].astype(float),
        "low": df["low"].astype(float), "close": df["close"].astype(float),
        "vol": df["volume"].astype(float),
        "amount": df["quote_volume"].astype(float),
    }).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    res["dt"] = pd.to_datetime(res["time"], unit="s", utc=True)
    res.to_parquet(out)
    return (f"{bsym} {tf}: {len(res)} candles "
            f"({res['dt'].iloc[0].date()} .. {res['dt'].iloc[-1].date()})")


def main():
    OUT.mkdir(exist_ok=True)
    listing = {}
    for bsym in SYMBOLS:
        for tf in ("1d", "4h"):
            msg = build(bsym, tf)
            print(msg, flush=True)
        f = OUT / f"{bsym}_1d.parquet"
        if f.exists():
            listing[bsym] = str(pd.read_parquet(f)["dt"].iloc[0].date())
    (OUT / "listing_dates.json").write_text(json.dumps(listing, indent=1))
    print("done", flush=True)


if __name__ == "__main__":
    main()
