"""
Signal DCA Bot v2 - Multi-TP Strategy with 2/3 Pyramiding

Architecture:
  1. Signal in (webhook/telegram) → parse → batch buffer → execute
  2. Price Monitor polls every 2s (all exits exchange-side):
     - PENDING: check E1 fill, timeout
     - OPEN: Safety SL at entry-10% (gives DCA room)
       → check TP1-4 fills (reduceOnly limits on Bybit)
       → TP1 fills: SL → BE + 0.1% buffer, cancel DCA orders
       → TP2 fills: Scale-in 1/3 more (if no DCA) + SL = exakt new Avg
         → Cancel TP3/TP4, recalculate quantities, replace orders
       → TP3 fills: SL → TP2 price (profit lock)
       → TP4 fills: trailing (1% CB)
     - DCA fills (before TP1): cancel signal TPs, place new TPs from avg
       → DCA TP1=+0.5% (50%), TP2=+1.25% (20%), trail 30% @1%CB
       → Hard SL at DCA-fill+3% (safety net)
       → Quick-Trail: +0.5% move → SL tightens to avg+0.5% (~1.1% eq risk)
       → DCA TP1 fills → SL to exakt avg
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
safety_task: asyncio.Task | None = None
sync_task: asyncio.Task | None = None


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
    """Process buffered batch: Neo Cloud pre-filter, place all valid, cancel after N fills."""
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

    # Pre-filter: Neo Cloud + can_open checks (order as received, no sorting)
    valid = []
    for signal in batch:
        can_open, reason = trade_mgr.can_open_trade(signal.symbol)
        if not can_open:
            logger.info(f"Batch pre-filter: {signal.symbol_display} rejected → {reason}")
            continue
        if config.neo_cloud_filter:
            neo_trend = db.get_neo_cloud(signal.symbol)
            if neo_trend:
                expected = "up" if signal.side == "long" else "down"
                if neo_trend != expected:
                    logger.info(
                        f"Batch pre-filter: {signal.symbol_display} {signal.side.upper()} "
                        f"filtered → Neo Cloud={neo_trend.upper()}"
                    )
                    continue
        if config.zone_filter_enabled:
            zones = zone_mgr.get_zones(signal.symbol)
            if zones and zones.is_valid:
                if signal.side == "short" and zones.s1 and signal.entry_price < zones.s1:
                    logger.info(
                        f"Batch pre-filter: {signal.symbol_display} SHORT "
                        f"filtered → price {signal.entry_price:.4f} < S1 {zones.s1:.4f}"
                    )
                    continue
                if signal.side == "long" and zones.r1 and signal.entry_price > zones.r1:
                    logger.info(
                        f"Batch pre-filter: {signal.symbol_display} LONG "
                        f"filtered → price {signal.entry_price:.4f} > R1 {zones.r1:.4f}"
                    )
                    continue
        valid.append(signal)

    if not valid:
        logger.info(f"Batch of {len(batch)} signals: all filtered/rejected")
        return

    # Place up to free_slots orders; batch_id groups them for fill tracking
    selected = valid[:free_slots]
    batch_id = f"batch_{int(time.time())}"

    logger.info(
        f"Batch processing: {len(batch)} signals → "
        f"{len(valid)} passed filter → {len(selected)} placed | "
        f"batch_id={batch_id} | max_fills={config.max_fills_per_batch} | "
        f"Signals: {', '.join(s.symbol_display for s in selected)}"
    )

    results = []
    for signal in selected:
        result = await execute_signal(signal, batch_id=batch_id)
        results.append(result)

    return results


async def execute_signal(signal: Signal, batch_id: str = "") -> dict:
    """Execute a single signal (open trade on Bybit).

    Neo Cloud filter is applied in flush_batch() before calling this.
    """
    can_open, reason = trade_mgr.can_open_trade(signal.symbol)
    if not can_open:
        logger.info(f"Signal rejected: {signal.symbol_display} | {reason}")
        return {"status": "rejected", "reason": reason}

    # Neo Cloud trend filter for non-batch calls (webhook direct)
    if not batch_id and config.neo_cloud_filter:
        neo_trend = db.get_neo_cloud(signal.symbol)
        if neo_trend:
            expected = "up" if signal.side == "long" else "down"
            if neo_trend != expected:
                reason = f"Neo Cloud filter: {signal.side} vs trend={neo_trend}"
                logger.info(
                    f"Signal FILTERED: {signal.symbol_display} {signal.side.upper()} | "
                    f"Neo Cloud says {neo_trend.upper()} → SKIP"
                )
                return {"status": "filtered", "reason": reason}

    # Reversal Zone filter: skip if price is already in the reversal zone
    # SHORT + price < S1 → shorting into support (likely bounce) → skip
    # LONG  + price > R1 → longing into resistance (likely rejection) → skip
    if config.zone_filter_enabled:
        zones = zone_mgr.get_zones(signal.symbol)
        if zones and zones.is_valid:
            if signal.side == "short" and zones.s1 and signal.entry_price < zones.s1:
                reason = (
                    f"Zone filter: SHORT but price {signal.entry_price:.4f} "
                    f"< S1 {zones.s1:.4f} (in support zone)"
                )
                logger.info(
                    f"Signal FILTERED: {signal.symbol_display} {signal.side.upper()} | {reason}"
                )
                return {"status": "filtered", "reason": reason}
            if signal.side == "long" and zones.r1 and signal.entry_price > zones.r1:
                reason = (
                    f"Zone filter: LONG but price {signal.entry_price:.4f} "
                    f"> R1 {zones.r1:.4f} (in resistance zone)"
                )
                logger.info(
                    f"Signal FILTERED: {signal.symbol_display} {signal.side.upper()} | {reason}"
                )
                return {"status": "filtered", "reason": reason}

    equity = bybit.get_equity()
    if equity <= 0:
        logger.error("Cannot get equity, skipping signal")
        return {"status": "error", "reason": "Cannot get equity"}

    logger.info(f"Current equity: ${equity:.2f}")

    # Create trade
    trade = trade_mgr.create_trade(signal, equity)
    if batch_id:
        trade.batch_id = batch_id

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
                limit_buffer_pct=config.dca_limit_buffer_pct,
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

    # Persist initial trade state
    trade_mgr.persist_trade(trade)

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

    SL Ladder (all exits exchange-side):
    - Safety SL at entry-10% initially (gives DCA room to fill at -5%)
    - TP1-4: reduceOnly limit orders at signal targets (50/10/10/10%)
    - After TP1: SL → BE + 0.1% buffer, cancel DCA orders
    - After TP2: SL stays at BE + buffer
    - After TP3: SL → TP1 price (profit lock)
    - After TP4: trailing stop 1% CB on remaining 20%
    - DCA fills (before TP1): cancel signal TPs, new TPs from avg (0.5%/1.25%), hard SL at DCA-fill+3%
    - DCA Quick-Trail: once price moves +0.5% from avg → SL tightens to avg+0.5% buffer
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
                        # Calculate TP qtys, consolidate small ones, place orders
                        trade_mgr.setup_tp_qtys(trade)
                        _consolidate_tp_qtys(trade)
                        _place_exchange_tps(trade)
                        # Set initial SL at entry-3%
                        _set_initial_sl(trade)
                        trade_mgr.persist_trade(trade)
                        logger.info(f"E1 filled → OPEN: {trade.symbol_display}")

                        # Batch fill cap: cancel surplus PENDING trades from same batch
                        if trade.batch_id and config.max_fills_per_batch > 0:
                            batch_fills = sum(
                                1 for t in trade_mgr.active_trades
                                if t.batch_id == trade.batch_id
                                and t.status != TradeStatus.PENDING
                            )
                            if batch_fills >= config.max_fills_per_batch:
                                pending_same_batch = [
                                    t for t in trade_mgr.active_trades
                                    if t.batch_id == trade.batch_id
                                    and t.status == TradeStatus.PENDING
                                ]
                                for pt in pending_same_batch:
                                    bybit.cancel_e1(pt)
                                    trade_mgr.close_trade(
                                        pt, 0, 0,
                                        f"Batch cap ({config.max_fills_per_batch} fills reached)"
                                    )
                                if pending_same_batch:
                                    logger.info(
                                        f"Batch cap reached: {batch_fills}/{config.max_fills_per_batch} fills | "
                                        f"Cancelled {len(pending_same_batch)} PENDING: "
                                        f"{', '.join(t.symbol_display for t in pending_same_batch)}"
                                    )
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

                # ── 0b. Check scale-in limit fill ──
                if trade.scale_in_pending and trade.scale_in_order_id:
                    si_filled, si_fill_price = bybit.check_order_filled(
                        trade.symbol, trade.scale_in_order_id
                    )
                    if si_filled:
                        trade.scale_in_price = si_fill_price
                        _complete_scale_in(trade)
                        logger.info(
                            f"Scale-in filled: {trade.symbol_display} @ {si_fill_price:.4f}"
                        )

                # ── 1. Check Multi-TP fills (exchange-side limit orders) ──
                # Works for both E1 mode (signal TPs) and DCA mode (avg-based TPs)
                if trade.status in (TradeStatus.OPEN, TradeStatus.DCA_ACTIVE):
                    for tp_idx in range(len(trade.tp_prices)):
                        if trade.tp_filled[tp_idx] or not trade.tp_order_ids[tp_idx]:
                            continue
                        tp_filled, tp_fill_price = bybit.check_order_filled(
                            trade.symbol, trade.tp_order_ids[tp_idx]
                        )
                        if tp_filled:
                            close_qty = trade.tp_close_qtys[tp_idx] if tp_idx < len(trade.tp_close_qtys) else 0
                            trade_mgr.record_tp_fill(trade, tp_idx, close_qty, tp_fill_price)

                            # ── SL Ladder: DCA mode (2 TPs + trail) ──
                            if trade.current_dca > 0:
                                if tp_idx == 0:
                                    # DCA TP1 → SL to BE (exakt avg, kein buffer)
                                    # Bei 0.5% TP1 ist der Abstand eh nur 0.5% —
                                    # ein Buffer würde SL fast zum zweiten TP machen
                                    buffer = config.dca_be_buffer_pct / 100
                                    if trade.side == "long":
                                        be_price = trade.avg_price * (1 + buffer)
                                    else:
                                        be_price = trade.avg_price * (1 - buffer)
                                    sl_ok = bybit.set_trading_stop(
                                        trade.symbol, trade.side,
                                        stop_loss=be_price,
                                    )
                                    trade.hard_sl_price = be_price
                                    trade_mgr.persist_trade(trade)
                                    if sl_ok:
                                        buf_str = f"avg+{config.dca_be_buffer_pct}% buffer" if buffer > 0 else "exakt avg"
                                        logger.info(
                                            f"DCA TP1 → SL=BE: {trade.symbol_display} | "
                                            f"SL={be_price:.4f} ({buf_str})"
                                        )
                                    else:
                                        logger.critical(
                                            f"DCA TP1 → SL=BE FAILED: {trade.symbol_display} | "
                                            f"SL={be_price:.4f} NOT VERIFIED!"
                                        )

                                # After all DCA TPs: trail remaining with SL floor at TP1
                                if all(trade.tp_filled):
                                    trail_dist = tp_fill_price * config.dca_trail_callback_pct / 100
                                    tp1_price = trade.tp_prices[0]
                                    sl_ok = bybit.set_trading_stop(
                                        trade.symbol, trade.side,
                                        stop_loss=tp1_price,
                                        trailing_stop=trail_dist,
                                    )
                                    trade.status = TradeStatus.TRAILING
                                    trade.hard_sl_price = tp1_price
                                    trade_mgr.persist_trade(trade)
                                    if sl_ok:
                                        logger.info(
                                            f"All DCA TPs → trailing: {trade.symbol_display} | "
                                            f"SL={tp1_price:.4f} + Trail={config.dca_trail_callback_pct}%CB on "
                                            f"{trade.remaining_qty:.6f} remaining"
                                        )
                                    else:
                                        logger.critical(
                                            f"DCA trailing FAILED: {trade.symbol_display} | "
                                            f"SL={tp1_price:.4f} NOT VERIFIED!"
                                        )

                            # ── SL Ladder: E1 mode (Strategy C: 4 TPs + trail) ──
                            else:
                                # TP1 → SL to BE (entry), cancel DCAs
                                # TP2 → SL stays at BE (let runners breathe)
                                # TP3 → SL to TP1 (lock some profit)
                                # TP4 → trailing on remaining 20%

                                if tp_idx == 0 and config.sl_to_be_after_tp1:
                                    # TP1: SL → breakeven + 0.1% buffer + cancel DCAs
                                    buffer = config.be_buffer_pct / 100
                                    if trade.side == "long":
                                        be_price = trade.signal_entry * (1 + buffer)
                                    else:
                                        be_price = trade.signal_entry * (1 - buffer)
                                    sl_ok = bybit.set_trading_stop(
                                        trade.symbol, trade.side,
                                        stop_loss=be_price,
                                    )
                                    trade.hard_sl_price = be_price
                                    for dca in trade.dca_levels[1:]:
                                        if dca.order_id and not dca.filled:
                                            bybit.cancel_order(trade.symbol, dca.order_id)
                                            dca.order_id = ""
                                    trade_mgr.persist_trade(trade)
                                    if sl_ok:
                                        logger.info(
                                            f"TP1 → SL=BE: {trade.symbol_display} | "
                                            f"SL={be_price:.4f} (entry+{config.be_buffer_pct}% buffer) | DCAs cancelled"
                                        )
                                    else:
                                        logger.critical(
                                            f"TP1 → SL=BE FAILED: {trade.symbol_display} | "
                                            f"SL={be_price:.4f} NOT VERIFIED on exchange! "
                                            f"Safety monitor will retry in 30s"
                                        )

                                elif tp_idx == 1:
                                    # TP2: Scale-in (if enabled + no DCA) or SL stays at BE
                                    if (config.scale_in_enabled
                                            and trade.current_dca == 0
                                            and not trade.scale_in_filled
                                            and not trade.scale_in_pending):
                                        # 2/3 Pyramiding: place limit at TP2 price
                                        _place_scale_in_limit(trade, tp_fill_price)
                                    else:
                                        # DCA already filled or scale-in disabled → SL stays
                                        trade_mgr.persist_trade(trade)
                                        logger.info(
                                            f"TP2 filled: {trade.symbol_display} | "
                                            f"SL stays at BE={trade.hard_sl_price:.4f} "
                                            f"(DCA active or scale-in disabled)"
                                        )

                                elif tp_idx == 2:
                                    # TP3: SL → TP2 price (if scale-in) or TP1 (no scale-in)
                                    if trade.scale_in_filled:
                                        # Scale-in active: SL to TP2 price (profit lock)
                                        tp2_price = trade.tp_prices[1]
                                        sl_ok = bybit.set_trading_stop(
                                            trade.symbol, trade.side,
                                            stop_loss=tp2_price,
                                        )
                                        trade.hard_sl_price = tp2_price
                                        trade_mgr.persist_trade(trade)
                                        if sl_ok:
                                            logger.info(
                                                f"TP3 → SL=TP2: {trade.symbol_display} | "
                                                f"SL={tp2_price:.4f} (scale-in profit locked)"
                                            )
                                        else:
                                            logger.critical(
                                                f"TP3 → SL=TP2 FAILED: {trade.symbol_display} | "
                                                f"SL={tp2_price:.4f} NOT VERIFIED! "
                                                f"Safety monitor will retry in 30s"
                                            )
                                    else:
                                        # No scale-in: SL to TP1 price (original behavior)
                                        tp1_price = trade.tp_prices[0]
                                        sl_ok = bybit.set_trading_stop(
                                            trade.symbol, trade.side,
                                            stop_loss=tp1_price,
                                        )
                                        trade.hard_sl_price = tp1_price
                                        trade_mgr.persist_trade(trade)
                                        if sl_ok:
                                            logger.info(
                                                f"TP3 → SL=TP1: {trade.symbol_display} | "
                                                f"SL={tp1_price:.4f} (profit locked, no scale-in)"
                                            )
                                        else:
                                            logger.critical(
                                                f"TP3 → SL=TP1 FAILED: {trade.symbol_display} | "
                                                f"SL={tp1_price:.4f} NOT VERIFIED! "
                                                f"Safety monitor will retry in 30s"
                                            )

                                # After last E1 TP (TP4): activate trailing on remaining
                                if all(trade.tp_filled):
                                    trail_dist = tp_fill_price * config.trailing_callback_pct / 100
                                    sl_ok = bybit.set_trading_stop(
                                        trade.symbol, trade.side,
                                        stop_loss=trade.hard_sl_price,
                                        trailing_stop=trail_dist,
                                    )
                                    trade.status = TradeStatus.TRAILING
                                    trade_mgr.persist_trade(trade)
                                    if sl_ok:
                                        logger.info(
                                            f"All TPs filled → trailing: {trade.symbol_display} | "
                                            f"SL={trade.hard_sl_price:.4f} + Trail={config.trailing_callback_pct}% CB on "
                                            f"{trade.remaining_qty:.6f} remaining"
                                        )
                                    else:
                                        logger.critical(
                                            f"Trailing SL FAILED: {trade.symbol_display} | "
                                            f"SL={trade.hard_sl_price:.4f} NOT VERIFIED! "
                                            f"Safety monitor will retry in 30s"
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

                        # Cancel all unfilled TP orders (DCA mode = new TPs from avg)
                        _cancel_unfilled_tps(trade)

                        # Setup new TPs from new average, consolidate, place on exchange
                        trade_mgr.setup_dca_tps(trade)
                        _consolidate_tp_qtys(trade)
                        _place_dca_tps(trade)

                        # Set hard SL only (TPs handle profit taking)
                        _set_exchange_stops_after_dca(trade)

                        trade_mgr.persist_trade(trade)
                        logger.info(
                            f"DCA{i} filled: {trade.symbol_display} @ {dca_fill_price:.4f} | "
                            f"New avg: {trade.avg_price:.4f} | DCA {trade.current_dca}/{trade.max_dca}"
                        )
                        break  # One DCA per cycle

                # ── 2b. DCA Quick-Trail: tighten SL once bounce confirms ──
                # After DCA fills, SL is at deepest_fill+3% (~4.7% equity risk).
                # Once price moves 0.5% in our favor → tighten SL to avg+0.5%
                # (~1.1% equity risk). Keeps -3% as safety net until bounce confirms.
                if (trade.status == TradeStatus.DCA_ACTIVE
                        and trade.current_dca > 0
                        and not trade.quick_trail_active
                        and trade.tps_hit == 0):
                    current_price = bybit.get_ticker_price(trade.symbol)
                    if current_price:
                        trigger_pct = config.dca_quick_trail_trigger_pct / 100
                        if trade.side == "long":
                            trigger_price = trade.avg_price * (1 + trigger_pct)
                            price_in_favor = current_price >= trigger_price
                        else:
                            trigger_price = trade.avg_price * (1 - trigger_pct)
                            price_in_favor = current_price <= trigger_price

                        if price_in_favor:
                            buffer_pct = config.dca_quick_trail_buffer_pct / 100
                            if trade.side == "long":
                                new_sl = trade.avg_price * (1 - buffer_pct)
                            else:
                                new_sl = trade.avg_price * (1 + buffer_pct)
                            sl_ok = bybit.set_trading_stop(
                                trade.symbol, trade.side,
                                stop_loss=new_sl,
                            )
                            trade.hard_sl_price = new_sl
                            trade.quick_trail_active = True
                            trade_mgr.persist_trade(trade)
                            if sl_ok:
                                logger.info(
                                    f"DCA Quick-Trail: {trade.symbol_display} | "
                                    f"Price {current_price:.4f} moved +{config.dca_quick_trail_trigger_pct}% | "
                                    f"SL tightened: {new_sl:.4f} (avg+{config.dca_quick_trail_buffer_pct}% buffer) | "
                                    f"Risk reduced from ~{config.hard_sl_pct}% to ~{config.dca_quick_trail_buffer_pct}%"
                                )
                            else:
                                logger.critical(
                                    f"DCA Quick-Trail FAILED: {trade.symbol_display} | "
                                    f"SL={new_sl:.4f} NOT VERIFIED! Safety monitor will retry"
                                )

                # ── 3. Detect position closed by exchange (SL/trailing triggered) ──
                if trade.status in (TradeStatus.TRAILING, TradeStatus.BE_TRAILING,
                                    TradeStatus.DCA_ACTIVE, TradeStatus.OPEN):
                    pos = bybit.get_position(trade.symbol)
                    if pos is None or pos["size"] == 0:
                        # Position closed by Bybit (SL or trailing stop triggered)
                        # Step 1: Cancel ALL remaining orders (TPs, DCAs)
                        bybit.cancel_all_orders(trade.symbol)

                        # Step 2: Re-verify position after cancelling orders
                        # (cancelling reduceOnly orders can't reopen, but be safe)
                        await asyncio.sleep(0.5)
                        pos_verify = bybit.get_position(trade.symbol)
                        if pos_verify and pos_verify["size"] > 0:
                            # Residual found! Force close with exchange qty
                            logger.warning(
                                f"RESIDUAL after SL/trail: {trade.symbol_display} "
                                f"size={pos_verify['size']} - force closing"
                            )
                            bybit.close_full(trade, "Residual after exchange stop")

                        # Get actual PnL from Bybit (includes fees + exact fill prices)
                        await asyncio.sleep(1)  # Wait for Bybit to settle closed PnL records
                        bybit_pnl = _get_bybit_realized_pnl(trade)
                        price = bybit.get_ticker_price(trade.symbol) or trade.avg_price

                        if bybit_pnl is not None:
                            # Use Bybit's actual total PnL (replaces all manual calcs)
                            trade.realized_pnl = bybit_pnl
                            logger.info(
                                f"PnL from Bybit: {trade.symbol_display} | ${bybit_pnl:+.4f}"
                            )
                        else:
                            # Fallback: estimate from mark price (less accurate)
                            remaining = trade.remaining_qty
                            if remaining > 0:
                                if trade.side == "long":
                                    pnl = (price - trade.avg_price) * remaining
                                else:
                                    pnl = (trade.avg_price - price) * remaining
                                trade.realized_pnl += pnl
                            logger.warning(
                                f"PnL fallback (mark price): {trade.symbol_display} | "
                                f"${trade.realized_pnl:+.4f} (Bybit closed_pnl unavailable)"
                            )

                        # Build specific close reason
                        if trade.status == TradeStatus.TRAILING:
                            reason = "Trailing stop"
                        elif trade.tps_hit > 0:
                            reason = f"SL (at TP{trade.tps_hit} level)"
                        else:
                            reason = "SL hit"
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


def _get_bybit_realized_pnl(trade: Trade) -> float | None:
    """Get total realized PnL from Bybit for a trade.

    Queries get_closed_pnl for all closing events since trade opened.
    Includes fees, exact fill prices, and all partial closes (TPs + SL).
    Returns total PnL or None if records not available yet.
    """
    start_ms = int(trade.opened_at * 1000)
    records = bybit.get_closed_pnl(limit=50, start_time_ms=start_ms)
    if not records:
        return None

    # Filter for this symbol and side
    matching = [
        r for r in records
        if r["symbol"] == trade.symbol and r["side"] == trade.side
    ]

    if not matching:
        return None

    total_pnl = sum(r["closed_pnl"] for r in matching)
    total_qty = sum(r["qty"] for r in matching)
    logger.info(
        f"Bybit closed PnL: {trade.symbol_display} | "
        f"{len(matching)} records | Total qty: {total_qty:.4f} | "
        f"PnL: ${total_pnl:+.4f}"
    )
    return total_pnl


def _place_scale_in_limit(trade: Trade, tp2_fill_price: float) -> None:
    """Place scale-in LIMIT order after TP2 fills.

    Places limit at TP2 price. Fill is checked by price_monitor on next cycle.
    If filled → _complete_scale_in() handles avg update, TP recalc, SL.
    If not filled (price moved away) → trade continues with BE SL, no scale-in.
    """
    e1 = trade.dca_levels[0]
    scale_in_margin = e1.margin  # Same 1/3 budget as E1
    scale_in_qty = scale_in_margin * trade.leverage / tp2_fill_price

    logger.info(
        f"Scale-in limit placing: {trade.symbol_display} | "
        f"{scale_in_qty:.6f} coins @ {tp2_fill_price:.4f} "
        f"(${scale_in_margin:.2f} margin) | "
        f"Current remaining: {trade.remaining_qty:.6f}"
    )

    order_id, rounded_qty = bybit.place_scale_in_order(
        trade, scale_in_qty, limit_price=tp2_fill_price
    )

    if not order_id:
        logger.error(
            f"Scale-in limit FAILED: {trade.symbol_display} | "
            f"SL stays at BE={trade.hard_sl_price:.4f}"
        )
        trade_mgr.persist_trade(trade)
        return

    trade.scale_in_pending = True
    trade.scale_in_order_id = order_id
    trade.scale_in_margin = scale_in_margin
    trade.scale_in_qty = rounded_qty  # Expected qty (updated on fill)
    trade_mgr.persist_trade(trade)

    logger.info(
        f"TP2 → Scale-in limit placed: {trade.symbol_display} | "
        f"Order: {order_id} | {rounded_qty} @ {tp2_fill_price:.4f} | "
        f"SL stays at BE until fill"
    )


def _complete_scale_in(trade: Trade) -> None:
    """Complete scale-in after limit order fills.

    Called by price_monitor when scale_in_order_id is detected as filled.
    1. Record fill → update avg, qty
    2. Use Bybit position avg as source of truth
    3. Cancel unfilled TP3/TP4 → recalculate quantities → replace
    4. Set SL to exact new avg (no buffer, zero risk)
    """
    # Get actual position from Bybit (source of truth for avg + size)
    pos = bybit.get_position(trade.symbol)
    if not pos:
        logger.error(f"Scale-in complete: position not found for {trade.symbol}")
        return

    new_bybit_avg = pos["avg_price"]
    new_bybit_size = pos["size"]

    # Calculate actual added qty
    actual_added = new_bybit_size - trade.remaining_qty
    if actual_added <= 0:
        actual_added = trade.scale_in_qty  # Fallback to expected qty

    fill_price = trade.scale_in_price if trade.scale_in_price > 0 else new_bybit_avg

    trade_mgr.fill_scale_in(trade, fill_price, actual_added, trade.scale_in_margin)

    # Use Bybit's avg as source of truth
    if new_bybit_avg > 0:
        trade.avg_price = new_bybit_avg

    trade.scale_in_pending = False

    # Cancel unfilled TP3/TP4 orders
    for i in range(len(trade.tp_order_ids)):
        if not trade.tp_filled[i] and trade.tp_order_ids[i]:
            bybit.cancel_order(trade.symbol, trade.tp_order_ids[i])
            trade.tp_order_ids[i] = ""
    logger.info(f"Unfilled TPs cancelled for recalculation: {trade.symbol_display}")

    # Recalculate TP quantities for new position size
    trade_mgr.recalc_tps_after_scale_in(trade)

    # Consolidate (drop TPs below min_qty) and place new orders
    _consolidate_tp_qtys(trade)
    for i in range(len(trade.tp_prices)):
        if trade.tp_filled[i] or not trade.tp_close_qtys[i]:
            continue
        order_id = bybit.place_tp_order(
            trade, trade.tp_prices[i], trade.tp_close_qtys[i],
            tp_num=i + 1, tag="STP"  # STP = Scale-in TP
        )
        if order_id:
            trade.tp_order_ids[i] = order_id

    placed = sum(1 for i, oid in enumerate(trade.tp_order_ids) if oid and not trade.tp_filled[i])
    logger.info(
        f"Scale-in TPs placed: {trade.symbol_display} | "
        f"{placed} new TPs | Remaining: {trade.remaining_qty:.6f}"
    )

    # Set SL to exact new avg (no buffer, zero risk on scale-in)
    sl_price = trade.avg_price
    sl_ok = bybit.set_trading_stop(
        trade.symbol, trade.side,
        stop_loss=sl_price,
    )
    trade.hard_sl_price = sl_price
    trade_mgr.persist_trade(trade)

    if sl_ok:
        logger.info(
            f"Scale-in complete → SL=Avg: {trade.symbol_display} | "
            f"SL={sl_price:.4f} (exakt avg, zero risk) | "
            f"+{actual_added:.6f} coins | New avg: {trade.avg_price:.4f}"
        )
    else:
        logger.critical(
            f"Scale-in SL FAILED: {trade.symbol_display} | "
            f"SL={sl_price:.4f} NOT VERIFIED! Safety monitor will retry"
        )


def _consolidate_tp_qtys(trade: Trade) -> None:
    """Remove TPs whose qty rounds below exchange min_qty.

    For small positions (e.g., XMR 0.09 coins), TP2/3/4 at 10% each = 0.009
    which is below min_qty (0.01). These get dropped and their share becomes
    part of the trailing portion instead.

    Must be called AFTER setup_tp_qtys() or setup_dca_tps(), BEFORE placing orders.
    """
    info = bybit.get_instrument_info(trade.symbol)
    if not info:
        return

    min_qty = info["min_qty"]
    qty_step = info["qty_step"]

    valid_indices = []
    for i, qty in enumerate(trade.tp_close_qtys):
        rounded = bybit.round_qty(qty, qty_step)
        if rounded >= min_qty:
            valid_indices.append(i)
        else:
            logger.info(
                f"TP{i + 1} qty {rounded} < min_qty {min_qty} for {trade.symbol_display}, "
                f"merging {trade.tp_close_pcts[i]}% into trail"
            )

    if len(valid_indices) == len(trade.tp_close_qtys):
        return  # All TPs valid

    if not valid_indices:
        # ALL TPs too small → trail everything
        logger.warning(
            f"ALL TPs below min_qty for {trade.symbol_display}, "
            f"trailing entire position"
        )
        trade.tp_prices = []
        trade.tp_close_pcts = []
        trade.tp_close_qtys = []
        trade.tp_filled = []
        trade.tp_order_ids = []
        trade.status = TradeStatus.TRAILING
        return

    # Keep only valid TPs
    trade.tp_prices = [trade.tp_prices[i] for i in valid_indices]
    trade.tp_close_pcts = [trade.tp_close_pcts[i] for i in valid_indices]
    trade.tp_close_qtys = [trade.tp_close_qtys[i] for i in valid_indices]
    trade.tp_filled = [False] * len(valid_indices)
    trade.tp_order_ids = [""] * len(valid_indices)

    trail_pct = 100 - sum(trade.tp_close_pcts)
    logger.info(
        f"TPs consolidated: {trade.symbol_display} | "
        f"{len(valid_indices)}/{len(valid_indices)} valid TPs | "
        f"Trail: {trail_pct:.0f}%"
    )


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

    sl_ok = bybit.set_trading_stop(
        trade.symbol, trade.side,
        stop_loss=trade.hard_sl_price,
    )
    if sl_ok:
        logger.info(
            f"Safety SL set: {trade.symbol_display} | "
            f"SL={trade.hard_sl_price:.4f} (entry-{config.safety_sl_pct}%)"
        )
    else:
        logger.critical(
            f"Safety SL FAILED: {trade.symbol_display} | "
            f"SL={trade.hard_sl_price:.4f} NOT VERIFIED! "
            f"Safety monitor will retry in 30s"
        )


def _cancel_unfilled_tps(trade: Trade) -> None:
    """Cancel all unfilled TP orders (when DCA fills, switch to DCA exit)."""
    for i, order_id in enumerate(trade.tp_order_ids):
        if order_id and not trade.tp_filled[i]:
            bybit.cancel_order(trade.symbol, order_id)
            trade.tp_order_ids[i] = ""
    logger.info(f"Unfilled TPs cancelled: {trade.symbol_display} (DCA mode)")


def _place_dca_tps(trade: Trade) -> None:
    """Place new TP limit orders after DCA fill.

    Uses avg-based TP prices set by trade_mgr.setup_dca_tps().
    TP1=50% at avg+0.5%, TP2=20% at avg+1.25%, remaining 30% trails.
    """
    for i, tp_price in enumerate(trade.tp_prices):
        if i >= len(trade.tp_close_qtys):
            break
        qty = trade.tp_close_qtys[i]
        order_id = bybit.place_tp_order(trade, tp_price, qty, tp_num=i + 1, tag="DTP")
        if order_id:
            trade.tp_order_ids[i] = order_id
        else:
            logger.warning(
                f"DCA TP{i + 1} placement failed: {trade.symbol_display} @ {tp_price}"
            )

    placed = sum(1 for oid in trade.tp_order_ids if oid)
    logger.info(
        f"DCA TPs placed: {trade.symbol_display} | "
        f"{placed}/{len(trade.tp_prices)} TPs | "
        f"Prices: {[f'{p:.4f}' for p in trade.tp_prices]}"
    )


def _set_exchange_stops_after_dca(trade: Trade) -> None:
    """Set exchange-side hard SL after a DCA fills.

    All DCAs filled (max_dca=1): hard SL at DCA-fill+3% only.
    TPs handle profit taking (no more BE-trail).
    """
    if trade.current_dca >= trade.max_dca:
        # ALL DCAs filled → hard SL only (TPs handle profit)
        sl_ok = bybit.set_trading_stop(
            trade.symbol, trade.side,
            stop_loss=trade.hard_sl_price,
        )
        if sl_ok:
            logger.info(
                f"All DCAs filled: {trade.symbol_display} | "
                f"SL={trade.hard_sl_price:.4f} (DCA-fill+{config.hard_sl_pct}%) | "
                f"TPs handle exit"
            )
        else:
            logger.critical(
                f"DCA SL FAILED: {trade.symbol_display} | "
                f"SL={trade.hard_sl_price:.4f} NOT VERIFIED! "
                f"Safety monitor will retry in 30s"
            )
    else:
        # More DCAs pending → hard SL only, wait for remaining DCAs
        sl_ok = bybit.set_trading_stop(
            trade.symbol, trade.side,
            stop_loss=trade.hard_sl_price,
        )
        if sl_ok:
            logger.info(
                f"DCA{trade.current_dca} SL set: {trade.symbol_display} | "
                f"SL={trade.hard_sl_price:.4f} | More DCAs pending"
            )
        else:
            logger.warning(
                f"DCA{trade.current_dca} SL failed: {trade.symbol_display} | "
                f"Safety monitor will retry"
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
                # Skip if FRESH LuxAlgo zones exist (< stale threshold)
                existing = zone_mgr.get_zones(symbol)
                if (existing and existing.is_valid
                        and existing.source == "luxalgo"
                        and existing.age_minutes < config.zone_luxalgo_stale_minutes):
                    continue

                candles = bybit.get_klines(
                    symbol, config.zone_candle_interval, config.zone_candle_count
                )
                if candles:
                    zones = calc_swing_zones(candles)
                    if zones:
                        zones.symbol = symbol
                        updated = zone_mgr.update_from_auto_calc(
                            symbol, zones,
                            luxalgo_stale_minutes=config.zone_luxalgo_stale_minutes,
                        )
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
            limit_buffer_pct=config.dca_limit_buffer_pct,
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
                trade_mgr.persist_trade(trade)
                logger.info(
                    f"DCA{i} re-snapped: {trade.symbol_display} | "
                    f"{old_price:.4f} → {new_price:.4f} ({source}, {pct_change:.1f}% shift)"
                )


# ══════════════════════════════════════════════════════════════════════════
# ▌ SAFETY MONITOR (periodic SL/position verification)
# ══════════════════════════════════════════════════════════════════════════

SAFETY_CHECK_INTERVAL = 30  # seconds

async def safety_monitor():
    """Background: verify all active trades have SL set on exchange.

    Runs every 30s. If a position exists on Bybit but has no SL,
    re-sets the SL. This prevents zombie positions from running unprotected.
    Also detects orphan positions (on exchange but not tracked by bot).
    """
    logger.info("Safety monitor started")

    # Startup recovery: load trades from DB, reconcile with Bybit
    await asyncio.sleep(15)
    await _recover_and_check_positions()

    while True:
        try:
            await asyncio.sleep(SAFETY_CHECK_INTERVAL)

            active = trade_mgr.active_trades
            if not active:
                continue

            for trade in active:
                if trade.status in (TradeStatus.CLOSED, TradeStatus.PENDING):
                    continue

                # Check position on exchange
                pos = bybit.get_position(trade.symbol)

                if pos is None or pos["size"] == 0:
                    # Position gone but trade still tracked → already handled by price_monitor
                    continue

                # Position exists - verify SL is set
                if pos["stop_loss"] == 0 and pos["trailing_stop"] == 0:
                    logger.warning(
                        f"SAFETY: No SL on {trade.symbol_display} {trade.side.upper()}! "
                        f"size={pos['size']} | Re-setting SL"
                    )

                    # Determine what SL to set based on trade state
                    if trade.hard_sl_price and trade.hard_sl_price > 0:
                        # Use existing hard SL
                        sl_ok = bybit.set_trading_stop(
                            trade.symbol, trade.side,
                            stop_loss=trade.hard_sl_price,
                        )
                        trade_mgr.persist_trade(trade)
                        if sl_ok:
                            logger.info(
                                f"SAFETY: SL restored: {trade.symbol_display} | "
                                f"SL={trade.hard_sl_price:.4f}"
                            )
                        else:
                            logger.critical(
                                f"SAFETY: SL restore FAILED: {trade.symbol_display} | "
                                f"SL={trade.hard_sl_price:.4f} NOT VERIFIED! "
                                f"Will retry next cycle"
                            )
                    else:
                        # Fallback: set safety SL at entry-10%
                        sl_pct = config.safety_sl_pct / 100
                        if trade.side == "long":
                            sl_price = trade.avg_price * (1 - sl_pct)
                        else:
                            sl_price = trade.avg_price * (1 + sl_pct)
                        trade.hard_sl_price = sl_price
                        sl_ok = bybit.set_trading_stop(
                            trade.symbol, trade.side,
                            stop_loss=sl_price,
                        )
                        trade_mgr.persist_trade(trade)
                        if sl_ok:
                            logger.info(
                                f"SAFETY: Emergency SL set: {trade.symbol_display} | "
                                f"SL={sl_price:.4f} (fallback entry-{config.safety_sl_pct}%)"
                            )
                        else:
                            logger.critical(
                                f"SAFETY: Emergency SL FAILED: {trade.symbol_display} | "
                                f"SL={sl_price:.4f} NOT VERIFIED! "
                                f"Will retry next cycle"
                            )

                await asyncio.sleep(0.3)  # Rate limit

        except Exception as e:
            logger.error(f"Safety monitor error: {e}", exc_info=True)
            await asyncio.sleep(10)


async def _recover_and_check_positions():
    """Recover active trades from DB, reconcile with Bybit, detect orphans.

    Called once on startup. Replaces the old _check_orphan_positions().

    Steps:
      1. Load persisted trades from DB → restore into trade_mgr
      2. For each recovered trade, verify position still exists on Bybit
      3. Check which TP/DCA orders filled during downtime
      4. Adjust trade state + SL accordingly
      5. Detect orphan positions (on Bybit but not tracked)
    """
    try:
        # ── Step 1: Trades already pre-loaded in lifespan startup ──
        active_count = trade_mgr.active_count
        if active_count:
            logger.info(f"RECOVERY: Reconciling {active_count} pre-loaded trades with Bybit...")

        # ── Step 2+3: Reconcile each recovered trade with Bybit ──
        for trade in list(trade_mgr.active_trades):
            pos = bybit.get_position(trade.symbol)

            if pos is None or pos["size"] == 0:
                # Position was closed during downtime (SL/TP triggered by Bybit)
                price = bybit.get_ticker_price(trade.symbol) or trade.avg_price
                remaining = trade.remaining_qty
                if remaining > 0:
                    if trade.side == "long":
                        pnl = (price - trade.avg_price) * remaining
                    else:
                        pnl = (trade.avg_price - price) * remaining
                    trade.realized_pnl += pnl
                trade_mgr.close_trade(
                    trade, price, trade.realized_pnl,
                    "Closed during bot downtime (exchange-side)"
                )
                logger.info(
                    f"RECOVERY: {trade.symbol_display} was closed during downtime | "
                    f"PnL: ${trade.realized_pnl:+.2f}"
                )
                continue

            # Position exists - update qty from exchange (source of truth)
            trade.total_qty = pos["size"]
            trade.avg_price = pos["avg_price"]

            # ── Check TP order fills that happened during downtime ──
            tps_updated = False
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
                        tps_updated = True
                        logger.info(
                            f"RECOVERY: TP{tp_idx+1} was filled during downtime | "
                            f"{trade.symbol_display} @ {tp_fill_price:.4f}"
                        )

            if tps_updated:
                # Apply Strategy C SL ladder that was missed
                # Find the highest TP that filled
                highest_tp = -1
                for i in range(len(trade.tp_filled)):
                    if trade.tp_filled[i]:
                        highest_tp = i

                if highest_tp >= 0:
                    if all(trade.tp_filled):
                        # All TPs filled → trailing mode (SL at TP1)
                        last_tp_price = trade.tp_prices[-1]
                        trail_dist = last_tp_price * config.trailing_callback_pct / 100
                        trade.hard_sl_price = trade.tp_prices[0]  # SL at TP1
                        sl_ok = bybit.set_trading_stop(
                            trade.symbol, trade.side,
                            stop_loss=trade.hard_sl_price,
                            trailing_stop=trail_dist,
                        )
                        trade.status = TradeStatus.TRAILING
                        if sl_ok:
                            logger.info(
                                f"RECOVERY: All TPs filled → trailing: {trade.symbol_display} | "
                                f"SL=TP1={trade.hard_sl_price:.4f}"
                            )
                        else:
                            logger.critical(
                                f"RECOVERY: Trailing SL FAILED: {trade.symbol_display} | "
                                f"SL={trade.hard_sl_price:.4f} NOT VERIFIED!"
                            )
                    elif highest_tp >= 2:
                        # TP3+: SL → TP2 price (if scale-in) or TP1 price (no scale-in)
                        if trade.scale_in_filled:
                            sl_target = trade.tp_prices[1]  # TP2 price
                            sl_label = "TP2 (scale-in)"
                        else:
                            sl_target = trade.tp_prices[0]  # TP1 price
                            sl_label = "TP1"
                        sl_ok = bybit.set_trading_stop(
                            trade.symbol, trade.side,
                            stop_loss=sl_target,
                        )
                        trade.hard_sl_price = sl_target
                        for dca in trade.dca_levels[1:]:
                            if dca.order_id and not dca.filled:
                                bybit.cancel_order(trade.symbol, dca.order_id)
                                dca.order_id = ""
                        if sl_ok:
                            logger.info(
                                f"RECOVERY: TP{highest_tp+1}→SL={sl_label}: {trade.symbol_display} | "
                                f"SL={sl_target:.4f}"
                            )
                        else:
                            logger.critical(
                                f"RECOVERY: TP{highest_tp+1}→SL={sl_label} FAILED: "
                                f"{trade.symbol_display} | SL={sl_target:.4f} NOT VERIFIED!"
                            )
                    elif highest_tp <= 1 and config.sl_to_be_after_tp1:
                        # TP1 or TP2: SL at BE + 0.1% buffer
                        # Note: if TP2 filled during downtime, scale-in is SKIPPED
                        # (too risky after downtime, market may have moved)
                        if highest_tp == 1 and config.scale_in_enabled and trade.current_dca == 0:
                            logger.warning(
                                f"RECOVERY: TP2 filled during downtime, scale-in SKIPPED: "
                                f"{trade.symbol_display} (market may have moved)"
                            )
                        buffer = config.be_buffer_pct / 100
                        if trade.side == "long":
                            be_price = trade.signal_entry * (1 + buffer)
                        else:
                            be_price = trade.signal_entry * (1 - buffer)
                        sl_ok = bybit.set_trading_stop(
                            trade.symbol, trade.side,
                            stop_loss=be_price,
                        )
                        trade.hard_sl_price = be_price
                        for dca in trade.dca_levels[1:]:
                            if dca.order_id and not dca.filled:
                                bybit.cancel_order(trade.symbol, dca.order_id)
                                dca.order_id = ""
                        if sl_ok:
                            logger.info(
                                f"RECOVERY: TP{highest_tp+1}→SL=BE: {trade.symbol_display} | "
                                f"SL={be_price:.4f} (entry+{config.be_buffer_pct}% buffer)"
                            )
                        else:
                            logger.critical(
                                f"RECOVERY: SL=BE FAILED: {trade.symbol_display} | "
                                f"SL={be_price:.4f} NOT VERIFIED!"
                            )

            # ── Check DCA order fills that happened during downtime ──
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
                    _cancel_unfilled_tps(trade)
                    trade_mgr.setup_dca_tps(trade)
                    _consolidate_tp_qtys(trade)
                    _place_dca_tps(trade)
                    _set_exchange_stops_after_dca(trade)
                    logger.info(
                        f"RECOVERY: DCA{i} was filled during downtime | "
                        f"{trade.symbol_display} @ {dca_fill_price:.4f}"
                    )

            # ── Verify SL is set on exchange ──
            if pos["stop_loss"] == 0 and pos["trailing_stop"] == 0:
                if trade.hard_sl_price > 0:
                    sl_ok = bybit.set_trading_stop(
                        trade.symbol, trade.side,
                        stop_loss=trade.hard_sl_price,
                    )
                    if sl_ok:
                        logger.warning(
                            f"RECOVERY: SL restored: {trade.symbol_display} | "
                            f"SL={trade.hard_sl_price:.4f}"
                        )
                    else:
                        logger.critical(
                            f"RECOVERY: SL restore FAILED: {trade.symbol_display} | "
                            f"SL={trade.hard_sl_price:.4f} NOT VERIFIED!"
                        )

            # Persist updated state
            trade_mgr.persist_trade(trade)

            logger.info(
                f"RECOVERY OK: {trade.symbol_display} {trade.side.upper()} | "
                f"Status: {trade.status.value} | Qty: {pos['size']} | "
                f"Avg: {pos['avg_price']:.4f} | SL: {trade.hard_sl_price:.4f} | "
                f"TPs: {trade.tps_hit}/{len(trade.tp_prices)}"
            )

            await asyncio.sleep(0.3)  # Rate limit

        # ── Step 4: Detect orphan positions (on Bybit but not tracked) ──
        all_positions = bybit.get_all_positions()
        if all_positions:
            tracked_symbols = set()
            for trade in trade_mgr.active_trades:
                if trade.status != TradeStatus.CLOSED:
                    tracked_symbols.add((trade.symbol, trade.side))

            for pos in all_positions:
                key = (pos["symbol"], pos["side"])
                if key not in tracked_symbols:
                    logger.warning(
                        f"ORPHAN POSITION: {pos['symbol']} {pos['side'].upper()} | "
                        f"size={pos['size']} | avg={pos['avg_price']} | "
                        f"SL={'SET' if pos['stop_loss'] > 0 else 'NONE!'} | "
                        f"uPnL={pos['unrealized_pnl']:+.2f} | "
                        f"NOT tracked by bot!"
                    )

        total_active = trade_mgr.active_count
        total_exchange = len(all_positions) if all_positions else 0
        logger.info(
            f"RECOVERY COMPLETE: {total_active} active trades, "
            f"{total_exchange} positions on Bybit"
        )

    except Exception as e:
        logger.error(f"Recovery error: {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════
# ▌ BYBIT TRADE SYNC (catch manual closes)
# ══════════════════════════════════════════════════════════════════════════

TRADE_SYNC_INTERVAL = 120  # seconds (every 2 minutes)


def _aggregate_closed_pnl(records: list[dict]) -> list[dict]:
    """Aggregate closed PnL records by (symbol, side) within time window.

    Bybit returns one record per execution fill. A single close order
    can produce multiple fills (matched against different counterparties).
    This aggregates them into one record per position close event.

    Groups records that share (symbol, side) and have close times within
    60 seconds of each other.
    """
    AGGREGATE_WINDOW_SECONDS = 60

    if not records:
        return []

    # Sort by symbol, side, time
    sorted_recs = sorted(
        records, key=lambda r: (r["symbol"], r["side"], r["created_time"])
    )

    aggregated = []
    i = 0
    while i < len(sorted_recs):
        group = [sorted_recs[i]]
        j = i + 1

        # Group consecutive records with same (symbol, side) within time window
        while j < len(sorted_recs):
            curr = sorted_recs[j]
            prev = group[-1]

            if (curr["symbol"] == prev["symbol"] and
                    curr["side"] == prev["side"] and
                    abs(curr["created_time"] - prev["created_time"]) <= AGGREGATE_WINDOW_SECONDS):
                group.append(curr)
                j += 1
            else:
                break

        # Aggregate the group
        total_qty = sum(r["qty"] for r in group)
        total_pnl = sum(r["closed_pnl"] for r in group)

        # Weighted average exit price
        if total_qty > 0:
            avg_exit = sum(r["qty"] * r["exit_price"] for r in group) / total_qty
        else:
            avg_exit = group[0]["exit_price"]

        aggregated.append({
            "symbol": group[0]["symbol"],
            "side": group[0]["side"],
            "qty": total_qty,
            "entry_price": group[0]["entry_price"],
            "exit_price": avg_exit,
            "closed_pnl": total_pnl,
            "order_type": group[0]["order_type"],
            "leverage": group[0]["leverage"],
            "created_time": group[0]["created_time"],
            "updated_time": group[-1]["updated_time"],
            "fill_count": len(group),
        })

        i = j

    return aggregated


async def bybit_trade_sync():
    """Background: sync closed PnL from Bybit into our DB.

    Only syncs trades closed AFTER bot startup (not historical).
    Catches trades closed manually, by liquidation, or any other
    method our bot didn't track.

    Aggregates multiple execution fills for the same position close
    into a single trade record (Bybit returns one record per fill).
    """
    logger.info("Bybit trade sync started")
    await asyncio.sleep(30)  # Wait for startup to settle

    # Only sync trades closed after bot started (prevents re-inserting deleted trades)
    sync_start_ms = int(time.time() * 1000)
    logger.info(f"Bybit sync: only syncing trades after {sync_start_ms}")

    while True:
        try:
            await asyncio.sleep(TRADE_SYNC_INTERVAL)

            records = bybit.get_closed_pnl(limit=20, start_time_ms=sync_start_ms)
            if not records:
                continue

            # Aggregate execution fills into position-level records.
            # A single close order can have multiple fills on Bybit.
            aggregated = _aggregate_closed_pnl(records)

            for rec in aggregated:
                # Check if already in DB (by open time match)
                existing = db.get_trade_by_symbol_time(
                    rec["symbol"], rec["created_time"]
                )
                if existing:
                    continue

                # Also check by close time (catches bot-managed trades
                # that were saved with different opened_at)
                existing_close = db.get_trade_by_symbol_close_time(
                    rec["symbol"], rec["updated_time"]
                )
                if existing_close:
                    continue

                # Check if this close event falls within an existing
                # trade's lifetime (catches partial TP fills like TP2
                # close events that are separate Bybit PnL records)
                if db.get_trade_by_symbol_in_range(
                    rec["symbol"], rec["created_time"]
                ):
                    continue

                # Check if bot-managed (don't double-save)
                is_tracked = False
                for trade in trade_mgr.active_trades:
                    if (trade.symbol == rec["symbol"] and
                            trade.side == rec["side"]):
                        is_tracked = True
                        break
                if is_tracked:
                    continue

                # Not tracked and not in DB → save it
                trade_id = f"bybit_{rec['symbol']}_{rec['side']}_{int(rec['created_time'])}"
                equity = bybit.get_equity() or 0
                db.save_trade(
                    trade_id=trade_id,
                    symbol=rec["symbol"],
                    side=rec["side"],
                    entry_price=rec["entry_price"],
                    avg_price=rec["entry_price"],
                    close_price=rec["exit_price"],
                    total_qty=rec["qty"],
                    total_margin=rec["qty"] * rec["entry_price"] / config.leverage,
                    realized_pnl=rec["closed_pnl"],
                    max_dca=0,
                    tp1_hit=False,
                    close_reason=f"Bybit sync ({rec['order_type']})",
                    opened_at=rec["created_time"],
                    closed_at=rec["updated_time"],
                    signal_leverage=config.leverage,
                    equity_at_entry=equity,
                    equity_at_close=equity,
                    leverage=config.leverage,
                    equity_pct_per_trade=config.equity_pct_per_trade,
                )
                fill_info = f" ({rec['fill_count']} fills)" if rec.get("fill_count", 1) > 1 else ""
                logger.info(
                    f"BYBIT SYNC: {rec['symbol']} {rec['side'].upper()} | "
                    f"PnL: ${rec['closed_pnl']:+.4f} | Qty: {rec['qty']}{fill_info} | "
                    f"Entry: {rec['entry_price']} → Exit: {rec['exit_price']:.4f}"
                )

        except Exception as e:
            logger.error(f"Bybit trade sync error: {e}", exc_info=True)
            await asyncio.sleep(30)


# ══════════════════════════════════════════════════════════════════════════
# ▌ FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════

async def handle_tg_tp_hit(tp_hit: dict):
    """Cancel unfilled PENDING orders when VIP Club reports TP hit.

    If the channel says "Target #1 Done" for a symbol and we have a PENDING
    (unfilled) limit order for it, the move already happened without us.
    No point waiting for a pullback — cancel and free the slot.
    """
    symbol = tp_hit["symbol"]
    tp_number = tp_hit["tp_number"]

    for trade in list(trade_mgr.active_trades):
        if trade.symbol != symbol:
            continue

        # Only cancel PENDING (unfilled E1 limit orders)
        if trade.status != TradeStatus.PENDING:
            logger.info(
                f"TP hit cancel: {trade.symbol_display} is {trade.status.value} "
                f"(not PENDING) — keeping trade"
            )
            continue

        # Cancel the E1 limit order on Bybit
        bybit.cancel_e1(trade)
        trade_mgr.close_trade(
            trade, 0, 0,
            f"TP#{tp_number} already hit (unfilled)"
        )
        logger.info(
            f"TP hit cancel: {trade.symbol_display} PENDING cancelled | "
            f"VIP Club Target #{tp_number} Done — entry missed"
        )


async def handle_tg_close(close_cmd: dict):
    """Handle a close signal from Telegram."""
    symbol = close_cmd["symbol"]
    for trade in trade_mgr.active_trades:
        if trade.symbol == symbol or trade.symbol_display == close_cmd.get("symbol_display", ""):
            price = bybit.get_ticker_price(trade.symbol)
            # close_full() handles: cancel_all → market close → verify → force-close residual
            success = bybit.close_full(trade, "TG close signal")
            if price:
                remaining = trade.remaining_qty
                if trade.side == "long":
                    pnl = (price - trade.avg_price) * remaining
                else:
                    pnl = (trade.avg_price - price) * remaining
                trade.realized_pnl += pnl
            trade_mgr.close_trade(
                trade, price or 0, trade.realized_pnl, "TG close signal"
            )
            logger.info(
                f"TG close executed: {symbol} PnL ${trade.realized_pnl:+.2f} | "
                f"success={success}"
            )
            return
    logger.info(f"TG close: no active trade for {symbol}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global monitor_task, zone_refresh_task, safety_task, sync_task, tg_listener

    logger.info("Signal DCA Bot v2 starting...")
    config.print_summary()

    # Init database + warmup zone cache
    db.init_tables()
    zone_mgr.warmup_cache()

    # Recover active trades from DB before starting monitors
    # (quick load from DB, Bybit reconciliation happens in safety_monitor)
    persisted_count = trade_mgr.load_persisted_trades()
    if persisted_count:
        logger.info(
            f"Startup: {persisted_count} trades pre-loaded from DB | "
            f"Bybit reconciliation in 15s..."
        )

    # Start Telegram listener (if configured)
    tg_listener = TelegramListener(
        config,
        on_signal=add_signal_to_batch,
        on_close=handle_tg_close,
        on_tp_hit=handle_tg_tp_hit,
    )
    await tg_listener.start()

    monitor_task = asyncio.create_task(price_monitor())
    zone_refresh_task = asyncio.create_task(zone_refresh_loop())
    safety_task = asyncio.create_task(safety_monitor())
    sync_task = asyncio.create_task(bybit_trade_sync())

    yield

    if monitor_task:
        monitor_task.cancel()
    if zone_refresh_task:
        zone_refresh_task.cancel()
    if safety_task:
        safety_task.cancel()
    if sync_task:
        sync_task.cancel()
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
                qty = trade.remaining_qty if trade.tps_hit > 0 else trade.total_qty
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

        # PENDING trades: E1 never filled, just cancel and remove
        if trade.status == TradeStatus.PENDING or trade.total_qty <= 0:
            bybit.cancel_all_orders(trade.symbol)
            trade_mgr.close_trade(
                trade, 0, 0,
                f"Neo Cloud trend switch ({direction}) - unfilled"
            )
            closed.append({
                "trade_id": trade.trade_id,
                "symbol": trade.symbol_display,
                "side": trade.side,
                "pnl": "$0.00 (unfilled)",
            })
            logger.info(
                f"Neo Cloud cancelled unfilled: {trade.symbol_display} {trade.side.upper()}"
            )
            continue

        # FILLED trades: close position on exchange
        # close_full() handles: cancel_all → market close → verify → force-close residual
        price = bybit.get_ticker_price(trade.symbol)
        success = bybit.close_full(trade, f"Neo Cloud {direction}")

        if price:
            remaining = trade.remaining_qty
            if trade.side == "long":
                pnl = (price - trade.avg_price) * remaining
            else:
                pnl = (trade.avg_price - price) * remaining
            trade.realized_pnl += pnl

        trade_mgr.close_trade(
            trade, price or 0, trade.realized_pnl,
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
            f"PnL: ${trade.realized_pnl:+.2f} | success={success}"
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
    """Receive zone data + Neo Cloud from LuxAlgo alert scripting.

    Combined endpoint: zones + neo cloud in one alert.
    Neo Cloud trend switch is detected server-side (neo_lead vs neo_lag).

    LuxAlgo alert script (fires on Neo Cloud cross):
      @alert("\"symbol\":\"{{ticker}}\",\"s1\":{rz_s1},\"r1\":{rz_r1},\"r2\":{rz_r2},\"r3\":{rz_r3},\"neo_lead\":{neo_lead},\"neo_lag\":{neo_lag}") = {neo_lead} cross {neo_lag}

    Also accepts legacy format (zone-only, no neo cloud).
    """
    import json as json_lib

    raw = await request.body()
    text = raw.decode("utf-8").strip()

    # TradingView sends the full @alert() script text, not just the message.
    # Extract JSON from: @alert("\"symbol\":\"X\",...") = condition
    if text.startswith("@alert("):
        # Find content between @alert(" and ") =
        start = text.find('("')
        end = text.find('") =')
        if start != -1 and end != -1:
            text = text[start + 2:end]  # Extract inner content
            text = text.replace('\\"', '"')  # Unescape quotes
            logger.debug(f"Extracted from @alert: {text[:100]}")

    # Wrap in braces if missing (TradingView JSON bypass)
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
    if not raw_symbol or raw_symbol.upper() in ("NAN", "NULL", ""):
        return JSONResponse(
            {"status": "error", "reason": "Symbol missing or NaN. LuxAlgo has no {{ticker}} placeholder - hardcode the symbol in your @alert() script, e.g. \"symbol\":\"HYPEUSDT\""},
            status_code=400,
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
        if s3 == 0 and r3 > 0:
            s3 = 2 * rz_avg - r3

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
        f"Zone push: {symbol_clean} | "
        f"S1={zones.s1:.4f} R1={zones.r1:.4f}"
    )

    # Re-snap any active DCA orders to updated zones
    await resnap_active_dcas(symbol_clean)

    # ── Neo Cloud trend detection (optional fields) ──
    neo_result = None
    neo_lead = body.get("neo_lead")
    neo_lag = body.get("neo_lag")

    if neo_lead is not None and neo_lag is not None:
        try:
            neo_lead = float(neo_lead)
            neo_lag = float(neo_lag)
        except (ValueError, TypeError):
            neo_lead = neo_lag = None

    if neo_lead is not None and neo_lag is not None and neo_lead != 0:
        # Determine current trend from Neo Cloud values
        new_direction = "up" if neo_lead > neo_lag else "down"

        # Check if trend CHANGED (switch)
        old_direction = db.get_neo_cloud(symbol_clean)
        db.upsert_neo_cloud(symbol_clean, new_direction)

        if old_direction and old_direction != new_direction:
            # TREND SWITCH detected → close opposing positions
            close_side = "short" if new_direction == "up" else "long"
            logger.info(
                f"NEO CLOUD SWITCH: {symbol_clean} {old_direction.upper()} → "
                f"{new_direction.upper()} | Closing {close_side.upper()} positions"
            )

            closed = []
            for trade in list(trade_mgr.active_trades):
                if trade.symbol != symbol_clean or trade.side != close_side:
                    continue

                # PENDING trades: E1 never filled, just cancel and remove
                if trade.status == TradeStatus.PENDING or trade.total_qty <= 0:
                    bybit.cancel_all_orders(trade.symbol)
                    trade_mgr.close_trade(
                        trade, 0, 0,
                        f"Neo Cloud switch ({new_direction}) - unfilled"
                    )
                    closed.append({
                        "trade_id": trade.trade_id,
                        "side": trade.side,
                        "pnl": "$0.00 (unfilled)",
                    })
                    logger.info(
                        f"Neo Cloud cancelled unfilled: {trade.symbol_display}"
                    )
                    continue

                # FILLED trades: close position on exchange
                # close_full() handles: cancel_all → market close → verify → force-close residual
                price = bybit.get_ticker_price(trade.symbol)
                success = bybit.close_full(trade, f"Neo Cloud {new_direction}")

                if price:
                    remaining = trade.remaining_qty
                    if trade.side == "long":
                        pnl = (price - trade.avg_price) * remaining
                    else:
                        pnl = (trade.avg_price - price) * remaining
                    trade.realized_pnl += pnl

                trade_mgr.close_trade(
                    trade, price or 0, trade.realized_pnl,
                    f"Neo Cloud switch ({new_direction})"
                )
                closed.append({
                    "trade_id": trade.trade_id,
                    "side": trade.side,
                    "pnl": f"${trade.realized_pnl:+.2f}",
                })
                logger.info(
                    f"Neo Cloud closed: {trade.symbol_display} {trade.side.upper()} | "
                    f"PnL: ${trade.realized_pnl:+.2f} | success={success}"
                )

            neo_result = {
                "switch": True,
                "from": old_direction,
                "to": new_direction,
                "closed": closed,
            }
        else:
            neo_result = {
                "switch": False,
                "direction": new_direction,
                "neo_lead": neo_lead,
                "neo_lag": neo_lag,
            }
            if not old_direction:
                logger.info(
                    f"Neo Cloud init: {symbol_clean} → {new_direction.upper()} "
                    f"(lead={neo_lead:.4f}, lag={neo_lag:.4f})"
                )

    result = {
        "status": "ok",
        "symbol": symbol_clean,
        "zones": {"s1": zones.s1, "r1": zones.r1},
    }
    if neo_result:
        result["neo_cloud"] = neo_result

    return JSONResponse(result)


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


@app.post("/recovery/reset")
async def recovery_reset():
    """Emergency: clear all persisted active trades from DB.

    Use when the bot has stale/incorrect trade state in the DB.
    Does NOT close positions on Bybit - only clears the DB state.
    """
    count = trade_mgr.active_count
    db.clear_all_active_trades()
    # Also clear in-memory trades (they'll become orphans on next safety check)
    trade_mgr.trades.clear()
    logger.warning(f"RECOVERY RESET: cleared {count} active trades from DB + memory")
    return JSONResponse({
        "status": "ok",
        "cleared": count,
        "warning": "Trades cleared from bot memory. Positions still open on Bybit!",
    })


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
        "max_fills_per_batch": config.max_fills_per_batch,
        "dca_levels": config.max_dca_levels,
        "dca_mults": config.dca_multipliers[:config.max_dca_levels + 1],
        "tp_pcts": config.tp_close_pcts,
        "trail_pct": 100 - sum(config.tp_close_pcts),
        "trail_cb": config.trailing_callback_pct,
        "safety_sl_pct": config.safety_sl_pct,
        "hard_sl_pct": config.hard_sl_pct,
        "dca_tp_pcts": config.dca_tp_pcts,
        "dca_trail_cb": config.dca_trail_callback_pct,
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


@app.get("/admin/fix-pnl")
async def admin_fix_pnl():
    """Re-sync PnL for recent trades from Bybit closed_pnl.

    Queries Bybit for actual realized PnL (includes fees) and updates DB.
    """
    recent = db.get_recent_trade_ids(days=7)
    if not recent:
        return JSONResponse({"status": "no trades to fix"})

    results = []
    for trade_rec in recent:
        symbol = trade_rec["symbol"]
        side = trade_rec["side"]
        start_ms = int(trade_rec["opened_at"] * 1000) if trade_rec["opened_at"] else 0

        if start_ms == 0:
            continue

        # Query Bybit for closed PnL records
        records = bybit.get_closed_pnl(limit=50, start_time_ms=start_ms)
        matching = [
            r for r in records
            if r["symbol"] == symbol and r["side"] == side
        ]

        if not matching:
            continue

        bybit_pnl = sum(r["closed_pnl"] for r in matching)
        old_pnl = trade_rec["realized_pnl"]
        diff = bybit_pnl - old_pnl

        if abs(diff) < 0.001:
            continue  # Already correct

        # Update DB with Bybit PnL
        updated = db.update_trade_pnl(
            trade_id=trade_rec["trade_id"],
            realized_pnl=bybit_pnl,
            total_margin=trade_rec["total_margin"],
            equity_at_entry=trade_rec["equity_at_entry"],
        )

        results.append({
            "trade_id": trade_rec["trade_id"],
            "symbol": symbol,
            "old_pnl": round(old_pnl, 4),
            "bybit_pnl": round(bybit_pnl, 4),
            "diff": round(diff, 4),
            "updated": updated,
        })

    return JSONResponse({
        "status": "done",
        "fixed": len(results),
        "details": results,
    })


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
    html += `TP: ${d.config.tp_pcts.map((p,i) => 'TP'+(i+1)+'='+p+'%').join(', ')} + Trail ${d.config.trail_pct}% (${d.config.trail_cb}% CB) | Safety SL entry-${d.config.safety_sl_pct}% → DCA SL fill+${d.config.hard_sl_pct}% | `;
    html += `DCA Exit: ${d.config.dca_tp_pcts ? d.config.dca_tp_pcts.map((p,i) => 'TP'+(i+1)+'='+p+'%').join(', ') : 'N/A'} from avg, trail @${d.config.dca_trail_cb || 1}%CB | `;
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
