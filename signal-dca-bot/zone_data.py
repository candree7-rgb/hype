"""
Zone Data Manager v2

Primary: LuxAlgo zones from TradingView → PostgreSQL
Fallback: Auto-calculated swing H/L from Bybit candles

PostgreSQL table: "coin_zones"
  symbol (PK), s1, s2, s3, r1, r2, r3, source, updated_at

Zone-snapping: DCAs snap to current S1 (long) / R1 (short).
S1/R1 are dynamic and shift with price. Only snaps in favorable
direction (deeper entries). Resnaps every 15min as zones update.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import database as db

logger = logging.getLogger(__name__)


@dataclass
class CoinZones:
    """Reversal zone levels for a coin."""
    symbol: str
    s1: float = 0  # Inner support (nearest to price)
    s2: float = 0  # Middle support
    s3: float = 0  # Outer support (deepest)
    r1: float = 0  # Inner resistance
    r2: float = 0  # Middle resistance
    r3: float = 0  # Outer resistance
    updated_at: float = 0  # Unix timestamp
    source: str = "unknown"  # "luxalgo", "swing", "manual"

    @property
    def is_valid(self) -> bool:
        """Check if zones are set and recent (< 2 hours)."""
        if self.s1 == 0 and self.r1 == 0:
            return False
        age_min = (time.time() - self.updated_at) / 60
        return age_min < 120

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.updated_at) / 60

    def long_zones(self) -> list[float]:
        """Support zones for long DCA (descending order)."""
        return sorted([z for z in [self.s1, self.s2, self.s3] if z > 0], reverse=True)

    def short_zones(self) -> list[float]:
        """Resistance zones for short DCA (ascending order)."""
        return sorted([z for z in [self.r1, self.r2, self.r3] if z > 0])


class ZoneDataManager:
    """Manages zone data from PostgreSQL + in-memory cache."""

    def __init__(self):
        self._cache: dict[str, CoinZones] = {}

    def warmup_cache(self) -> int:
        """Load all zones from DB into memory cache. Call on startup."""
        rows = db.get_all_zones()
        count = 0
        for row in rows:
            zones = CoinZones(
                symbol=row["symbol"],
                s1=row["s1"], s2=row["s2"], s3=row["s3"],
                r1=row["r1"], r2=row["r2"], r3=row["r3"],
                updated_at=row["updated_at"],
                source=row["source"],
            )
            self._cache[row["symbol"]] = zones
            count += 1

        if count > 0:
            logger.info(f"Zone cache warmed: {count} coins loaded from DB")
        return count

    def get_zones(self, symbol: str) -> Optional[CoinZones]:
        """Get zone data. Checks cache first, then DB."""
        if symbol in self._cache and self._cache[symbol].is_valid:
            return self._cache[symbol]

        # Try DB
        row = db.get_zone(symbol)
        if row:
            zones = CoinZones(
                symbol=symbol,
                s1=row["s1"], s2=row["s2"], s3=row["s3"],
                r1=row["r1"], r2=row["r2"], r3=row["r3"],
                updated_at=row["updated_at"],
                source=row["source"],
            )
            self._cache[symbol] = zones
            if zones.is_valid:
                logger.info(
                    f"Zones loaded ({zones.source}): {symbol} | "
                    f"S: {zones.s1}/{zones.s2}/{zones.s3} | "
                    f"R: {zones.r1}/{zones.r2}/{zones.r3}"
                )
                return zones

        return self._cache.get(symbol)

    def update_zones(self, symbol: str, zones: CoinZones) -> bool:
        """Update zone data in cache and DB."""
        zones.updated_at = time.time()
        self._cache[symbol] = zones

        db.upsert_zone(
            symbol, zones.s1, zones.s2, zones.s3,
            zones.r1, zones.r2, zones.r3, zones.source,
        )
        return True

    def update_from_auto_calc(self, symbol: str, zones: CoinZones) -> bool:
        """Update zones from auto-calc, but ONLY if no recent LuxAlgo zones exist."""
        existing = self._cache.get(symbol)
        if existing and existing.is_valid and existing.source == "luxalgo":
            return False  # LuxAlgo zones take priority
        zones.source = "swing"
        return self.update_zones(symbol, zones)


# ══════════════════════════════════════════════════════════════════════════
# ▌ AUTO ZONE CALCULATOR (Swing Highs/Lows from candles)
# ══════════════════════════════════════════════════════════════════════════

def calc_swing_zones(candles: list[dict], lookback: int = 5) -> CoinZones | None:
    """Calculate support/resistance zones from OHLC candles.

    Args:
        candles: List of {"open": f, "high": f, "low": f, "close": f}
                 ordered oldest → newest
        lookback: Bars on each side to confirm swing (default 5)

    Returns:
        CoinZones with S1/S2/S3 (swing lows) and R1/R2/R3 (swing highs)
    """
    if len(candles) < lookback * 2 + 1:
        return None

    swing_lows = []
    swing_highs = []

    for i in range(lookback, len(candles) - lookback):
        low = candles[i]["low"]
        high = candles[i]["high"]

        # Check if this is a swing low (lowest low in window)
        is_swing_low = all(
            low <= candles[i + j]["low"]
            for j in range(-lookback, lookback + 1) if j != 0
        )
        if is_swing_low:
            swing_lows.append(low)

        # Check if this is a swing high
        is_swing_high = all(
            high >= candles[i + j]["high"]
            for j in range(-lookback, lookback + 1) if j != 0
        )
        if is_swing_high:
            swing_highs.append(high)

    if not swing_lows and not swing_highs:
        return None

    # Take the 3 most recent of each
    recent_lows = swing_lows[-3:] if swing_lows else []
    recent_highs = swing_highs[-3:] if swing_highs else []

    # Sort: S1 = highest (nearest to price), S3 = lowest (deepest support)
    recent_lows.sort(reverse=True)
    # Sort: R1 = lowest (nearest to price), R3 = highest (deepest resistance)
    recent_highs.sort()

    zones = CoinZones(
        symbol="",
        s1=recent_lows[0] if len(recent_lows) > 0 else 0,
        s2=recent_lows[1] if len(recent_lows) > 1 else 0,
        s3=recent_lows[2] if len(recent_lows) > 2 else 0,
        r1=recent_highs[0] if len(recent_highs) > 0 else 0,
        r2=recent_highs[1] if len(recent_highs) > 1 else 0,
        r3=recent_highs[2] if len(recent_highs) > 2 else 0,
        updated_at=time.time(),
        source="swing",
    )
    return zones


# ══════════════════════════════════════════════════════════════════════════
# ▌ DCA ZONE SNAPPING (Dynamic S1/R1)
# ══════════════════════════════════════════════════════════════════════════

def calc_smart_dca_levels(
    entry_price: float,
    fixed_spacing_pcts: list[float],
    zones: CoinZones | None,
    side: str,
    snap_threshold_pct: float = 2.0,
    filled_levels: list[bool] | None = None,
) -> list[tuple[float, str]]:
    """Calculate DCA levels with dynamic S1/R1 zone-lock snapping.

    S1/R1 are DYNAMIC - they shift with every candle as the reversal zone
    moves. All DCAs snap to the CURRENT S1 (longs) or R1 (shorts).

    When DCA1 fills and price drops further, S1 moves deeper. On next
    resnap (every 15min), DCA2 snaps to the new, deeper S1. The fixed
    spacing [0, 5, 11, 18]% acts as minimum distance buffer - a DCA only
    snaps when S1 has moved past its fixed trigger level.

    Only ONE unfilled DCA gets the zone snap per calculation (the next
    one in line). Filled DCAs don't consume the zone.

    Args:
        entry_price: E1 entry price
        fixed_spacing_pcts: Fixed DCA spacing [0, 5, 11, 18]
        zones: Zone data (or None)
        side: "long" or "short"
        snap_threshold_pct: Not used (kept for API compat)
        filled_levels: Boolean list [E1, DCA1, DCA2, DCA3] - True if filled.
                       Filled levels don't consume the zone snap.

    Returns:
        List of (price, source) for each level including E1.
    """
    results = [(entry_price, "entry")]

    # Calculate fixed DCA prices
    fixed_dcas = []
    for pct in fixed_spacing_pcts[1:]:
        if side == "long":
            fixed_dcas.append(entry_price * (1 - pct / 100))
        else:
            fixed_dcas.append(entry_price * (1 + pct / 100))

    # No zones? Return fixed
    if zones is None or not zones.is_valid:
        for price in fixed_dcas:
            results.append((price, "fixed"))
        return results

    # Only use S1 (long) or R1 (short) - the dynamic primary reversal zone
    if side == "long":
        zone_price = zones.s1
        zone_label = "zone_s1"
    else:
        zone_price = zones.r1
        zone_label = "zone_r1"

    # No valid zone price? Return fixed
    if zone_price <= 0:
        for price in fixed_dcas:
            results.append((price, "fixed"))
        return results

    zone_used = False

    for i, fixed_price in enumerate(fixed_dcas):
        level_idx = i + 1  # 1-indexed (DCA1=1, DCA2=2, DCA3=3)

        # Filled levels don't consume the zone - skip them
        if filled_levels and level_idx < len(filled_levels) and filled_levels[level_idx]:
            results.append((fixed_price, "filled"))
            continue

        # Only ONE unfilled DCA gets the zone snap
        if not zone_used:
            # Zone-lock: only snap in favorable direction
            # Long: S1 must be <= fixed price (deeper = better entry)
            # Short: R1 must be >= fixed price (higher = better entry)
            can_snap = (
                (side == "long" and zone_price <= fixed_price) or
                (side == "short" and zone_price >= fixed_price)
            )
            if can_snap:
                zone_used = True
                dist_pct = abs(zone_price - fixed_price) / fixed_price * 100
                logger.info(
                    f"DCA{level_idx} zone-locked to {'S1' if side == 'long' else 'R1'}: "
                    f"{fixed_price:.4f} → {zone_price:.4f} ({dist_pct:.1f}% deeper)"
                )
                results.append((zone_price, zone_label))
                continue

        results.append((fixed_price, "fixed"))

    return results


# ── Test ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    entry = 113.14
    spacing = [0, 5, 11, 18]

    # === Scenario 1: Fresh trade, S1 near DCA1 ===
    print("=== Scenario 1: Fresh trade, S1 near DCA1 ===")
    zones = CoinZones(
        symbol="AAVEUSDT",
        s1=106.50, s2=103.00, s3=99.00,
        r1=115.80, r2=118.50, r3=121.00,
        updated_at=time.time(), source="luxalgo",
    )
    print(f"Entry: {entry} | S1={zones.s1} (dynamic)")
    print(f"Fixed DCA1={entry*0.95:.2f}, DCA2={entry*0.89:.2f}, DCA3={entry*0.82:.2f}")

    levels = calc_smart_dca_levels(entry, spacing, zones, "long")
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← S1 SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.2f} ({source}){marker}")

    # === Scenario 2: DCA1 filled, S1 moved deeper ===
    print("\n=== Scenario 2: DCA1 filled, S1 moved deeper ===")
    zones.s1 = 99.80  # S1 shifted down as price dropped
    filled = [False, True, False, False]  # E1=unfilled(ignored), DCA1=filled
    print(f"S1 moved to {zones.s1} | DCA1 filled")

    levels = calc_smart_dca_levels(entry, spacing, zones, "long", filled_levels=filled)
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← S1 SNAP" if "zone" in source else ""
        status = " (FILLED)" if source == "filled" else ""
        print(f"  {label}: {price:.2f} ({source}){marker}{status}")

    # === Scenario 3: DCA1+2 filled, S1 even deeper ===
    print("\n=== Scenario 3: DCA1+2 filled, S1 even deeper ===")
    zones.s1 = 91.50  # S1 shifted way down during crash
    filled = [False, True, True, False]  # DCA1+2 filled
    print(f"S1 moved to {zones.s1} | DCA1+2 filled")

    levels = calc_smart_dca_levels(entry, spacing, zones, "long", filled_levels=filled)
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← S1 SNAP" if "zone" in source else ""
        status = " (FILLED)" if source == "filled" else ""
        print(f"  {label}: {price:.2f} ({source}){marker}{status}")

    # Test auto swing calculation
    print("\n--- Auto Swing Zones ---")
    import math
    fake_candles = []
    for i in range(100):
        base = 100 + 5 * math.sin(i / 10) + 0.05 * i
        fake_candles.append({
            "open": base - 0.5,
            "high": base + 1,
            "low": base - 1,
            "close": base + 0.5,
        })

    auto_zones = calc_swing_zones(fake_candles)
    if auto_zones:
        print(f"  S: {auto_zones.s1:.2f} / {auto_zones.s2:.2f} / {auto_zones.s3:.2f}")
        print(f"  R: {auto_zones.r1:.2f} / {auto_zones.r2:.2f} / {auto_zones.r3:.2f}")
