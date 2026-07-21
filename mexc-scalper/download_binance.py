"""Download 12 months of Binance USDT-M futures 1m klines from
data.binance.vision (public bulk CSVs) for cross-venue validation.

Space-efficient: downloads one monthly zip at a time, converts, deletes.
Output: data_binance/<MEXC_SYMBOL>_Min1.parquet with the same columns as the
MEXC data so engine.py works unchanged (point DATA_DIR at data_binance).
"""
import io
import time
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent / "data_binance"

# Binance symbol -> MEXC-style name used by the engine
SYMBOLS = {
    "BTCUSDT": "BTC_USDT", "ETHUSDT": "ETH_USDT", "SOLUSDT": "SOL_USDT",
    "XRPUSDT": "XRP_USDT", "HYPEUSDT": "HYPE_USDT", "ZECUSDT": "ZEC_USDT",
    "1000PEPEUSDT": "PEPE_USDT", "AVAXUSDT": "AVAX_USDT", "TAOUSDT": "TAO_USDT",
    "DOGEUSDT": "DOGE_USDT", "SUIUSDT": "SUI_USDT", "NEARUSDT": "NEAR_USDT",
    "WLDUSDT": "WLD_USDT", "PUMPUSDT": "PUMPFUN_USDT", "LTCUSDT": "LTC_USDT",
    "BNBUSDT": "BNB_USDT", "ADAUSDT": "ADA_USDT", "LINKUSDT": "LINK_USDT",
}

MONTHS = [f"{y}-{m:02d}" for y, ms in ((2025, range(7, 13)), (2026, range(1, 7)))
          for m in ms]

CSV_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "count", "taker_buy_vol", "taker_buy_quote", "ignore"]


def fetch_month(bsym: str, month: str) -> pd.DataFrame | None:
    url = (f"https://data.binance.vision/data/futures/um/monthly/klines/"
           f"{bsym}/1m/{bsym}-1m-{month}.zip")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                raw = r.read()
            break
        except Exception as e:
            if attempt == 3:
                print(f"  {month}: FAILED {e}", flush=True)
                return None
            time.sleep(2 ** attempt)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    with zf.open(zf.namelist()[0]) as f:
        first = f.readline().decode()
        f.seek(0) if hasattr(f, "seek") else None
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
        frames = []
        for month in MONTHS:
            df = fetch_month(bsym, month)
            if df is not None:
                frames.append(df)
        if not frames:
            continue
        df = pd.concat(frames)
        # open_time is ms (or us in newer dumps) — normalize to seconds
        ot = df["open_time"].astype("int64")
        unit = "us" if ot.iloc[0] > 10**14 else "ms"
        res = pd.DataFrame({
            "time": (ot // (10**6 if unit == "us" else 10**3)).astype("int64"),
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
