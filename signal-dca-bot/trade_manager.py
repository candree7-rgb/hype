"""
Trade Manager v2 - Manages active trades, DCA levels, TP/SL logic.

Exit Logic:
  E1-only (no DCA):
    → TP1 hit → close 50% → trail remaining with 0.5% CB
    → Trail floor = TP1 level (remaining never closes below TP1 profit)

  DCA1+ active:
    → Hard SL at avg - 3% (always active)
    → Price returns to avg → activate BE-trail (0.5% CB)
    → Close 100% on trail callback

  Priority: Hard SL > BE-Trail > TP1/Trail
"""

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from config import BotConfig
from telegram_parser import Signal
import database as db

logger = logging.getLogger(__name__)


class TradeStatus(str, Enum):
    PENDING = "pending"     # E1 limit order placed, waiting for fill
    OPEN = "open"           # E1 filled, no DCA, waiting for TP
    TRAILING = "trailing"   # TP1 hit, trailing remaining 50%
    DCA_ACTIVE = "dca"      # DCA1+ filled, waiting for BE-trail
    BE_TRAILING = "be_trail"  # Price returned to avg, trailing from BE
    CLOSED = "closed"       # Fully closed


@dataclass
class DCALevel:
    """Tracks a single DCA entry level."""
    level: int          # 0=E1, 1=DCA1, 2=DCA2, 3=DCA3
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
    max_dca: int = 3            # Max DCA levels for this trade

    # TP tracking (E1-only mode)
    tp1_hit: bool = False
    tp1_price: float = 0.0      # TP1 level (for floor)
    tp1_closed_qty: float = 0.0 # Qty closed at TP1
    trailing_peak: float = 0.0  # Highest (long) or lowest (short) since TP1

    # BE-trail tracking (DCA mode)
    be_trail_active: bool = False    # Activated when price returns to avg
    be_trail_peak: float = 0.0      # Peak since BE-trail activated

    # Hard SL (active from DCA1+)
    hard_sl_price: float = 0.0

    # Orders
    tp1_order_id: str = ""          # Bybit order ID for TP1 reduceOnly limit
    dca_order_ids: list[str] = field(default_factory=list)

    # Timing
    opened_at: float = 0.0
    closed_at: float = 0.0

    # P&L
    realized_pnl: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.status != TradeStatus.CLOSED

    @property
    def remaining_qty(self) -> float:
        """Qty still in position after TP1 partial close."""
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

    def can_open_trade(self, symbol: str) -> tuple[bool, str]:
        """Check if we can open a new trade."""
        if not self.has_free_slot:
            return False, f"Max {self.config.max_simultaneous_trades} trades reached"

        for t in self.active_trades:
            if t.symbol == symbol:
                return False, f"Already in {symbol}"

        if self.config.blocked_coins:
            base = symbol.replace("USDT", "")
            if base in self.config.blocked_coins:
                return False, f"{base} is blocked"

        if self.config.allowed_coins:
            base = symbol.replace("USDT", "")
            if base not in self.config.allowed_coins:
                return False, f"{base} not in allowed list"

        return True, "OK"

    def create_trade(self, signal: Signal, equity: float) -> Trade:
        """Create a new trade from a signal."""
        self._trade_counter += 1
        trade_id = f"{signal.symbol}_{int(time.time())}_{self._trade_counter}"

        # Calculate DCA levels
        dca_levels = []
        total_budget = equity * self.config.equity_pct_per_trade / 100
        base_margin = total_budget / self.config.sum_multipliers

        for i in range(self.config.max_dca_levels + 1):
            price = self.config.dca_price(signal.entry_price, i, signal.side)
            margin = base_margin * self.config.dca_multipliers[i]
            qty = margin * self.config.leverage / price

            level = DCALevel(
                level=i,
                price=price,
                qty=qty,
                margin=margin,
                filled=False,
            )
            dca_levels.append(level)

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
            total_qty=0 if initial_status == TradeStatus.PENDING else dca_levels[0].qty,
            total_margin=0 if initial_status == TradeStatus.PENDING else dca_levels[0].margin,
            avg_price=signal.entry_price,
            current_dca=0,
            max_dca=self.config.max_dca_levels,
            opened_at=time.time(),
        )

        self.trades[trade_id] = trade

        logger.info(
            f"Trade created: {trade.trade_id} | {signal.side.upper()} "
            f"{signal.symbol_display} @ {signal.entry_price} | "
            f"E1: {dca_levels[0].qty:.6f} coins, ${dca_levels[0].margin:.2f} margin"
        )

        return trade

    # ══════════════════════════════════════════════════════════════════════
    # ▌ DCA FILL
    # ══════════════════════════════════════════════════════════════════════

    def fill_dca(self, trade: Trade, level: int, fill_price: float) -> None:
        """Record a DCA level as filled. Activates Hard SL."""
        if level > len(trade.dca_levels) - 1:
            return

        dca = trade.dca_levels[level]
        dca.filled = True
        dca.price = fill_price

        # Recalculate qty based on actual fill price
        actual_qty = dca.margin * self.config.leverage / fill_price
        dca.qty = actual_qty

        # Update weighted average
        old_cost = trade.avg_price * trade.total_qty
        new_cost = fill_price * actual_qty
        trade.total_qty += actual_qty
        trade.total_margin += dca.margin
        trade.avg_price = (old_cost + new_cost) / trade.total_qty
        trade.current_dca = level

        # Enter DCA mode
        trade.status = TradeStatus.DCA_ACTIVE
        trade.be_trail_active = False
        trade.be_trail_peak = 0.0

        # Activate Hard SL at avg - hard_sl_pct%
        self._update_hard_sl(trade)

        logger.info(
            f"DCA{level} filled: {trade.symbol_display} @ {fill_price:.4f} | "
            f"New avg: {trade.avg_price:.4f} | Margin: ${trade.total_margin:.2f} | "
            f"Hard SL: {trade.hard_sl_price:.4f} | DCA {level}/{trade.max_dca}"
        )

    def _update_hard_sl(self, trade: Trade) -> None:
        """Update hard stop loss based on current avg price."""
        sl_pct = self.config.hard_sl_pct / 100
        if trade.side == "long":
            trade.hard_sl_price = trade.avg_price * (1 - sl_pct)
        else:
            trade.hard_sl_price = trade.avg_price * (1 + sl_pct)

    # ══════════════════════════════════════════════════════════════════════
    # ▌ CHECK: TP1 (E1-only trades)
    # ══════════════════════════════════════════════════════════════════════

    def check_tp1(self, trade: Trade, current_price: float) -> dict | None:
        """Check if TP1 should trigger. Only for E1-only trades (no DCA)."""
        if trade.tp1_hit:
            return None
        if trade.current_dca > 0:
            return None  # In DCA mode, use BE-trail instead
        if trade.status not in (TradeStatus.OPEN,):
            return None

        tp1_price = self._tp_price(trade, self.config.tp1_pct)

        triggered = (
            (trade.side == "long" and current_price >= tp1_price) or
            (trade.side == "short" and current_price <= tp1_price)
        )

        if triggered:
            close_qty = trade.total_qty * self.config.tp1_close_pct / 100
            trade.tp1_price = tp1_price
            return {
                "action": "tp1_partial",
                "trade_id": trade.trade_id,
                "qty": close_qty,
                "price": current_price,
                "tp_price": tp1_price,
            }

        return None

    def record_tp1(self, trade: Trade, closed_qty: float, close_price: float) -> None:
        """Record TP1 hit and start trailing."""
        trade.tp1_hit = True
        trade.tp1_closed_qty = closed_qty
        trade.status = TradeStatus.TRAILING
        trade.trailing_peak = close_price

        pnl_pct = abs(close_price - trade.avg_price) / trade.avg_price * 100
        logger.info(
            f"TP1 hit: {trade.symbol_display} | Closed {closed_qty:.6f} @ {close_price:.4f} | "
            f"+{pnl_pct:.2f}% | Remaining {trade.remaining_qty:.6f} trailing from TP1 floor"
        )

    # ══════════════════════════════════════════════════════════════════════
    # ▌ CHECK: TRAILING (after TP1, E1-only)
    # ══════════════════════════════════════════════════════════════════════

    def check_trailing(self, trade: Trade, current_price: float) -> dict | None:
        """Check trailing stop for remaining position after TP1.

        Trail with 0.5% callback. Floor at TP1 level = remaining never
        closes below TP1 profit.
        """
        if trade.status != TradeStatus.TRAILING:
            return None
        if trade.remaining_qty <= 0:
            return None

        cb_pct = self.config.trailing_callback_pct / 100

        if trade.side == "long":
            # Track peak
            if current_price > trade.trailing_peak:
                trade.trailing_peak = current_price

            # Trail price = peak - callback
            trail_price = trade.trailing_peak * (1 - cb_pct)

            # Floor: never close below TP1 level
            floor = trade.tp1_price
            if trail_price < floor:
                trail_price = floor

            if current_price <= trail_price:
                pnl_pct = (current_price - trade.avg_price) / trade.avg_price * 100
                return {
                    "action": "close_remaining",
                    "trade_id": trade.trade_id,
                    "qty": trade.remaining_qty,
                    "price": current_price,
                    "reason": f"Trail CB from peak {trade.trailing_peak:.4f} (floor={floor:.4f}, +{pnl_pct:.2f}%)",
                }

        else:  # short
            if current_price < trade.trailing_peak or trade.trailing_peak == 0:
                trade.trailing_peak = current_price

            trail_price = trade.trailing_peak * (1 + cb_pct)
            floor = trade.tp1_price  # For shorts, TP1 is below entry
            if trail_price > floor:
                trail_price = floor

            if current_price >= trail_price:
                pnl_pct = (trade.avg_price - current_price) / trade.avg_price * 100
                return {
                    "action": "close_remaining",
                    "trade_id": trade.trade_id,
                    "qty": trade.remaining_qty,
                    "price": current_price,
                    "reason": f"Trail CB from peak {trade.trailing_peak:.4f} (floor={floor:.4f}, +{pnl_pct:.2f}%)",
                }

        return None

    # ══════════════════════════════════════════════════════════════════════
    # ▌ CHECK: DCA TRIGGER
    # ══════════════════════════════════════════════════════════════════════

    def check_dca_trigger(self, trade: Trade, current_price: float) -> dict | None:
        """Check if next DCA level should trigger."""
        next_level = trade.current_dca + 1
        if next_level > trade.max_dca:
            return None
        if next_level >= len(trade.dca_levels):
            return None

        dca = trade.dca_levels[next_level]
        if dca.filled:
            return None

        triggered = (
            (trade.side == "long" and current_price <= dca.price) or
            (trade.side == "short" and current_price >= dca.price)
        )

        if triggered:
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

    # ══════════════════════════════════════════════════════════════════════
    # ▌ CHECK: BE-TRAIL (DCA mode)
    # ══════════════════════════════════════════════════════════════════════

    def check_be_trail(self, trade: Trade, current_price: float) -> dict | None:
        """Check BE-trail for DCA trades.

        When price returns to avg after DCA → activate trailing.
        Close 100% on 0.5% callback from peak above avg.
        """
        if trade.current_dca == 0:
            return None  # No DCA active
        if trade.status not in (TradeStatus.DCA_ACTIVE, TradeStatus.BE_TRAILING):
            return None

        cb_pct = self.config.be_trail_callback_pct / 100
        avg = trade.avg_price

        if trade.side == "long":
            # Activate BE-trail when price crosses above avg
            if not trade.be_trail_active and current_price >= avg:
                trade.be_trail_active = True
                trade.be_trail_peak = current_price
                trade.status = TradeStatus.BE_TRAILING
                logger.info(
                    f"BE-Trail activated: {trade.symbol_display} | "
                    f"Price {current_price:.4f} >= Avg {avg:.4f}"
                )
                return None  # Don't close on the same tick

            if trade.be_trail_active:
                # Track peak
                if current_price > trade.be_trail_peak:
                    trade.be_trail_peak = current_price

                # Trail = peak - callback, but never below avg (BE floor)
                trail_price = trade.be_trail_peak * (1 - cb_pct)
                if trail_price < avg:
                    trail_price = avg

                if current_price <= trail_price:
                    close_qty = trade.remaining_qty if trade.tp1_hit else trade.total_qty
                    pnl_pct = (current_price - avg) / avg * 100
                    return {
                        "action": "be_trail_close",
                        "trade_id": trade.trade_id,
                        "qty": close_qty,
                        "price": current_price,
                        "reason": f"BE-Trail CB from {trade.be_trail_peak:.4f} (avg={avg:.4f}, +{pnl_pct:.2f}%)",
                    }

        else:  # short
            if not trade.be_trail_active and current_price <= avg:
                trade.be_trail_active = True
                trade.be_trail_peak = current_price
                trade.status = TradeStatus.BE_TRAILING
                logger.info(
                    f"BE-Trail activated: {trade.symbol_display} | "
                    f"Price {current_price:.4f} <= Avg {avg:.4f}"
                )
                return None

            if trade.be_trail_active:
                if current_price < trade.be_trail_peak or trade.be_trail_peak == 0:
                    trade.be_trail_peak = current_price

                trail_price = trade.be_trail_peak * (1 + cb_pct)
                if trail_price > avg:
                    trail_price = avg

                if current_price >= trail_price:
                    close_qty = trade.remaining_qty if trade.tp1_hit else trade.total_qty
                    pnl_pct = (avg - current_price) / avg * 100
                    return {
                        "action": "be_trail_close",
                        "trade_id": trade.trade_id,
                        "qty": close_qty,
                        "price": current_price,
                        "reason": f"BE-Trail CB from {trade.be_trail_peak:.4f} (avg={avg:.4f}, +{pnl_pct:.2f}%)",
                    }

        return None

    # ══════════════════════════════════════════════════════════════════════
    # ▌ CHECK: HARD STOP LOSS (DCA mode)
    # ══════════════════════════════════════════════════════════════════════

    def check_hard_sl(self, trade: Trade, current_price: float) -> dict | None:
        """Check hard stop loss. Only active after ALL DCAs are filled."""
        if trade.current_dca < trade.max_dca:
            return None  # No SL until all DCAs filled
        if trade.hard_sl_price == 0:
            return None

        triggered = (
            (trade.side == "long" and current_price <= trade.hard_sl_price) or
            (trade.side == "short" and current_price >= trade.hard_sl_price)
        )

        if triggered:
            close_qty = trade.remaining_qty if trade.tp1_hit else trade.total_qty
            pnl_pct = abs(current_price - trade.avg_price) / trade.avg_price * 100
            notional = trade.total_margin * self.config.leverage
            loss_usd = notional * self.config.hard_sl_pct / 100
            return {
                "action": "hard_sl_close",
                "trade_id": trade.trade_id,
                "qty": close_qty,
                "price": current_price,
                "reason": f"Hard SL at {trade.hard_sl_price:.4f} (avg={trade.avg_price:.4f}, -{pnl_pct:.2f}%, ~${loss_usd:.0f})",
            }

        return None

    # ══════════════════════════════════════════════════════════════════════
    # ▌ CLOSE TRADE
    # ══════════════════════════════════════════════════════════════════════

    def close_trade(self, trade: Trade, close_price: float, pnl: float, reason: str) -> None:
        """Mark a trade as fully closed."""
        trade.status = TradeStatus.CLOSED
        trade.closed_at = time.time()
        trade.realized_pnl = pnl

        if pnl > 0.01:
            self.total_wins += 1
        elif pnl < -0.01:
            self.total_losses += 1
        else:
            self.total_breakeven += 1

        self.total_pnl += pnl

        self.closed_trades.append(trade)
        if trade.trade_id in self.trades:
            del self.trades[trade.trade_id]

        # Persist to DB
        db.save_trade(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            side=trade.side,
            entry_price=trade.signal_entry,
            avg_price=trade.avg_price,
            close_price=close_price,
            total_qty=trade.total_qty,
            total_margin=trade.total_margin,
            realized_pnl=pnl,
            max_dca=trade.current_dca,
            tp1_hit=trade.tp1_hit,
            close_reason=reason,
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
            signal_leverage=trade.signal_leverage,
        )

        total = self.total_wins + self.total_losses + self.total_breakeven
        wr = self.total_wins / total * 100 if total > 0 else 0

        logger.info(
            f"Trade closed: {trade.symbol_display} {trade.side.upper()} | "
            f"PnL: ${pnl:+.2f} | Reason: {reason} | "
            f"Duration: {trade.age_hours:.1f}h | DCA: {trade.current_dca}/{trade.max_dca} | "
            f"Stats: {self.total_wins}W/{self.total_losses}L/{self.total_breakeven}BE "
            f"({wr:.0f}% WR) | Total PnL: ${self.total_pnl:+.2f}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # ▌ HELPERS
    # ══════════════════════════════════════════════════════════════════════

    def _tp_price(self, trade: Trade, tp_pct: float) -> float:
        """Calculate TP price from avg."""
        if trade.side == "long":
            return trade.avg_price * (1 + tp_pct / 100)
        else:
            return trade.avg_price * (1 - tp_pct / 100)

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
                    "avg": round(t.avg_price, 4),
                    "dca": f"{t.current_dca}/{t.max_dca}",
                    "margin": f"${t.total_margin:.2f}",
                    "status": t.status.value,
                    "age": f"{t.age_hours:.1f}h",
                    "tp1_hit": t.tp1_hit,
                    "sl": round(t.hard_sl_price, 4) if t.hard_sl_price > 0 else "-",
                    "be_trail": "active" if t.be_trail_active else "-",
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
