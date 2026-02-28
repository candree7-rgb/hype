"""
Telegram Listener - Reads signals from VIP Club channel via Telethon.

Uses StringSession for Railway deployment (no interactive auth needed).
Runs in the same async event loop as FastAPI. Calls add_signal_to_batch()
directly (same process, no HTTP overhead).

Setup:
  1. Get API_ID and API_HASH from https://my.telegram.org
  2. Generate STRING_SESSION locally (see generate_session() below)
  3. Set TELEGRAM_STRING_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH in .env
  4. Set TELEGRAM_CHANNEL to channel name, title, or numeric ID
"""

import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from config import BotConfig
from telegram_parser import parse_signal, parse_close_signal, parse_tp_hit

logger = logging.getLogger("telegram")


class TelegramListener:
    """Listens to a Telegram channel and forwards signals to the bot."""

    def __init__(self, config: BotConfig, on_signal=None, on_close=None, on_tp_hit=None):
        self.config = config
        self.on_signal = on_signal    # async callback: Signal → dict
        self.on_close = on_close      # async callback: dict → None
        self.on_tp_hit = on_tp_hit    # async callback: dict → None (cancel unfilled on TP hit)
        self.client: TelegramClient | None = None
        self._running = False

    @property
    def is_configured(self) -> bool:
        """Check if Telegram credentials are set."""
        return bool(
            self.config.telegram_api_id
            and self.config.telegram_api_hash
            and self.config.telegram_string_session
        )

    async def start(self):
        """Start the Telegram client and register handlers."""
        if not self.is_configured:
            logger.warning(
                "Telegram not configured (missing API_ID/HASH/STRING_SESSION). "
                "Signals will only come via webhook."
            )
            return

        self.client = TelegramClient(
            StringSession(self.config.telegram_string_session),
            self.config.telegram_api_id,
            self.config.telegram_api_hash,
        )

        # Register message handler
        self.client.add_event_handler(
            self._on_message,
            events.NewMessage(),
        )

        await self.client.start()
        self._running = True

        me = await self.client.get_me()
        logger.info(
            f"Telegram connected as {me.first_name} (ID: {me.id}) | "
            f"Listening for channel: {self.config.telegram_channel or 'ALL'}"
        )

    async def stop(self):
        """Disconnect the Telegram client."""
        if self.client and self._running:
            self._running = False
            await self.client.disconnect()
            logger.info("Telegram disconnected")

    def _match_chat(self, event) -> bool:
        """Check if the message is from the target channel."""
        channel = self.config.telegram_channel
        if not channel:
            return True  # No filter = accept all

        # Match by numeric chat ID
        if channel.lstrip("-").isdigit():
            return str(event.chat_id) == str(channel)

        # Match by title or username
        chat = event.chat
        title = getattr(chat, "title", "") or ""
        username = getattr(chat, "username", "") or ""

        return (
            title.lower() == channel.lower()
            or username.lower() == channel.lower()
        )

    async def _on_message(self, event):
        """Handle incoming Telegram messages."""
        if not self._match_chat(event):
            return

        text = event.raw_text
        if not text:
            return

        # Try parsing as a trading signal
        signal = parse_signal(text)
        if signal:
            logger.info(
                f"TG Signal: {signal.side.upper()} {signal.symbol_display} "
                f"@ {signal.entry_price} (Lev: {signal.signal_leverage}x)"
            )
            if self.on_signal:
                try:
                    result = await self.on_signal(signal)
                    logger.info(f"Signal result: {result}")
                except Exception as e:
                    logger.error(f"Error processing signal: {e}", exc_info=True)
            return

        # Try parsing as a close signal
        close_cmd = parse_close_signal(text)
        if close_cmd:
            logger.info(f"TG Close: {close_cmd['symbol_display']}")
            if self.on_close:
                try:
                    await self.on_close(close_cmd)
                except Exception as e:
                    logger.error(f"Error processing close: {e}", exc_info=True)
            return

        # Try parsing as a TP hit notification (cancel unfilled PENDING orders)
        tp_hit = parse_tp_hit(text)
        if tp_hit:
            logger.info(
                f"TG TP hit: {tp_hit['symbol_display']} Target #{tp_hit['tp_number']}"
            )
            if self.on_tp_hit:
                try:
                    await self.on_tp_hit(tp_hit)
                except Exception as e:
                    logger.error(f"Error processing TP hit: {e}", exc_info=True)
            return

        # Not a signal - log for visibility (truncate long messages)
        preview = text[:80].replace("\n", " ")
        logger.info(f"TG msg (not signal): {preview}...")


def generate_session():
    """Interactive helper to generate a StringSession.

    Run this locally ONCE:
        python telegram_listener.py

    It will prompt for phone number and auth code, then print the
    StringSession string to put in your .env file.
    """
    import os
    from dotenv import load_dotenv
    load_dotenv()

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")

    if not api_id or not api_hash:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first!")
        return

    print("Generating Telegram StringSession...")
    print("You will be prompted for your phone number and auth code.\n")

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()
        print(f"\nYour StringSession (add to .env):\n")
        print(f"TELEGRAM_STRING_SESSION={session_string}")
        print(f"\nKeep this secret! Anyone with this string can access your account.")


if __name__ == "__main__":
    generate_session()
