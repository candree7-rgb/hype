"""Download the same ~3.5-day window as data_hl/ from Binance USDT-M daily
1m kline zips (data.binance.vision), for the 18 coins shared with data_hl/.

Output: data_bin3d/<HL_NAME>_Min1.parquet in engine format (time, open, high,
low, close, vol, amount, dt) using HL-style symbol names (BTC_Min1.parquet).

Days 2026-07-17 .. 2026-07-21 are attempted; a 404 (day not yet published)
is skipped with a notice. New files only — does not touch existing engines
or data dirs.
"""
import io
import time
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent / "data_bin3d"

# Binance symbol -> HL-style name (matches data_hl/ file stems)
SYMBOLS = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "XRPUSDT": "XRP",
    "HYPEUSDT": "HYPE", "ZECUSDT": "ZEC", "1000PEPEUSDT": "PEPE",
    "AVAXUSDT": "AVAX", "TAOUSDT": "TAO", "DOGEUSDT": "DOGE",
    "SUIUSDT": "SUI", "NEARUSDT": "NEAR", "WLDUSDT": "WLD",
    "PUMPUSDT": "PUMPFUN", "LTCUSDT": "LTC", "BNBUSDT": "BNB",
    "ADAUSDT": "ADA", "LINKUSDT": "LINK",
}

DAYS = ["2026-07-17", "2026-07-18", "2026-07-19", "2026-07-20", "2026-07-21"]

CSV_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "count", "taker_buy_vol", "taker_buy_quote", "ignore"]


def fetch_day(bsym: str, day: str) -> pd.DataFrame | None:
    url = (f"https://data.binance.vision/data/futures/um/daily/klines/"
           f"{bsym}/1m/{bsym}-1m-{day}.zip")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  {bsym} {day}: 404 (not published), skip", flush=True)
                return None
            if attempt == 3:
                print(f"  {bsym} {day}: FAILED {e}", flush=True)
                return None
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == 3:
                print(f"  {bsym} {day}: FAILED {e}", flush=True)
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


def main():
    OUT.mkdir(exist_ok=True)
    for bsym, name in SYMBOLS.items():
        out = OUT / f"{name}_Min1.parquet"
        if out.exists():
            print(f"{name}: exists, skip", flush=True)
            continue
        frames = [d for day in DAYS if (d := fetch_day(bsym, day)) is not None]
        if not frames:
            print(f"{name}: NO DATA", flush=True)
            continue
        df = pd.concat(frames)
        ot = df["open_time"].astype("int64")
        unit_div = 10**6 if ot.iloc[0] > 10**14 else 10**3
        res = pd.DataFrame({
            "time": (ot // unit_div).astype("int64"),
            "open": df["open"].astype(float), "high": df["high"].astype(float),
            "low": df["low"].astype(float), "close": df["close"].astype(float),
            "vol": df["volume"].astype(float),
            "amount": df["quote_volume"].astype(float),
        }).drop_duplicates("time").sort_values("time").reset_index(drop=True)
        res["dt"] = pd.to_datetime(res["time"], unit="s", utc=True)
        res.to_parquet(out)
        print(f"{name}: {len(res)} candles ({res['dt'].iloc[0]} .. {res['dt'].iloc[-1]})",
              flush=True)
    print("done")


if __name__ == "__main__":
    main()
