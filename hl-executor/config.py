"""Hyperliquid executor configuration.

Strategy: hl_native_shock_freq (frozen 2026-07-21, see mexc-scalper/RESULTS.md).
12mo validation @ HL tier-0 fees: WR 62.3% vs BE 56.1%, avg +0.132R,
11/12 months positive, max DD 68R. All params here are the frozen set —
do not tune in production; re-run the research pipeline instead.
"""
import os
from dataclasses import dataclass, field


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


@dataclass
class Config:
    # --- Mode ---
    paper: bool = field(default_factory=lambda: _flag("PAPER", "true"))
    # Live trading additionally requires HL_PRIVATE_KEY + PAPER=false.
    # (The key itself is read ONLY inside live.LiveBroker._make_clients.)
    hl_testnet: bool = field(default_factory=lambda: _flag("HL_TESTNET", "false"))
    hl_account_address: str = os.getenv("HL_ACCOUNT_ADDRESS", "")  # master addr when key is an API/agent wallet
    live_adopt: str = os.getenv("LIVE_ADOPT", "close")  # untracked venue positions on reconcile: "close" | "adopt"
    min_notional: float = 10.0          # HL minimum order value (USD)

    # --- Strategy (frozen: hl_native_shock_freq) ---
    shock_atr: float = 3.5      # candle range must exceed this multiple of ATR60
    offset_atr: float = 2.5     # limit offset beyond shock extreme
    ttl_min: int = 5            # entry order lifetime (minutes)
    cooldown_min: int = 15      # per-coin cooldown between signals
    atr_n: int = 60
    sl_atr_mult: float = 3.0
    sl_floor: float = 0.003     # min tp/sl distance (fraction)
    atr_min: float = 0.001      # GATE: skip signals when ATR60 < 0.1%
    max_hold_min: int = 240     # time stop

    # --- Risk ---
    equity_start_paper: float = float(os.getenv("PAPER_EQUITY", "1000"))
    risk_per_trade: float = float(os.getenv("RISK_PER_TRADE", "0.02"))
    max_concurrent: int = int(os.getenv("MAX_CONCURRENT", "6"))
    margin_budget: float = 0.80          # max fraction of equity as margin
    leverage_cap: int = 20               # per-coin: min(this, venue max)

    # --- Fees (tier 0; refreshed live from exchange when trading real) ---
    maker_fee: float = 0.00015
    taker_fee: float = 0.00045
    assumed_sl_slippage: float = 0.0003  # paper accounting only; live is measured

    # --- Kill-switches (from RESULTS.md) ---
    kill_min_fills: int = 200            # evaluate after this many fills
    kill_wr_threshold: float = 0.56      # stop if realized WR below breakeven
    kill_slippage_mult: float = 2.0      # stop if measured slippage > 2x model
    kill_daily_loss_r: float = 20.0      # outer safety: halt after -20R day

    # --- DD overlay C2 (adopted 2026-07-21, see RESULTS.md):
    #     validated on 12mo: year DD 68->32R, holdout total +17% ---
    breaker_r24: float = 6.0             # no new entries while trailing-24h realized PnL <= -6R
    streak_k: int = 6                    # after 6 consecutive losses ...
    streak_window_h: float = 6.0         # ... whose exits fall within 6h ...
    streak_pause_h: float = 2.0          # ... pause new entries 2h

    # --- Universe: validated coins that exist on HL (HL naming) ---
    coins: tuple = (
        "BTC", "ETH", "SOL", "XRP", "HYPE", "ZEC", "kPEPE", "AVAX", "TAO",
        "DOGE", "SUI", "NEAR", "WLD", "PUMP", "LTC", "BNB", "ADA", "LINK",
    )

    # --- Infra (testnet routing when HL_TESTNET=true) ---
    ws_url: str = field(default_factory=lambda: (
        "wss://api.hyperliquid-testnet.xyz/ws"
        if _flag("HL_TESTNET", "false") else "wss://api.hyperliquid.xyz/ws"))
    info_url: str = field(default_factory=lambda: (
        "https://api.hyperliquid-testnet.xyz/info"
        if _flag("HL_TESTNET", "false") else "https://api.hyperliquid.xyz/info"))
    state_file: str = os.getenv("STATE_FILE", "state/executor_state.json")
    record_dir: str = os.getenv("RECORD_DIR", "state/candles")
    port: int = int(os.getenv("PORT", "8000"))


CFG = Config()
