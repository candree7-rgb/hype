"""Hyperliquid executor — paper (default) or live.

Subscribes to 1m candles for the validated universe over the public
Hyperliquid websocket, runs the frozen hl_native_shock_freq signal engine,
records every closed candle to build venue-native history, and serves a
/status endpoint.

Paper (default): simulates fills with backtest-identical rules. No keys.
    python3 main.py
Live: routes real orders via the hyperliquid-python-sdk (see live.py).
    PAPER=false HL_PRIVATE_KEY=0x... [HL_ACCOUNT_ADDRESS=0x...] \
    [HL_TESTNET=true] python3 main.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

import websockets

from config import CFG
from paper import PaperBroker
from strategy import Candle, SignalEngine


class Recorder:
    """Appends closed candles as JSONL, one file per coin per UTC day."""

    def __init__(self) -> None:
        self.dir = Path(CFG.record_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def record(self, coin: str, c: Candle) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(c.t))
        p = self.dir / f"{coin}_{day}.jsonl"
        with p.open("a") as f:
            f.write(json.dumps({"t": c.t, "o": c.open, "h": c.high,
                                "l": c.low, "c": c.close, "v": c.vol}) + "\n")


class Executor:
    def __init__(self) -> None:
        self.engine = SignalEngine()
        if CFG.paper:
            self.broker = PaperBroker()
        else:
            from live import LiveBroker   # lazy: SDK only needed for live
            self.broker = LiveBroker()
        self.recorder = Recorder()
        self.current: dict[str, Candle] = {}   # forming candle per coin
        self.backfilled_t: dict[str, int] = {}  # last historical candle per coin
        self.started = time.time()
        self._load_state()

    def _backfill(self) -> None:
        """Load recent 1m history so the 150-candle warmup is satisfied at
        boot instead of ~2.5h after every (re)deploy. Signals returned from
        historical candles are discarded — only cooldown/ATR state matters."""
        import urllib.request
        base = ("https://api.hyperliquid-testnet.xyz"
                if CFG.hl_testnet else "https://api.hyperliquid.xyz")
        now_ms = int(time.time() * 1000)
        for coin in CFG.coins:
            try:
                body = json.dumps({"type": "candleSnapshot", "req": {
                    "coin": coin, "interval": "1m",
                    "startTime": now_ms - 230 * 60_000, "endTime": now_ms}}).encode()
                req = urllib.request.Request(f"{base}/info", data=body,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    candles = json.load(r)
                fed = 0
                for d in candles:
                    if int(d["T"]) > now_ms:      # still-forming candle
                        continue
                    c = Candle(t=int(d["t"]) // 1000, open=float(d["o"]),
                               high=float(d["h"]), low=float(d["l"]),
                               close=float(d["c"]), vol=float(d["v"]))
                    self.engine.on_closed_candle(coin, c)   # signal discarded
                    self.backfilled_t[coin] = c.t
                    fed += 1
                print(f"backfill {coin}: {fed} candles", flush=True)
            except Exception as e:
                print(f"backfill {coin} failed: {e!r} (warmup runs live)", flush=True)

    # ---------- candle stream ----------
    def on_candle_update(self, d: dict) -> None:
        coin = d["s"]
        c = Candle(t=int(d["t"]) // 1000, open=float(d["o"]), high=float(d["h"]),
                   low=float(d["l"]), close=float(d["c"]), vol=float(d["v"]))
        prev = self.current.get(coin)
        if prev is not None and c.t > prev.t:
            self._on_closed(coin, prev)
        self.current[coin] = c

    def _on_closed(self, coin: str, c: Candle) -> None:
        if c.t <= self.backfilled_t.get(coin, 0):
            return                     # already fed via historical backfill
        self.recorder.record(coin, c)
        self.broker.on_closed_candle(coin, c)
        sig = self.engine.on_closed_candle(coin, c)
        if sig is not None:
            notional = self._size(coin, sig.sl_dist)
            if notional > 0:
                self.broker.place(sig, notional)

    def _size(self, coin: str, sl_dist: float) -> float:
        # LiveBroker's positions/equity are also mutated by the reconcile
        # thread — read them under its lock. PaperBroker has no lock (no-op).
        lock = getattr(self.broker, "_lock", None) or contextlib.nullcontext()
        with lock:
            eq = self.broker.equity
            notional = eq * CFG.risk_per_trade / sl_dist
            # margin budget with PER-COIN venue leverage (13/18 coins cap at
            # 10x, TAO at 5x) and resting entry limits counted (F1/F4)
            used = self.broker.margin_used()
            if used + notional / self.broker.eff_leverage(coin) > CFG.margin_budget * eq:
                return 0.0
            return notional

    # ---------- persistence (paper only; LiveBroker persists itself) ----------
    def _save_state(self) -> None:
        if not CFG.paper:
            self.broker.save_state()
            return
        p = Path(CFG.state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        # F2: persist overlay/kill state so redeploys don't reset the 24h
        # breaker, streak pause, or daily loss counter. Atomic replace.
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"equity": self.broker.equity,
                                   "fills": self.broker.fills,
                                   "wins": self.broker.wins,
                                   "halted": self.broker.halted,
                                   "closed_r": self.broker.closed_r,
                                   "streak_pause_until": self.broker.streak_pause_until,
                                   "day_r": self.broker.day_r,
                                   "day_key": self.broker.day_key}))
        os.replace(tmp, p)

    def _load_state(self) -> None:
        if not CFG.paper:
            return
        p = Path(CFG.state_file)
        if p.exists():
            try:
                s = json.loads(p.read_text())
            except ValueError:
                return
            self.broker.equity = s.get("equity", self.broker.equity)
            self.broker.fills = s.get("fills", 0)
            self.broker.wins = s.get("wins", 0)
            self.broker.halted = s.get("halted", "")
            self.broker.closed_r = [tuple(x) for x in s.get("closed_r", [])]
            self.broker.streak_pause_until = s.get("streak_pause_until", 0)
            self.broker.day_r = s.get("day_r", 0.0)
            self.broker.day_key = s.get("day_key", "")

    # ---------- loops ----------
    async def ws_loop(self) -> None:
        await asyncio.to_thread(self._backfill)   # warm ATR/cooldown state once
        while True:
            try:
                async with websockets.connect(CFG.ws_url, ping_interval=20) as ws:
                    for coin in CFG.coins:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "candle", "coin": coin,
                                             "interval": "1m"}}))
                    if not CFG.paper:
                        for sub_type in ("userFills", "orderUpdates"):
                            await ws.send(json.dumps({
                                "method": "subscribe",
                                "subscription": {"type": sub_type,
                                                 "user": self.broker.address}}))
                    print(f"subscribed to {len(CFG.coins)} coins", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        ch = msg.get("channel")
                        if ch == "candle":
                            self.on_candle_update(msg["data"])
                        elif ch == "userFills" and not CFG.paper:
                            self.broker.on_user_fills(msg["data"])
                        elif ch == "orderUpdates" and not CFG.paper:
                            self.broker.on_order_updates(msg["data"])
            except Exception as e:
                print(f"ws error: {e!r}; reconnecting in 5s", flush=True)
                await asyncio.sleep(5)

    async def housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if not CFG.paper:
                try:   # REST reconciliation fallback (blocking SDK -> thread)
                    await asyncio.to_thread(self.broker.reconcile)
                except Exception as e:
                    print(f"reconcile error: {e!r}", flush=True)
            self._save_state()
            snap = self.broker.snapshot()
            print(f"[{time.strftime('%H:%M')}] eq={snap['equity']} fills={snap['fills']} "
                  f"wr={snap['wr']} open={len(snap['open_positions'])} "
                  f"pending={snap['pending']} halted={snap['halted'] or '-'}", flush=True)

    async def status_server(self) -> None:
        async def handle(reader, writer):
            await reader.read(1024)
            body = json.dumps({
                "mode": "paper" if CFG.paper else "live",
                "uptime_h": round((time.time() - self.started) / 3600, 2),
                **self.broker.snapshot()}, indent=2)
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n{body}".encode())
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "0.0.0.0", CFG.port)
        async with server:
            await server.serve_forever()


async def main() -> None:
    ex = Executor()
    print(f"mode={'paper' if CFG.paper else 'LIVE'}"
          f"{' (testnet)' if CFG.hl_testnet else ''}", flush=True)
    await asyncio.gather(ex.ws_loop(), ex.housekeeping_loop(), ex.status_server())


if __name__ == "__main__":
    asyncio.run(main())
