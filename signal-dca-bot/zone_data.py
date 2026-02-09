"""
Zone Data Manager - Fetches reversal zone levels from Supabase.

Supabase table schema: "coin_zones"
┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬─────────────┐
│ symbol   │ s1       │ s2       │ s3       │ r1       │ r2       │ r3       │ updated_at  │
├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼─────────────┤
│ BTCUSDT  │ 94500.00 │ 93200.00 │ 91800.00 │ 97500.00 │ 98800.00 │ 100200.0 │ 2026-02-09  │
│ AAVEUSDT │ 111.50   │ 108.20   │ 105.00   │ 115.80   │ 118.50   │ 121.00   │ 2026-02-09  │
└──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴─────────────┘

Zone levels can be updated via:
1. TradingView webhook (Pine Script pushes zone values every 15min)
2. Manual input via /zones API endpoint
3. Supabase dashboard directly

The bot reads these zones and snaps DCA levels to the nearest zone
if the zone is better (closer to entry = fills sooner = bounce more likely).
"""

import logging
import time
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CoinZones:
    """Reversal zone levels for a coin."""
    symbol: str
    s1: float = 0  # Inner support (first long zone)
    s2: float = 0  # Middle support
    s3: float = 0  # Outer support (deep long zone)
    r1: float = 0  # Inner resistance (first short zone)
    r2: float = 0  # Middle resistance
    r3: float = 0  # Outer resistance (deep short zone)
    updated_at: float = 0  # Unix timestamp

    @property
    def is_valid(self) -> bool:
        """Check if zones are set and recent (< 1 hour)."""
        if self.s1 == 0 and self.r1 == 0:
            return False
        age_min = (time.time() - self.updated_at) / 60
        return age_min < 60  # Valid for 1 hour

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.updated_at) / 60

    def long_zones(self) -> list[float]:
        """Support zones for long DCA (descending order)."""
        zones = [z for z in [self.s1, self.s2, self.s3] if z > 0]
        return sorted(zones, reverse=True)  # Highest first

    def short_zones(self) -> list[float]:
        """Resistance zones for short DCA (ascending order)."""
        zones = [z for z in [self.r1, self.r2, self.r3] if z > 0]
        return sorted(zones)  # Lowest first


