"""
Zone Data Manager v2

Primary: LuxAlgo zones from TradingView → Supabase
Fallback: Auto-calculated swing H/L from Bybit candles

Supabase table: "coin_zones"
  symbol (PK), s1, s2, s3, r1, r2, r3, updated_at

Zone-snapping: For each fixed DCA level, if a zone is within 2%,
snap the DCA to the zone price (better bounce probability).
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
    """Manages zone data from Supabase + auto-calc fallback."""

    def __init__(self, supabase_url: str = "", supabase_key: str = ""):
        self._supabase = None
        self._cache: dict[str, CoinZones] = {}
        self._supabase_url = supabase_url or os.getenv("SUPABASE_URL", "")
        self._supabase_key = supabase_key or os.getenv("SUPABASE_KEY", "")
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
        """Get zone data. Checks cache first, then Supabase."""
        if symbol in self._cache and self._cache[symbol].is_valid:
            return self._cache[symbol]

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
                        updated_at=time.time(),
                        source=row.get("source", "luxalgo"),
                    )
                    self._cache[symbol] = zones
                    logger.info(
                        f"Zones loaded ({zones.source}): {symbol} | "
                        f"S: {zones.s1}/{zones.s2}/{zones.s3} | "
                        f"R: {zones.r1}/{zones.r2}/{zones.r3}"
                    )
                    return zones
            except Exception as e:
                logger.error(f"Supabase query failed for {symbol}: {e}")

        return self._cache.get(symbol)

    def update_zones(self, symbol: str, zones: CoinZones) -> bool:
        """Update zone data in cache and Supabase."""
        zones.updated_at = time.time()
        self._cache[symbol] = zones

        if self.supabase:
            try:
                data = {
                    "symbol": symbol,
                    "s1": zones.s1, "s2": zones.s2, "s3": zones.s3,
                    "r1": zones.r1, "r2": zones.r2, "r3": zones.r3,
                    "source": zones.source,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                self.supabase.table(self._table_name).upsert(data).execute()
                return True
            except Exception as e:
                logger.error(f"Supabase upsert failed for {symbol}: {e}")
                return False

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
# ▌ DCA ZONE SNAPPING
# ══════════════════════════════════════════════════════════════════════════

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
        fixed_spacing_pcts: Fixed DCA spacing [0, 5, 11, 18]
        zones: Zone data (or None)
        side: "long" or "short"
        snap_threshold_pct: Max % distance from fixed DCA to snap (default 2%)

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

    # Get available zones for this side
    if side == "long":
        available_zones = [
            (zones.s1, "zone_s1"),
            (zones.s2, "zone_s2"),
            (zones.s3, "zone_s3"),
        ]
        available_zones = [(p, n) for p, n in available_zones if 0 < p < entry_price]
    else:
        available_zones = [
            (zones.r1, "zone_r1"),
            (zones.r2, "zone_r2"),
            (zones.r3, "zone_r3"),
        ]
        available_zones = [(p, n) for p, n in available_zones if p > entry_price]

    # Simple snap: for each DCA, find closest zone within threshold
    used_zones: set[str] = set()

    for fixed_price in fixed_dcas:
        best_zone = None
        best_dist = float("inf")

        for zone_price, zone_name in available_zones:
            if zone_name in used_zones:
                continue

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

    # Test zone snapping with 3 DCAs
    zones = CoinZones(
        symbol="AAVEUSDT",
        s1=111.50, s2=108.20, s3=105.00,
        r1=115.80, r2=118.50, r3=121.00,
        updated_at=time.time(),
        source="luxalgo",
    )

    entry = 113.14
    spacing = [0, 5, 11, 18]

    print(f"Entry: {entry}")
    print(f"Zones: S1={zones.s1} S2={zones.s2} S3={zones.s3}")
    print(f"Config: 3 DCAs, spacing {spacing}")
    print()

    levels = calc_smart_dca_levels(entry, spacing, zones, "long")
    for i, (price, source) in enumerate(levels):
        label = "E1" if i == 0 else f"DCA{i}"
        fixed = entry * (1 - spacing[i] / 100) if i > 0 else entry
        marker = " ← ZONE SNAP" if "zone" in source else ""
        print(f"  {label}: {price:.2f} ({source}) [fixed: {fixed:.2f}]{marker}")

    # Test auto swing calculation
    print("\n--- Auto Swing Zones ---")
    fake_candles = []
    import math
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
