"""
Trade Manager v2 - Multi-TP Strategy with 2/3 Pyramiding

Exit Logic (with Scale-In at TP2):
  E1-only (Multi-TP from signal targets):
    → Safety SL at entry-10% (wide, gives DCA room to fill)
    → Place TP1-TP4 as reduceOnly limits: 50%/10%/20%/10%
    → After TP1 fills → SL moves to breakeven + 0.1% buffer, DCA orders cancelled
    → After TP2 fills → Scale-in 1/3 more (if no DCA), SL = exakt new Avg
      → Cancel TP3/TP4, recalculate quantities for new position size
    → After TP3 fills → SL = TP2 price (profit lock)
    → After TP4 fills → remaining trails (1% CB)
    → All exits are exchange-side (Bybit handles TP/SL/trailing)
    → If DCA already filled: no scale-in, SL stays at BE after TP2

  DCA1 fills (price dipped to -5% before TP1):
    → Cancel signal TPs → New TPs from avg: TP1=+0.5% (50%), TP2=+1.25% (20%)
    → Trail remaining 30% with 1% CB after all DCA TPs
    → Hard SL at DCA-fill+3% → Quick-Trail to avg+0.5% once +0.5% reached
    → DCA TP1 fills → SL to exakt avg

  Neo Cloud trend switch → close all opposing positions
"""

import time
import logging
import json
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
    leverage: int = 20      # Actual leverage used (after cap/fallback)

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

    # DCA Quick-Trail: tighten SL once bounce confirms (+0.5%)
    quick_trail_active: bool = False  # True = SL already tightened from -3% to avg+0.5%

    # 2/3 Pyramiding (scale-in at TP2)
    scale_in_filled: bool = False     # True = scale-in executed at TP2
    scale_in_qty: float = 0.0        # Qty added via scale-in (coin units)
    scale_in_price: float = 0.0      # Fill price of scale-in order
    scale_in_margin: float = 0.0     # Margin used for scale-in (USD)

    # Hard SL
    hard_sl_price: float = 0.0

    # Orders
    dca_order_ids: list[str] = field(default_factory=list)

    # Timing
    opened_at: float = 0.0
    closed_at: float = 0.0

    # P&L
    realized_pnl: float = 0.0

    # Trail analysis: trail contribution as % of margin (total_pnl - tp_pnl) / margin
    # Positive = trail captured extra profit, negative = trail lost (SL hit)
    # Universal: comparable across trades regardless of position size
    trail_pnl_pct: float = 0.0

    # Equity snapshot (for PnL % calculation)
    equity_at_entry: float = 0.0

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


# ══════════════════════════════════════════════════════════════════════════
# ▌ TRADE SERIALIZATION (for DB persistence / crash recovery)
# ══════════════════════════════════════════════════════════════════════════

def trade_to_dict(trade: Trade) -> dict:
    """Serialize a Trade object to a dict (JSON-safe)."""
    return {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "symbol_display": trade.symbol_display,
        "side": trade.side,
        "signal_entry": trade.signal_entry,
        "signal_leverage": trade.signal_leverage,
        "leverage": trade.leverage,
        "dca_levels": [
            {
                "level": d.level,
                "price": d.price,
                "qty": d.qty,
                "margin": d.margin,
                "filled": d.filled,
                "order_id": d.order_id,
            }
            for d in trade.dca_levels
        ],
        "status": trade.status.value,
        "total_qty": trade.total_qty,
        "total_margin": trade.total_margin,
        "avg_price": trade.avg_price,
        "current_dca": trade.current_dca,
        "max_dca": trade.max_dca,
        "tp_prices": trade.tp_prices,
        "tp_order_ids": trade.tp_order_ids,
        "tp_filled": trade.tp_filled,
        "tp_close_pcts": trade.tp_close_pcts,
        "tp_close_qtys": trade.tp_close_qtys,
        "tps_hit": trade.tps_hit,
        "total_tp_closed_qty": trade.total_tp_closed_qty,
        "be_trail_active": trade.be_trail_active,
        "be_trail_peak": trade.be_trail_peak,
        "quick_trail_active": trade.quick_trail_active,
        "scale_in_filled": trade.scale_in_filled,
        "scale_in_qty": trade.scale_in_qty,
        "scale_in_price": trade.scale_in_price,
        "scale_in_margin": trade.scale_in_margin,
        "hard_sl_price": trade.hard_sl_price,
        "dca_order_ids": trade.dca_order_ids,
        "opened_at": trade.opened_at,
        "closed_at": trade.closed_at,
        "realized_pnl": trade.realized_pnl,
        "trail_pnl_pct": trade.trail_pnl_pct,
        "equity_at_entry": trade.equity_at_entry,
    }