class ZoneDataManager:
    """Manages zone data from Supabase + local cache."""

    def __init__(self):
        self._supabase = None
        self._cache: dict[str, CoinZones] = {}  # symbol → CoinZones
        self._supabase_url = os.getenv("SUPABASE_URL", "")
        self._supabase_key = os.getenv("SUPABASE_KEY", "")
        self._table_name = "coin_zones"

    @property
    def supabase(self):
        if self._supabase is None and self._supabase_url:
            try:
                from supabase import create_client
                self._supabase = create_client(self._supabase_url, self._supabase_key)
                logger.info("Supabase connected")
            except ImportError:
                logger.warning("supabase-py not installed, using local cache only")
            except Exception as e:
                logger.error(f"Supabase connection failed: {e}")
        return self._supabase

    def get_zones(self, symbol: str) -> Optional[CoinZones]:
        """Get zone data for a symbol. Checks cache first, then Supabase."""
        # Check cache
        if symbol in self._cache and self._cache[symbol].is_valid:
            return self._cache[symbol]

        # Try Supabase
        if self.supabase:
            try:
                result = (
                    self.supabase.table(self._table_name)
                    .select("*")
                    .eq("symbol", symbol)
                    .limit(1)
                    .execute()
                )
                if result.data:
                    row = result.data[0]
                    zones = CoinZones(
                        symbol=symbol,
                        s1=float(row.get("s1", 0) or 0),
                        s2=float(row.get("s2", 0) or 0),
                        s3=float(row.get("s3", 0) or 0),
                        r1=float(row.get("r1", 0) or 0),
                        r2=float(row.get("r2", 0) or 0),
                        r3=float(row.get("r3", 0) or 0),
                        updated_at=time.time(),  # Use current time as fetch time
                    )
                    self._cache[symbol] = zones
                    logger.info(
                        f"Zones loaded: {symbol} | "
                        f"S: {zones.s1}/{zones.s2}/{zones.s3} | "
                        f"R: {zones.r1}/{zones.r2}/{zones.r3}"
                    )
                    return zones
            except Exception as e:
                logger.error(f"Supabase query failed for {symbol}: {e}")

        # Return cached even if stale, or None
        return self._cache.get(symbol)

    def update_zones(self, symbol: str, zones: CoinZones) -> bool:
        """Update zone data in cache and Supabase."""
        zones.updated_at = time.time()
        self._cache[symbol] = zones

        if self.supabase:
            try:
                data = {
                    "symbol": symbol,
                    "s1": zones.s1,
                    "s2": zones.s2,
                    "s3": zones.s3,
                    "r1": zones.r1,
                    "r2": zones.r2,
                    "r3": zones.r3,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                self.supabase.table(self._table_name).upsert(data).execute()
                logger.info(f"Zones saved to Supabase: {symbol}")
                return True
            except Exception as e:
                logger.error(f"Supabase upsert failed for {symbol}: {e}")
                return False

        return True  # Cache-only mode

    def refresh_all_active(self, symbols: list[str]) -> None:
        """Refresh zones for all active trade symbols."""
        for symbol in symbols:
            self.get_zones(symbol)


def calc_smart_dca_levels(
    entry_price: float,
    fixed_spacing_pcts: list[float],
    zones: CoinZones | None,
    side: str,
    snap_threshold_pct: float = 2.0,
) -> list[tuple[float, str]]:
    """Calculate DCA levels with simple zone-snapping.

    Simple rule: For each fixed DCA level, check if a zone is within
    snap_threshold_pct (default 2%). If yes, use the zone instead.
    Each zone can only be used ONCE (closest DCA gets it).

    Args:
        entry_price: E1 entry price
        fixed_spacing_pcts: Fixed DCA spacing [0, 5, 10, 16, 23, 30]
        zones: Zone data (or None)
        side: "long" or "short"
        snap_threshold_pct: Max % distance from fixed DCA to snap (default 2%)

    Returns:
        List of (price, source) for each level including E1.
    """
    results = [(entry_price, "entry")]  # E1 is always the signal price

    # Calculate fixed DCA prices
    fixed_dcas = []
    for pct in fixed_spacing_pcts[1:]:  # Skip index 0 (E1)
        if side == "long":
            fixed_dcas.append(entry_price * (1 - pct / 100))
        else:
            fixed_dcas.append(entry_price * (1 + pct / 100))

    # No zones? Return fixed
    if zones is None or not zones.is_valid:
        for price in fixed_dcas:
            results.append((price, "fixed"))
        return results

    # Get available zones for this side
    if side == "long":
        available_zones = [
            (zones.s1, "zone_s1"),
            (zones.s2, "zone_s2"),
            (zones.s3, "zone_s3"),
        ]
        # Filter: zones must be below entry
        available_zones = [(p, n) for p, n in available_zones if 0 < p < entry_price]
    else:
        available_zones = [
            (zones.r1, "zone_r1"),
            (zones.r2, "zone_r2"),
            (zones.r3, "zone_r3"),
        ]
        # Filter: zones must be above entry
        available_zones = [(p, n) for p, n in available_zones if p > entry_price]

    # Simple snap: for each DCA, find closest zone within threshold
    used_zones: set[str] = set()

    for fixed_price in fixed_dcas:
        best_zone = None
        best_dist = float("inf")

        for zone_price, zone_name in available_zones:
            if zone_name in used_zones:
                continue

            # Simple % distance between zone and fixed DCA price
            dist_pct = abs(zone_price - fixed_price) / fixed_price * 100

            if dist_pct <= snap_threshold_pct and dist_pct < best_dist:
                best_dist = dist_pct
                best_zone = (zone_price, zone_name)

        if best_zone:
            used_zones.add(best_zone[1])
            results.append(best_zone)
            logger.info(
                f"DCA snapped: {fixed_price:.4f} → {best_zone[0]:.4f} "
                f"({best_zone[1]}, {best_dist:.1f}% diff)"
            )
        else:
            results.append((fixed_price, "fixed"))

    return results


# ── Test ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Example: AAVE Long @ 113.14
    zones = CoinZones(
        symbol="AAVEUSDT",
        s1=111.50,  # -1.45% from entry
        s2=108.20,  # -4.36%
        s3=105.00,  # -7.19%
        r1=115.80,
        r2=118.50,
        r3=121.00,
        updated_at=time.time(),
    )

    entry = 113.14
    spacing = [0, 5, 10, 16, 23, 30]

    print(f"Entry: {entry}")
    print(f"Zones: S1={zones.s1} S2={zones.s2} S3={zones.s3}")
    print()

    levels = calc_smart_dca_levels(entry, spacing, zones, "long")
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        fixed = entry * (1 - spacing[i] / 100) if i > 0 else entry
        marker = " ← ZONE SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.2f} ({source}) [fixed would be {fixed:.2f}]{marker}")

    print()
    print("Without zones:")
    levels_no_zone = calc_smart_dca_levels(entry, spacing, None, "long")
    for i, (price, source) in enumerate(levels_no_zone):
        label = "E1" if i == 0 else f"DCA{i}"
        print(f"  {label}: {price:.2f} ({source})")

    print()
    print("Short example: ONDO @ 0.2592")
    short_zones = CoinZones(
        symbol="ONDOUSDT",
        s1=0.2450, s2=0.2380, s3=0.2300,
        r1=0.2650, r2=0.2720, r3=0.2800,
        updated_at=time.time(),
    )
    levels_short = calc_smart_dca_levels(0.2592, [0, 5, 10, 16, 23, 30], short_zones, "short")
    for i, (price, source) in enumerate(levels_short):
        label = "E1" if i == 0 else f"DCA{i}"
        marker = " ← ZONE SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.4f} ({source}){marker}")
