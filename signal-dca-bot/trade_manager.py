"""
Trade Manager v2 - Multi-TP Strategy

Exit Logic:
  E1-only (Multi-TP from signal targets):
    → Place TP1-TP4 as reduceOnly limits: 50%/10%/10%/10%
    → SL at entry-3%. After TP1 fills → SL moves to breakeven
    → After TP4 fills → remaining 20% trails (0.5% CB)
    → All exits are exchange-side (Bybit handles TP/SL/trailing)

  DCA1 active:
    → Cancel unfilled TPs → Hard SL at avg-3%
    → BE-Trail when price returns to avg (0.5% CB)
    → Close 100% on trail callback

  Neo Cloud trend switch → close all opposing positions
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
    OPEN = "open"           # E1 filled, TPs placed, waiting
    TRAILING = "trailing"   # All TPs hit, trailing remaining 20%
    DCA_ACTIVE = "dca"      # DCA filled, waiting for BE-trail
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
    max_dca: int = 1            # Max DCA levels for this trade

    # Multi-TP tracking (E1-only mode, exchange-side)
    tp_prices: list[float] = field(default_factory=list)      # TP price levels from signal
    tp_order_ids: list[str] = field(default_factory=list)      # Bybit order IDs for each TP
    tp_filled: list[bool] = field(default_factory=list)        # Which TPs have filled
    tp_close_pcts: list[float] = field(default_factory=list)   # % of position per TP
    tp_close_qtys: list[float] = field(default_factory=list)   # Qty per TP
    tps_hit: int = 0            # Number of TPs filled so far
    total_tp_closed_qty: float = 0.0  # Total qty closed via TPs

    # BE-trail tracking (DCA mode)
    be_trail_active: bool = False    # Activated when price returns to avg
    be_trail_peak: float = 0.0      # Peak since BE-trail activated

    # Hard SL
    hard_sl_price: float = 0.0

    # Orders
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
        """Qty still in position after partial TP closes."""
        return self.total_qty - self.total_tp_closed_qty

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

        # Setup Multi-TP from signal targets
        tp_close_pcts = list(self.config.tp_close_pcts)
        tp_prices = signal.targets[:len(tp_close_pcts)]
        tp_filled = [False] * len(tp_prices)
        tp_order_ids = [""] * len(tp_prices)
        # tp_close_qtys calculated when TPs are placed (after E1 fills, qty confirmed)

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
            tp_prices=tp_prices,
            tp_order_ids=tp_order_ids,
            tp_filled=tp_filled,
            tp_close_pcts=tp_close_pcts[:len(tp_prices)],
            opened_at=time.time(),
        )

        self.trades[trade_id] = trade

        tp_pct_str = " / ".join(f"TP{i+1}={p}%" for i, p in enumerate(trade.tp_close_pcts))
        logger.info(
            f"Trade created: {trade.trade_id} | {signal.side.upper()} "
            f"{signal.symbol_display} @ {signal.entry_price} | "
            f"E1: {dca_levels[0].qty:.6f} coins, ${dca_levels[0].margin:.2f} margin | "
            f"TPs: {tp_pct_str} | Targets: {tp_prices}"
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
    # ▌ MULTI-TP: Record TP fill (exchange-side)
    # ══════════════════════════════════════════════════════════════════════

    def setup_tp_qtys(self, trade: Trade) -> None:
        """Calculate TP close quantities from confirmed E1 qty.

        Called after E1 fills and total_qty is confirmed.
        """
        trade.tp_close_qtys = []
        for pct in trade.tp_close_pcts:
            qty = trade.total_qty * pct / 100
            trade.tp_close_qtys.append(qty)

    def record_tp_fill(self, trade: Trade, tp_idx: int,
                       closed_qty: float, fill_price: float) -> None:
        """Record a TP level as filled (exchange-side reduceOnly limit)."""
        if tp_idx >= len(trade.tp_filled):
            return

        trade.tp_filled[tp_idx] = True
        trade.tps_hit += 1
        trade.total_tp_closed_qty += closed_qty

        if trade.side == "long":
            pnl = (fill_price - trade.avg_price) * closed_qty
        else:
            pnl = (trade.avg_price - fill_price) * closed_qty
        trade.realized_pnl += pnl

        pnl_pct = abs(fill_price - trade.avg_price) / trade.avg_price * 100

        logger.info(
            f"TP{tp_idx + 1} filled: {trade.symbol_display} | "
            f"Closed {closed_qty:.6f} @ {fill_price:.4f} | "
            f"+{pnl_pct:.2f}% | PnL: ${pnl:+.2f} | "
            f"TPs: {trade.tps_hit}/{len(trade.tp_prices)} | "
            f"Remaining: {trade.remaining_qty:.6f}"
        )

        # Check if all TPs are filled → enter trailing mode
        if all(trade.tp_filled):
            trade.status = TradeStatus.TRAILING
            logger.info(
                f"All TPs filled: {trade.symbol_display} | "
                f"Remaining {trade.remaining_qty:.6f} → trailing"
            )

    # ══════════════════════════════════════════════════════════════════════
    # ▌ CHECK: HARD STOP LOSS + BE-TRAIL (DCA mode, exchange-side managed)
    # ══════════════════════════════════════════════════════════════════════
    # Note: BE-trail and hard SL are now set via Bybit set_trading_stop.
    # The price_monitor detects position close (position gone) and
    # records the trade as closed. No price-based checks needed here.

    # Hard SL is set via Bybit set_trading_stop → detected by position close

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
            tp1_hit=trade.tps_hit > 0,
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
            f"TPs: {trade.tps_hit}/{len(trade.tp_prices)} | "
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
                    "tps": f"{t.tps_hit}/{len(t.tp_prices)}",
                    "margin": f"${t.total_margin:.2f}",
                    "status": t.status.value,
                    "age": f"{t.age_hours:.1f}h",
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
