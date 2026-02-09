"""
Trade Manager - Manages active trades, DCA levels, TP/SL logic.

Core responsibilities:
- Track active trades (max 6 slots)
- Calculate DCA prices and sizes
- Determine when to close (TP, trail, BE)
- Track P&L and statistics
"""

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from config import BotConfig
from telegram_parser import Signal

logger = logging.getLogger(__name__)


class TradeStatus(str, Enum):
    PENDING = "pending"     # E1 limit order placed, waiting for fill
    OPEN = "open"           # E1 filled, waiting for TP or DCA
    DCA_ACTIVE = "dca"      # In DCA mode (price went against us)
    TRAILING = "trailing"   # TP1 hit, trailing remainder
    CLOSING = "closing"     # Closing orders placed
    CLOSED = "closed"       # Fully closed


@dataclass
class DCALevel:
    """Tracks a single DCA entry level."""
    level: int          # 0=E1, 1=DCA1, ..., 5=DCA5
    price: float        # Trigger price
    qty: float = 0.0    # Filled quantity (in coin units)
    margin: float = 0.0 # Margin used (USD)
    filled: bool = False
    order_id: str = ""


@dataclass
class Trade:
    """Represents an active trade with all its DCA levels."""
    # Identity
    trade_id: str
    symbol: str
    symbol_display: str
    side: str               # "long" or "short"

    # Signal info
    signal_entry: float     # Original signal entry price
    signal_leverage: int    # Original signal leverage

    # DCA levels
    dca_levels: list[DCALevel] = field(default_factory=list)

    # Position state
    status: TradeStatus = TradeStatus.OPEN
    total_qty: float = 0.0      # Total position size (coin units)
    total_margin: float = 0.0   # Total margin used
    avg_price: float = 0.0      # Weighted average entry price
    current_dca: int = 0        # Highest DCA level filled (0=only E1)
    max_dca: int = 5            # Max DCA levels for this trade

    # TP tracking
    tp1_hit: bool = False
    tp1_closed_qty: float = 0.0  # Qty closed at TP1
    trailing_high: float = 0.0   # Highest price since TP1 (for trailing)
    trailing_low: float = 0.0    # Lowest price since TP1 (for trailing short)

    # Stop tracking
    stop_price: float = 0.0
    stop_active: bool = False

    # Orders
    tp_order_id: str = ""
    sl_order_id: str = ""
    dca_order_ids: list[str] = field(default_factory=list)

    # Timing
    opened_at: float = 0.0
    closed_at: float = 0.0

    # P&L
    realized_pnl: float = 0.0

    @property
    def unrealized_pnl_pct(self) -> float:
        """Unrealized P&L as % of avg price (not leveraged)."""
        if self.avg_price == 0 or self.total_qty == 0:
            return 0
        # We'd need current price here, so this is calculated externally
        return 0

    @property
    def is_active(self) -> bool:
        return self.status not in (TradeStatus.CLOSED, TradeStatus.CLOSING)

    @property
    def remaining_qty(self) -> float:
        """Qty still in position after partial closes."""
        return self.total_qty - self.tp1_closed_qty

    @property
    def age_hours(self) -> float:
        if self.opened_at == 0:
            return 0
        end = self.closed_at if self.closed_at > 0 else time.time()
        return (end - self.opened_at) / 3600


