"""Download 12mo Binance USDT-M 1m klines for CANDIDATE expansion coins.

Same approach/columns as download_binance.py (monthly zips, one at a time).
Writes data_binance/<NAME>_USDT_Min1.parquet. Never overwrites existing files.
Skips cleanly on missing months; reports coverage so partial coins can be
excluded from the 12mo validation.
"""
import io
import time
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent / "data_binance"

# Binance USDT-M perp symbol -> HL/engine name
SYMBOLS = {
    "ENAUSDT": "ENA", "ONDOUSDT": "ONDO", "AAVEUSDT": "AAVE", "ARBUSDT": "ARB",
    "INJUSDT": "INJ", "OPUSDT": "OP", "FETUSDT": "FET", "LDOUSDT": "LDO",
    "UNIUSDT": "UNI", "WIFUSDT": "WIF", "TIAUSDT": "TIA", "JUPUSDT": "JUP",
    "XMRUSDT": "XMR", "POLUSDT": "POL", "PENGUUSDT": "PENGU", "ENSUSDT": "ENS",
    "SEIUSDT": "SEI", "TONUSDT": "TON", "APTUSDT": "APT",
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
        header = 0 if first.startswith("open_time") else None
    with zf.open(zf.namelist()[0]) as f:
        df = pd.read_csv(f, header=header, names=None if header == 0 else CSV_COLS)
    df.columns = CSV_COLS[: len(df.columns)]
    return df


def main():
    OUT.mkdir(exist_ok=True)
    summary = []
    for bsym, name in SYMBOLS.items():
        out = OUT / f"{name}_USDT_Min1.parquet"
        if out.exists():
            print(f"{name}: exists, skip", flush=True)
            summary.append((name, "exists", None, None))
            continue
        frames, got_months = [], 0
        for month in MONTHS:
            df = fetch_month(bsym, month)
            if df is not None:
                frames.append(df)
                got_months += 1
        if not frames:
            print(f"{name}: NO DATA (skip)", flush=True)
            summary.append((name, "missing", 0, None))
            continue
        df = pd.concat(frames)
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
        print(f"{name}: {len(res)} candles, {got_months}/12 months "
              f"({res['dt'].iloc[0]} .. {res['dt'].iloc[-1]})", flush=True)
        summary.append((name, "ok", got_months, len(res)))
    print("\n=== SUMMARY ===")
    for name, status, months, n in summary:
        print(f"  {name}: {status} months={months} candles={n}")
    print("done")


if __name__ == "__main__":
    main()
