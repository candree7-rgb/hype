"""Hyperliquid executor — paper mode.

Subscribes to 1m candles for the validated universe over the public
Hyperliquid websocket, runs the frozen hl_native_shock_freq signal engine,
simulates fills with backtest-identical rules (paper broker), records every
closed candle to build venue-native history, and serves a /status endpoint.

Run: python3 main.py   (PAPER=true is the default; no keys needed)
"""
from __future__ import annotations

import asyncio
import json
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
        self.broker = PaperBroker()
        self.recorder = Recorder()
        self.current: dict[str, Candle] = {}   # forming candle per coin
        self.started = time.time()
        self._load_state()

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
        self.recorder.record(coin, c)
        self.broker.on_closed_candle(coin, c)
        sig = self.engine.on_closed_candle(coin, c)
        if sig is not None:
            notional = self._size(sig.sl_dist)
            if notional > 0:
                self.broker.place(sig, notional)

    def _size(self, sl_dist: float) -> float:
        eq = self.broker.equity
        notional = eq * CFG.risk_per_trade / sl_dist
        # margin budget check (leverage_cap; venue max per coin is >= 10 for universe)
        used = sum(p.notional for p in self.broker.positions.values()) / CFG.leverage_cap
        if used + notional / CFG.leverage_cap > CFG.margin_budget * eq:
            return 0.0
        return notional

    # ---------- persistence ----------
    def _save_state(self) -> None:
        p = Path(CFG.state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"equity": self.broker.equity,
                                 "fills": self.broker.fills,
                                 "wins": self.broker.wins,
                                 "halted": self.broker.halted}))

    def _load_state(self) -> None:
        p = Path(CFG.state_file)
        if p.exists():
            s = json.loads(p.read_text())
            self.broker.equity = s.get("equity", self.broker.equity)
            self.broker.fills = s.get("fills", 0)
            self.broker.wins = s.get("wins", 0)
            self.broker.halted = s.get("halted", "")

    # ---------- loops ----------
    async def ws_loop(self) -> None:
        while True:
            try:
                async with websockets.connect(CFG.ws_url, ping_interval=20) as ws:
                    for coin in CFG.coins:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "candle", "coin": coin,
                                             "interval": "1m"}}))
                    print(f"subscribed to {len(CFG.coins)} coins", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("channel") == "candle":
                            self.on_candle_update(msg["data"])
            except Exception as e:
                print(f"ws error: {e!r}; reconnecting in 5s", flush=True)
                await asyncio.sleep(5)

    async def housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
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
    if not CFG.paper:
        raise SystemExit("Live mode not enabled in this build — run PAPER=true. "
                         "Live order routing ships after the paper phase validates "
                         "fill rates and slippage (see README).")
    ex = Executor()
    await asyncio.gather(ex.ws_loop(), ex.housekeeping_loop(), ex.status_server())


if __name__ == "__main__":
    asyncio.run(main())
