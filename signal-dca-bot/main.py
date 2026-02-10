"""
Signal DCA Bot v2 - Main Application

Architecture:
  1. Signal in (webhook/telegram) → parse → batch buffer → execute
  2. Price Monitor polls every 2s:
     - PENDING: check E1 fill, timeout
     - OPEN (E1 only): check TP1 → close 50%, trail rest
     - TRAILING: check trail callback (floor=TP1)
     - DCA_ACTIVE: check hard SL, check BE-trail, check next DCA
     - BE_TRAILING: check trail callback from avg
  3. Zone Refresh every 15min: auto-calc swing H/L for active symbols
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import load_config, BotConfig
from telegram_parser import parse_signal, Signal
from trade_manager import TradeManager, TradeStatus
from bybit_engine import BybitEngine
from zone_data import (
    ZoneDataManager, CoinZones, calc_smart_dca_levels, calc_swing_zones
)
from telegram_listener import TelegramListener
import database as db

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# ── Globals ──
config: BotConfig = load_config()
trade_mgr: TradeManager = TradeManager(config)
bybit: BybitEngine = BybitEngine(config)
zone_mgr: ZoneDataManager = ZoneDataManager()
tg_listener: TelegramListener | None = None
monitor_task: asyncio.Task | None = None
zone_refresh_task: asyncio.Task | None = None


# ══════════════════════════════════════════════════════════════════════════
# ▌ SIGNAL BATCH BUFFER
# ══════════════════════════════════════════════════════════════════════════

BATCH_BUFFER_SECONDS = 5

signal_buffer: list[Signal] = []
buffer_lock = asyncio.Lock()
_batch_flush_handle: asyncio.TimerHandle | None = None


async def add_signal_to_batch(signal: Signal) -> dict:
    """Add signal to batch buffer. Processes after BATCH_BUFFER_SECONDS."""
    global _batch_flush_handle

    async with buffer_lock:
        for s in signal_buffer:
            if s.symbol == signal.symbol:
                return {"status": "duplicate", "symbol": signal.symbol_display}

        signal_buffer.append(signal)
        count = len(signal_buffer)
        logger.info(
            f"Signal buffered: {signal.side.upper()} {signal.symbol_display} "
            f"(Sig Lev: {signal.signal_leverage}x) | "
            f"Buffer: {count} signals, flushing in {BATCH_BUFFER_SECONDS}s"
        )

    loop = asyncio.get_event_loop()
    if _batch_flush_handle:
        _batch_flush_handle.cancel()
    _batch_flush_handle = loop.call_later(
        BATCH_BUFFER_SECONDS,
        lambda: asyncio.ensure_future(flush_batch())
    )

    return {"status": "buffered", "buffer_size": count}


async def flush_batch():
    """Process buffered batch: sort by priority, take top N."""
    global _batch_flush_handle
    _batch_flush_handle = None

    async with buffer_lock:
        if not signal_buffer:
            return
        batch = list(signal_buffer)
        signal_buffer.clear()

    free_slots = config.max_simultaneous_trades - trade_mgr.active_count
    if free_slots <= 0:
        logger.info(f"Batch of {len(batch)} signals: NO free slots, all rejected")
        return

    batch.sort(key=lambda s: s.signal_leverage, reverse=True)
    selected = batch[:free_slots]
    rejected = batch[free_slots:]

    logger.info(
        f"Batch processing: {len(batch)} signals → "
        f"{len(selected)} selected, {len(rejected)} rejected | "
        f"Priority: {', '.join(f'{s.symbol_display}({s.signal_leverage}x)' for s in selected)}"
    )

    results = []
    for signal in selected:
        result = await execute_signal(signal)
        results.append(result)

    return results


async def execute_signal(signal: Signal) -> dict:
    """Execute a single signal (open trade on Bybit)."""
    can_open, reason = trade_mgr.can_open_trade(signal.symbol)
    if not can_open:
        logger.info(f"Signal rejected: {signal.symbol_display} | {reason}")
        return {"status": "rejected", "reason": reason}

    equity = bybit.get_equity()
    if equity <= 0:
        logger.error("Cannot get equity, skipping signal")
        return {"status": "error", "reason": "Cannot get equity"}

    logger.info(f"Current equity: ${equity:.2f}")

    # Create trade
    trade = trade_mgr.create_trade(signal, equity)

    # Zone-snap DCA levels
    if config.zone_snap_enabled:
        zones = zone_mgr.get_zones(signal.symbol)

        # Fallback: auto-calculate if no zones in cache/Supabase
        if (zones is None or not zones.is_valid):
            candles = bybit.get_klines(
                signal.symbol, config.zone_candle_interval, config.zone_candle_count
            )
            if candles:
                auto_zones = calc_swing_zones(candles)
                if auto_zones:
                    auto_zones.symbol = signal.symbol
                    zone_mgr.update_from_auto_calc(signal.symbol, auto_zones)
                    zones = auto_zones
                    logger.info(f"Auto-zones calculated for {signal.symbol}")

        if zones and zones.is_valid:
            smart_levels = calc_smart_dca_levels(
                signal.entry_price, config.dca_spacing_pct, zones, signal.side,
                config.zone_snap_threshold_pct,
            )
            for i, (price, source) in enumerate(smart_levels):
                if i < len(trade.dca_levels) and source not in ("entry", "fixed"):
                    old_price = trade.dca_levels[i].price
                    trade.dca_levels[i].price = price
                    trade.dca_levels[i].qty = (
                        trade.dca_levels[i].margin * config.leverage / price
                    )
                    logger.info(
                        f"DCA{i} snapped: {old_price:.4f} → {price:.4f} ({source})"
                    )

    # Execute on Bybit
    success = bybit.open_trade(trade, use_limit=config.e1_limit_order)
    if not success:
        trade_mgr.close_trade(trade, 0, 0, "Failed to open")
        return {"status": "error", "reason": "Order execution failed"}

    logger.info(
        f"Trade opened: {signal.side.upper()} {signal.symbol_display} | "
        f"E1 @ {signal.entry_price} | Slots: {trade_mgr.active_count}/{config.max_simultaneous_trades}"
    )

    return {
        "status": "opened",
        "trade_id": trade.trade_id,
        "symbol": signal.symbol_display,
        "side": signal.side,
        "e1_price": signal.entry_price,
        "slots_used": trade_mgr.active_count,
    }


# ══════════════════════════════════════════════════════════════════════════
# ▌ PRICE MONITOR
# ══════════════════════════════════════════════════════════════════════════

async def price_monitor():
    """Background task: poll prices and manage all trade exits."""
    logger.info("Price monitor started")

    while True:
        try:
            active = trade_mgr.active_trades
            if not active:
                await asyncio.sleep(5)
                continue

            for trade in active:
                if trade.status == TradeStatus.CLOSED:
                    continue

                # ── 0. PENDING: check E1 limit fill / timeout ──
                if trade.status == TradeStatus.PENDING:
                    filled = bybit.check_e1_filled(trade)
                    if filled:
                        trade.status = TradeStatus.OPEN
                        bybit.place_dca_for_trade(trade)
                        logger.info(f"E1 filled → OPEN: {trade.symbol_display}")
                    else:
                        age_min = (time.time() - trade.opened_at) / 60
                        if age_min >= config.e1_timeout_minutes:
                            bybit.cancel_e1(trade)
                            trade_mgr.close_trade(
                                trade, 0, 0,
                                f"E1 timeout ({config.e1_timeout_minutes}min)"
                            )
                    await asyncio.sleep(0.2)
                    continue

                # Get current price
                price = bybit.get_ticker_price(trade.symbol)
                if price is None:
                    continue

                # ── 1. HARD SL (highest priority, DCA mode) ──
                sl_action = trade_mgr.check_hard_sl(trade, price)
                if sl_action:
                    success = bybit.close_full(trade, sl_action["reason"])
                    if success:
                        qty = sl_action["qty"]
                        if trade.side == "long":
                            pnl = (price - trade.avg_price) * qty
                        else:
                            pnl = (trade.avg_price - price) * qty
                        trade.realized_pnl += pnl
                        trade_mgr.close_trade(trade, price, trade.realized_pnl, sl_action["reason"])
                    continue

                # ── 2. BE-TRAIL (DCA mode, price returned to avg) ──
                be_action = trade_mgr.check_be_trail(trade, price)
                if be_action:
                    success = bybit.close_full(trade, be_action["reason"])
                    if success:
                        qty = be_action["qty"]
                        if trade.side == "long":
                            pnl = (price - trade.avg_price) * qty
                        else:
                            pnl = (trade.avg_price - price) * qty
                        trade.realized_pnl += pnl
                        trade_mgr.close_trade(trade, price, trade.realized_pnl, be_action["reason"])
                    continue

                # ── 3. TP1 (E1-only, no DCA) ──
                tp_action = trade_mgr.check_tp1(trade, price)
                if tp_action:
                    qty = tp_action["qty"]
                    success = bybit.close_partial(trade, qty, "TP1")
                    if success:
                        trade_mgr.record_tp1(trade, qty, price)
                        if trade.side == "long":
                            pnl = (price - trade.avg_price) * qty
                        else:
                            pnl = (trade.avg_price - price) * qty
                        trade.realized_pnl += pnl
                    continue

                # ── 4. TRAILING (after TP1, E1-only) ──
                trail_action = trade_mgr.check_trailing(trade, price)
                if trail_action:
                    success = bybit.close_full(trade, trail_action["reason"])
                    if success:
                        qty = trail_action["qty"]
                        if trade.side == "long":
                            pnl = (price - trade.avg_price) * qty
                        else:
                            pnl = (trade.avg_price - price) * qty
                        trade.realized_pnl += pnl
                        trade_mgr.close_trade(trade, price, trade.realized_pnl, trail_action["reason"])
                    continue

                # ── 5. DCA TRIGGER ──
                dca_action = trade_mgr.check_dca_trigger(trade, price)
                if dca_action:
                    level = dca_action["level"]
                    trade_mgr.fill_dca(trade, level, price)
                    logger.info(
                        f"DCA{level} triggered: {trade.symbol_display} @ {price:.4f} | "
                        f"New avg: {trade.avg_price:.4f} | SL: {trade.hard_sl_price:.4f}"
                    )

                await asyncio.sleep(0.2)

            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Price monitor error: {e}", exc_info=True)
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════════════
# ▌ ZONE REFRESH (auto-calc swing zones for active symbols)
# ══════════════════════════════════════════════════════════════════════════

async def zone_refresh_loop():
    """Background: refresh auto-calc zones every 15min for active symbols."""
    logger.info("Zone refresh loop started")

    while True:
        try:
            await asyncio.sleep(config.zone_refresh_minutes * 60)

            if not config.zone_snap_enabled:
                continue

            symbols = list(set(t.symbol for t in trade_mgr.active_trades))
            if not symbols:
                continue

            logger.info(f"Zone refresh: {len(symbols)} active symbols")

            for symbol in symbols:
                # Skip if recent LuxAlgo zones exist
                existing = zone_mgr.get_zones(symbol)
                if existing and existing.is_valid and existing.source == "luxalgo":
                    continue

                candles = bybit.get_klines(
                    symbol, config.zone_candle_interval, config.zone_candle_count
                )
                if candles:
                    zones = calc_swing_zones(candles)
                    if zones:
                        zones.symbol = symbol
                        zone_mgr.update_from_auto_calc(symbol, zones)

                await asyncio.sleep(0.5)  # Rate limit

        except Exception as e:
            logger.error(f"Zone refresh error: {e}", exc_info=True)
            await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════
# ▌ FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════

async def handle_tg_close(close_cmd: dict):
    """Handle a close signal from Telegram."""
    symbol = close_cmd["symbol"]
    for trade in trade_mgr.active_trades:
        if trade.symbol == symbol or trade.symbol_display == close_cmd.get("symbol_display", ""):
            price = bybit.get_ticker_price(trade.symbol)
            success = bybit.close_full(trade, "TG close signal")
            if success and price:
                qty = trade.remaining_qty if trade.tp1_hit else trade.total_qty
                if trade.side == "long":
                    pnl = (price - trade.avg_price) * qty
                else:
                    pnl = (trade.avg_price - price) * qty
                trade.realized_pnl += pnl
                trade_mgr.close_trade(trade, price, trade.realized_pnl, "TG close signal")
                logger.info(f"TG close executed: {symbol} PnL ${pnl:+.2f}")
            return
    logger.info(f"TG close: no active trade for {symbol}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global monitor_task, zone_refresh_task, tg_listener

    logger.info("Signal DCA Bot v2 starting...")
    config.print_summary()

    # Init database + warmup zone cache
    db.init_tables()
    zone_mgr.warmup_cache()

    # Start Telegram listener (if configured)
    tg_listener = TelegramListener(
        config,
        on_signal=add_signal_to_batch,
        on_close=handle_tg_close,
    )
    await tg_listener.start()

    monitor_task = asyncio.create_task(price_monitor())
    zone_refresh_task = asyncio.create_task(zone_refresh_loop())

    yield

    if monitor_task:
        monitor_task.cancel()
    if zone_refresh_task:
        zone_refresh_task.cancel()
    if tg_listener:
        await tg_listener.stop()
    logger.info("Bot stopped")


app = FastAPI(title="Signal DCA Bot v2", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    """Receive signal via webhook."""
    content_type = request.headers.get("content-type", "")

    if "json" in content_type:
        body = await request.json()
        message = body.get("message", body.get("text", ""))
    else:
        message = (await request.body()).decode("utf-8")

    if not message:
        return JSONResponse({"status": "error", "reason": "empty message"}, status_code=400)

    logger.info(f"Webhook received: {message[:100]}...")

    signal = parse_signal(message)
    if signal is None:
        return JSONResponse({"status": "ignored", "reason": "not a valid signal"})

    result = await add_signal_to_batch(signal)
    return JSONResponse(result)


@app.post("/close/{symbol}")
async def close_position(symbol: str):
    """Manually close a position."""
    for trade in trade_mgr.active_trades:
        if trade.symbol == symbol or trade.symbol_display == symbol:
            price = bybit.get_ticker_price(trade.symbol)
            success = bybit.close_full(trade, "Manual close")
            if success and price:
                qty = trade.remaining_qty if trade.tp1_hit else trade.total_qty
                if trade.side == "long":
                    pnl = (price - trade.avg_price) * qty
                else:
                    pnl = (trade.avg_price - price) * qty
                trade.realized_pnl += pnl
                trade_mgr.close_trade(trade, price, trade.realized_pnl, "Manual close")
                return {"status": "closed", "symbol": symbol, "pnl": f"${pnl:+.2f}"}
            return {"status": "error", "reason": "Close order failed"}

    return {"status": "error", "reason": f"No active trade for {symbol}"}


@app.post("/flush")
async def flush():
    """Manually flush the signal buffer."""
    results = await flush_batch()
    return JSONResponse({"status": "flushed", "results": results or []})


@app.post("/zones/push")
async def push_zones(request: Request):
    """Receive zone data from TradingView watchlist alerts.

    Handles both JSON and text/plain (TradingView sends text/plain).
    Auto-calculates S2/R2 as midpoints if not provided.

    Message format:
      {"symbol":"HYPEUSDT","s1":25.12,"s3":23.90,"r1":26.78,"r3":28.01,"source":"luxalgo"}
    """
    import json as json_lib

    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    text = raw.decode("utf-8").strip()

    # TradingView sends text/plain, but the content is JSON
    try:
        body = json_lib.loads(text)
    except (json_lib.JSONDecodeError, ValueError):
        logger.warning(f"Zone push: invalid JSON: {text[:200]}")
        return JSONResponse(
            {"status": "error", "reason": "invalid JSON"}, status_code=400
        )

    raw_symbol = body.get("symbol", "")
    if not raw_symbol:
        return JSONResponse(
            {"status": "error", "reason": "missing symbol in body"}, status_code=400
        )

    # Clean symbol: "HYPEUSDT.P" → "HYPEUSDT", "HYPE/USDT" → "HYPEUSDT"
    symbol_clean = raw_symbol.upper().replace("/", "").split(".")[0]

    s1 = float(body.get("s1", 0) or 0)
    s2 = float(body.get("s2", 0) or 0)
    s3 = float(body.get("s3", 0) or 0)
    r1 = float(body.get("r1", 0) or 0)
    r2 = float(body.get("r2", 0) or 0)
    r3 = float(body.get("r3", 0) or 0)

    # Auto-calculate S2/R2 as midpoints if not provided
    if s2 == 0 and s1 > 0 and s3 > 0:
        s2 = (s1 + s3) / 2
    if r2 == 0 and r1 > 0 and r3 > 0:
        r2 = (r1 + r3) / 2

    zones = CoinZones(
        symbol=symbol_clean,
        s1=s1, s2=s2, s3=s3,
        r1=r1, r2=r2, r3=r3,
        source=body.get("source", "luxalgo"),
    )
    zone_mgr.update_zones(symbol_clean, zones)

    logger.info(
        f"Zone push: {symbol_clean} ({zones.source}) | "
        f"S: {zones.s1}/{zones.s2}/{zones.s3} | R: {zones.r1}/{zones.r2}/{zones.r3}"
    )

    return JSONResponse({
        "status": "ok",
        "symbol": symbol_clean,
        "source": zones.source,
        "zones": {
            "s1": zones.s1, "s2": zones.s2, "s3": zones.s3,
            "r1": zones.r1, "r2": zones.r2, "r3": zones.r3,
        },
    })


@app.post("/zones/{symbol}")
async def update_zones(symbol: str, request: Request):
    """Update reversal zone levels (manual / direct API).

    JSON body: {"s1": 111.5, "s2": 108.2, "s3": 105.0, "r1": 115.8, "r2": 118.5, "r3": 121.0}
    """
    body = await request.json()
    symbol_clean = symbol.upper().replace("/", "")

    zones = CoinZones(
        symbol=symbol_clean,
        s1=float(body.get("s1", 0) or 0),
        s2=float(body.get("s2", 0) or 0),
        s3=float(body.get("s3", 0) or 0),
        r1=float(body.get("r1", 0) or 0),
        r2=float(body.get("r2", 0) or 0),
        r3=float(body.get("r3", 0) or 0),
        source=body.get("source", "luxalgo"),
    )
    zone_mgr.update_zones(symbol_clean, zones)

    return JSONResponse({
        "status": "ok",
        "symbol": symbol_clean,
        "source": zones.source,
        "zones": {
            "s1": zones.s1, "s2": zones.s2, "s3": zones.s3,
            "r1": zones.r1, "r2": zones.r2, "r3": zones.r3,
        },
    })


@app.get("/zones")
async def list_zones():
    """List all cached zone data."""
    result = {}
    for symbol, z in zone_mgr._cache.items():
        result[symbol] = {
            "s1": z.s1, "s2": z.s2, "s3": z.s3,
            "r1": z.r1, "r2": z.r2, "r3": z.r3,
            "source": z.source,
            "age_min": round(z.age_minutes, 1),
            "valid": z.is_valid,
        }
    return JSONResponse(result)


@app.get("/status")
async def status():
    """Dashboard data as JSON."""
    data = trade_mgr.get_dashboard_data()
    data["buffer"] = len(signal_buffer)

    try:
        equity = bybit.get_equity()
        data["equity"] = f"${equity:,.2f}"
    except Exception:
        data["equity"] = "N/A"

    data["config"] = {
        "leverage": config.leverage,
        "equity_pct": config.equity_pct_per_trade,
        "max_trades": config.max_simultaneous_trades,
        "dca_levels": config.max_dca_levels,
        "dca_mults": config.dca_multipliers[:config.max_dca_levels + 1],
        "tp1_pct": config.tp1_pct,
        "hard_sl_pct": config.hard_sl_pct,
        "zones": config.zone_snap_enabled,
        "testnet": config.bybit_testnet,
    }

    return JSONResponse(data)


@app.get("/trades")
async def trade_history():
    """Recent trade history from DB."""
    trades = db.get_recent_trades(50)
    stats = db.get_trade_stats()
    return JSONResponse({"stats": stats, "trades": trades})


@app.get("/equity")
async def equity_history():
    """Equity curve data for dashboard chart."""
    history = db.get_equity_history(90)
    return JSONResponse({"history": history})


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """HTML dashboard."""
    return """
