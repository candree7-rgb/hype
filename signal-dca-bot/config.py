"""
Signal DCA Bot v2 - Configuration
Telegram Signal → Bybit DCA Trading Bot

Strategy:
- 3 DCAs [1, 2, 4, 8] with growing spacing [0, 5, 11, 18]
- 50% close at TP1, trail rest from TP1 floor (0.5% CB)
- BE-Trail from DCA1+ (0.5% CB when price returns to avg)
- Hard SL at avg - 3% from DCA1
- Zone-snapping: auto-calculated swing H/L from Bybit candles
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
    leverage: int = 20
    equity_pct_per_trade: float = 20.0  # 20% of equity per trade
    max_simultaneous_trades: int = 6
    e1_limit_order: bool = True         # True = Limit at signal price, False = Market
    e1_timeout_minutes: int = 10        # Cancel E1 limit if not filled after X minutes

    # ── DCA Configuration ──
    # Sizing: exponential doubling [1, 2, 4, 8] = sum 15
    dca_multipliers: list[float] = field(
        default_factory=lambda: [1, 2, 4, 8]
    )
    # Spacing: growing gaps (5%, 6%, 7%)
    # E1=signal, DCA1=entry-5%, DCA2=entry-11%, DCA3=entry-18%
    dca_spacing_pct: list[float] = field(
        default_factory=lambda: [0, 5, 11, 18]
    )
    max_dca_levels: int = 3  # 3 DCAs = total 4 entries (E1 + DCA1-3)

    # ── Take Profit (E1-only trades, no DCA) ──
    tp1_pct: float = 1.0        # TP1 at 1% from entry
    tp1_close_pct: float = 50.0 # Close 50% at TP1
    trailing_callback_pct: float = 0.5  # Trail remaining 50% with 0.5% CB
    # After TP1: trail floor = TP1 level (remaining can never close below TP1 profit)

    # ── DCA Exit (BE-Trail, activates from DCA1+) ──
    be_trail_callback_pct: float = 0.5  # Trail from avg with 0.5% CB
    # When price returns to avg after DCA → activate trailing
    # Close 100% on callback

    # ── Hard Stop Loss (from DCA1+) ──
    hard_sl_pct: float = 3.0  # SL at avg - 3% (always active from DCA1)

    # ── Zone Snapping ──
    zone_snap_enabled: bool = True
    zone_snap_threshold_pct: float = 2.0  # Max 2% diff to snap DCA to zone
    zone_refresh_minutes: int = 15        # Refresh zones every 15min
    zone_candle_count: int = 100          # Candles to analyze for swing H/L
    zone_candle_interval: str = "15"      # 15min candles

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

    @property
    def base_fraction(self) -> float:
        """E1 fraction of total budget: 1/sum."""
        return 1.0 / self.sum_multipliers

    def e1_margin(self, equity: float) -> float:
        """E1 margin in USD given current equity."""
        total_budget = equity * self.equity_pct_per_trade / 100
        return total_budget / self.sum_multipliers

    def e1_notional(self, equity: float) -> float:
        """E1 notional (leveraged) in USD."""
        return self.e1_margin(equity) * self.leverage

    def dca_margin(self, equity: float, level: int) -> float:
        """Margin for a specific DCA level (0=E1, 1=DCA1, etc.)."""
        base = self.e1_margin(equity)
        return base * self.dca_multipliers[level]

    def dca_price(self, entry_price: float, level: int, side: str) -> float:
        """Price at which a DCA level triggers."""
        if level == 0:
            return entry_price
        pct = self.dca_spacing_pct[level] / 100
        if side == "long":
            return entry_price * (1 - pct)
        else:
            return entry_price * (1 + pct)

    def print_summary(self, equity: float = 2400):
        """Print configuration summary with example equity."""
        sm = self.sum_multipliers
        e1m = self.e1_margin(equity)
        e1n = self.e1_notional(equity)
        total_budget = equity * self.equity_pct_per_trade / 100
        total_notional = total_budget * self.leverage

        print(f"╔══════════════════════════════════════════════╗")
        print(f"║  SIGNAL DCA BOT v2 - CONFIG                  ║")
        print(f"╠══════════════════════════════════════════════╣")
        print(f"║  Equity:         ${equity:,.0f}")
        print(f"║  Leverage:       {self.leverage}x")
        print(f"║  Max Trades:     {self.max_simultaneous_trades}")
        print(f"║  Budget/Trade:   {self.equity_pct_per_trade}% = ${total_budget:,.0f}")
        print(f"║  Max Notional:   ${total_notional:,.0f}")
        print(f"║")
        print(f"║  DCA Mults:      {self.dca_multipliers[:self.max_dca_levels+1]}")
        print(f"║  DCA Spacing:    {self.dca_spacing_pct[:self.max_dca_levels+1]}%")
        print(f"║  Sum Mults:      {sm}")
        print(f"║")
        print(f"║  E1 Margin:      ${e1m:.2f}")
        print(f"║  E1 Notional:    ${e1n:.2f}")
        print(f"║  TP1 ({self.tp1_pct}%):      ${e1n * self.tp1_pct / 100:.2f} profit")
        print(f"║")
        print(f"║  Exit Logic:")
        print(f"║    E1 only:  50% close TP1, trail rest (0.5% CB, floor=TP1)")
        print(f"║    DCA1+:    BE-Trail (0.5% CB when price returns to avg)")
        print(f"║    Hard SL:  Avg - {self.hard_sl_pct}% from DCA1")
        print(f"║")
        print(f"║  DCA Levels (Long entry at $100):")
        for i in range(self.max_dca_levels + 1):
            p = self.dca_price(100, i, "long")
            m = self.dca_margin(equity, i)
            n = m * self.leverage
            label = "E1" if i == 0 else f"DCA{i}"
            print(f"║    {label}: ${p:.2f}  {self.dca_multipliers[i]:>2.0f}x  ${m:.2f} margin  ${n:.2f} notional")
        print(f"║")
        print(f"║  Max Loss (all DCAs + SL): ${total_notional * self.hard_sl_pct / 100:.2f} = {self.equity_pct_per_trade * self.hard_sl_pct / 100 * self.leverage:.1f}% equity")
        print(f"║  Zones:          {'ON (auto swing H/L)' if self.zone_snap_enabled else 'OFF'}")
        print(f"║  Testnet:        {'YES' if self.bybit_testnet else 'NO ⚠️  LIVE!'}")
        print(f"╚══════════════════════════════════════════════╝")


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
        equity_pct_per_trade=float(os.getenv("EQUITY_PCT", "20")),
        max_simultaneous_trades=int(os.getenv("MAX_TRADES", "6")),
        database_url=os.getenv("DATABASE_URL", ""),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        telegram_notify_chat_id=os.getenv("TELEGRAM_NOTIFY_CHAT_ID", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )
    return config


if __name__ == "__main__":
    cfg = BotConfig()
    cfg.print_summary(2400)