class TradeManager:
    """Manages all active trades and trading logic."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.trades: dict[str, Trade] = {}  # trade_id → Trade
        self.closed_trades: list[Trade] = []
        self._trade_counter = 0

        # Stats
        self.total_wins = 0
        self.total_losses = 0
        self.total_breakeven = 0
        self.total_pnl = 0.0

    @property
    def active_trades(self) -> list[Trade]:
        return [t for t in self.trades.values() if t.is_active]

    @property
    def active_count(self) -> int:
        return len(self.active_trades)

    @property
    def has_free_slot(self) -> bool:
        return self.active_count < self.config.max_simultaneous_trades

    @property
    def active_long_count(self) -> int:
        return sum(1 for t in self.active_trades if t.side == "long")

    @property
    def active_short_count(self) -> int:
        return sum(1 for t in self.active_trades if t.side == "short")

    def can_open_trade(self, symbol: str) -> tuple[bool, str]:
        """Check if we can open a new trade.

        Returns:
            (can_open, reason)
        """
        if not self.has_free_slot:
            return False, f"Max {self.config.max_simultaneous_trades} trades reached"

        # Check if already in this symbol
        for t in self.active_trades:
            if t.symbol == symbol:
                return False, f"Already in {symbol}"

        # Check blocked coins
        if self.config.blocked_coins:
            base = symbol.replace("USDT", "")
            if base in self.config.blocked_coins:
                return False, f"{base} is blocked"

        # Check allowed coins
        if self.config.allowed_coins:
            base = symbol.replace("USDT", "")
            if base not in self.config.allowed_coins:
                return False, f"{base} not in allowed list"

        return True, "OK"

    def create_trade(self, signal: Signal, equity: float) -> Trade:
        """Create a new trade from a signal.

        Args:
            signal: Parsed signal
            equity: Current account equity in USD

        Returns:
            Trade object with DCA levels calculated
        """
        self._trade_counter += 1
        trade_id = f"{signal.symbol}_{int(time.time())}_{self._trade_counter}"

        # Calculate DCA levels
        dca_levels = []
        total_budget = equity * self.config.equity_pct_per_trade / 100
        base_margin = total_budget / self.config.sum_multipliers

        for i in range(self.config.max_dca_levels + 1):
            price = self.config.dca_price(signal.entry_price, i, signal.side)
            margin = base_margin * self.config.dca_multipliers[i]
            # Qty in coin units: margin × leverage / price
            qty = margin * self.config.leverage / price

            level = DCALevel(
                level=i,
                price=price,
                qty=qty,
                margin=margin,
                filled=False,  # Nothing filled yet (limit order)
            )
            dca_levels.append(level)

        e1 = dca_levels[0]

        # Status depends on order type (limit = pending, market = open)
        initial_status = (
            TradeStatus.PENDING if self.config.e1_limit_order
            else TradeStatus.OPEN
        )

        trade = Trade(
            trade_id=trade_id,
            symbol=signal.symbol,
            symbol_display=signal.symbol_display,
            side=signal.side,
            signal_entry=signal.entry_price,
            signal_leverage=signal.signal_leverage,
            dca_levels=dca_levels,
            status=initial_status,
            total_qty=0 if initial_status == TradeStatus.PENDING else e1.qty,
            total_margin=0 if initial_status == TradeStatus.PENDING else e1.margin,
            avg_price=signal.entry_price,
            current_dca=0,
            max_dca=self.config.max_dca_levels,
            opened_at=time.time(),
        )

        self.trades[trade_id] = trade

        logger.info(
            f"Trade created: {trade.trade_id} | {signal.side.upper()} "
            f"{signal.symbol_display} @ {signal.entry_price} | "
            f"E1: {e1.qty:.6f} coins, ${e1.margin:.2f} margin"
        )

        return trade

    def fill_dca(self, trade: Trade, level: int, fill_price: float) -> None:
        """Record a DCA level as filled.

        Args:
            trade: The trade
            level: DCA level that filled (1-5)
            fill_price: Actual fill price
        """
        if level > len(trade.dca_levels) - 1:
            return

        dca = trade.dca_levels[level]
        dca.filled = True
        dca.price = fill_price  # Update with actual fill price

        # Recalculate qty based on actual fill price
        actual_qty = dca.margin * self.config.leverage / fill_price
        dca.qty = actual_qty

        # Update position
        old_cost = trade.avg_price * trade.total_qty
        new_cost = fill_price * actual_qty
        trade.total_qty += actual_qty
        trade.total_margin += dca.margin
        trade.avg_price = (old_cost + new_cost) / trade.total_qty
        trade.current_dca = level
        trade.status = TradeStatus.DCA_ACTIVE

        logger.info(
            f"DCA{level} filled: {trade.symbol_display} @ {fill_price:.4f} | "
            f"New avg: {trade.avg_price:.4f} | "
            f"Total margin: ${trade.total_margin:.2f} | "
            f"DCA {level}/{trade.max_dca}"
        )

        # Activate stop after last DCA
        if level >= trade.max_dca and self.config.trail_to_breakeven:
            trade.stop_active = True
            trade.stop_price = trade.avg_price  # Trail to breakeven
            logger.info(
                f"Last DCA reached for {trade.symbol_display} | "
                f"Stop activated at avg: {trade.avg_price:.4f}"
            )

    def check_tp(self, trade: Trade, current_price: float) -> dict | None:
        """Check if TP1 should trigger.

        Returns:
            Action dict or None
        """
        if trade.status == TradeStatus.CLOSED:
            return None

        tp1_price = self._calc_tp_price(trade, self.config.tp1_pct)

        if trade.side == "long" and current_price >= tp1_price:
            if not trade.tp1_hit:
                return self._create_tp1_action(trade, current_price, tp1_price)
        elif trade.side == "short" and current_price <= tp1_price:
            if not trade.tp1_hit:
                return self._create_tp1_action(trade, current_price, tp1_price)

        return None

    def check_trailing(self, trade: Trade, current_price: float) -> dict | None:
        """Check trailing stop for remaining position after TP1.

        Returns:
            Action dict or None
        """
        if not trade.tp1_hit or trade.remaining_qty <= 0:
            return None

        if trade.side == "long":
            # Track highest price since TP1
            if current_price > trade.trailing_high:
                trade.trailing_high = current_price

            # Check if price dropped callback% from peak
            callback_price = trade.trailing_high * (1 - self.config.trailing_callback_pct / 100)
            if current_price <= callback_price and current_price > trade.avg_price:
                return {
                    "action": "close_remaining",
                    "trade_id": trade.trade_id,
                    "qty": trade.remaining_qty,
                    "reason": f"Trail callback from {trade.trailing_high:.4f}",
                    "price": current_price,
                }

            # Also check TP2-4 for full close
            for i, tp_pct in enumerate([self.config.tp2_pct, self.config.tp3_pct, self.config.tp4_pct], 2):
                tp_price = self._calc_tp_price(trade, tp_pct)
                if current_price >= tp_price:
                    return {
                        "action": "close_remaining",
                        "trade_id": trade.trade_id,
                        "qty": trade.remaining_qty,
                        "reason": f"TP{i} hit at {tp_pct}%",
                        "price": current_price,
                    }

        else:  # short
            if current_price < trade.trailing_low or trade.trailing_low == 0:
                trade.trailing_low = current_price

            callback_price = trade.trailing_low * (1 + self.config.trailing_callback_pct / 100)
            if current_price >= callback_price and current_price < trade.avg_price:
                return {
                    "action": "close_remaining",
                    "trade_id": trade.trade_id,
                    "qty": trade.remaining_qty,
                    "reason": f"Trail callback from {trade.trailing_low:.4f}",
                    "price": current_price,
                }

            for i, tp_pct in enumerate([self.config.tp2_pct, self.config.tp3_pct, self.config.tp4_pct], 2):
                tp_price = self._calc_tp_price(trade, tp_pct)
                if current_price <= tp_price:
                    return {
                        "action": "close_remaining",
                        "trade_id": trade.trade_id,
                        "qty": trade.remaining_qty,
                        "reason": f"TP{i} hit at {tp_pct}%",
                        "price": current_price,
                    }

        return None

    def check_dca_trigger(self, trade: Trade, current_price: float) -> dict | None:
        """Check if next DCA level should trigger.

        Returns:
            Action dict with DCA order info, or None
        """
        next_level = trade.current_dca + 1
        if next_level > trade.max_dca:
            return None
        if next_level >= len(trade.dca_levels):
            return None

        dca = trade.dca_levels[next_level]
        if dca.filled:
            return None

        if trade.side == "long" and current_price <= dca.price:
            return {
                "action": "dca_fill",
                "trade_id": trade.trade_id,
                "level": next_level,
                "price": dca.price,
                "qty": dca.qty,
                "margin": dca.margin,
                "multiplier": self.config.dca_multipliers[next_level],
            }
        elif trade.side == "short" and current_price >= dca.price:
            return {
                "action": "dca_fill",
                "trade_id": trade.trade_id,
                "level": next_level,
                "price": dca.price,
                "qty": dca.qty,
                "margin": dca.margin,
                "multiplier": self.config.dca_multipliers[next_level],
            }

        return None

    def check_stop(self, trade: Trade, current_price: float) -> dict | None:
        """Check if stop loss should trigger (only after all DCAs).

        Returns:
            Action dict or None
        """
        if not trade.stop_active:
            return None

        if trade.side == "long":
            # Trail stop up: if price goes above avg, move stop up
            if current_price > trade.avg_price:
                new_stop = trade.avg_price  # At minimum, BE
                if new_stop > trade.stop_price:
                    trade.stop_price = new_stop

            if current_price <= trade.stop_price:
                pnl = (trade.stop_price - trade.avg_price) / trade.avg_price * 100
                return {
                    "action": "stop_close",
                    "trade_id": trade.trade_id,
                    "qty": trade.total_qty,
                    "reason": f"Stop at {trade.stop_price:.4f} (avg: {trade.avg_price:.4f}, {pnl:+.2f}%)",
                    "price": current_price,
                    "is_profit": current_price >= trade.avg_price,
                }

        else:  # short
            if current_price < trade.avg_price:
                new_stop = trade.avg_price
                if trade.stop_price == 0 or new_stop < trade.stop_price:
                    trade.stop_price = new_stop

            if current_price >= trade.stop_price:
                pnl = (trade.avg_price - trade.stop_price) / trade.avg_price * 100
                return {
                    "action": "stop_close",
                    "trade_id": trade.trade_id,
                    "qty": trade.total_qty,
                    "reason": f"Stop at {trade.stop_price:.4f} (avg: {trade.avg_price:.4f}, {pnl:+.2f}%)",
                    "price": current_price,
                    "is_profit": current_price <= trade.avg_price,
                }

        return None

    def close_trade(self, trade: Trade, close_price: float, pnl: float, reason: str) -> None:
        """Mark a trade as fully closed."""
        trade.status = TradeStatus.CLOSED
        trade.closed_at = time.time()
        trade.realized_pnl = pnl

        # Classify
        if pnl > 0.01:
            self.total_wins += 1
        elif pnl < -0.01:
            self.total_losses += 1
        else:
            self.total_breakeven += 1

        self.total_pnl += pnl

        # Move to closed list
        self.closed_trades.append(trade)
        if trade.trade_id in self.trades:
            del self.trades[trade.trade_id]

        total = self.total_wins + self.total_losses + self.total_breakeven
        wr = self.total_wins / total * 100 if total > 0 else 0

        logger.info(
            f"Trade closed: {trade.symbol_display} {trade.side.upper()} | "
            f"PnL: ${pnl:+.2f} | Reason: {reason} | "
            f"Duration: {trade.age_hours:.1f}h | "
            f"Stats: {self.total_wins}W/{self.total_losses}L/{self.total_breakeven}BE "
            f"({wr:.0f}% WR) | Total PnL: ${self.total_pnl:+.2f}"
        )

    def _calc_tp_price(self, trade: Trade, tp_pct: float) -> float:
        """Calculate TP price from avg."""
        if trade.side == "long":
            return trade.avg_price * (1 + tp_pct / 100)
        else:
            return trade.avg_price * (1 - tp_pct / 100)

    def _create_tp1_action(self, trade: Trade, current_price: float, tp_price: float) -> dict:
        """Create TP1 partial close action."""
        close_qty = trade.total_qty * self.config.tp1_close_pct / 100
        return {
            "action": "tp1_partial",
            "trade_id": trade.trade_id,
            "qty": close_qty,
            "price": current_price,
            "tp_price": tp_price,
            "remaining_qty": trade.total_qty - close_qty,
        }

    def record_tp1(self, trade: Trade, closed_qty: float, close_price: float) -> None:
        """Record that TP1 has been hit and partial close done."""
        trade.tp1_hit = True
        trade.tp1_closed_qty = closed_qty
        trade.status = TradeStatus.TRAILING

        # Initialize trailing
        if trade.side == "long":
            trade.trailing_high = close_price
        else:
            trade.trailing_low = close_price

        pnl_pct = abs(close_price - trade.avg_price) / trade.avg_price * 100
        logger.info(
            f"TP1 hit: {trade.symbol_display} | Closed {closed_qty:.6f} @ {close_price:.4f} | "
            f"+{pnl_pct:.2f}% | Remaining: {trade.remaining_qty:.6f} trailing"
        )

    def get_dashboard_data(self) -> dict:
        """Get all data for the dashboard."""
        active = self.active_trades
        total = self.total_wins + self.total_losses + self.total_breakeven

        return {
            "active_trades": [
                {
                    "symbol": t.symbol_display,
                    "side": t.side,
                    "entry": t.signal_entry,
                    "avg": t.avg_price,
                    "dca": f"{t.current_dca}/{t.max_dca}",
                    "margin": f"${t.total_margin:.2f}",
                    "status": t.status.value,
                    "age": f"{t.age_hours:.1f}h",
                    "tp1_hit": t.tp1_hit,
                }
                for t in active
            ],
            "slots": f"{self.active_count}/{self.config.max_simultaneous_trades}",
            "stats": {
                "wins": self.total_wins,
                "losses": self.total_losses,
                "breakeven": self.total_breakeven,
                "total": total,
                "win_rate": f"{self.total_wins / total * 100:.1f}%" if total > 0 else "0%",
                "total_pnl": f"${self.total_pnl:+.2f}",
            },
        }
