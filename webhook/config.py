"""Configuration from environment variables."""

import os


class Config:
    # Webhook
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # Bybit API
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
    BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")
    BYBIT_TESTNET: bool = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

    # Trading
    SYMBOL: str = os.getenv("SYMBOL", "HYPEUSDT")
    LIMIT_TIMEOUT_SEC: float = float(os.getenv("LIMIT_TIMEOUT_SEC", "8"))
    PRICE_BUFFER_PCT: float = float(os.getenv("PRICE_BUFFER_PCT", "0.05"))

    # Telegram (optional)
    TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "")
    TG_CHAT_ID: str = os.getenv("TG_CHAT_ID", "")


cfg = Config()
