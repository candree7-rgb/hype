"""
Signal DCA Bot v1 - Main Application

Telegram Signal → Bybit DCA Trading Bot

Architecture:
1. Telegram Listener → parses VIP Club signals
2. Trade Manager → slot management, DCA logic, TP/trail
3. Bybit Engine → executes orders on Bybit
4. Price Monitor → polls prices, checks TP/DCA/Stop
5. Dashboard → /status endpoint for monitoring

Flow:
  Signal received → parse → check slot → open E1 + DCA limits
  → poll prices → TP1 hit → close 50% → trail rest
  → DCA triggered → update avg → trail to BE
  → all closed → slot free → next signal
"""

import asyncio
import logging
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import load_config, BotConfig
from telegram_parser import parse_signal, Signal
from trade_manager import TradeManager, TradeStatus
from bybit_engine import BybitEngine

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
monitor_task: asyncio.Task | None = None


# ══════════════════════════════════════════════════════════════════════════════
# ▌ SIGNAL PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

async def process_signal(signal: Signal) -> dict:
    """Process a parsed trading signal.

    1. Check if we can open
    2. Get equity
    3. Create trade
    4. Execute on Bybit
    """
    # Check slot availability
    can_open, reason = trade_mgr.can_open_trade(signal.symbol)
    if not can_open:
        logger.info(f"Signal rejected: {signal.symbol_display} | {reason}")
        return {"status": "rejected", "reason": reason}

    # Get current equity
    equity = bybit.get_equity()
    if equity <= 0:
        logger.error("Cannot get equity, skipping signal")
        return {"status": "error", "reason": "Cannot get equity"}

    logger.info(f"Current equity: ${equity:.2f}")

    # Create trade in manager
    trade = trade_mgr.create_trade(signal, equity)

    # Execute on Bybit
    success = bybit.open_trade(trade)
    if not success:
        # Clean up failed trade
        trade_mgr.close_trade(trade, 0, 0, "Failed to open")
        return {"status": "error", "reason": "Order execution failed"}

    logger.info(
        f"Trade opened: {signal.side.upper()} {signal.symbol_display} | "
        f"E1 @ {signal.entry_price} | "
        f"Slots: {trade_mgr.active_count}/{config.max_simultaneous_trades}"
    )

    return {
        "status": "opened",
        "trade_id": trade.trade_id,
        "symbol": signal.symbol_display,
        "side": signal.side,
        "e1_price": signal.entry_price,
        "slots_used": trade_mgr.active_count,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ▌ PRICE MONITOR (polls prices, checks TP/DCA/Stop)
# ══════════════════════════════════════════════════════════════════════════════

async def price_monitor():
    """Background task: poll prices and check TP/DCA/Stop for all active trades."""
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

                price = bybit.get_ticker_price(trade.symbol)
                if price is None:
                    continue

                # ── 1. Check TP1 ──
                tp_action = trade_mgr.check_tp(trade, price)
                if tp_action:
                    qty = tp_action["qty"]
                    success = bybit.close_partial(trade, qty, "TP1")
                    if success:
                        trade_mgr.record_tp1(trade, qty, price)
                        # Calculate partial PnL
                        if trade.side == "long":
                            pnl = (price - trade.avg_price) * qty
                        else:
                            pnl = (trade.avg_price - price) * qty
                        trade.realized_pnl += pnl
                    continue

                # ── 2. Check trailing (after TP1) ──
                trail_action = trade_mgr.check_trailing(trade, price)
                if trail_action:
                    success = bybit.close_full(trade, trail_action["reason"])
                    if success:
                        remaining = trade.remaining_qty
                        if trade.side == "long":
                            pnl = (price - trade.avg_price) * remaining
                        else:
                            pnl = (trade.avg_price - price) * remaining
                        trade.realized_pnl += pnl
                        trade_mgr.close_trade(trade, price, trade.realized_pnl, trail_action["reason"])
                    continue

                # ── 3. Check DCA fill ──
                # Note: DCA limit orders are on Bybit, but we also check here
                # to update our tracking if they've been filled
                dca_action = trade_mgr.check_dca_trigger(trade, price)
                if dca_action:
                    # The limit order should fill on Bybit side
                    # We just update our tracking
                    level = dca_action["level"]
                    trade_mgr.fill_dca(trade, level, price)

                    # Recalculate TP after DCA (avg changed)
                    # Cancel old TP orders if needed, new TP from new avg
                    logger.info(
                        f"DCA{level} triggered: {trade.symbol_display} @ {price:.4f} | "
                        f"New avg: {trade.avg_price:.4f}"
                    )

                # ── 4. Check stop (after all DCAs) ──
                stop_action = trade_mgr.check_stop(trade, price)
                if stop_action:
                    success = bybit.close_full(trade, stop_action["reason"])
                    if success:
                        total = trade.total_qty
                        if trade.side == "long":
                            pnl = (price - trade.avg_price) * total
                        else:
                            pnl = (trade.avg_price - price) * total
                        trade.realized_pnl += pnl
                        trade_mgr.close_trade(trade, price, trade.realized_pnl, stop_action["reason"])

                # Small delay between trades to avoid rate limits
                await asyncio.sleep(0.2)

            # Poll interval
            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Price monitor error: {e}", exc_info=True)
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════════════════
# ▌ FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup."""
    global monitor_task

    logger.info("Signal DCA Bot v1 starting...")
    config.print_summary()

    # Start price monitor
    monitor_task = asyncio.create_task(price_monitor())

    yield

    # Shutdown
    if monitor_task:
        monitor_task.cancel()
    logger.info("Bot stopped")


app = FastAPI(title="Signal DCA Bot v1", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    """Receive signal via webhook (manual or from Telegram forwarder).

    Accepts both JSON and plain text.
    """
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

    result = await process_signal(signal)
    return JSONResponse(result)


@app.post("/close/{symbol}")
async def close_position(symbol: str):
    """Manually close a position."""
    for trade in trade_mgr.active_trades:
        if trade.symbol == symbol or trade.symbol_display == symbol:
            price = bybit.get_ticker_price(trade.symbol)
            success = bybit.close_full(trade, "Manual close")
            if success and price:
                total = trade.total_qty
                if trade.side == "long":
                    pnl = (price - trade.avg_price) * total
                else:
                    pnl = (trade.avg_price - price) * total
                trade.realized_pnl += pnl
                trade_mgr.close_trade(trade, price, trade.realized_pnl, "Manual close")
                return {"status": "closed", "symbol": symbol, "pnl": f"${pnl:+.2f}"}
            return {"status": "error", "reason": "Close order failed"}

    return {"status": "error", "reason": f"No active trade for {symbol}"}


@app.get("/status")
async def status():
    """Dashboard data as JSON."""
    data = trade_mgr.get_dashboard_data()

    # Add equity if connected
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
        "tp1_pct": config.tp1_pct,
        "testnet": config.bybit_testnet,
    }

    return JSONResponse(data)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Simple HTML dashboard."""
    return """
<!DOCTYPE html>
<html><head>
<title>Signal DCA Bot v1</title>
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
    .status-open { background: #0d419d; } .status-dca { background: #9a6700; } .status-trailing { background: #1a7f37; }
</style>
</head><body>
<h1>Signal DCA Bot v1</h1>
<div id="dashboard">Loading...</div>
<script>
async function update() {
    const res = await fetch('/status');
    const d = await res.json();
    let html = '';

    // Config
    html += '<div class="card">';
    html += `<b class="blue">Config:</b> ${d.config.leverage}x | ${d.config.equity_pct}% per trade | Max ${d.config.max_trades} trades | ${d.config.dca_levels} DCA | TP1 ${d.config.tp1_pct}%`;
    html += ` | ${d.config.testnet ? '<span class="yellow">TESTNET</span>' : '<span class="red">LIVE</span>'}`;
    html += ` | Equity: <b>${d.equity}</b>`;
    html += '</div>';

    // Stats
    html += '<div class="card">';
    html += `<b class="blue">Stats:</b> Slots: <b>${d.slots}</b> | `;
    html += `<span class="green">${d.stats.wins}W</span> / <span class="red">${d.stats.losses}L</span> / ${d.stats.breakeven}BE | `;
    html += `WR: <b>${d.stats.win_rate}</b> | PnL: <b class="${d.stats.total_pnl.includes('-') ? 'red' : 'green'}">${d.stats.total_pnl}</b>`;
    html += '</div>';

    // Active Trades
    if (d.active_trades.length > 0) {
        html += '<div class="card"><b class="blue">Active Trades:</b>';
        html += '<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Avg</th><th>DCA</th><th>Margin</th><th>Status</th><th>Age</th></tr>';
        for (const t of d.active_trades) {
            const sideClass = t.side === 'long' ? 'green' : 'red';
            const statusClass = t.status === 'open' ? 'status-open' : t.status === 'dca' ? 'status-dca' : 'status-trailing';
            html += `<tr>
                <td><b>${t.symbol}</b></td>
                <td class="${sideClass}">${t.side.toUpperCase()}</td>
                <td>${t.entry}</td>
                <td>${t.avg}</td>
                <td>${t.dca}</td>
                <td>${t.margin}</td>
                <td><span class="status ${statusClass}">${t.status}${t.tp1_hit ? ' TP1✓' : ''}</span></td>
                <td>${t.age}</td>
            </tr>`;
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


# ══════════════════════════════════════════════════════════════════════════════
# ▌ ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Signal DCA Bot v1...")
    uvicorn.run(
        "main:app",
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
    )
