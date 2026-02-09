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

    def setup_symbol(self, symbol: str) -> bool:
        """Set leverage and margin mode for a symbol.

        Only runs once per symbol per session.
        """
        if symbol in self._initialized_symbols:
            return True

        try:
            # Set cross margin mode
            try:
                self.session.set_margin_mode(
                    category="linear",
                    symbol=symbol,
                    tradeMode=0,  # 0 = cross
                )
            except Exception:
                pass  # Already set

            # Set leverage
            try:
                self.session.set_leverage(
                    category="linear",
                    symbol=symbol,
                    buyLeverage=str(self.config.leverage),
                    sellLeverage=str(self.config.leverage),
                )
            except Exception:
                pass  # Already set

            self._initialized_symbols.add(symbol)
            logger.info(f"Symbol setup: {symbol} | Cross {self.config.leverage}x")
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

    def round_qty(self, qty: float, qty_step: float) -> float:
        """Round quantity to valid step size."""
        if qty_step <= 0:
            return qty
        precision = len(str(qty_step).rstrip('0').split('.')[-1]) if '.' in str(qty_step) else 0
        rounded = round(qty // qty_step * qty_step, precision)
        return rounded

    def round_price(self, price: float, tick_size: float) -> float:
        """Round price to valid tick size."""
        if tick_size <= 0:
            return price
        precision = len(str(tick_size).rstrip('0').split('.')[-1]) if '.' in str(tick_size) else 0
        rounded = round(price // tick_size * tick_size, precision)
        return rounded

    def open_trade(self, trade: Trade) -> bool:
        """Execute E1 market order and place DCA limit orders.

        Returns True if E1 was successful.
        """
        symbol = trade.symbol

        # Setup symbol (leverage, margin mode)
        if not self.setup_symbol(symbol):
            return False

        # Get instrument info for rounding
        info = self.get_instrument_info(symbol)
        if not info:
            logger.error(f"Cannot get instrument info for {symbol}")
            return False

        qty_step = info["qty_step"]
        tick_size = info["tick_size"]
        min_qty = info["min_qty"]

        # ── E1: Market order ──
        e1 = trade.dca_levels[0]
        e1_qty = self.round_qty(e1.qty, qty_step)

        if e1_qty < min_qty:
            logger.error(
                f"E1 qty too small: {e1_qty} < {min_qty} for {symbol}"
            )
            return False

        side_str = "Buy" if trade.side == "long" else "Sell"

        try:
            result = self.session.place_order(
                category="linear",
                symbol=symbol,
                side=side_str,
                orderType="Market",
                qty=str(e1_qty),
                timeInForce="GTC",
                orderLinkId=f"{trade.trade_id}_E1",
            )

            order_id = result["result"]["orderId"]
            e1.order_id = order_id
            e1.filled = True
            logger.info(f"E1 filled: {symbol} {side_str} {e1_qty} | Order: {order_id}")

        except Exception as e:
            logger.error(f"E1 order failed for {symbol}: {e}")
            return False

        # ── DCA: Limit orders ──
        for i in range(1, trade.max_dca + 1):
            if i >= len(trade.dca_levels):
                break

            dca = trade.dca_levels[i]
            dca_qty = self.round_qty(dca.qty, qty_step)
            dca_price = self.round_price(dca.price, tick_size)

            if dca_qty < min_qty:
                logger.warning(f"DCA{i} qty too small: {dca_qty} for {symbol}, skipping")
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

        return True

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

        info = self.get_instrument_info(trade.symbol)
        if not info:
            return False

        qty = self.round_qty(remaining, info["qty_step"])
        close_side = "Sell" if trade.side == "long" else "Buy"

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
                    }
            return None
        except Exception as e:
            logger.error(f"Get position failed for {symbol}: {e}")
            return None

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
