"""
Signal DCA Bot v1 - Configuration
Telegram Signal → Bybit DCA Trading Bot

Based on Cornix reverse-engineering:
- Exponential DCA sizing: 1:2:4:8:16:32
- Growing DCA spacing: 3%, 4%, 5%, 6%, 7%
- No SL until all DCAs filled, then trail to avg
- TP: 50% at TP1, rest trails to TP2+ or BE
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
    telegram_channel: str = ""  # VIP Club channel name or ID

    # ── Capital & Risk ──
    leverage: int = 20
    equity_pct_per_trade: float = 20.0  # 20% of equity per trade
    max_simultaneous_trades: int = 6
    e1_limit_order: bool = True         # True = Limit at signal price, False = Market
    e1_timeout_minutes: int = 10        # Cancel E1 limit if not filled after X minutes

    # ── DCA Configuration ──
    # Sizing: exponential doubling (Cornix-style)
    dca_multipliers: list[float] = field(
        default_factory=lambda: [1, 2, 4, 8, 16, 32]
    )
    # Spacing: growing gaps between DCA levels
    # E1=signal entry, E2=entry-3%, E3=entry-7%, E4=entry-12%, E5=entry-18%, E6=entry-25%
    dca_spacing_pct: list[float] = field(
        default_factory=lambda: [0, 3, 7, 12, 18, 25]
    )
    max_dca_levels: int = 5  # 5 = total 6 entries (E1 + 5 DCA)

    # ── Take Profit ──
    tp1_pct: float = 1.0     # TP1 at 1% from avg
    tp1_close_pct: float = 50.0  # Close 50% of position at TP1
    tp2_pct: float = 2.0     # TP2 at 2% (for trailing portion)
    tp3_pct: float = 3.0
    tp4_pct: float = 4.0
    trailing_after_tp1: bool = True  # Trail remaining to BE or TP2+
    trailing_callback_pct: float = 0.5  # Trail callback: close if drops 0.5% from peak

    # ── Stop Loss ──
    use_stop_loss: bool = False  # Cornix-style: no SL, hold until bounce
    sl_after_last_dca_pct: float = 5.0  # If enabled: SL at avg-5% after all DCAs
    trail_to_breakeven: bool = True  # After all DCAs: trail stop to avg (breakeven)

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
        """Price at which a DCA level triggers.

        Args:
            entry_price: E1 entry price
            level: DCA level (1-5, 0=E1)
            side: 'long' or 'short'
        """
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

        print(f"╔══════════════════════════════════════════════╗")
        print(f"║  SIGNAL DCA BOT v1 - CONFIG                  ║")
        print(f"╠══════════════════════════════════════════════╣")
        print(f"║  Equity:         ${equity:,.0f}")
        print(f"║  Leverage:       {self.leverage}x")
        print(f"║  Max Trades:     {self.max_simultaneous_trades}")
        print(f"║  Budget/Trade:   {self.equity_pct_per_trade}% = ${equity * self.equity_pct_per_trade / 100:,.0f}")
        print(f"║")
        print(f"║  DCA Mults:      {self.dca_multipliers[:self.max_dca_levels+1]}")
        print(f"║  DCA Spacing:    {self.dca_spacing_pct[:self.max_dca_levels+1]}%")
        print(f"║  Sum Mults:      {sm}")
        print(f"║")
        print(f"║  E1 Margin:      ${e1m:.2f}")
        print(f"║  E1 Notional:    ${e1n:.2f}")
        print(f"║  TP1 ({self.tp1_pct}%):      ${e1n * self.tp1_pct / 100:.2f} profit")
        print(f"║")
        print(f"║  DCA Levels (Long entry at $100):")
        for i in range(self.max_dca_levels + 1):
            p = self.dca_price(100, i, "long")
            m = self.dca_margin(equity, i)
            n = m * self.leverage
            label = "E1" if i == 0 else f"DCA{i}"
            print(f"║    {label}: ${p:.2f}  {self.dca_multipliers[i]:>2.0f}x  ${m:.2f} margin  ${n:.2f} notional")
        print(f"║")
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
        telegram_channel=os.getenv("TELEGRAM_CHANNEL", ""),
        leverage=int(os.getenv("LEVERAGE", "20")),
        equity_pct_per_trade=float(os.getenv("EQUITY_PCT", "20")),
        max_simultaneous_trades=int(os.getenv("MAX_TRADES", "6")),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        telegram_notify_chat_id=os.getenv("TELEGRAM_NOTIFY_CHAT_ID", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )
    return config


if __name__ == "__main__":
    cfg = BotConfig()
    cfg.print_summary(2400)
