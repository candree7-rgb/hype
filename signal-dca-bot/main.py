"""
Signal DCA Bot v2 - Multi-TP Strategy (Two-Tier SL)

Architecture:
  1. Signal in (webhook/telegram) → parse → batch buffer → execute
  2. Price Monitor polls every 2s (all exits exchange-side):
     - PENDING: check E1 fill, timeout
     - OPEN: Safety SL at entry-10% (gives DCA room)
       → check TP1-4 fills (reduceOnly limits on Bybit)
       → TP1 fills: SL → breakeven, cancel DCA orders
       → TP4 fills: trailing 20% (0.5% CB)
     - DCA fills (before TP1): cancel TPs, hard SL at avg-3%, BE-trail
     - Detect position close (SL/trailing triggered by Bybit)
  3. Zone Refresh every 15min: auto-calc swing H/L for active symbols
  4. Neo Cloud trend switch: close opposing positions on /signal/trend-switch
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import load_config, BotConfig
from telegram_parser import parse_signal, Signal
from trade_manager import TradeManager, TradeStatus, Trade
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

    # Neo Cloud trend filter: skip counter-trend signals
    if config.neo_cloud_filter:
        neo_trend = db.get_neo_cloud(signal.symbol)
        if neo_trend:
            # Long signal requires "up" trend, Short requires "down"
            expected = "up" if signal.side == "long" else "down"
            if neo_trend != expected:
                reason = f"Neo Cloud filter: {signal.side} vs trend={neo_trend}"
                logger.info(
                    f"Signal FILTERED: {signal.symbol_display} {signal.side.upper()} | "
                    f"Neo Cloud says {neo_trend.upper()} → SKIP"
                )
                return {"status": "filtered", "reason": reason}
        # No Neo Cloud data for this symbol → allow trade (no filter)

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
                snap_min_pct=config.zone_snap_min_pct,
            )
            for i, (price, source) in enumerate(smart_levels):
                if i < len(trade.dca_levels) and source not in ("entry", "fixed", "filled"):
                    old_price = trade.dca_levels[i].price
                    trade.dca_levels[i].price = price
                    trade.dca_levels[i].qty = (
                        trade.dca_levels[i].margin * trade.leverage / price
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
    """Background task: poll order fills and detect exchange-side closes.

    Two-tier SL (all exits exchange-side):
    - Safety SL at entry-10% initially (gives DCA room to fill at -5%)
    - TP1-4: reduceOnly limit orders at signal targets (50/10/10/10%)
    - After TP1: SL → breakeven, cancel DCA orders (profit protection mode)
    - After TP4: trailing stop 0.5% CB on remaining 20%
    - DCA fills (before TP1): cancel TPs, hard SL at avg-3%, BE-trail from avg
    """
    logger.info("Price monitor started (Multi-TP)")

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
                        # Place DCA orders
                        bybit.place_dca_for_trade(trade)
                        # Calculate TP qtys and place Multi-TP orders
                        trade_mgr.setup_tp_qtys(trade)
                        _place_exchange_tps(trade)
                        # Set initial SL at entry-3%
                        _set_initial_sl(trade)
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

                # ── 1. Check Multi-TP fills (exchange-side limit orders) ──
                if trade.status == TradeStatus.OPEN and trade.current_dca == 0:
                    for tp_idx in range(len(trade.tp_prices)):
                        if trade.tp_filled[tp_idx] or not trade.tp_order_ids[tp_idx]:
                            continue
                        tp_filled, tp_fill_price = bybit.check_order_filled(
                            trade.symbol, trade.tp_order_ids[tp_idx]
                        )
                        if tp_filled:
                            close_qty = trade.tp_close_qtys[tp_idx] if tp_idx < len(trade.tp_close_qtys) else 0
                            trade_mgr.record_tp_fill(trade, tp_idx, close_qty, tp_fill_price)

                            # After TP1: move SL to breakeven + cancel DCA
                            if tp_idx == 0 and config.sl_to_be_after_tp1:
                                bybit.set_trading_stop(
                                    trade.symbol, trade.side,
                                    stop_loss=trade.signal_entry,
                                )
                                trade.hard_sl_price = trade.signal_entry
                                # Cancel DCA orders - no longer needed after TP1
                                # (SL at BE protects remaining position)
                                for dca in trade.dca_levels[1:]:
                                    if dca.order_id and not dca.filled:
                                        bybit.cancel_order(trade.symbol, dca.order_id)
                                        dca.order_id = ""
                                logger.info(
                                    f"SL → BE: {trade.symbol_display} | "
                                    f"SL={trade.signal_entry:.4f} | DCAs cancelled"
                                )

                            # After last TP: activate trailing on remaining 20%
                            if all(trade.tp_filled):
                                trail_dist = tp_fill_price * config.trailing_callback_pct / 100
                                bybit.set_trading_stop(
                                    trade.symbol, trade.side,
                                    trailing_stop=trail_dist,
                                )
                                logger.info(
                                    f"All TPs filled → trailing: {trade.symbol_display} | "
                                    f"Trail={config.trailing_callback_pct}% CB on "
                                    f"{trade.remaining_qty:.6f} remaining"
                                )

                            break  # One TP per cycle

                # ── 2. Check DCA fills (exchange-side limit orders) ──
                for i in range(1, trade.max_dca + 1):
                    if i >= len(trade.dca_levels):
                        break
                    dca = trade.dca_levels[i]
                    if dca.filled or not dca.order_id:
                        continue
                    dca_filled, dca_fill_price = bybit.check_order_filled(
                        trade.symbol, dca.order_id
                    )
                    if dca_filled:
                        trade_mgr.fill_dca(trade, i, dca_fill_price)

                        # Cancel all unfilled TP orders (DCA mode = different exit)
                        _cancel_unfilled_tps(trade)

                        # Set exchange-side stops for DCA mode
                        _set_exchange_stops_after_dca(trade)

                        logger.info(
                            f"DCA{i} filled: {trade.symbol_display} @ {dca_fill_price:.4f} | "
                            f"New avg: {trade.avg_price:.4f} | DCA {trade.current_dca}/{trade.max_dca}"
                        )
                        break  # One DCA per cycle

                # ── 3. Detect position closed by exchange (SL/trailing triggered) ──
                if trade.status in (TradeStatus.TRAILING, TradeStatus.BE_TRAILING,
                                    TradeStatus.DCA_ACTIVE, TradeStatus.OPEN):
                    pos = bybit.get_position(trade.symbol)
                    if pos is None or pos["size"] == 0:
                        # Position closed by Bybit (SL or trailing stop triggered)
                        price = bybit.get_ticker_price(trade.symbol) or trade.avg_price
                        remaining = trade.remaining_qty
                        if remaining > 0:
                            if trade.side == "long":
                                pnl = (price - trade.avg_price) * remaining
                            else:
                                pnl = (trade.avg_price - price) * remaining
                            trade.realized_pnl += pnl
                        reason = "Exchange stop (SL/trailing)"
                        if trade.tps_hit > 0:
                            reason += f" after TP{trade.tps_hit}"
                        trade_mgr.close_trade(trade, price, trade.realized_pnl, reason)
                        logger.info(
                            f"Position closed by Bybit: {trade.symbol_display} | "
                            f"PnL: ${trade.realized_pnl:+.2f}"
                        )
                        continue

                await asyncio.sleep(0.2)

            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Price monitor error: {e}", exc_info=True)
            await asyncio.sleep(5)


def _place_exchange_tps(trade: Trade) -> None:
    """Place Multi-TP reduceOnly limit orders on Bybit after E1 fills.

    Places TP1-TP4 at signal target prices with configured close percentages.
    """
    for i, tp_price in enumerate(trade.tp_prices):
        if i >= len(trade.tp_close_qtys):
            break
        qty = trade.tp_close_qtys[i]
        order_id = bybit.place_tp_order(trade, tp_price, qty, tp_num=i + 1)
        if order_id:
            trade.tp_order_ids[i] = order_id
        else:
            logger.warning(
                f"TP{i + 1} placement failed: {trade.symbol_display} @ {tp_price}"
            )

    placed = sum(1 for oid in trade.tp_order_ids if oid)
    logger.info(
        f"Multi-TP placed: {trade.symbol_display} | "
        f"{placed}/{len(trade.tp_prices)} TPs | "
        f"Prices: {[f'{p:.4f}' for p in trade.tp_prices]}"
    )


def _set_initial_sl(trade: Trade) -> None:
    """Set safety SL at entry-10% after E1 fills.

    Wide safety SL gives DCA room to fill at -5% before stopping out.
    After DCA fills → SL tightens to avg-3% (in _set_exchange_stops_after_dca).
    """
    sl_pct = config.safety_sl_pct / 100
    if trade.side == "long":
        trade.hard_sl_price = trade.avg_price * (1 - sl_pct)
    else:
        trade.hard_sl_price = trade.avg_price * (1 + sl_pct)

    bybit.set_trading_stop(
        trade.symbol, trade.side,
        stop_loss=trade.hard_sl_price,
    )
    logger.info(
        f"Safety SL set: {trade.symbol_display} | "
        f"SL={trade.hard_sl_price:.4f} (entry-{config.safety_sl_pct}%)"
    )


def _cancel_unfilled_tps(trade: Trade) -> None:
    """Cancel all unfilled TP orders (when DCA fills, switch to DCA exit)."""
    for i, order_id in enumerate(trade.tp_order_ids):
        if order_id and not trade.tp_filled[i]:
            bybit.cancel_order(trade.symbol, order_id)
            trade.tp_order_ids[i] = ""
    logger.info(f"Unfilled TPs cancelled: {trade.symbol_display} (DCA mode)")


def _set_exchange_stops_after_dca(trade: Trade) -> None:
    """Set exchange-side SL/trailing after a DCA fills.

    All DCAs filled (max_dca=1): hard SL at avg-3% + BE trailing from avg
    """
    trail_dist = trade.avg_price * config.be_trail_callback_pct / 100

    if trade.current_dca >= trade.max_dca:
        # ALL DCAs filled → hard SL at avg-3% + BE trailing
        bybit.set_trading_stop(
            trade.symbol, trade.side,
            stop_loss=trade.hard_sl_price,
            trailing_stop=trail_dist,
            active_price=trade.avg_price,
        )
        logger.info(
            f"All DCAs filled: {trade.symbol_display} | "
            f"SL={trade.hard_sl_price:.4f} | Trail={config.be_trail_callback_pct}% "
            f"(activates at avg={trade.avg_price:.4f})"
        )
    else:
        # More DCAs pending → trailing only (no hard SL, DCAs are safety net)
        bybit.set_trading_stop(
            trade.symbol, trade.side,
            trailing_stop=trail_dist,
            active_price=trade.avg_price,
        )
        logger.info(
            f"DCA{trade.current_dca} stops: {trade.symbol_display} | "
            f"Trail={config.be_trail_callback_pct}% "
            f"(activates at avg={trade.avg_price:.4f})"
        )


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
                        updated = zone_mgr.update_from_auto_calc(symbol, zones)
                        if updated:
                            await resnap_active_dcas(symbol)

                await asyncio.sleep(0.5)  # Rate limit

        except Exception as e:
            logger.error(f"Zone refresh error: {e}", exc_info=True)
            await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════
# ▌ DCA RE-SNAP (dynamic zone tracking)
# ══════════════════════════════════════════════════════════════════════════

MIN_RESNAP_PCT = 0.3  # Only move DCA if zone shifted > 0.3%

async def resnap_active_dcas(symbol: str):
    """Re-snap unfilled DCA orders to updated zones for a symbol.

    Called after /zones/push or zone_refresh_loop updates zone data.
    Only moves orders if the new zone price differs by > MIN_RESNAP_PCT.
    """
    if not config.zone_snap_enabled:
        return

    zones = zone_mgr.get_zones(symbol)
    if not zones or not zones.is_valid:
        return

    for trade in trade_mgr.active_trades:
        if trade.symbol != symbol:
            continue

        # Build filled mask so filled DCAs don't consume the zone snap
        filled_mask = [dca.filled for dca in trade.dca_levels]

        # Re-calculate smart DCA levels with fresh zones + filled status
        smart_levels = calc_smart_dca_levels(
            trade.signal_entry, config.dca_spacing_pct, zones, trade.side,
            snap_min_pct=config.zone_snap_min_pct,
            filled_levels=filled_mask,
        )

        for i, (new_price, source) in enumerate(smart_levels):
            if i == 0:
                continue  # Skip E1
            if i >= len(trade.dca_levels):
                break

            dca = trade.dca_levels[i]

            # Skip already filled DCAs
            if dca.filled:
                continue

            # Skip if no order on Bybit
            if not dca.order_id:
                continue

            # Skip if source is entry or filled marker
            if source in ("entry", "filled"):
                continue

            # Check if price actually changed significantly
            old_price = dca.price
            pct_change = abs(new_price - old_price) / old_price * 100
            if pct_change < MIN_RESNAP_PCT:
                continue

            # Amend the order on Bybit
            success = bybit.amend_order_price(trade.symbol, dca.order_id, new_price)
            if success:
                dca.price = new_price
                dca.qty = dca.margin * trade.leverage / new_price
                logger.info(
                    f"DCA{i} re-snapped: {trade.symbol_display} | "
                    f"{old_price:.4f} → {new_price:.4f} ({source}, {pct_change:.1f}% shift)"
                )


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


@app.post("/signal/trend-switch")
async def trend_switch(request: Request):
    """Neo Cloud trend switch: close opposing positions on clear reversal.

    TradingView sends alert when Neo Cloud switches direction (UPTREND/DOWNTREND).
    This is a CLEAR trend reversal, not volatile noise.

    JSON body: {"symbol": "HYPEUSDT", "direction": "up"} or {"direction": "down"}
    Text body: "HYPEUSDT up" or "HYPEUSDT down"

    direction "up"   → close all SHORT positions for that symbol
    direction "down"  → close all LONG positions for that symbol
    """
    import json as json_lib

    raw = await request.body()
    text = raw.decode("utf-8").strip()

    # Parse JSON or text format
    symbol = ""
    direction = ""

    try:
        if text.startswith("{"):
            body = json_lib.loads(text)
            symbol = body.get("symbol", "").upper().replace("/", "").split(".")[0]
            direction = body.get("direction", "").lower()
        else:
            # Text format: "HYPEUSDT up" or "HYPEUSDT down"
            parts = text.split()
            if len(parts) >= 2:
                symbol = parts[0].upper().replace("/", "").split(".")[0]
                direction = parts[1].lower()
    except Exception:
        pass

    if not symbol or direction not in ("up", "down"):
        return JSONResponse(
            {"status": "error", "reason": f"Invalid: symbol={symbol}, direction={direction}"},
            status_code=400,
        )

    # Store trend in DB for signal filtering
    db.upsert_neo_cloud(symbol, direction)

    # up = bullish reversal → close shorts
    # down = bearish reversal → close longs
    close_side = "short" if direction == "up" else "long"

    logger.info(
        f"Neo Cloud trend switch: {symbol} → {direction.upper()} | "
        f"Closing {close_side.upper()} positions | Stored in DB"
    )

    closed = []
    for trade in list(trade_mgr.active_trades):
        if trade.symbol != symbol:
            continue
        if trade.side != close_side:
            continue

        price = bybit.get_ticker_price(trade.symbol)

        # Cancel all open orders (TPs, DCAs)
        bybit.cancel_all_orders(trade.symbol)

        success = bybit.close_full(trade, f"Neo Cloud {direction}")
        if success and price:
            remaining = trade.remaining_qty
            if trade.side == "long":
                pnl = (price - trade.avg_price) * remaining
            else:
                pnl = (trade.avg_price - price) * remaining
            trade.realized_pnl += pnl
            trade_mgr.close_trade(
                trade, price, trade.realized_pnl,
                f"Neo Cloud trend switch ({direction})"
            )
            closed.append({
                "trade_id": trade.trade_id,
                "symbol": trade.symbol_display,
                "side": trade.side,
                "pnl": f"${trade.realized_pnl:+.2f}",
            })
            logger.info(
                f"Neo Cloud closed: {trade.symbol_display} {trade.side.upper()} | "
                f"PnL: ${trade.realized_pnl:+.2f}"
            )

    if not closed:
        logger.info(f"Neo Cloud: no {close_side} trades for {symbol}")

    return JSONResponse({
        "status": "ok",
        "symbol": symbol,
        "direction": direction,
        "closed_side": close_side,
        "closed": closed,
    })


@app.post("/flush")
async def flush():
    """Manually flush the signal buffer."""
    results = await flush_batch()
    return JSONResponse({"status": "flushed", "results": results or []})


@app.post("/zones/push")
async def push_zones(request: Request):
    """Receive zone data from TradingView watchlist alerts.

    Handles both JSON and text/plain (TradingView sends text/plain).
    Calculates S2/S3 from RZ Average symmetry: S2 = 2*avg - R2, S3 = 2*avg - R3.

    TradingView alert format (without outer braces to bypass JSON validator):
      "symbol":"{{ticker}}","s1":{{plot("RZ S1 Band")}},"r1":{{plot("RZ R1 Band")}},
      "r2":{{plot("RZ R2 Band")}},"r3":{{plot("RZ R3 Band")}},
      "rz_avg":{{plot("Reversal Zones Average")}}
    """
    import json as json_lib

    raw = await request.body()
    text = raw.decode("utf-8").strip()

    # TradingView sends without outer {} to bypass JSON validator
    # Wrap in braces if missing
    if not text.startswith("{"):
        text = "{" + text + "}"

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
    rz_avg = float(body.get("rz_avg", 0) or 0)

    # Calculate S2/S3 from RZ Average symmetry (LuxAlgo zones are symmetric)
    # Formula: S_n = 2 * RZ_Average - R_n
    if rz_avg > 0:
        if s2 == 0 and r2 > 0:
            s2 = 2 * rz_avg - r2
            logger.info(f"S2 calculated: 2 × {rz_avg:.4f} - {r2:.4f} = {s2:.4f}")
        if s3 == 0 and r3 > 0:
            s3 = 2 * rz_avg - r3
            logger.info(f"S3 calculated: 2 × {rz_avg:.4f} - {r3:.4f} = {s3:.4f}")

    # Fallback: midpoint if still missing and we have s1+s3
    if s2 == 0 and s1 > 0 and s3 > 0:
        s2 = (s1 + s3) / 2

    zones = CoinZones(
        symbol=symbol_clean,
        s1=s1, s2=s2, s3=s3,
        r1=r1, r2=r2, r3=r3,
        source=body.get("source", "luxalgo"),
    )
    zone_mgr.update_zones(symbol_clean, zones)

    logger.info(
        f"Zone push: {symbol_clean} ({zones.source}) | "
        f"S: {zones.s1:.4f}/{zones.s2:.4f}/{zones.s3:.4f} | "
        f"R: {zones.r1:.4f}/{zones.r2:.4f}/{zones.r3:.4f}"
        + (f" | avg: {rz_avg:.4f}" if rz_avg > 0 else "")
    )

    # Re-snap any active DCA orders to updated zones
    await resnap_active_dcas(symbol_clean)

    return JSONResponse({
        "status": "ok",
        "symbol": symbol_clean,
        "source": zones.source,
        "zones": {
            "s1": zones.s1, "s2": zones.s2, "s3": zones.s3,
            "r1": zones.r1, "r2": zones.r2, "r3": zones.r3,
        },
        "rz_avg": rz_avg,
        "s2_s3_method": "symmetry" if rz_avg > 0 else "direct",
    })


@app.post("/zones/discover")
async def discover_plots(request: Request):
    """Diagnostic: receive all 20 LuxAlgo plot values to identify S2/S3.

    TradingView alert sends plot_0 through plot_19 + known S1.
    Logs all values so you can identify which index = which zone.
    """
    import json as json_lib

    raw = await request.body()
    text = raw.decode("utf-8").strip()

    try:
        body = json_lib.loads(text)
    except (json_lib.JSONDecodeError, ValueError):
        logger.warning(f"Discover: invalid JSON: {text[:300]}")
        return JSONResponse(
            {"status": "error", "reason": "invalid JSON"}, status_code=400
        )

    symbol = body.get("symbol", "?")
    s1_known = body.get("s1", "?")

    # Log all plot values in a readable format
    logger.info(f"=== PLOT DISCOVERY: {symbol} ===")
    logger.info(f"  Known S1 = {s1_known}")

    plot_values = {}
    for i in range(20):
        key = f"p{i}"
        val = body.get(key, "missing")
        plot_values[key] = val
        logger.info(f"  plot_{i} = {val}")

    # Try to identify which plots match zone prices
    # S1 is known, so look for values near S1 that could be S2/S3
    try:
        s1_f = float(s1_known)
        matches = []
        for key, val in plot_values.items():
            try:
                v = float(val)
                if v > 0 and v != s1_f:
                    pct_diff = ((v - s1_f) / s1_f) * 100
                    matches.append((key, v, pct_diff))
            except (ValueError, TypeError):
                pass
        matches.sort(key=lambda x: x[1])
        logger.info(f"  --- Non-zero values sorted by price ---")
        for key, val, pct in matches:
            label = "SUPPORT?" if val < s1_f else "RESIST?"
            logger.info(f"  {key} = {val} ({pct:+.2f}% from S1) [{label}]")
    except (ValueError, TypeError):
        pass

    return JSONResponse({
        "status": "ok",
        "symbol": symbol,
        "s1": s1_known,
        "plots": plot_values,
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
        "tp_pcts": config.tp_close_pcts,
        "hard_sl_pct": config.hard_sl_pct,
        "zones": config.zone_snap_enabled,
        "neo_cloud": config.neo_cloud_filter,
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
    html += `<b class="blue">Config:</b> ${d.config.leverage}x | ${d.config.equity_pct}% eq/trade | `;
    html += `Max ${d.config.max_trades} trades | ${d.config.dca_levels} DCA ${JSON.stringify(d.config.dca_mults)} | `;
    html += `TP: ${d.config.tp_pcts.map((p,i) => 'TP'+(i+1)+'='+p+'%').join(', ')} | SL avg-${d.config.hard_sl_pct}% | `;
    html += `Neo Cloud: ${d.config.neo_cloud ? 'ON' : 'OFF'} | `;
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
        html += '<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Avg</th><th>DCA</th><th>TPs</th><th>SL</th><th>Margin</th><th>Status</th><th>Age</th></tr>';
        for (const t of d.active_trades) {
            const sc = t.side === 'long' ? 'green' : 'red';
            const stc = 'status-' + t.status;
            html += '<tr>';
            html += `<td><b>${t.symbol}</b></td>`;
            html += `<td class="${sc}">${t.side.toUpperCase()}</td>`;
            html += `<td>${t.entry}</td><td>${t.avg}</td>`;
            html += `<td>${t.dca}</td><td class="green">${t.tps}</td><td>${t.sl}</td>`;
            html += `<td>${t.margin}</td>`;
            html += `<td><span class="status ${stc}">${t.status}</span></td>`;
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
