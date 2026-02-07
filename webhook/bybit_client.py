"""Bybit Unified Trading API wrapper for hedge-mode perpetuals."""

import asyncio
import logging
from pybit.unified_trading import HTTP

from config import cfg

log = logging.getLogger("bybit")


# Action → (side, positionIdx, reduceOnly)
ACTION_MAP = {
    "open_long":    ("Buy",  1, False),
    "open_short":   ("Sell", 2, False),
    "dca_long":     ("Buy",  1, False),
    "dca_short":    ("Sell", 2, False),
    "flip_long":    ("Buy",  1, False),
    "flip_short":   ("Sell", 2, False),
    "close_long":   ("Sell", 1, True),
    "close_short":  ("Buy",  2, True),
    "exit_x_long":  ("Sell", 1, True),
    "exit_x_short": ("Buy",  2, True),
    "trail_long":   ("Sell", 1, True),
    "trail_short":  ("Buy",  2, True),
    "sl_long":      ("Sell", 1, True),
    "sl_short":     ("Buy",  2, True),
    "emerg_long":   ("Sell", 1, True),
    "emerg_short":  ("Buy",  2, True),
}


class BybitClient:
    def __init__(self):
        self.session = HTTP(
            testnet=cfg.BYBIT_TESTNET,
            api_key=cfg.BYBIT_API_KEY,
            api_secret=cfg.BYBIT_API_SECRET,
        )
        self.symbol = cfg.SYMBOL
        # Track active SL order IDs per side
        self.sl_order_ids: dict[int, str] = {}  # positionIdx → orderId

    def get_ticker_price(self) -> dict:
        """Get current bid/ask/last price."""
        resp = self.session.get_tickers(category="linear", symbol=self.symbol)
        ticker = resp["result"]["list"][0]
        return {
            "bid": float(ticker["bid1Price"]),
            "ask": float(ticker["ask1Price"]),
            "last": float(ticker["lastPrice"]),
        }

    def get_tick_size(self) -> float:
        """Get minimum price increment for the symbol."""
        resp = self.session.get_instruments_info(
            category="linear", symbol=self.symbol
        )
        return float(resp["result"]["list"][0]["priceFilter"]["tickSize"])

    def round_price(self, price: float, tick_size: float) -> str:
        """Round price to valid tick size."""
        rounded = round(price / tick_size) * tick_size
        # Format without trailing zeros but respecting tick precision
        decimals = len(str(tick_size).rstrip('0').split('.')[-1]) if '.' in str(tick_size) else 0
        return f"{rounded:.{decimals}f}"

    def round_qty(self, qty: float) -> str:
        """Round qty to valid step size."""
        resp = self.session.get_instruments_info(
            category="linear", symbol=self.symbol
        )
        step = float(resp["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
        rounded = round(qty / step) * step
        decimals = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0
        return f"{rounded:.{decimals}f}"

    def place_limit_order(
        self,
        side: str,
        qty: float,
        price: float,
        position_idx: int,
        reduce_only: bool = False,
    ) -> dict:
        """Place a limit order. Returns order result."""
        tick = self.get_tick_size()
        params = dict(
            category="linear",
            symbol=self.symbol,
            side=side,
            orderType="Limit",
            qty=self.round_qty(qty),
            price=self.round_price(price, tick),
            positionIdx=position_idx,
            timeInForce="GTC",
        )
        if reduce_only:
            params["reduceOnly"] = True

        log.info("LIMIT %s %s qty=%s price=%s posIdx=%d reduce=%s",
                 side, self.symbol, params["qty"], params["price"],
                 position_idx, reduce_only)
        resp = self.session.place_order(**params)
        log.info("Order response: %s", resp)
        return resp

    def place_market_order(
        self,
        side: str,
        qty: float,
        position_idx: int,
        reduce_only: bool = False,
    ) -> dict:
        """Place a market order. Returns order result."""
        params = dict(
            category="linear",
            symbol=self.symbol,
            side=side,
            orderType="Market",
            qty=self.round_qty(qty),
            positionIdx=position_idx,
            timeInForce="GTC",
        )
        if reduce_only:
            params["reduceOnly"] = True

        log.info("MARKET %s %s qty=%s posIdx=%d reduce=%s",
                 side, self.symbol, params["qty"], position_idx, reduce_only)
        resp = self.session.place_order(**params)
        log.info("Order response: %s", resp)
        return resp

    def get_order_status(self, order_id: str) -> dict:
        """Check if an order has been filled."""
        resp = self.session.get_open_orders(
            category="linear",
            symbol=self.symbol,
            orderId=order_id,
        )
        orders = resp["result"]["list"]
        if orders:
            return orders[0]
        # Order not in open orders → check history
        resp = self.session.get_order_history(
            category="linear",
            symbol=self.symbol,
            orderId=order_id,
        )
        orders = resp["result"]["list"]
        return orders[0] if orders else {}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        log.info("CANCEL order %s", order_id)
        try:
            resp = self.session.cancel_order(
                category="linear",
                symbol=self.symbol,
                orderId=order_id,
            )
            return resp
        except Exception as e:
            log.warning("Cancel failed (may already be filled/cancelled): %s", e)
            return {}

    def set_stop_loss(self, position_idx: int, sl_price: float) -> dict:
        """Set or update stop loss for a position via trading stop."""
        tick = self.get_tick_size()
        price_str = self.round_price(sl_price, tick)
        log.info("SET SL posIdx=%d sl=%s", position_idx, price_str)
        try:
            resp = self.session.set_trading_stop(
                category="linear",
                symbol=self.symbol,
                positionIdx=position_idx,
                stopLoss=price_str,
                slTriggerBy="LastPrice",
            )
            log.info("SL response: %s", resp)
            return resp
        except Exception as e:
            log.error("Set SL failed: %s", e)
            return {}

    def cancel_stop_loss(self, position_idx: int) -> dict:
        """Remove stop loss from a position."""
        log.info("CANCEL SL posIdx=%d", position_idx)
        try:
            resp = self.session.set_trading_stop(
                category="linear",
                symbol=self.symbol,
                positionIdx=position_idx,
                stopLoss="0",
            )
            return resp
        except Exception as e:
            log.warning("Cancel SL failed: %s", e)
            return {}

    def get_positions(self) -> list:
        """Get current open positions."""
        resp = self.session.get_positions(
            category="linear",
            symbol=self.symbol,
        )
        return resp["result"]["list"]

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders for the symbol."""
        log.warning("CANCEL ALL orders for %s", self.symbol)
        return self.session.cancel_all_orders(
            category="linear",
            symbol=self.symbol,
        )
