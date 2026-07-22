"""Download 3 years (2023-07 .. 2026-06) of Binance USDT-M futures DAILY klines
from data.binance.vision for the cross-sectional momentum (xsmom) research.

Candidate universe: ~50 of the most liquid USDT perps, deliberately including
coins that were liquid in 2023-2025 but later delisted/renamed (MATIC, etc.)
to avoid survivorship bias. Months that 404 (pre-listing / post-delisting)
are simply skipped; the engine applies point-in-time eligibility.

Output: data_xsmom/<SYM>.parquet with columns
  dt (UTC date), open, high, low, close, vol, quote_vol, trades
"""
import io
import time
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent / "data_xsmom"

SYMBOLS = [
    # majors
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT", "DOTUSDT",
    "TRXUSDT", "ATOMUSDT", "UNIUSDT", "NEARUSDT", "FILUSDT", "ETCUSDT",
    "XLMUSDT",
    # liquid alts (incl. later-delisted MATIC for anti-survivorship)
    "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT", "SEIUSDT",
    "TIAUSDT", "LDOUSDT", "AAVEUSDT", "MKRUSDT", "CRVUSDT", "RUNEUSDT",
    "FETUSDT", "WLDUSDT", "ORDIUSDT", "1000PEPEUSDT", "1000SHIBUSDT",
    "1000BONKUSDT", "ICPUSDT", "HBARUSDT", "STXUSDT", "IMXUSDT", "GRTUSDT",
    "ALGOUSDT", "SANDUSDT", "EOSUSDT", "MATICUSDT", "GALAUSDT",
    # 2024 listings (partial history; point-in-time eligibility handles it)
    "JUPUSDT", "WIFUSDT", "ENAUSDT", "TAOUSDT", "TONUSDT",
]

MONTHS = [f"{y}-{m:02d}" for y, ms in
          ((2023, range(7, 13)), (2024, range(1, 13)),
           (2025, range(1, 13)), (2026, range(1, 7)))
          for m in ms]

CSV_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "count", "taker_buy_vol", "taker_buy_quote", "ignore"]


def fetch_month(bsym: str, month: str) -> pd.DataFrame | None:
    url = (f"https://data.binance.vision/data/futures/um/monthly/klines/"
           f"{bsym}/1d/{bsym}-1d-{month}.zip")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None            # not listed that month
            if attempt == 3:
                print(f"  {bsym} {month}: FAILED {e}", flush=True)
                return None
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == 3:
                print(f"  {bsym} {month}: FAILED {e}", flush=True)
                return None
            time.sleep(2 ** attempt)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    with zf.open(zf.namelist()[0]) as f:
        first = f.readline().decode()
    header = 0 if first.startswith("open_time") else None
    with zf.open(zf.namelist()[0]) as f:
        df = pd.read_csv(f, header=header,
                         names=None if header == 0 else CSV_COLS)
    df.columns = CSV_COLS[: len(df.columns)]
    return df


def fetch_symbol(bsym: str):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        frames = [d for d in ex.map(lambda m: fetch_month(bsym, m), MONTHS)
                  if d is not None]
    return frames


def main():
    OUT.mkdir(exist_ok=True)
    for bsym in SYMBOLS:
        out = OUT / f"{bsym}.parquet"
        if out.exists():
            print(f"{bsym}: exists, skip", flush=True)
            continue
        frames = fetch_symbol(bsym)
        if not frames:
            print(f"{bsym}: NO DATA", flush=True)
            continue
        df = pd.concat(frames)
        ot = df["open_time"].astype("int64")
        unit_div = 10**6 if ot.iloc[0] > 10**14 else 10**3
        res = pd.DataFrame({
            "time": (ot // unit_div).astype("int64"),
            "open": df["open"].astype(float), "high": df["high"].astype(float),
            "low": df["low"].astype(float), "close": df["close"].astype(float),
            "vol": df["volume"].astype(float),
            "quote_vol": df["quote_volume"].astype(float),
            "trades": df["count"].astype("int64"),
        }).drop_duplicates("time").sort_values("time").reset_index(drop=True)
        res["dt"] = pd.to_datetime(res["time"], unit="s", utc=True)
        res.to_parquet(out)
        print(f"{bsym}: {len(res)} days ({res['dt'].iloc[0].date()} .. "
              f"{res['dt'].iloc[-1].date()})", flush=True)
    print("done")


if __name__ == "__main__":
    main()
