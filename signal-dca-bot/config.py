"""
Signal DCA Bot v2 - Configuration
Telegram Signal → Bybit DCA Trading Bot

Strategy:
- 1 DCA [1, 2] at entry-5% (zone-snapped to S1/R1 with 3% min)
- Multi-TP: TP1 50%, TP2 10%, TP3 10%, TP4 10% (signal targets)
- Trail remaining 20% after TP4 (1% CB)
- SL Ladder:
    TP1 → SL = BE + 0.1% buffer, cancel DCA orders
    TP2 → SL stays at BE + buffer
    TP3 → SL = TP1 price (profit lock)
    TP4 → Trail 1% CB
- Two-tier SL: Safety SL at entry-10% (pre-DCA), Hard SL at DCA-fill+3% (post-DCA)
- DCA exit: New TPs from avg (TP1=0.5%, TP2=1.25%, trail 30% @1%CB)
- Neo Cloud trend switch: close on clear reversal
- Zone-snapping: S1/R1 dynamic zones from LuxAlgo/Bybit candles
- Crash recovery: active trades persisted to PostgreSQL, full Bybit reconciliation on startup
"""

from dataclasses import dataclass, field
from typing import Optional
import os


@dataclass
class BotConfig:
    # ── Account ──
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True  # START on testnet!

    # ── Telegram ──
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_string_session: str = ""  # Generated via: python telegram_listener.py
    telegram_channel: str = ""  # VIP Club channel name, username, or numeric ID

    # ── Capital & Risk ──
    leverage: int = 20                  # Fixed leverage for all trades
    equity_pct_per_trade: float = 5.0   # 5% of equity per trade
    max_simultaneous_trades: int = 6
    max_fills_per_batch: int = 3        # Max E1 fills per signal batch (extras cancelled after N fills)
    e1_limit_order: bool = True         # True = Limit at signal price, False = Market
    e1_timeout_minutes: int = 30        # Cancel E1 limit if not filled after X minutes

    # ── Neo Cloud Trend Filter ──
    neo_cloud_filter: bool = True       # Only take trades matching Neo Cloud trend

    # ── Reversal Zone Filter ──
    # Skip signals where price is already in the reversal zone:
    #   SHORT + price < S1 → skip (already at/below support, likely to bounce)
    #   LONG  + price > R1 → skip (already at/above resistance, likely to reject)
    zone_filter_enabled: bool = True

    # ── DCA Configuration ──
    # 1 DCA: E1 + DCA1 with sizing [1, 2] = sum 3
    dca_multipliers: list[float] = field(
        default_factory=lambda: [1, 2]
    )
    # DCA1 at entry-5% (before zone snap)
    dca_spacing_pct: list[float] = field(
        default_factory=lambda: [0, 5]
    )
    max_dca_levels: int = 1  # 1 DCA = total 2 entries (E1 + DCA1)
    dca_limit_buffer_pct: float = 0.2  # 0.2% buffer on DCA limit (deeper into zone, 1-candle lag compensation)

    # ── Multi-TP (E1-only mode, uses signal targets) ──
    # Close portions at signal's TP1-TP4 price targets.
    # Remaining position trails after last TP.
    tp_close_pcts: list[float] = field(
        default_factory=lambda: [50, 10, 10, 10]  # TP1=50%, TP2=10%, TP3=10%, TP4=10%
    )
    trailing_callback_pct: float = 1.0  # 1% CB for trail after all TPs (room for runners)
    sl_to_be_after_tp1: bool = True     # TP1→BE+0.1%, TP2→stay BE, TP3→SL@TP1, TP4→trail
    be_buffer_pct: float = 0.1          # 0.1% buffer above/below entry for BE stop (covers fees)

    # ── 2/3 Pyramiding (scale-in at TP2) ──
    # When TP2 hits → add another 1/3 position (same as E1 size) → 2/3 in trade
    # Only if DCA NOT already filled (DCA already uses the 2/3 budget)
    # 8/10 trades that reach TP2 also reach TP3 → double exposure at low risk
    scale_in_enabled: bool = False
    scale_in_at_tp: int = 2  # 1-indexed: scale in when TP2 fills

    # ── DCA Exit TPs (replaces BE-trail after DCA fills) ──
    # After DCA: place new TPs from avg, trail remaining after all DCA TPs
    # TP1 = rescue-only (0.5% from avg), TP2 = 1.25% from avg
    # At 3x size (E1+DCA), 0.75% spacing gives fat returns without needing big moves
    dca_tp_pcts: list[float] = field(
        default_factory=lambda: [0.5, 1.25]  # TP1=+0.5%, TP2=+1.25% from avg
    )
    dca_tp_close_pcts: list[float] = field(
        default_factory=lambda: [50, 20]  # TP1=50%, TP2=20%, remaining 30% trails
    )
    dca_trail_callback_pct: float = 1.0  # 1% CB trail for remaining 30% after DCA TPs
    dca_be_buffer_pct: float = 0.0  # No buffer for DCA SL→BE (0.5% TP1 is tight enough)

    # ── DCA Quick-Trail (tighten SL once bounce confirms) ──
    # After DCA fills, SL starts at deepest_fill+3%. Once price moves 0.5%
    # in our favor → SL tightens to avg+0.5% buffer. Reduces loss from ~4.7%
    # equity to ~1.1% equity per stop-out, while keeping -3% as safety net.
    dca_quick_trail_trigger_pct: float = 0.5  # Trail when price moves 0.5% in our favor
    dca_quick_trail_buffer_pct: float = 0.5   # SL at avg + 0.5% (against us)

    # ── Stop Loss (two-tier) ──
    # Pre-DCA: safety SL at entry-10% (wide, gives DCA room to fill)
    # Post-DCA: hard SL at avg-3% (tight, protects averaged position)
    # Post-DCA + quick trail: SL at avg+0.5% (once bounce confirms)
    safety_sl_pct: float = 10.0   # Initial SL before DCA fills (entry-10%)
    hard_sl_pct: float = 3.0      # SL after DCA fills (avg-3%)

    # ── Zone Snapping ──
    zone_snap_enabled: bool = True
    zone_snap_min_pct: float = 3.0        # Min distance for zone snap (default, signal lev <75x)
    zone_snap_min_pct_high: float = 2.0   # Zone snap min for signal lev 75x+ (tighter moves)
    zone_snap_min_pct_ultra: float = 1.5  # Zone snap min for signal lev 100x+ (BTC/majors)
    zone_snap_lev_high: int = 75          # Signal leverage threshold for "high" tier
    zone_snap_lev_ultra: int = 100        # Signal leverage threshold for "ultra" tier
    zone_refresh_minutes: int = 15        # Refresh zones every 15min
    zone_candle_count: int = 100          # Candles to analyze for swing H/L
    zone_candle_interval: str = "15"      # 15min candles

    # ── Neo Cloud + DCA interaction ──
    # When Neo Cloud flips AFTER DCA filled: don't close immediately.
    # Instead, tighten hard SL from 3% to 1.5% (from deepest DCA fill).
    # This gives DCA recovery a fair chance while capping loss at ~3% equity.
    # Set to 0 to disable (= always close immediately on Neo flip).
    neo_dca_tight_sl_pct: float = 1.5  # Tightened SL after Neo flip + DCA

    # ── Filters ──
    min_leverage_signal: int = 0    # Skip signals below this leverage
    max_leverage_signal: int = 100  # Skip signals above this leverage
    allowed_coins: list[str] = field(default_factory=list)  # Empty = all coins
    blocked_coins: list[str] = field(default_factory=list)

    # ── Server ──
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # ── Notifications ──
    telegram_notify_chat_id: str = ""  # Chat ID for bot notifications
    telegram_bot_token: str = ""       # Bot token for sending notifications

    # ── Database (Railway PostgreSQL) ──
    database_url: str = ""  # Set automatically by Railway when you add PostgreSQL

    @property
    def sum_multipliers(self) -> float:
        """Sum of all DCA multipliers used."""
        return sum(self.dca_multipliers[:self.max_dca_levels + 1])

    def trade_budget(self, equity: float) -> float:
        """Total margin budget for a trade."""
        return equity * self.equity_pct_per_trade / 100

    def e1_margin(self, equity: float) -> float:
        """E1 margin in USD."""
        return self.trade_budget(equity) / self.sum_multipliers

    def e1_notional(self, equity: float) -> float:
        """E1 notional (leveraged) in USD."""
        return self.e1_margin(equity) * self.leverage

    def dca_margin(self, equity: float, level: int) -> float:
        """Margin for a specific DCA level (0=E1, 1=DCA1, etc.)."""
        return self.e1_margin(equity) * self.dca_multipliers[level]

    def dca_price(self, entry_price: float, level: int, side: str) -> float:
        """Price at which a DCA level triggers.

        Includes limit buffer (0.2%) to push limit deeper into zone,
        compensating for 1-candle lag from NEOCloud zone data.
        Long: 0.2% lower, Short: 0.2% higher.
        """
        if level == 0:
            return entry_price
        pct = self.dca_spacing_pct[level] / 100
        buf = self.dca_limit_buffer_pct / 100
        if side == "long":
            return entry_price * (1 - pct) * (1 - buf)
        else:
            return entry_price * (1 + pct) * (1 + buf)

    def get_zone_snap_min_pct(self, signal_leverage: int) -> float:
        """Get zone snap minimum % based on signal leverage tier.

        High-leverage signals (ETH, BTC) have tighter moves, so zone snap
        minimum is reduced to allow snapping at closer support/resistance.
        """
        if signal_leverage >= self.zone_snap_lev_ultra:
            return self.zone_snap_min_pct_ultra
        if signal_leverage >= self.zone_snap_lev_high:
            return self.zone_snap_min_pct_high
        return self.zone_snap_min_pct

    def print_summary(self, equity: float = 2400):
        """Print configuration summary with example equity."""
        sm = self.sum_multipliers
        budget = self.trade_budget(equity)
        notional = budget * self.leverage
        e1n = self.e1_notional(equity)
        safety_loss = e1n * self.safety_sl_pct / 100  # E1-only, pre-DCA
        dca_loss = notional * self.hard_sl_pct / 100   # Full position, post-DCA

        print(f"╔══════════════════════════════════════════════════════╗")
        print(f"║  SIGNAL DCA BOT v2 - Multi-TP                        ║")
        print(f"╠══════════════════════════════════════════════════════╣")
        print(f"║  Equity:         ${equity:,.0f}")
        print(f"║  Leverage:       {self.leverage}x (fixed)")
        print(f"║  Equity/Trade:   {self.equity_pct_per_trade}% = ${budget:.0f} margin")
        print(f"║  Notional/Trade: ${notional:.0f}")
        print(f"║  Max Loss (no DCA): ${safety_loss:.0f} ({safety_loss/equity*100:.1f}% eq) [entry-{self.safety_sl_pct}%]")
        print(f"║  Max Loss (DCA):    ${dca_loss:.0f} ({dca_loss/equity*100:.1f}% eq) [avg-{self.hard_sl_pct}%]")
        print(f"║  Max Trades:     {self.max_simultaneous_trades}")
        print(f"║  Batch Cap:      {self.max_fills_per_batch} fills/batch (extras cancelled)")
        print(f"║")
        print(f"║  DCA:            {self.max_dca_levels} DCA {self.dca_multipliers[:self.max_dca_levels+1]} (sum={sm})")
        print(f"║  DCA Spacing:    {self.dca_spacing_pct[:self.max_dca_levels+1]}% (+{self.dca_limit_buffer_pct}% limit buffer)")
        print(f"║  E1 Notional:    ${e1n:.0f}")
        print(f"║")
        print(f"║  Multi-TP (signal targets):")
        tp_labels = [f"TP{i+1}={p}%" for i, p in enumerate(self.tp_close_pcts)]
        trail_pct = 100 - sum(self.tp_close_pcts)
        print(f"║    {', '.join(tp_labels)}, Trail={trail_pct}%")
        print(f"║    SL Ladder (with scale-in):")
        if self.scale_in_enabled:
            print(f"║      TP1→BE+{self.be_buffer_pct}%, TP2→Scale-In+SL=Avg, TP3→SL@TP2, TP4→Trail {self.trailing_callback_pct}% CB")
        else:
            print(f"║      TP1→BE+{self.be_buffer_pct}%, TP2→stay BE, TP3→SL@TP1, TP4→Trail {self.trailing_callback_pct}% CB")
        print(f"║    DCA SL: TP1→BE+{self.dca_be_buffer_pct}% (exakt avg)")
        print(f"║    TP qty consolidation: TPs below min_qty auto-merge into trail")
        print(f"║")
        dca_tp_str = ", ".join(f"TP{i+1}={p}%" for i, p in enumerate(self.dca_tp_pcts))
        dca_trail_pct = 100 - sum(self.dca_tp_close_pcts)
        print(f"║  DCA Exit:       {dca_tp_str} from avg, trail {dca_trail_pct}% @{self.dca_trail_callback_pct}%CB")
        print(f"║  Safety SL:      Entry - {self.safety_sl_pct}% (pre-DCA)")
        print(f"║  Hard SL:        Avg - {self.hard_sl_pct}% (post-DCA)")
        print(f"║  Quick Trail:    +{self.dca_quick_trail_trigger_pct}% → SL=avg+{self.dca_quick_trail_buffer_pct}%")
        snap_info = (f"ON ({self.zone_snap_min_pct}%/<{self.zone_snap_lev_high}x, "
                     f"{self.zone_snap_min_pct_high}%/{self.zone_snap_lev_high}x+, "
                     f"{self.zone_snap_min_pct_ultra}%/{self.zone_snap_lev_ultra}x+)")
        print(f"║  Zone Snap:      {snap_info if self.zone_snap_enabled else 'OFF'}")
        print(f"║  Neo Cloud:      {'FILTER ON' if self.neo_cloud_filter else 'OFF'}")
        print(f"║  Zone Filter:    {'ON (skip shorts<S1, longs>R1)' if self.zone_filter_enabled else 'OFF'}")
        print(f"║  Testnet:        {'YES' if self.bybit_testnet else 'NO ⚠️  LIVE!'}")
        print(f"║")
        print(f"║  Levels (Long @ $100):")
        for i in range(self.max_dca_levels + 1):
            p = self.dca_price(100, i, "long")
            m = self.dca_margin(equity, i)
            n = m * self.leverage
            label = "E1" if i == 0 else f"DCA{i}"
            print(f"║    {label}: ${p:.2f}  {self.dca_multipliers[i]:>2.0f}x  ${m:.2f} margin  ${n:.0f} notional")
        print(f"╚══════════════════════════════════════════════════════╝")


