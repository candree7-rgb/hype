"""
Signal DCA Bot v2 - Configuration
Telegram Signal → Bybit DCA Trading Bot

Strategy:
- 1 DCA [1, 2] at entry-5% (zone-snapped to S1/R1 with 2% min)
- Multi-TP: TP1 50%, TP2 10%, TP3 10%, TP4 10% (signal targets)
- Trail remaining 20% after TP4 (0.5% CB)
- SL-to-BE after TP1 fills
- DCA exit: BE-Trail from avg (0.5% CB)
- Hard SL at avg-3%
- Neo Cloud trend switch: close on clear reversal
- Zone-snapping: S1/R1 dynamic zones from LuxAlgo/Bybit candles
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
    # 1 DCA: E1 + DCA1 with sizing [1, 2] = sum 3
    dca_multipliers: list[float] = field(
        default_factory=lambda: [1, 2]
    )
    # DCA1 at entry-5% (before zone snap)
    dca_spacing_pct: list[float] = field(
        default_factory=lambda: [0, 5]
    )
    max_dca_levels: int = 1  # 1 DCA = total 2 entries (E1 + DCA1)

    # ── Multi-TP (E1-only mode, uses signal targets) ──
    # Close portions at signal's TP1-TP4 price targets.
    # Remaining position trails after last TP.
    tp_close_pcts: list[float] = field(
        default_factory=lambda: [50, 10, 10, 10]  # TP1=50%, TP2=10%, TP3=10%, TP4=10%
    )
    trailing_callback_pct: float = 0.5  # 0.5% CB for trail after all TPs
    sl_to_be_after_tp1: bool = True     # Move SL to breakeven after TP1 fills

    # ── DCA Exit (BE-Trail, activates from DCA1) ──
    be_trail_callback_pct: float = 0.5  # Trail from avg with 0.5% CB

    # ── Hard Stop Loss ──
    hard_sl_pct: float = 3.0  # SL at entry/avg - 3%

    # ── Zone Snapping ──
    zone_snap_enabled: bool = True
    zone_snap_min_pct: float = 2.0        # Min distance for zone snap (hybrid mode)
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
        print(f"║  SIGNAL DCA BOT v2 - Multi-TP CONFIG         ║")
        print(f"╠══════════════════════════════════════════════╣")
        print(f"║  Equity:         ${equity:,.0f}")
        print(f"║  Leverage:       {self.leverage}x")
        print(f"║  Max Trades:     {self.max_simultaneous_trades}")
        print(f"║  Budget/Trade:   {self.equity_pct_per_trade}% = ${total_budget:,.0f}")
        print(f"║  Max Notional:   ${total_notional:,.0f}")
        print(f"║")
        print(f"║  DCA:            {self.max_dca_levels} DCA {self.dca_multipliers[:self.max_dca_levels+1]} (sum={sm})")
        print(f"║  DCA Spacing:    {self.dca_spacing_pct[:self.max_dca_levels+1]}%")
        print(f"║  E1 Margin:      ${e1m:.2f} → ${e1n:.2f} notional")
        print(f"║")
        print(f"║  Multi-TP (signal targets):")
        tp_labels = [f"TP{i+1}={p}%" for i, p in enumerate(self.tp_close_pcts)]
        trail_pct = 100 - sum(self.tp_close_pcts)
        print(f"║    {', '.join(tp_labels)}, Trail={trail_pct}%")
        print(f"║    SL-to-BE after TP1: {'YES' if self.sl_to_be_after_tp1 else 'NO'}")
        print(f"║    Trail CB: {self.trailing_callback_pct}%")
        print(f"║")
        print(f"║  DCA Exit:       BE-Trail ({self.be_trail_callback_pct}% CB from avg)")
        print(f"║  Hard SL:        Entry/Avg - {self.hard_sl_pct}%")
        print(f"║  Zone Snap:      {'ON (hybrid, min ' + str(self.zone_snap_min_pct) + '%)' if self.zone_snap_enabled else 'OFF'}")
        print(f"║  Testnet:        {'YES' if self.bybit_testnet else 'NO ⚠️  LIVE!'}")
        print(f"║")
        print(f"║  Example (Long @ $100, E1 only):")
        for i in range(self.max_dca_levels + 1):
            p = self.dca_price(100, i, "long")
            m = self.dca_margin(equity, i)
            n = m * self.leverage
            label = "E1" if i == 0 else f"DCA{i}"
            print(f"║    {label}: ${p:.2f}  {self.dca_multipliers[i]:>2.0f}x  ${m:.2f} margin  ${n:.2f} notional")
        print(f"║  Max Loss (SL):  ${total_notional * self.hard_sl_pct / 100:.2f}")
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
