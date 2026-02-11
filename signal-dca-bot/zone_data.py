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
    snap_min_pct: float = 2.0,
    filled_levels: list[bool] | None = None,
) -> list[tuple[float, str]]:
    """Calculate DCA levels with hybrid S1/R1 zone snapping.

    HYBRID MODE: Zone has priority over fixed spacing, but with minimum
    distance. This allows DCA to snap CLOSER than fixed when zone says so.

    Examples (Long, entry=$100, fixed=5%, min=2%):
      S1 at $93 (7%) → snap to $93 (deeper than fixed ✓)
      S1 at $97 (3%) → snap to $97 (closer than fixed, but > 2% min ✓)
      S1 at $99 (1%) → use fixed $95 (too close, < 2% min ✗)
      No zone        → use fixed $95 (fallback)

    Only ONE unfilled DCA gets the zone snap per calculation.
    Filled DCAs don't consume the zone.

    Args:
        entry_price: E1 entry price
        fixed_spacing_pcts: Fixed DCA spacing [0, 5]
        zones: Zone data (or None)
        side: "long" or "short"
        snap_min_pct: Minimum distance from entry for zone snap (hybrid mode)
        filled_levels: Boolean list [E1, DCA1] - True if filled.

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
        level_idx = i + 1  # 1-indexed (DCA1=1)

        # Filled levels don't consume the zone - skip them
        if filled_levels and level_idx < len(filled_levels) and filled_levels[level_idx]:
            results.append((fixed_price, "filled"))
            continue

        # Only ONE unfilled DCA gets the zone snap
        if not zone_used:
            # Calculate zone distance from entry
            zone_dist_pct = abs(zone_price - entry_price) / entry_price * 100

            # HYBRID: Zone has priority if it meets minimum distance
            # Long: S1 must be below entry by at least snap_min_pct
            # Short: R1 must be above entry by at least snap_min_pct
            zone_far_enough = zone_dist_pct >= snap_min_pct

            # Zone must be in favorable direction from entry
            zone_favorable = (
                (side == "long" and zone_price < entry_price) or
                (side == "short" and zone_price > entry_price)
            )

            if zone_far_enough and zone_favorable:
                zone_used = True
                fixed_dist_pct = abs(fixed_price - entry_price) / entry_price * 100
                direction = "deeper" if zone_dist_pct > fixed_dist_pct else "closer"
                logger.info(
                    f"DCA{level_idx} zone-snapped to {'S1' if side == 'long' else 'R1'}: "
                    f"{fixed_price:.4f} → {zone_price:.4f} "
                    f"({zone_dist_pct:.1f}% from entry, {direction} than fixed {fixed_dist_pct:.1f}%)"
                )
                results.append((zone_price, zone_label))
                continue

        results.append((fixed_price, "fixed"))

    return results


# ── Test ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    entry = 113.14
    spacing = [0, 5]  # 1 DCA at entry-5%

    # === Scenario 1: S1 at 3% (hybrid: closer than fixed, > 2% min) ===
    print("=== Scenario 1: S1 at 3% from entry (hybrid snap closer) ===")
    zones = CoinZones(
        symbol="AAVEUSDT",
        s1=109.75, s2=103.00, s3=99.00,  # S1 at ~3% below entry
        r1=115.80, r2=118.50, r3=121.00,
        updated_at=time.time(), source="luxalgo",
    )
    print(f"Entry: {entry} | S1={zones.s1} ({(entry-zones.s1)/entry*100:.1f}% from entry)")
    print(f"Fixed DCA1={entry*0.95:.2f} (5%)")

    levels = calc_smart_dca_levels(entry, spacing, zones, "long", snap_min_pct=2.0)
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← S1 SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.2f} ({source}){marker}")

    # === Scenario 2: S1 at 7% (deeper than fixed) ===
    print("\n=== Scenario 2: S1 at 7% from entry (deeper snap) ===")
    zones.s1 = 105.22  # ~7% below entry
    print(f"Entry: {entry} | S1={zones.s1} ({(entry-zones.s1)/entry*100:.1f}%)")

    levels = calc_smart_dca_levels(entry, spacing, zones, "long", snap_min_pct=2.0)
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← S1 SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.2f} ({source}){marker}")

    # === Scenario 3: S1 at 1% (too close, below minimum) ===
    print("\n=== Scenario 3: S1 at 1% from entry (too close, use fixed) ===")
    zones.s1 = 112.00  # ~1% below entry
    print(f"Entry: {entry} | S1={zones.s1} ({(entry-zones.s1)/entry*100:.1f}%)")

    levels = calc_smart_dca_levels(entry, spacing, zones, "long", snap_min_pct=2.0)
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← S1 SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.2f} ({source}){marker}")

    # === Scenario 4: Short with R1 at 4% ===
    print("\n=== Scenario 4: Short, R1 at 4% above entry ===")
    zones.r1 = 117.67  # ~4% above entry
    print(f"Entry: {entry} | R1={zones.r1} ({(zones.r1-entry)/entry*100:.1f}%)")

    levels = calc_smart_dca_levels(entry, spacing, zones, "short", snap_min_pct=2.0)
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← R1 SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.2f} ({source}){marker}")

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