def load_config() -> BotConfig:
    """Load config from environment variables."""
    config = BotConfig(
        bybit_api_key=os.getenv("BYBIT_API_KEY", ""),
        bybit_api_secret=os.getenv("BYBIT_API_SECRET", ""),
        bybit_testnet=os.getenv("BYBIT_TESTNET", "true").lower() == "true",
        telegram_api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
        telegram_string_session=os.getenv("TELEGRAM_STRING_SESSION", ""),
        telegram_channel=os.getenv("TELEGRAM_CHANNEL", ""),
        leverage=int(os.getenv("LEVERAGE", "20")),
        equity_pct_per_trade=float(os.getenv("EQUITY_PCT", "5")),
        max_simultaneous_trades=int(os.getenv("MAX_TRADES", "6")),
        max_fills_per_batch=int(os.getenv("MAX_FILLS_PER_BATCH", "3")),
        database_url=os.getenv("DATABASE_URL", ""),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        telegram_notify_chat_id=os.getenv("TELEGRAM_NOTIFY_CHAT_ID", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        blocked_coins=[c.strip().upper().replace("USDT", "") for c in os.getenv("BLOCKED_COINS", "").split(",") if c.strip()],
        allowed_coins=[c.strip().upper().replace("USDT", "") for c in os.getenv("ALLOWED_COINS", "").split(",") if c.strip()],
    )
    return config


if __name__ == "__main__":
    cfg = BotConfig()
    cfg.print_summary(2400)
