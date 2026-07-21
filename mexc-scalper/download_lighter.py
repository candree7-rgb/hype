"""Download 12+ months of Lighter 1m candles (public REST, max 500/call).

Endpoint: GET mainnet.zklighter.elliot.ai/api/v1/candles
  ?market_id=<int>&resolution=1m&start_timestamp=<s>&end_timestamp=<s>&count_back=N
Market ids come from /api/v1/orderBooks. Output: data_lighter/<NAME>_Min1.parquet
in engine format.
"""
import json
import time
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
OUT = Path(__file__).parent / "data_lighter"

# Lighter symbol -> engine name (align with data_binance naming)
SYMBOLS = {
    "BTC": "BTC_USDT", "ETH": "ETH_USDT", "SOL": "SOL_USDT", "XRP": "XRP_USDT",
    "HYPE": "HYPE_USDT", "ZEC": "ZEC_USDT", "1000PEPE": "PEPE_USDT",
    "AVAX": "AVAX_USDT", "TAO": "TAO_USDT", "DOGE": "DOGE_USDT",
    "SUI": "SUI_USDT", "NEAR": "NEAR_USDT", "WLD": "WLD_USDT",
    "PUMP": "PUMPFUN_USDT", "LTC": "LTC_USDT", "ADA": "ADA_USDT",
    "LINK": "LINK_USDT", "FARTCOIN": "FARTCOIN_USDT", "1000BONK": "1000BONK_USDT",
    "ONDO": "ONDO_USDT",
}

DAYS = 385  # bis Juni 2025, soweit vorhanden


def get(url: str, retries: int = 5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def market_ids() -> dict:
    d = get(f"{BASE}/orderBooks")
    return {m["symbol"]: m["market_id"] for m in d["order_books"]}


def download(mid: int, name: str) -> pd.DataFrame:
    now = int(time.time())
    start = now - DAYS * 86400
    frames = []
    cursor = start
    step = 500 * 60
    while cursor < now:
        end = min(cursor + step, now)
        d = get(f"{BASE}/candles?market_id={mid}&resolution=1m"
                f"&start_timestamp={cursor}&end_timestamp={end}&count_back=500")
        c = d.get("candlesticks") or d.get("candles") or []
        if c:
            frames.append(pd.DataFrame(c))
            last_t = int(c[-1]["timestamp"] if "timestamp" in c[-1] else c[-1]["t"])
            last_s = last_t // 1000 if last_t > 10**12 else last_t
            cursor = max(last_s + 60, cursor + step)
        else:
            cursor = end
        time.sleep(0.12)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    tcol = "timestamp" if "timestamp" in df.columns else "t"
    ts = df[tcol].astype("int64")
    ts = (ts // 1000).where(ts > 10**12, ts)
    ren = {"open": "open", "high": "high", "low": "low", "close": "close"}
    for a, b in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
        if a in df.columns:
            ren[a] = b
    out = pd.DataFrame({"time": ts.astype("int64")})
    for src, dst in ren.items():
        if src in df.columns:
            out[dst] = df[src].astype(float)
    vol = df["v"] if "v" in df.columns else df.get("base_volume", df.get("volume"))
    quote = df["V"] if "V" in df.columns else df.get("quote_volume")
    out["vol"] = pd.to_numeric(vol, errors="coerce")
    out["amount"] = (pd.to_numeric(quote, errors="coerce")
                     if quote is not None else out["vol"] * out["close"])
    out = out.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    out["dt"] = pd.to_datetime(out["time"], unit="s", utc=True)
    return out


def main():
    OUT.mkdir(exist_ok=True)
    mids = market_ids()
    for sym, name in SYMBOLS.items():
        f = OUT / f"{name}_Min1.parquet"
        if f.exists():
            print(f"{name}: exists, skip", flush=True)
            continue
        if sym not in mids:
            print(f"{name}: NOT LISTED on Lighter", flush=True)
            continue
        df = download(mids[sym], name)
        if df.empty:
            print(f"{name}: no data", flush=True)
            continue
        df.to_parquet(f)
        print(f"{name}: {len(df)} candles ({df['dt'].iloc[0]} .. {df['dt'].iloc[-1]})",
              flush=True)
    print("done")


if __name__ == "__main__":
    main()
