"""
Database layer - Railway PostgreSQL for zone persistence + trade history.

Railway provides DATABASE_URL automatically when you add PostgreSQL.
Uses psycopg2 (sync) - fine for our low query volume.

Tables:
  coin_zones:    400+ coins × 6 zone prices, updated every 15min
  trade_history: Every closed trade with PnL, DCA count, close reason
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Global connection
_conn = None


def get_connection():
    """Get or create PostgreSQL connection."""
    global _conn

    if _conn is not None:
        try:
            _conn.cursor().execute("SELECT 1")
            return _conn
        except Exception:
            _conn = None

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return None

    try:
        import psycopg2
        _conn = psycopg2.connect(db_url)
        _conn.autocommit = True
        logger.info("PostgreSQL connected")
        return _conn
    except ImportError:
        logger.warning("psycopg2 not installed, running without DB")
        return None
    except Exception as e:
        logger.error(f"PostgreSQL connection failed: {e}")
        return None


def init_tables():
    """Create tables if they don't exist."""
    conn = get_connection()
    if not conn:
        logger.warning("No DB connection - running in memory-only mode")
        return False

    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS coin_zones (
            symbol VARCHAR(30) PRIMARY KEY,
            s1 DOUBLE PRECISION DEFAULT 0,
            s2 DOUBLE PRECISION DEFAULT 0,
            s3 DOUBLE PRECISION DEFAULT 0,
            r1 DOUBLE PRECISION DEFAULT 0,
            r2 DOUBLE PRECISION DEFAULT 0,
            r3 DOUBLE PRECISION DEFAULT 0,
            source VARCHAR(20) DEFAULT 'unknown',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            trade_id VARCHAR(100) PRIMARY KEY,
            symbol VARCHAR(30) NOT NULL,
            side VARCHAR(10) NOT NULL,
            entry_price DOUBLE PRECISION,
            avg_price DOUBLE PRECISION,
            close_price DOUBLE PRECISION,
            total_qty DOUBLE PRECISION,
            total_margin DOUBLE PRECISION,
            realized_pnl DOUBLE PRECISION DEFAULT 0,
            max_dca_reached INTEGER DEFAULT 0,
            tp1_hit BOOLEAN DEFAULT FALSE,
            close_reason VARCHAR(200),
            opened_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            signal_leverage INTEGER DEFAULT 0
        )
    """)

    cur.close()
    logger.info("DB tables initialized (coin_zones + trade_history)")
    return True


# ══════════════════════════════════════════════════════════════════════════
# ▌ ZONE OPERATIONS
# ══════════════════════════════════════════════════════════════════════════

def upsert_zone(symbol: str, s1: float, s2: float, s3: float,
                r1: float, r2: float, r3: float, source: str) -> bool:
    """Insert or update zone data for a symbol."""
    conn = get_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO coin_zones (symbol, s1, s2, s3, r1, r2, r3, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol)
            DO UPDATE SET s1=%s, s2=%s, s3=%s, r1=%s, r2=%s, r3=%s,
                          source=%s, updated_at=NOW()
        """, (symbol, s1, s2, s3, r1, r2, r3, source,
              s1, s2, s3, r1, r2, r3, source))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB upsert_zone failed for {symbol}: {e}")
        return False


def get_zone(symbol: str) -> Optional[dict]:
    """Get zone data for a symbol. Returns None if not found."""
    conn = get_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT s1, s2, s3, r1, r2, r3, source, updated_at "
            "FROM coin_zones WHERE symbol = %s",
            (symbol,)
        )
        row = cur.fetchone()
        cur.close()

        if not row:
            return None

        return {
            "s1": row[0], "s2": row[1], "s3": row[2],
            "r1": row[3], "r2": row[4], "r3": row[5],
            "source": row[6],
            "updated_at": row[7].timestamp() if row[7] else 0,
        }
    except Exception as e:
        logger.error(f"DB get_zone failed for {symbol}: {e}")
        return None