<!DOCTYPE html>
<html><head>
<title>Signal DCA Bot v2</title>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<style>
    body { background: #0d1117; color: #c9d1d9; font-family: monospace; padding: 20px; }
    h1 { color: #58a6ff; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin: 10px 0; }
    .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; } .blue { color: #58a6ff; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px; border-bottom: 1px solid #21262d; }
    th { color: #8b949e; }
    .status { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
    .status-open { background: #0d419d; } .status-dca { background: #9a6700; }
    .status-trailing { background: #1a7f37; } .status-be_trail { background: #1a7f37; }
</style>
</head><body>
<h1>Signal DCA Bot v2</h1>
<div id="dashboard">Loading...</div>
<script>
async function update() {
    const res = await fetch('/status');
    const d = await res.json();
    let html = '';

    html += '<div class="card">';
    html += `<b class="blue">Config:</b> ${d.config.leverage}x | ${d.config.equity_pct}% per trade | `;
    html += `Max ${d.config.max_trades} trades | ${d.config.dca_levels} DCA ${JSON.stringify(d.config.dca_mults)} | `;
    html += `TP1 ${d.config.tp1_pct}% | SL avg-${d.config.hard_sl_pct}% | `;
    html += `Zones: ${d.config.zones ? 'ON' : 'OFF'} | `;
    html += d.config.testnet ? '<span class="yellow">TESTNET</span>' : '<span class="red">LIVE</span>';
    html += ` | Equity: <b>${d.equity}</b>`;
    html += '</div>';

    html += '<div class="card">';
    html += `<b class="blue">Stats:</b> Slots: <b>${d.slots}</b> | `;
    html += `<span class="green">${d.stats.wins}W</span> / <span class="red">${d.stats.losses}L</span> / ${d.stats.breakeven}BE | `;
    html += `WR: <b>${d.stats.win_rate}</b> | PnL: <b class="${d.stats.total_pnl.includes('-') ? 'red' : 'green'}">${d.stats.total_pnl}</b>`;
    html += '</div>';

    if (d.active_trades.length > 0) {
        html += '<div class="card"><b class="blue">Active Trades:</b>';
        html += '<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Avg</th><th>DCA</th><th>SL</th><th>BE-Trail</th><th>Margin</th><th>Status</th><th>Age</th></tr>';
        for (const t of d.active_trades) {
            const sc = t.side === 'long' ? 'green' : 'red';
            const stc = 'status-' + t.status;
            html += '<tr>';
            html += `<td><b>${t.symbol}</b></td>`;
            html += `<td class="${sc}">${t.side.toUpperCase()}</td>`;
            html += `<td>${t.entry}</td><td>${t.avg}</td>`;
            html += `<td>${t.dca}</td><td>${t.sl}</td><td>${t.be_trail}</td>`;
            html += `<td>${t.margin}</td>`;
            html += `<td><span class="status ${stc}">${t.status}${t.tp1_hit ? ' TP1' : ''}</span></td>`;
            html += `<td>${t.age}</td></tr>`;
        }
        html += '</table></div>';
    } else {
        html += '<div class="card"><span class="yellow">No active trades</span></div>';
    }

    document.getElementById('dashboard').innerHTML = html;
}
update();
setInterval(update, 10000);
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════
# ▌ ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Signal DCA Bot v2...")
    uvicorn.run(
        "main:app",
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
    )
