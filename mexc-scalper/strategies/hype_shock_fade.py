"""HYPE-specialized cascade-overshoot fade (single-coin config).

Same mechanism and code as hl_native_shock_freq (imported — no logic fork);
only two parameters differ, tuned on IS Jul-2025..Jan-2026 HYPE Binance 1m
@ HL base fees, holdout Feb-Jun-2026 opened once:

- shock_atr 3.5 -> 3.0: HYPE shocks often; milder cascades still overshoot,
- offset_atr 2.5 -> 3.5: HYPE's deep book absorbs shallow overshoots — the
  universal 2.5-ATR limit fills on flow that doesn't snap back (adverse
  selection). Depth is what pays on HYPE, the opposite of the thin-alt result.

Everything else stays at the frozen universal values (cooldown 15, ttl 5,
sl 3 ATR / 0.3% floor, 1:1, atr_min 0.001). IS plateau is broad: shock
2.75-3.25 x offset 3.25-3.75 all give avg_r +0.15..+0.30 (universal offset
2.5 gives +0.07 on HYPE). Variant count for this selection: 48 (stage A)
+ 23 (stage B refinement) + 24 (second-family search, all rejected) = 95.

IS (Jul25-Jan26):    n=182, WR 68.7%, avg_r +0.274, +49.8R, PF 1.74
Holdout (Feb-Jun26): evaluated once — see RESULTS/report for the numbers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hl_native_shock_freq import generate_signals, COLS  # noqa: F401
from hl_native_shock_freq import DEFAULT_PARAMS as _BASE

DEFAULT_PARAMS = {**_BASE, "shock_atr": 3.0, "offset_atr": 3.5}