def get_all_zones() -> list[dict]:
    """Get all zone data. Used for cache warmup on startup."""
    conn = get_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, s1, s2, s3, r1, r2, r3, source, updated_at "
            "FROM coin_zones ORDER BY updated_at DESC"
        )
        rows = cur.fetchall()
        cur.close()

        return [
            {
                "symbol": r[0],
                "s1": r[1], "s2": r[2], "s3": r[3],
                "r1": r[4], "r2": r[5], "r3": r[6],
                "source": r[7],
                "updated_at": r[8].timestamp() if r[8] else 0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_all_zones failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════
# ▌ TRADE HISTORY
# ══════════════════════════════════════════════════════════════════════════

def save_trade(trade_id: str, symbol: str, side: str, entry_price: float,
               avg_price: float, close_price: float, total_qty: float,
               total_margin: float, realized_pnl: float, max_dca: int,
               tp1_hit: bool, close_reason: str, opened_at: float,
               closed_at: float, signal_leverage: int) -> bool:
    """Save a closed trade to history."""
    conn = get_connection()
    if not conn:
        return False

    try:
        opened_dt = datetime.fromtimestamp(opened_at, tz=timezone.utc) if opened_at else None
        closed_dt = datetime.fromtimestamp(closed_at, tz=timezone.utc) if closed_at else None

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trade_history
                (trade_id, symbol, side, entry_price, avg_price, close_price,
                 total_qty, total_margin, realized_pnl, max_dca_reached,
                 tp1_hit, close_reason, opened_at, closed_at, signal_leverage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (trade_id) DO UPDATE SET
                realized_pnl=%s, close_price=%s, close_reason=%s, closed_at=%s
        """, (trade_id, symbol, side, entry_price, avg_price, close_price,
              total_qty, total_margin, realized_pnl, max_dca,
              tp1_hit, close_reason, opened_dt, closed_dt, signal_leverage,
              realized_pnl, close_price, close_reason, closed_dt))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB save_trade failed for {trade_id}: {e}")
        return False


def get_trade_stats() -> dict:
    """Get aggregate trade stats from history."""
    conn = get_connection()
    if not conn:
        return {}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE realized_pnl > 0.01) as wins,
                COUNT(*) FILTER (WHERE realized_pnl < -0.01) as losses,
                COUNT(*) FILTER (WHERE realized_pnl BETWEEN -0.01 AND 0.01) as breakeven,
                COALESCE(SUM(realized_pnl), 0) as total_pnl,
                COALESCE(AVG(realized_pnl), 0) as avg_pnl,
                MAX(realized_pnl) as best_trade,
                MIN(realized_pnl) as worst_trade
            FROM trade_history
        """)
        row = cur.fetchone()
        cur.close()

        if not row:
            return {}

        total = row[0]
        return {
            "total": total,
            "wins": row[1],
            "losses": row[2],
            "breakeven": row[3],
            "total_pnl": round(row[4], 2),
            "avg_pnl": round(row[5], 2),
            "best_trade": round(row[6], 2) if row[6] else 0,
            "worst_trade": round(row[7], 2) if row[7] else 0,
            "win_rate": round(row[1] / total * 100, 1) if total > 0 else 0,
        }
    except Exception as e:
        logger.error(f"DB get_trade_stats failed: {e}")
        return {}


def get_recent_trades(limit: int = 20) -> list[dict]:
    """Get recent trade history."""
    conn = get_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_id, symbol, side, entry_price, avg_price, close_price,
                   total_margin, realized_pnl, max_dca_reached, tp1_hit,
                   close_reason, opened_at, closed_at
            FROM trade_history
            ORDER BY closed_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()

        return [
            {
                "trade_id": r[0], "symbol": r[1], "side": r[2],
                "entry": r[3], "avg": r[4], "close": r[5],
                "margin": f"${r[6]:.2f}", "pnl": f"${r[7]:+.2f}",
                "dca": r[8], "tp1": r[9], "reason": r[10],
                "duration": f"{(r[12] - r[11]).total_seconds() / 3600:.1f}h" if r[11] and r[12] else "?",
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_recent_trades failed: {e}")
        return []


# ── Test ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("Testing database module...")
    print(f"DATABASE_URL set: {bool(os.getenv('DATABASE_URL'))}")

    conn = get_connection()
    if conn:
        init_tables()

        # Test zone upsert
        ok = upsert_zone("TESTUSDT", 100, 98, 95, 105, 108, 112, "test")
        print(f"upsert_zone: {ok}")

        # Test zone get
        z = get_zone("TESTUSDT")
        print(f"get_zone: {z}")

        # Test get all
        all_z = get_all_zones()
        print(f"get_all_zones: {len(all_z)} rows")

        # Test trade save
        ok = save_trade(
            "TEST_123", "TESTUSDT", "long", 100.0, 99.5, 101.0,
            10.0, 50.0, 15.0, 1, True, "TP1+trail",
            time.time() - 3600, time.time(), 50
        )
        print(f"save_trade: {ok}")

        # Test stats
        stats = get_trade_stats()
        print(f"trade_stats: {stats}")

        # Cleanup
        cur = conn.cursor()
        cur.execute("DELETE FROM coin_zones WHERE symbol = 'TESTUSDT'")
        cur.execute("DELETE FROM trade_history WHERE trade_id = 'TEST_123'")
        cur.close()
        print("Cleanup done")
    else:
        print("No DATABASE_URL - module works in no-op mode (all functions return None/False/[])")
        print("This is expected for local dev. Set DATABASE_URL to enable persistence.")
