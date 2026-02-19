"""
Telegram Signal Parser for VIP Club format.

Parses messages like:
    🔴 Short
    Name: 1000BONK/USDT
    Margin mode: Cross (50.0X)

    ⓒ Entry price(USDT):
    0.0063220

    Targets(USDT):
    1) 0.0062590
    2) 0.0061960
    3) 0.0061320
    4) 0.0060690
    5) 🔝 unlimited

Also handles:
    🟢 Long
    Name: XMR/USDT
    Margin mode: Cross (25.0X)
    ...
"""

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """Parsed trading signal from Telegram."""
    side: str           # "long" or "short"
    symbol: str         # e.g. "1000BONKUSDT" (Bybit format, no slash)
    symbol_display: str # e.g. "1000BONK/USDT" (display format)
    entry_price: float
    targets: list[float] = field(default_factory=list)
    signal_leverage: int = 50  # Original signal leverage (we override with our own)
    raw_message: str = ""

    @property
    def tp1_pct(self) -> float:
        """TP1 distance from entry in %."""
        if not self.targets:
            return 0
        if self.side == "long":
            return (self.targets[0] - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - self.targets[0]) / self.entry_price * 100

    @property
    def tp_pcts(self) -> list[float]:
        """All TP distances from entry in %."""
        result = []
        for t in self.targets:
            if self.side == "long":
                pct = (t - self.entry_price) / self.entry_price * 100
            else:
                pct = (self.entry_price - t) / self.entry_price * 100
            result.append(round(pct, 2))
        return result


def parse_signal(message: str) -> Signal | None:
    """Parse a VIP Club signal message into a Signal object.

    Returns None if the message is not a valid signal.
    """
    if not message:
        return None

    # Normalize unicode and whitespace
    text = message.strip()

    # ── Detect side ──
    side = None
    if re.search(r"🔴\s*Short|Short\s*$", text, re.MULTILINE | re.IGNORECASE):
        side = "short"
    elif re.search(r"🟢\s*Long|Long\s*$", text, re.MULTILINE | re.IGNORECASE):
        side = "long"

    if side is None:
        return None

    # ── Extract symbol ──
    symbol_match = re.search(r"Name:\s*(\S+)", text)
    if not symbol_match:
        return None
    symbol_display = symbol_match.group(1).strip()
    # Convert "1000BONK/USDT" → "1000BONKUSDT" for Bybit API
    symbol = symbol_display.replace("/", "")

    # ── Extract leverage ──
    lev_match = re.search(r"Cross\s*\((\d+(?:\.\d+)?)X\)", text, re.IGNORECASE)
    signal_leverage = int(float(lev_match.group(1))) if lev_match else 50

    # ── Extract entry price ──
    entry_match = re.search(
        r"Entry\s*price\s*\(?USDT\)?\s*:\s*\n?\s*([\d.]+)",
        text,
        re.IGNORECASE
    )
    if not entry_match:
        return None
    entry_price = float(entry_match.group(1))

    if entry_price <= 0:
        return None

    # ── Extract targets ──
    targets = []
    target_pattern = re.findall(
        r"(\d+)\)\s*([\d.]+)",
        text[text.lower().find("target"):]
    ) if "target" in text.lower() else []

    for _, price_str in target_pattern:
        try:
            price = float(price_str)
            if price > 0:
                targets.append(price)
        except ValueError:
            continue

    if not targets:
        logger.warning(f"No targets found in signal for {symbol_display}")
        return None

    # ── Validate signal makes sense ──
    if side == "long" and targets[0] <= entry_price:
        logger.warning(f"Long signal but TP1 <= entry: {symbol_display}")
        return None
    if side == "short" and targets[0] >= entry_price:
        logger.warning(f"Short signal but TP1 >= entry: {symbol_display}")
        return None

    signal = Signal(
        side=side,
        symbol=symbol,
        symbol_display=symbol_display,
        entry_price=entry_price,
        targets=targets,
        signal_leverage=signal_leverage,
        raw_message=text,
    )

    logger.info(
        f"Parsed signal: {side.upper()} {symbol_display} @ {entry_price} "
        f"| TPs: {signal.tp_pcts}% | Signal Lev: {signal_leverage}x"
    )

    return signal


def parse_close_signal(message: str) -> dict | None:
    """Parse a close/cancel signal.

    Messages like:
        "Close 1000BONK/USDT"
        "Cancel ONDO/USDT"
    """
    text = message.strip()

    close_match = re.search(
        r"(?:Close|Cancel|Schliessen)\s+(\S+/USDT)",
        text,
        re.IGNORECASE
    )
    if not close_match:
        return None

    symbol_display = close_match.group(1)
    symbol = symbol_display.replace("/", "")

    return {
        "action": "close",
        "symbol": symbol,
        "symbol_display": symbol_display,
    }


def parse_tp_hit(message: str) -> dict | None:
    """Parse a TP hit notification from VIP Club.

    Messages like:
        "💸 MOODENG/USDT ✅ Target #1 Done Current profit: 50.0%..."
        "💸 FARTCOIN/USDT ✅ Target #1 Done Current profit: 75.0%..."
        "💸 BTC/USDT ✅ Target #2 Done Current profit: 120.5%..."
    """
    text = message.strip()

    tp_match = re.search(
        r"(\S+/USDT)\s*✅\s*Target\s*#(\d+)\s*Done",
        text,
        re.IGNORECASE
    )
    if not tp_match:
        return None

    symbol_display = tp_match.group(1)
    symbol = symbol_display.replace("/", "")
    tp_number = int(tp_match.group(2))

    return {
        "action": "tp_hit",
        "symbol": symbol,
        "symbol_display": symbol_display,
        "tp_number": tp_number,
    }


# ── Tests ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_signals = [
        # Short signal
        """🔴 Short
Name: 1000BONK/USDT
Margin mode: Cross (50.0X)

ⓒ Entry price(USDT):
0.0063220

Targets(USDT):
1) 0.0062590
2) 0.0061960
3) 0.0061320
4) 0.0060690
5) 🔝 unlimited""",

        # Long signal
        """🟢 Long
Name: XMR/USDT
Margin mode: Cross (25.0X)

ⓒ Entry price(USDT):
326.26

Targets(USDT):
1) 329.52
2) 332.79
3) 336.05
4) 339.31
5) 🔝 unlimited""",

        # Long signal with higher leverage
        """🟢 Long
Name: AAVE/USDT
Margin mode: Cross (75.0X)

ⓒ Entry price(USDT):
113.14

Targets(USDT):
1) 114.27
2) 115.40
3) 116.53
4) 117.67
5) 🔝 unlimited""",

        # Not a signal
        "Hello world, this is not a signal",
    ]

    for i, msg in enumerate(test_signals):
        print(f"\n{'='*50}")
        print(f"Test {i+1}:")
        result = parse_signal(msg)
        if result:
            print(f"  Side:     {result.side}")
            print(f"  Symbol:   {result.symbol} ({result.symbol_display})")
            print(f"  Entry:    {result.entry_price}")
            print(f"  Targets:  {result.targets}")
            print(f"  TP %:     {result.tp_pcts}")
            print(f"  Sig Lev:  {result.signal_leverage}x")
        else:
            print("  Not a valid signal")
