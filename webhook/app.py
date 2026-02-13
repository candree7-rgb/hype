"""
LuxAlgo Hedge DCA v6 – Webhook Server
Receives TradingView alerts, places orders on Bybit (Unified Trading, Hedge Mode).

Architecture:
  TradingView (Pine Script) → JSON alerts → this server → Bybit API

Order Flow:
  1. Event alert (open/close/dca/sl/flip/emergency) → place order
  2. Heartbeat alert (every 15min bar) → update pending limits, sync SL
  3. Limit orders use maker fee (0.02%) with market fallback after timeout
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from config import cfg
from bybit_client import BybitClient, ACTION_MAP

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("webhook")


# ── State ────────────────────────────────────────────────────────────────────
class ServerState:
    """Tracks pending orders and positions for state sync."""

    def __init__(self):
        # Pending limit orders on Bybit (waiting for fill)
        # key: action like "open_long", value: {order_id, qty, price, placed_at}
        self.pending: dict[str, dict] = {}

        # Last known zones from heartbeat
        self.zones: dict[str, float] = {}

        # Last known position state from heartbeat
        self.positions: dict[str, dict] = {}

        # Stats
        self.total_orders = 0
        self.total_fills = 0
        self.total_fallbacks = 0
        self.start_time = time.time()


state = ServerState()
bybit: BybitClient | None = None


# ── Telegram ─────────────────────────────────────────────────────────────────
async def tg_notify(msg: str):
    """Send notification via Telegram (if configured)."""
    if not cfg.TG_BOT_TOKEN or not cfg.TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{cfg.TG_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={
                "chat_id": cfg.TG_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
            }, timeout=5)
    except Exception as e:
        log.warning("Telegram notify failed: %s", e)


# ── Order Execution ─────────────────────────────────────────────────────────
async def execute_order(action: str, qty: float, price: float, sl: float, comment: str):
    """
    Execute an order with limit+market fallback.

    Strategy:
    1. Place limit at best bid/ask (maker fee 0.02%)
    2. Wait LIMIT_TIMEOUT_SEC seconds
    3. If not filled → cancel limit → place market (taker fee 0.055%)
    """
    if action not in ACTION_MAP:
        log.error("Unknown action: %s", action)
        return

    side, pos_idx, reduce_only = ACTION_MAP[action]

    # Get current market price for limit placement
    ticker = bybit.get_ticker_price()

    # For buys: limit at ask (or slightly below for maker)
    # For sells: limit at bid (or slightly above for maker)
    buffer = cfg.PRICE_BUFFER_PCT / 100
    if side == "Buy":
        limit_price = ticker["ask"] * (1 + buffer)  # slightly above ask = fills immediately as taker
        # Actually: place AT bid to be maker, but risk no fill
        # Compromise: place at mid-price or at ask for fast fill
        limit_price = ticker["ask"]  # at ask = fills as maker if there's liquidity
    else:
        limit_price = ticker["bid"]  # at bid = fills as maker

    state.total_orders += 1
    log.info("═══ ORDER: %s qty=%.4f comment=%s ═══", action, qty, comment)

    # 1. Place limit order
    try:
        resp = bybit.place_limit_order(side, qty, limit_price, pos_idx, reduce_only)
        order_id = resp["result"]["orderId"]
    except Exception as e:
        log.error("Limit order failed, trying market: %s", e)
        try:
            bybit.place_market_order(side, qty, pos_idx, reduce_only)
            state.total_fallbacks += 1
        except Exception as e2:
            log.error("Market order also failed: %s", e2)
            await tg_notify(f"❌ ORDER FAILED: {action} {qty} - {e2}")
            return
        state.total_fills += 1
        await handle_post_fill(action, qty, sl, comment, "market")
        return

    # 2. Wait for fill
    await asyncio.sleep(cfg.LIMIT_TIMEOUT_SEC)

    # 3. Check fill status
    try:
        order_status = bybit.get_order_status(order_id)
        filled = order_status.get("orderStatus") == "Filled"
        remaining = float(order_status.get("leavesQty", qty))
    except Exception as e:
        log.warning("Could not check order status: %s", e)
        filled = False
        remaining = qty

    if filled or remaining == 0:
        log.info("✓ FILLED as limit (maker fee)")
        state.total_fills += 1
        await handle_post_fill(action, qty, sl, comment, "limit")
        return

    # 4. Not filled → cancel and market
    log.info("Limit not filled after %.1fs, falling back to market", cfg.LIMIT_TIMEOUT_SEC)
    bybit.cancel_order(order_id)

    # Small delay for cancel to process
    await asyncio.sleep(0.3)

    try:
        bybit.place_market_order(side, remaining, pos_idx, reduce_only)
        state.total_fills += 1
        state.total_fallbacks += 1
        await handle_post_fill(action, qty, sl, comment, "market_fallback")
    except Exception as e:
        log.error("Market fallback failed: %s", e)
        await tg_notify(f"❌ MARKET FALLBACK FAILED: {action} {qty} - {e}")


async def handle_post_fill(action: str, qty: float, sl: float, comment: str, fill_type: str):
    """After a fill: set/update/cancel SL on Bybit."""
    _, pos_idx, reduce_only = ACTION_MAP[action]

    if reduce_only:
        # Closing order
        if sl > 0:
            # Partial close with updated SL → update SL on exchange
            bybit.set_stop_loss(pos_idx, sl)
            log.info("SL updated to %.4f after partial close", sl)
        else:
            # Full close → cancel SL
            bybit.cancel_stop_loss(pos_idx)
            log.info("SL cancelled after full close")
    else:
        # Opening order (entry/dca/flip) → set SL
        if sl > 0:
            bybit.set_stop_loss(pos_idx, sl)
            log.info("SL set at %.4f", sl)

    # Notify
    emoji = "🟢" if "long" in action else "🔴"
    msg = f"{emoji} <b>{action}</b>\nQty: {qty}\nSL: {sl}\nFill: {fill_type}\nComment: {comment}"
    await tg_notify(msg)


# ── Heartbeat Processing ────────────────────────────────────────────────────
async def process_heartbeat(data: dict):
    """
    Process heartbeat alert (fires every 15min bar).
    Updates pending limit orders if zones shifted.
    Syncs SL prices with Bybit.
    """
    # Store latest zones
    state.zones = {
        "s1": data.get("s1", 0),
        "s3": data.get("s3", 0),
        "r1": data.get("r1", 0),
        "r3": data.get("r3", 0),
        "trail": data.get("tr", 0),
    }

    # Store position state from TradingView
    state.positions = {
        "long_qty": data.get("lq", 0),
        "long_avg": data.get("la", 0),
        "long_sl": data.get("ls", 0),
        "long_dca": data.get("ld", 0),
        "long_exit": data.get("le", 0),
        "short_qty": data.get("sq", 0),
        "short_avg": data.get("sa", 0),
        "short_sl": data.get("ss", 0),
        "short_dca": data.get("sd", 0),
        "short_exit": data.get("se", 0),
    }

    # Sync SL prices with Bybit
    long_sl = float(data.get("ls", 0))
    short_sl = float(data.get("ss", 0))
    long_qty = float(data.get("lq", 0))
    short_qty = float(data.get("sq", 0))

    if long_qty > 0 and long_sl > 0:
        try:
            bybit.set_stop_loss(1, long_sl)
        except Exception as e:
            log.warning("SL sync long failed: %s", e)

    if short_qty > 0 and short_sl > 0:
        try:
            bybit.set_stop_loss(2, short_sl)
        except Exception as e:
            log.warning("SL sync short failed: %s", e)

    log.debug("Heartbeat: zones=%s long=%.4f short=%.4f", state.zones, long_qty, short_qty)


# ── FastAPI App ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bybit
    bybit = BybitClient()

    # Verify connection
    try:
        ticker = bybit.get_ticker_price()
        log.info("Connected to Bybit %s: %s last=$%.4f",
                 "TESTNET" if cfg.BYBIT_TESTNET else "MAINNET",
                 cfg.SYMBOL, ticker["last"])
        positions = bybit.get_positions()
        for pos in positions:
            side_label = "LONG" if pos["side"] == "Buy" else "SHORT" if pos["side"] == "Sell" else "NONE"
            log.info("Position %s: size=%s avgPrice=%s",
                     side_label, pos["size"], pos["avgPrice"])
    except Exception as e:
        log.error("Bybit connection failed: %s", e)

    await tg_notify("🚀 Webhook server started")
    yield
    await tg_notify("🛑 Webhook server stopped")


app = FastAPI(title="LuxAlgo Hedge DCA v6 Webhook", lifespan=lifespan)


@app.get("/health")
async def health():
    uptime = int(time.time() - state.start_time)
    return {
        "status": "ok",
        "uptime_sec": uptime,
        "testnet": cfg.BYBIT_TESTNET,
        "symbol": cfg.SYMBOL,
        "total_orders": state.total_orders,
        "total_fills": state.total_fills,
        "total_fallbacks": state.total_fallbacks,
        "zones": state.zones,
        "positions": state.positions,
    }


@app.post("/webhook")
async def webhook(request: Request):
    """Receive TradingView alert webhook."""
    try:
        body = await request.body()
        data = json.loads(body)
    except Exception as e:
        log.error("Invalid JSON: %s", e)
        return Response(status_code=400, content="Invalid JSON")

    # Auth check
    secret = data.get("s", "")
    if cfg.WEBHOOK_SECRET and secret != cfg.WEBHOOK_SECRET:
        log.warning("Invalid secret from %s", request.client.host if request.client else "unknown")
        return Response(status_code=401, content="Unauthorized")

    action = data.get("a", "")

    # Heartbeat
    if action == "hb":
        await process_heartbeat(data)
        return {"status": "ok", "action": "heartbeat"}

    # Event alert
    qty = float(data.get("q", 0))
    price = float(data.get("p", 0))
    sl = float(data.get("sl", 0))
    comment = data.get("c", "")

    log.info("══════════════════════════════════════════")
    log.info("ALERT: action=%s qty=%.4f price=%.4f sl=%.4f comment=%s",
             action, qty, price, sl, comment)

    if qty <= 0:
        log.warning("Ignoring alert with qty=0")
        return {"status": "ok", "action": action, "note": "qty=0, skipped"}

    # Execute order in background (don't block webhook response)
    asyncio.create_task(execute_order(action, qty, price, sl, comment))

    return {"status": "ok", "action": action}


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=cfg.HOST, port=cfg.PORT, log_level="info")