def trade_from_dict(data: dict) -> Trade:
    """Deserialize a dict back into a Trade object."""
    dca_levels = [
        DCALevel(
            level=d["level"],
            price=d["price"],
            qty=d["qty"],
            margin=d["margin"],
            filled=d["filled"],
            order_id=d.get("order_id", ""),
        )
        for d in data.get("dca_levels", [])
    ]

    return Trade(
        trade_id=data["trade_id"],
        symbol=data["symbol"],
        symbol_display=data["symbol_display"],
        side=data["side"],
        signal_entry=data["signal_entry"],
        signal_leverage=data["signal_leverage"],
        leverage=data.get("leverage", 20),
        dca_levels=dca_levels,
        status=TradeStatus(data["status"]),
        total_qty=data.get("total_qty", 0),
        total_margin=data.get("total_margin", 0),
        avg_price=data.get("avg_price", 0),
        current_dca=data.get("current_dca", 0),
        max_dca=data.get("max_dca", 1),
        tp_prices=data.get("tp_prices", []),
        tp_order_ids=data.get("tp_order_ids", []),
        tp_filled=data.get("tp_filled", []),
        tp_close_pcts=data.get("tp_close_pcts", []),
        tp_close_qtys=data.get("tp_close_qtys", []),
        tps_hit=data.get("tps_hit", 0),
        total_tp_closed_qty=data.get("total_tp_closed_qty", 0),
        be_trail_active=data.get("be_trail_active", False),
        be_trail_peak=data.get("be_trail_peak", 0),
        quick_trail_active=data.get("quick_trail_active", False),
        scale_in_filled=data.get("scale_in_filled", False),
        scale_in_qty=data.get("scale_in_qty", 0),
        scale_in_price=data.get("scale_in_price", 0),
        scale_in_margin=data.get("scale_in_margin", 0),
        hard_sl_price=data.get("hard_sl_price", 0),
        dca_order_ids=data.get("dca_order_ids", []),
        opened_at=data.get("opened_at", 0),
        closed_at=data.get("closed_at", 0),
        realized_pnl=data.get("realized_pnl", 0),
        trail_pnl_pct=data.get("trail_pnl_pct", 0),
        equity_at_entry=data.get("equity_at_entry", 0),
    )


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

    # ── Persistence ──

    def persist_trade(self, trade: Trade) -> None:
        """Save trade state to DB for crash recovery."""
        state = trade_to_dict(trade)
        db.save_active_trade(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            side=trade.side,
            status=trade.status.value,
            state_json=state,
        )

    def remove_persisted_trade(self, trade_id: str) -> None:
        """Remove trade from active_trades DB (trade closed)."""
        db.delete_active_trade(trade_id)

    def load_persisted_trades(self) -> int:
        """Load active trades from DB on startup. Returns count loaded."""
        rows = db.get_all_active_trades()
        loaded = 0
        for row in rows:
            try:
                trade = trade_from_dict(row["state"])
                # Skip if already closed (shouldn't happen, but safe)
                if trade.status == TradeStatus.CLOSED:
                    db.delete_active_trade(trade.trade_id)
                    continue
                self.trades[trade.trade_id] = trade
                # Update trade counter to avoid ID collisions
                self._trade_counter = max(self._trade_counter, loaded + 1)
                loaded += 1
                logger.info(
                    f"Recovered trade: {trade.symbol_display} {trade.side.upper()} | "
                    f"Status: {trade.status.value} | Avg: {trade.avg_price:.4f} | "
                    f"TPs: {trade.tps_hit}/{len(trade.tp_prices)} | "
                    f"DCA: {trade.current_dca}/{trade.max_dca} | "
                    f"SL: {trade.hard_sl_price:.4f}"
                )
            except Exception as e:
                logger.error(f"Failed to recover trade {row['trade_id']}: {e}")
        if loaded:
            logger.info(f"Trade recovery: {loaded} trades restored from DB")
        return loaded

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

        # Fixed position sizing: 5% equity, 20x leverage
        total_budget = self.config.trade_budget(equity)
        base_margin = total_budget / self.config.sum_multipliers

        # Calculate DCA levels
        dca_levels = []

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
            leverage=self.config.leverage,
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
            equity_at_entry=equity,
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
        actual_qty = dca.margin * trade.leverage / fill_price
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
        """Update hard stop loss: DCA fill price - 3%.

        Always calculated from the deepest DCA fill price, not avg.
        This prevents SL from being above current price when DCA is deep.
        (With avg-3%, DCA deeper than -8.5% would put SL above fill price!)
        """
        sl_pct = self.config.hard_sl_pct / 100

        # Find the deepest filled DCA price
        deepest_fill = None
        for dca in trade.dca_levels[1:]:
            if dca.filled and dca.price > 0:
                if trade.side == "long":
                    deepest_fill = min(deepest_fill, dca.price) if deepest_fill else dca.price
                else:
                    deepest_fill = max(deepest_fill, dca.price) if deepest_fill else dca.price

        if deepest_fill:
            # SL at DCA fill price - 3% (always safe, always below fill)
            if trade.side == "long":
                trade.hard_sl_price = deepest_fill * (1 - sl_pct)
            else:
                trade.hard_sl_price = deepest_fill * (1 + sl_pct)
        else:
            # No DCA filled yet (shouldn't happen, but fallback to avg)
            if trade.side == "long":
                trade.hard_sl_price = trade.avg_price * (1 - sl_pct)
            else:
                trade.hard_sl_price = trade.avg_price * (1 + sl_pct)

    # ══════════════════════════════════════════════════════════════════════
    # ▌ 2/3 PYRAMIDING: Scale-In at TP2
    # ══════════════════════════════════════════════════════════════════════

    def fill_scale_in(self, trade: Trade, fill_price: float,
                      actual_qty: float, margin: float) -> None:
        """Record scale-in as filled. Updates avg, qty, and marks scale_in_filled.

        Called after TP2 fills and market order for scale-in is confirmed.
        New avg is calculated from remaining position + scale-in qty.
        """
        remaining = trade.remaining_qty
        old_cost = trade.avg_price * remaining
        new_cost = fill_price * actual_qty
        new_total_remaining = remaining + actual_qty

        trade.avg_price = (old_cost + new_cost) / new_total_remaining
        trade.total_qty += actual_qty
        trade.total_margin += margin
        trade.scale_in_filled = True
        trade.scale_in_qty = actual_qty
        trade.scale_in_price = fill_price
        trade.scale_in_margin = margin

        logger.info(
            f"Scale-in filled: {trade.symbol_display} @ {fill_price:.4f} | "
            f"+{actual_qty:.6f} coins (${margin:.2f} margin) | "
            f"New avg: {trade.avg_price:.4f} | "
            f"Total: {trade.total_qty:.6f} | Remaining: {trade.remaining_qty:.6f}"
        )

    def recalc_tps_after_scale_in(self, trade: Trade) -> None:
        """Recalculate TP3/TP4 quantities after scale-in.

        After scale-in, remaining qty is much larger. Redistribute TP3/TP4/trail
        proportionally across the new remaining qty.
        TP prices stay the same (signal targets), only quantities change.
        """
        remaining = trade.remaining_qty

        unfilled_pcts = []
        unfilled_indices = []
        for i in range(len(trade.tp_filled)):
            if not trade.tp_filled[i]:
                unfilled_pcts.append(trade.tp_close_pcts[i])
                unfilled_indices.append(i)

        trail_pct = 100 - sum(trade.tp_close_pcts)
        total_unfilled = sum(unfilled_pcts) + trail_pct

        if total_unfilled <= 0:
            return

        for i, tp_idx in enumerate(unfilled_indices):
            share = unfilled_pcts[i] / total_unfilled
            trade.tp_close_qtys[tp_idx] = remaining * share

        logger.info(
            f"TPs recalculated after scale-in: {trade.symbol_display} | "
            f"Remaining: {remaining:.6f} | "
            f"TP qtys: {[f'{q:.6f}' for q in trade.tp_close_qtys]} | "
            f"Trail: {trail_pct}/{total_unfilled:.0f} share"
        )

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

    def setup_dca_tps(self, trade: Trade) -> None:
        """Recalculate TP prices and quantities after DCA fill.

        Replaces cancelled signal TPs with avg-based TPs for the full position.
        DCA TPs: TP1=+0.5% (rescue), TP2=+1.25% from new avg, remaining trails.
        SL after DCA TP1 = exakt avg (kein buffer, bei 0.5% TP1 unnötig).
        """
        # New TP prices based on new avg
        trade.tp_prices = []
        for pct in self.config.dca_tp_pcts:
            if trade.side == "long":
                tp = trade.avg_price * (1 + pct / 100)
            else:
                tp = trade.avg_price * (1 - pct / 100)
            trade.tp_prices.append(tp)

        trade.tp_filled = [False] * len(trade.tp_prices)
        trade.tp_order_ids = [""] * len(trade.tp_prices)
        trade.tp_close_pcts = list(self.config.dca_tp_close_pcts)
        trade.tps_hit = 0
        trade.total_tp_closed_qty = 0

        # Recalculate close quantities from full position (E1 + DCA)
        trade.tp_close_qtys = []
        for pct in trade.tp_close_pcts:
            qty = trade.total_qty * pct / 100
            trade.tp_close_qtys.append(qty)

        logger.info(
            f"DCA TPs set: {trade.symbol_display} | Avg: {trade.avg_price:.4f} | "
            f"TP1={trade.tp_prices[0]:.4f} ({self.config.dca_tp_pcts[0]}%), "
            f"TP2={trade.tp_prices[1]:.4f} ({self.config.dca_tp_pcts[1]}%) | "
            f"Qty: TP1={trade.tp_close_qtys[0]:.2f}, TP2={trade.tp_close_qtys[1]:.2f}"
        )

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
        was_filled = trade.total_qty > 0  # Entry was filled on exchange

        trade.status = TradeStatus.CLOSED
        trade.closed_at = time.time()

        # Calculate trail_pnl_pct: trail contribution = total PnL minus TP PnL
        # trade.realized_pnl has accumulated TP fills from record_tp_fill() before this call
        # pnl = total trade PnL from Bybit (includes TPs + trail/SL close)
        if was_filled and trade.total_margin > 0:
            tp_pnl = trade.realized_pnl  # sum of TP fill PnLs (before overwrite)
            trail_pnl = pnl - tp_pnl      # what the remaining trail portion added/lost
            trade.trail_pnl_pct = trail_pnl / trade.total_margin * 100

        trade.realized_pnl = pnl

        if was_filled:
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

        # Remove from active_trades persistence
        self.remove_persisted_trade(trade.trade_id)

        # Only persist filled trades to DB (skip failed opens / timeouts)
        if not was_filled:
            logger.info(f"Trade skipped DB save (unfilled): {trade.symbol} | {reason}")
            return

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
            equity_at_entry=trade.equity_at_entry,
            equity_at_close=trade.equity_at_entry + pnl,
            tps_hit=trade.tps_hit,
            trail_pnl_pct=round(trade.trail_pnl_pct, 4),
        )

        total = self.total_wins + self.total_losses + self.total_breakeven
        wr = self.total_wins / total * 100 if total > 0 else 0

        logger.info(
            f"Trade closed: {trade.symbol_display} {trade.side.upper()} | "
            f"PnL: ${pnl:+.2f} | Trail: {trade.trail_pnl_pct:+.2f}% | Reason: {reason} | "
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
                    "scale_in": "filled" if t.scale_in_filled else "-",
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
