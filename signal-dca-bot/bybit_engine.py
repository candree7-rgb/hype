"""
Bybit Trading Engine - Executes orders on Bybit via pybit.

Handles:
- Setting leverage
- Market orders (E1 entry)
- Limit orders (DCA levels)
- Take profit orders (partial close)
- Closing positions
- Position & balance queries
"""

import logging
import time
from config import BotConfig
from trade_manager import Trade

logger = logging.getLogger(__name__)


class BybitEngine:
    """Handles all Bybit API interactions."""

    def __init__(self, config: BotConfig):
        self.config = config
        self._session = None
        self._initialized_symbols: set[str] = set()
        self._hedge_mode: bool = False  # Detected at first setup_symbol call

    @property
    def session(self):
        if self._session is None:
            self._connect()
        return self._session

    def _connect(self):
        """Initialize pybit HTTP session."""
        try:
            from pybit.unified_trading import HTTP

            self._session = HTTP(
                testnet=self.config.bybit_testnet,
                api_key=self.config.bybit_api_key,
                api_secret=self.config.bybit_api_secret,
            )
            logger.info(
                f"Bybit connected ({'TESTNET' if self.config.bybit_testnet else 'LIVE'})"
            )
        except ImportError:
            logger.error("pybit not installed. Run: pip install pybit")
            raise
        except Exception as e:
            logger.error(f"Bybit connection failed: {e}")
            raise

    def get_equity(self) -> float:
        """Get current USDT equity."""
        try:
            result = self.session.get_wallet_balance(
                accountType="UNIFIED",
                coin="USDT",
            )
            coins = result["result"]["list"][0]["coin"]
            for coin in coins:
                if coin["coin"] == "USDT":
                    return float(coin["equity"])
            return 0.0
        except Exception as e:
            logger.error(f"Failed to get equity: {e}")
            return 0.0

    def detect_position_mode(self, symbol: str) -> None:
        """Auto-detect Bybit position mode (One-Way vs Hedge).

        In Hedge mode, get_positions returns 2 entries per symbol
        (Buy side + Sell side). In One-Way mode, returns 1 entry.
        """
        try:
            result = self.session.get_positions(
                category="linear",
                symbol=symbol,
            )
            positions = result["result"]["list"]
            self._hedge_mode = len(positions) >= 2
            mode_str = "Hedge (BothSide)" if self._hedge_mode else "One-Way"
            logger.info(f"Position mode detected: {mode_str}")
        except Exception as e:
            logger.warning(f"Could not detect position mode: {e}")
            self._hedge_mode = False

    def _position_idx(self, trade_side: str) -> dict:
        """Get positionIdx kwarg for Bybit orders.

        Hedge mode: long=1, short=2
        One-Way mode: empty dict (don't send positionIdx)
        """
        if not self._hedge_mode:
            return {}
        return {"positionIdx": 1 if trade_side == "long" else 2}

    def setup_symbol(self, symbol: str, leverage: int = 0) -> bool:
        """Set leverage and margin mode for a symbol.

        Args:
            symbol: Trading pair
            leverage: Leverage to set (0 = use config default)
        """
        lev = leverage if leverage > 0 else self.config.leverage

        try:
            # Detect position mode on first symbol setup
            if not self._initialized_symbols:
                self.detect_position_mode(symbol)

            # Set cross margin mode
            try:
                self.session.set_margin_mode(
                    category="linear",
                    symbol=symbol,
                    tradeMode=0,  # 0 = cross
                )
            except Exception:
                pass  # Already set

            # Set leverage (always update, may differ per trade)
            try:
                self.session.set_leverage(
                    category="linear",
                    symbol=symbol,
                    buyLeverage=str(lev),
                    sellLeverage=str(lev),
                )
            except Exception:
                pass  # Already set to same value

            self._initialized_symbols.add(symbol)
            logger.info(f"Symbol setup: {symbol} | Cross {lev}x")
            return True

        except Exception as e:
            logger.error(f"Symbol setup failed for {symbol}: {e}")
            return False

    def get_ticker_price(self, symbol: str) -> float | None:
        """Get current mark price for a symbol."""
        try:
            result = self.session.get_tickers(
                category="linear",
                symbol=symbol,
            )
            return float(result["result"]["list"][0]["markPrice"])
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None

    def get_instrument_info(self, symbol: str) -> dict | None:
        """Get trading rules (min qty, tick size, etc.)."""
        try:
            result = self.session.get_instruments_info(
                category="linear",
                symbol=symbol,
            )
            info = result["result"]["list"][0]
            return {
                "min_qty": float(info["lotSizeFilter"]["minOrderQty"]),
                "max_qty": float(info["lotSizeFilter"]["maxOrderQty"]),
                "qty_step": float(info["lotSizeFilter"]["qtyStep"]),
                "tick_size": float(info["priceFilter"]["tickSize"]),
                "min_price": float(info["priceFilter"]["minPrice"]),
            }
        except Exception as e:
            logger.error(f"Failed to get instrument info for {symbol}: {e}")
            return None

    def _tick_precision(self, step: float) -> int:
        """Get decimal precision from tick/step size.

        Handles scientific notation (1e-05 → 5 decimals).
        """
        # Format without scientific notation: 1e-05 → "0.00001"
        s = f"{step:.10f}".rstrip('0')
        if '.' in s:
            return len(s.split('.')[-1])
        return 0

    def round_qty(self, qty: float, qty_step: float) -> float:
        """Round quantity to valid step size."""
        if qty_step <= 0:
            return qty
        precision = self._tick_precision(qty_step)
        rounded = round(qty // qty_step * qty_step, precision)
        return rounded

    def round_price(self, price: float, tick_size: float) -> float:
        """Round price to valid tick size."""
        if tick_size <= 0:
            return price
        precision = self._tick_precision(tick_size)
        rounded = round(price // tick_size * tick_size, precision)
        return rounded

    def open_trade(self, trade: Trade, use_limit: bool = True) -> bool:
        """Place E1 order and DCA limit orders.

        Args:
            trade: Trade object with DCA levels calculated
            use_limit: True = Limit order at signal price (no slippage)
                       False = Market order (immediate fill)

        Returns True if E1 order was placed successfully.
        """
        symbol = trade.symbol

        # Setup symbol (leverage from signal, margin mode)
        if not self.setup_symbol(symbol, trade.leverage):
            return False

        # Get instrument info for rounding
        info = self.get_instrument_info(symbol)
        if not info:
            logger.error(f"Cannot get instrument info for {symbol}")
            return False

        qty_step = info["qty_step"]
        tick_size = info["tick_size"]
        min_qty = info["min_qty"]

        # ── E1: Limit order at signal price (or Market) ──
        e1 = trade.dca_levels[0]
        e1_qty = self.round_qty(e1.qty, qty_step)

        if e1_qty < min_qty:
            logger.error(
                f"E1 qty too small: {e1_qty} < {min_qty} for {symbol}"
            )
            return False

        side_str = "Buy" if trade.side == "long" else "Sell"

        pos_idx = self._position_idx(trade.side)

        try:
            if use_limit:
                e1_price = self.round_price(trade.signal_entry, tick_size)
                if e1_price <= 0:
                    logger.error(
                        f"E1 price rounded to 0 for {symbol} "
                        f"(signal={trade.signal_entry}, tick={tick_size})"
                    )
                    return False
                result = self.session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=side_str,
                    orderType="Limit",
                    qty=str(e1_qty),
                    price=str(e1_price),
                    timeInForce="GTC",
                    orderLinkId=f"{trade.trade_id}_E1",
                    **pos_idx,
                )
                order_id = result["result"]["orderId"]
                e1.order_id = order_id
                e1.filled = False  # Not filled yet! Limit order pending
                logger.info(
                    f"E1 limit placed: {symbol} {side_str} {e1_qty} @ {e1_price} | "
                    f"Order: {order_id}"
                )
            else:
                result = self.session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=side_str,
                    orderType="Market",
                    qty=str(e1_qty),
                    timeInForce="GTC",
                    orderLinkId=f"{trade.trade_id}_E1",
                    **pos_idx,
                )
                order_id = result["result"]["orderId"]
                e1.order_id = order_id
                e1.filled = True
                logger.info(
                    f"E1 market filled: {symbol} {side_str} {e1_qty} | "
                    f"Order: {order_id}"
                )

        except Exception as e:
            logger.error(f"E1 order failed for {symbol}: {e}")
            return False

        # ── DCA: Limit orders ──
        # For limit E1: DCA orders are placed LATER (after E1 confirms fill)
        # For market E1: place DCA immediately
        if use_limit and not e1.filled:
            logger.info(f"DCA orders deferred until E1 fills for {symbol}")
            return True

        self._place_dca_orders(trade, info)
        return True

    def _place_dca_orders(self, trade: Trade, info: dict) -> None:
        """Place all DCA limit orders for a trade."""
        symbol = trade.symbol
        side_str = "Buy" if trade.side == "long" else "Sell"
        qty_step = info["qty_step"]
        tick_size = info["tick_size"]
        min_qty = info["min_qty"]
        pos_idx = self._position_idx(trade.side)

        for i in range(1, trade.max_dca + 1):
            if i >= len(trade.dca_levels):
                break

            dca = trade.dca_levels[i]
            dca_qty = self.round_qty(dca.qty, qty_step)
            dca_price = self.round_price(dca.price, tick_size)

            if dca_qty < min_qty:
                logger.warning(f"DCA{i} qty too small: {dca_qty} for {symbol}, skipping")
                continue

            if dca_price <= 0:
                logger.warning(
                    f"DCA{i} price rounded to 0 for {symbol} "
                    f"(raw={dca.price}, tick={tick_size}), skipping"
                )
                continue

            try:
                result = self.session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=side_str,
                    orderType="Limit",
                    qty=str(dca_qty),
                    price=str(dca_price),
                    timeInForce="GTC",
                    orderLinkId=f"{trade.trade_id}_DCA{i}",
                    **pos_idx,
                )

                order_id = result["result"]["orderId"]
                dca.order_id = order_id
                trade.dca_order_ids.append(order_id)
                logger.info(
                    f"DCA{i} placed: {symbol} {side_str} {dca_qty} @ {dca_price} "
                    f"({self.config.dca_multipliers[i]}x) | Order: {order_id}"
                )

            except Exception as e:
                logger.error(f"DCA{i} order failed for {symbol}: {e}")

    def place_dca_for_trade(self, trade: Trade) -> bool:
        """Place DCA orders after E1 limit fills. Called by price monitor."""
        info = self.get_instrument_info(trade.symbol)
        if not info:
            return False
        self._place_dca_orders(trade, info)
        return True

    def check_e1_filled(self, trade: Trade) -> bool:
        """Check if E1 limit order has been filled."""
        e1 = trade.dca_levels[0]
        if e1.filled or not e1.order_id:
            return e1.filled

        try:
            result = self.session.get_order_history(
                category="linear",
                symbol=trade.symbol,
                orderId=e1.order_id,
            )
            orders = result["result"]["list"]
            if orders:
                status = orders[0]["orderStatus"]
                if status == "Filled":
                    fill_price = float(orders[0]["avgPrice"])
                    e1.filled = True
                    e1.price = fill_price
                    trade.avg_price = fill_price
                    trade.total_qty = float(orders[0]["cumExecQty"])
                    trade.total_margin = e1.margin
                    logger.info(
                        f"E1 limit filled: {trade.symbol} @ {fill_price} | "
                        f"Qty: {trade.total_qty}"
                    )
                    return True
                elif status in ("Cancelled", "Rejected", "Deactivated"):
                    logger.info(f"E1 limit cancelled/rejected: {trade.symbol}")
                    e1.order_id = ""
                    return False
        except Exception as e:
            logger.error(f"Check E1 fill failed for {trade.symbol}: {e}")

        return False

    def cancel_e1(self, trade: Trade) -> bool:
        """Cancel unfilled E1 limit order (timeout)."""
        e1 = trade.dca_levels[0]
        if e1.filled or not e1.order_id:
            return False

        try:
            self.session.cancel_order(
                category="linear",
                symbol=trade.symbol,
                orderId=e1.order_id,
            )
            logger.info(f"E1 limit cancelled (timeout): {trade.symbol}")
            return True
        except Exception as e:
            logger.debug(f"Cancel E1 failed for {trade.symbol}: {e}")
            return False

    def close_partial(self, trade: Trade, qty: float, reason: str) -> bool:
        """Close part of a position (e.g., TP1 50% close).

        Args:
            trade: The trade
            qty: Quantity to close
            reason: For logging
        """
        info = self.get_instrument_info(trade.symbol)
        if not info:
            return False

        qty = self.round_qty(qty, info["qty_step"])
        if qty < info["min_qty"]:
            logger.warning(f"Partial close qty too small: {qty} for {trade.symbol}")
            return False

        # Close = opposite side
        close_side = "Sell" if trade.side == "long" else "Buy"

        pos_idx = self._position_idx(trade.side)

        try:
            result = self.session.place_order(
                category="linear",
                symbol=trade.symbol,
                side=close_side,
                orderType="Market",
                qty=str(qty),
                timeInForce="GTC",
                reduceOnly=True,
                orderLinkId=f"{trade.trade_id}_TP1",
                **pos_idx,
            )

            order_id = result["result"]["orderId"]
            trade.tp_order_id = order_id
            logger.info(f"Partial close: {trade.symbol} {qty} | {reason} | Order: {order_id}")
            return True

        except Exception as e:
            logger.error(f"Partial close failed for {trade.symbol}: {e}")
            return False

    def close_full(self, trade: Trade, reason: str) -> bool:
        """Close entire remaining position.

        Args:
            trade: The trade
            reason: For logging
        """
        remaining = trade.remaining_qty
        if remaining <= 0:
            remaining = trade.total_qty

        # Guard: can't close a position with 0 qty (PENDING/unfilled trades)
        if remaining <= 0:
            logger.warning(
                f"close_full skipped: {trade.symbol} has 0 qty "
                f"(status={trade.status}, reason={reason})"
            )
            return False

        info = self.get_instrument_info(trade.symbol)
        if not info:
            return False

        qty = self.round_qty(remaining, info["qty_step"])
        if qty <= 0:
            logger.warning(
                f"close_full skipped: {trade.symbol} rounded qty=0 "
                f"(remaining={remaining}, reason={reason})"
            )
            return False

        close_side = "Sell" if trade.side == "long" else "Buy"

        pos_idx = self._position_idx(trade.side)

        try:
            result = self.session.place_order(
                category="linear",
                symbol=trade.symbol,
                side=close_side,
                orderType="Market",
                qty=str(qty),
                timeInForce="GTC",
                reduceOnly=True,
                orderLinkId=f"{trade.trade_id}_CLOSE",
                **pos_idx,
            )

            order_id = result["result"]["orderId"]
            logger.info(f"Full close: {trade.symbol} {qty} | {reason} | Order: {order_id}")

            # Cancel remaining DCA limit orders
            self._cancel_dca_orders(trade)

            return True

        except Exception as e:
            logger.error(f"Full close failed for {trade.symbol}: {e}")
            return False

    def _cancel_dca_orders(self, trade: Trade) -> None:
        """Cancel all unfilled DCA limit orders for a trade."""
        for dca in trade.dca_levels:
            if not dca.filled and dca.order_id:
                try:
                    self.session.cancel_order(
                        category="linear",
                        symbol=trade.symbol,
                        orderId=dca.order_id,
                    )
                    logger.info(f"Cancelled DCA{dca.level} order: {dca.order_id}")
                except Exception as e:
                    # Order might already be cancelled or filled
                    logger.debug(f"Cancel DCA{dca.level} failed (may be ok): {e}")

    def cancel_all_orders(self, symbol: str) -> None:
        """Cancel ALL open orders for a symbol."""
        try:
            self.session.cancel_all_orders(
                category="linear",
                symbol=symbol,
            )
            logger.info(f"All orders cancelled for {symbol}")
        except Exception as e:
            logger.error(f"Cancel all orders failed for {symbol}: {e}")

    def get_position(self, symbol: str) -> dict | None:
        """Get current position for a symbol."""
        try:
            result = self.session.get_positions(
                category="linear",
                symbol=symbol,
            )
            positions = result["result"]["list"]
            for pos in positions:
                if float(pos["size"]) > 0:
                    return {
                        "symbol": pos["symbol"],
                        "side": "long" if pos["side"] == "Buy" else "short",
                        "size": float(pos["size"]),
                        "avg_price": float(pos["avgPrice"]),
                        "unrealized_pnl": float(pos["unrealisedPnl"]),
                        "leverage": pos["leverage"],
                        "stop_loss": float(pos.get("stopLoss", 0) or 0),
                        "trailing_stop": float(pos.get("trailingStop", 0) or 0),
                    }
            return None
        except Exception as e:
            logger.error(f"Get position failed for {symbol}: {e}")
            return None

    def get_all_positions(self) -> list[dict]:
        """Get ALL open positions (for orphan detection)."""
        try:
            result = self.session.get_positions(
                category="linear",
                settleCoin="USDT",
            )
            positions = []
            for pos in result["result"]["list"]:
                if float(pos["size"]) > 0:
                    positions.append({
                        "symbol": pos["symbol"],
                        "side": "long" if pos["side"] == "Buy" else "short",
                        "size": float(pos["size"]),
                        "avg_price": float(pos["avgPrice"]),
                        "unrealized_pnl": float(pos["unrealisedPnl"]),
                        "leverage": pos["leverage"],
                        "stop_loss": float(pos.get("stopLoss", 0) or 0),
                        "trailing_stop": float(pos.get("trailingStop", 0) or 0),
                    })
            return positions
        except Exception as e:
            logger.error(f"Get all positions failed: {e}")
            return []

    def get_klines(self, symbol: str, interval: str = "15", limit: int = 100) -> list[dict]:
        """Fetch OHLC candles from Bybit.

        Args:
            symbol: e.g. "BTCUSDT"
            interval: "1", "5", "15", "60", "240", "D"
            limit: Number of candles (max 200)

        Returns:
            List of {"open": f, "high": f, "low": f, "close": f} oldest→newest
        """
        try:
            result = self.session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            raw = result["result"]["list"]
            # Bybit returns newest first, reverse for oldest→newest
            candles = []
            for c in reversed(raw):
                candles.append({
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                })
            return candles
        except Exception as e:
            logger.error(f"Failed to get klines for {symbol}: {e}")
            return []

    def amend_order_price(self, symbol: str, order_id: str, new_price: float) -> bool:
        """Amend an existing order's price (e.g., re-snap DCA to new zone).

        Uses Bybit's amend_order API - faster than cancel+replace.
        """
        info = self.get_instrument_info(symbol)
        if not info:
            return False

        rounded_price = self.round_price(new_price, info["tick_size"])

        try:
            self.session.amend_order(
                category="linear",
                symbol=symbol,
                orderId=order_id,
                price=str(rounded_price),
            )
            logger.info(f"Order amended: {order_id} → new price {rounded_price}")
            return True
        except Exception as e:
            logger.error(f"Amend order failed for {order_id}: {e}")
            return False

    def get_open_orders(self, symbol: str) -> list[dict]:
        """Get all open orders for a symbol."""
        try:
            result = self.session.get_open_orders(
                category="linear",
                symbol=symbol,
            )
            return [
                {
                    "order_id": o["orderId"],
                    "link_id": o.get("orderLinkId", ""),
                    "side": o["side"],
                    "price": float(o["price"]),
                    "qty": float(o["qty"]),
                    "status": o["orderStatus"],
                }
                for o in result["result"]["list"]
            ]
        except Exception as e:
            logger.error(f"Get orders failed for {symbol}: {e}")
            return []

    # ══════════════════════════════════════════════════════════════════════
    # ▌ EXCHANGE-SIDE TP / SL / TRAILING
    # ══════════════════════════════════════════════════════════════════════

    def place_tp_order(self, trade: Trade, tp_price: float, qty: float,
                       tp_num: int = 1) -> str | None:
        """Place TP as reduceOnly limit order on Bybit.

        Args:
            trade: The trade
            tp_price: TP target price
            qty: Quantity to close
            tp_num: TP number (1-4) for orderLinkId

        Returns order_id if successful, None otherwise.
        """
        info = self.get_instrument_info(trade.symbol)
        if not info:
            return None

        tp_price = self.round_price(tp_price, info["tick_size"])
        qty = self.round_qty(qty, info["qty_step"])

        if qty < info["min_qty"]:
            logger.warning(f"TP{tp_num} qty too small: {qty} for {trade.symbol}")
            return None

        if tp_price <= 0:
            logger.warning(f"TP{tp_num} price rounded to 0 for {trade.symbol}")
            return None

        close_side = "Sell" if trade.side == "long" else "Buy"
        pos_idx = self._position_idx(trade.side)

        try:
            result = self.session.place_order(
                category="linear",
                symbol=trade.symbol,
                side=close_side,
                orderType="Limit",
                qty=str(qty),
                price=str(tp_price),
                timeInForce="GTC",
                reduceOnly=True,
                orderLinkId=f"{trade.trade_id}_TP{tp_num}",
                **pos_idx,
            )
            order_id = result["result"]["orderId"]
            logger.info(
                f"TP{tp_num} placed: {trade.symbol} {close_side} {qty} @ {tp_price} | "
                f"Order: {order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"TP{tp_num} order failed for {trade.symbol}: {e}")
            return None

    def set_trading_stop(self, symbol: str, trade_side: str,
                         stop_loss: float = 0, trailing_stop: float = 0,
                         active_price: float = 0) -> bool:
        """Set exchange-side SL and/or trailing stop via Bybit API.

        Args:
            symbol: Trading pair
            trade_side: "long" or "short"
            stop_loss: SL price (0 = don't change)
            trailing_stop: Trailing distance in price units (0 = don't change)
            active_price: Price at which trailing activates (0 = immediate)
        """
        pos_idx = self._position_idx(trade_side)

        body = {
            "category": "linear",
            "symbol": symbol,
            "tpslMode": "Full",
        }
        body.update(pos_idx)

        info = self.get_instrument_info(symbol)
        if not info:
            return False

        if stop_loss > 0:
            body["stopLoss"] = str(self.round_price(stop_loss, info["tick_size"]))
        if trailing_stop > 0:
            body["trailingStop"] = str(self.round_price(trailing_stop, info["tick_size"]))
        if active_price > 0:
            body["activePrice"] = str(self.round_price(active_price, info["tick_size"]))

        try:
            self.session.set_trading_stop(**body)
            parts = []
            if stop_loss > 0:
                parts.append(f"SL={stop_loss:.4f}")
            if trailing_stop > 0:
                parts.append(f"Trail={trailing_stop:.4f}")
            if active_price > 0:
                parts.append(f"ActiveAt={active_price:.4f}")
            logger.info(f"Trading stop set: {symbol} | {' | '.join(parts)}")
            return True
        except Exception as e:
            err_str = str(e)
            if "34040" in err_str:
                logger.debug(f"Trading stop unchanged for {symbol}")
                return True
            logger.error(f"Set trading stop failed for {symbol}: {e}")
            return False

    def check_order_filled(self, symbol: str, order_id: str) -> tuple[bool, float]:
        """Check if an order has been filled.

        Returns (is_filled, fill_price).
        """
        try:
            result = self.session.get_order_history(
                category="linear",
                symbol=symbol,
                orderId=order_id,
            )
            orders = result["result"]["list"]
            if orders:
                status = orders[0]["orderStatus"]
                if status == "Filled":
                    fill_price = float(orders[0]["avgPrice"])
                    return True, fill_price
        except Exception as e:
            logger.error(f"Check order fill failed for {order_id}: {e}")
        return False, 0.0

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel a single order by ID."""
        try:
            self.session.cancel_order(
                category="linear",
                symbol=symbol,
                orderId=order_id,
            )
            return True
        except Exception as e:
            logger.debug(f"Cancel order {order_id} failed: {e}")
            return False
