"""FROZEN Lighter config — shock candle overshoot catch, v1 (2026-07-21).

Tuned ONLY on Jul 2025 - Jan 2026 (in-sample); Feb - Jun 2026 held out.
Run with engine_lighter.py, sl_mode="stoplimit", sl_grace=10.

Deviations from the validated Binance/MEXC default and why:
- offset_atr 3.0 -> 3.5   (edge monotone in offset depth; deeper entry pays
                           for the harsher SL-slippage regime; 2.5 was worse,
                           4.0 statistically indistinguishable)
- tp/sl floor .003 -> .002 (0/0 fees remove the fee tax on tight stops; the
                           improvement was monotone .003->.0025->.002 under
                           all three slippage regimes; .0015 was not
                           meaningfully better and thins the eps margin)
- SL as stop-LIMIT         (venue-mechanical: Lighter taker = 200-300ms
                           latency, maker/cancel = 0ms; trigger places a
                           passive limit at the SL price; ~91% of stops
                           recover through the level within 10 min and fill
                           at SL with zero slippage; the rest bail via taker
                           market with full regime slippage)
Everything else stays at the validated defaults (shock_atr 4, ttl 5,
cooldown 20, atr_n 60, tp=sl=3 ATR, max_hold 240).

The RESULTS.md floor filter (skip 3*ATR60/close < 0.30%) is deliberately NOT
baked in: it is deep-book-conditional and Lighter books are thin — deploy it
as a monitored A/B overlay, not a hard skip.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shock_lighter_base import generate_signals as _gen  # noqa: E402

FROZEN_EXEC = {"sl_mode": "stoplimit", "sl_grace": 10, "max_hold": 240}

DEFAULT_PARAMS = {
    "shock_atr": 4.0,
    "offset_atr": 3.5,
    "ttl_entry": 5,
    "cooldown": 20,
    "atr_n": 60,
    "tp_atr_mult": 3.0,
    "sl_atr_mult": 3.0,
    "tp_floor": 0.002,
    "sl_floor": 0.002,
    "warmup": 150,
}


def generate_signals(df, params):
    return _gen(df, {**DEFAULT_PARAMS, **(params or {})})
