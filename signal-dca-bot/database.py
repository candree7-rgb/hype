"""
Database layer - Railway PostgreSQL for zone persistence + trade history.

Railway provides DATABASE_URL automatically when you add PostgreSQL.
Uses psycopg2 (sync) - fine for our low query volume.

Schema: database/schema.sql

Tables:
  coin_zones:    400+ coins × 6 zone prices, updated every 15min
  trades:        Every closed trade with full PnL, DCA, zone details
  daily_equity:  Daily equity snapshots for dashboard chart
"""

import logging
import os
import time
from datetime import datetime, date, timezone
from pathlib import Path
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
    """Initialize database from schema.sql."""
    conn = get_connection()
    if not conn:
        logger.warning("No DB connection - running in memory-only mode")
        return False

    schema_path = Path(__file__).parent / "database" / "schema.sql"
    if schema_path.exists():
        sql = schema_path.read_text()
        cur = conn.cursor()
        cur.execute(sql)
        cur.close()
        logger.info("DB initialized from database/schema.sql")
    else:
        logger.warning(f"schema.sql not found at {schema_path}, creating tables inline")
        _init_tables_inline(conn)

    return True


def _init_tables_inline(conn):
    """Fallback: create tables inline if schema.sql is missing."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coin_zones (
            symbol VARCHAR(30) PRIMARY KEY,
            s1 DECIMAL(20,8) DEFAULT 0, s2 DECIMAL(20,8) DEFAULT 0, s3 DECIMAL(20,8) DEFAULT 0,
            r1 DECIMAL(20,8) DEFAULT 0, r2 DECIMAL(20,8) DEFAULT 0, r3 DECIMAL(20,8) DEFAULT 0,
            source VARCHAR(20) DEFAULT 'unknown', updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            trade_id VARCHAR(100) PRIMARY KEY, symbol VARCHAR(30) NOT NULL, side VARCHAR(10) NOT NULL,
            entry_price DECIMAL(20,8), avg_price DECIMAL(20,8), close_price DECIMAL(20,8),
            total_qty DECIMAL(20,8), total_margin DECIMAL(20,8), leverage INTEGER DEFAULT 20,
            realized_pnl DECIMAL(20,8) DEFAULT 0, pnl_pct_margin DECIMAL(10,4),
            pnl_pct_equity DECIMAL(10,6), equity_at_entry DECIMAL(12,2), equity_at_close DECIMAL(12,2),
            is_win BOOLEAN, max_dca_reached INTEGER DEFAULT 0, tp1_hit BOOLEAN DEFAULT FALSE,
            close_reason VARCHAR(200), signal_leverage INTEGER DEFAULT 0,
            zone_source VARCHAR(20), zones_used INTEGER DEFAULT 0,
            opened_at TIMESTAMPTZ, closed_at TIMESTAMPTZ, duration_minutes INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_equity (
            date DATE PRIMARY KEY, equity DECIMAL(20,8) NOT NULL,
            daily_pnl DECIMAL(20,8), daily_pnl_pct DECIMAL(10,4),
            trades_count INTEGER DEFAULT 0, wins_count INTEGER DEFAULT 0,
            losses_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS neo_cloud_trends (
            symbol VARCHAR(30) PRIMARY KEY,
            direction VARCHAR(10) NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_trades (
            trade_id VARCHAR(100) PRIMARY KEY, symbol VARCHAR(30) NOT NULL,
            side VARCHAR(10) NOT NULL, status VARCHAR(20) NOT NULL,
            state_json JSONB NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.close()
    logger.info("DB tables created inline")


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
            "s1": float(row[0]), "s2": float(row[1]), "s3": float(row[2]),
            "r1": float(row[3]), "r2": float(row[4]), "r3": float(row[5]),
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
                "s1": float(r[1]), "s2": float(r[2]), "s3": float(r[3]),
                "r1": float(r[4]), "r2": float(r[5]), "r3": float(r[6]),
                "source": r[7],
                "updated_at": r[8].timestamp() if r[8] else 0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_all_zones failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════
# ▌ NEO CLOUD TRENDS
# ══════════════════════════════════════════════════════════════════════════

def upsert_neo_cloud(symbol: str, direction: str) -> bool:
    """Store Neo Cloud trend direction for a symbol.

    Args:
        symbol: e.g. "XRPUSDT"
        direction: "up" or "down"
    """
    conn = get_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO neo_cloud_trends (symbol, direction, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (symbol)
            DO UPDATE SET direction=%s, updated_at=NOW()
        """, (symbol, direction, direction))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB upsert_neo_cloud failed for {symbol}: {e}")
        return False


def get_neo_cloud(symbol: str) -> Optional[str]:
    """Get Neo Cloud trend direction for a symbol.

    Returns "up", "down", or None if no data.
    """
    conn = get_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT direction FROM neo_cloud_trends WHERE symbol = %s",
            (symbol,)
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"DB get_neo_cloud failed for {symbol}: {e}")
        return None


def get_all_neo_cloud() -> dict[str, str]:
    """Get all Neo Cloud trends. Returns {symbol: direction}."""
    conn = get_connection()
    if not conn:
        return {}

    try:
        cur = conn.cursor()
        cur.execute("SELECT symbol, direction FROM neo_cloud_trends")
        rows = cur.fetchall()
        cur.close()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.error(f"DB get_all_neo_cloud failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════
# ▌ TRADE HISTORY
# ══════════════════════════════════════════════════════════════════════════

def save_trade(trade_id: str, symbol: str, side: str, entry_price: float,
               avg_price: float, close_price: float, total_qty: float,
               total_margin: float, realized_pnl: float, max_dca: int,
               tp1_hit: bool, close_reason: str, opened_at: float,
               closed_at: float, signal_leverage: int,
               equity_at_entry: float = 0, equity_at_close: float = 0,
               leverage: int = 20) -> bool:
    """Save a closed trade to history."""
    conn = get_connection()
    if not conn:
        return False

    try:
        opened_dt = datetime.fromtimestamp(opened_at, tz=timezone.utc) if opened_at else None
        closed_dt = datetime.fromtimestamp(closed_at, tz=timezone.utc) if closed_at else None
        duration_min = int((closed_at - opened_at) / 60) if opened_at and closed_at else None

        # Calculate PnL percentages
        pnl_pct_margin = (realized_pnl / total_margin * 100) if total_margin > 0 else 0
        pnl_pct_equity = (realized_pnl / equity_at_entry * 100) if equity_at_entry > 0 else 0
        is_win = realized_pnl > 0.01

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades
                (trade_id, symbol, side, entry_price, avg_price, close_price,
                 total_qty, total_margin, leverage, realized_pnl,
                 pnl_pct_margin, pnl_pct_equity, equity_at_entry, equity_at_close,
                 is_win, max_dca_reached, tp1_hit, close_reason, signal_leverage,
                 opened_at, closed_at, duration_minutes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (trade_id) DO UPDATE SET
                realized_pnl=%s, close_price=%s, close_reason=%s, closed_at=%s,
                pnl_pct_margin=%s, pnl_pct_equity=%s, equity_at_close=%s, is_win=%s,
                duration_minutes=%s
        """, (trade_id, symbol, side, entry_price, avg_price, close_price,
              total_qty, total_margin, leverage, realized_pnl,
              pnl_pct_margin, pnl_pct_equity, equity_at_entry, equity_at_close,
              is_win, max_dca, tp1_hit, close_reason, signal_leverage,
              opened_dt, closed_dt, duration_min,
              # ON CONFLICT updates:
              realized_pnl, close_price, close_reason, closed_dt,
              pnl_pct_margin, pnl_pct_equity, equity_at_close, is_win,
              duration_min))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB save_trade failed for {trade_id}: {e}")
        return False


def get_trade_by_symbol_time(symbol: str, created_time: float) -> bool:
    """Check if a trade exists in DB for a symbol near a given time.

    Used by Bybit sync to avoid duplicate inserts.
    Matches within a 60-second window.
    """
    conn = get_connection()
    if not conn:
        return False

    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromtimestamp(created_time, tz=timezone.utc)
        dt_start = dt - timedelta(seconds=60)
        dt_end = dt + timedelta(seconds=60)

        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM trades WHERE symbol = %s AND opened_at BETWEEN %s AND %s LIMIT 1",
            (symbol, dt_start, dt_end)
        )
        row = cur.fetchone()
        cur.close()
        return row is not None
    except Exception as e:
        logger.error(f"DB get_trade_by_symbol_time failed: {e}")
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
                COUNT(*) FILTER (WHERE is_win = TRUE) as wins,
                COUNT(*) FILTER (WHERE is_win = FALSE) as losses,
                COUNT(*) FILTER (WHERE realized_pnl BETWEEN -0.01 AND 0.01) as breakeven,
                COALESCE(SUM(realized_pnl), 0) as total_pnl,
                COALESCE(AVG(realized_pnl), 0) as avg_pnl,
                MAX(realized_pnl) as best_trade,
                MIN(realized_pnl) as worst_trade,
                COALESCE(AVG(duration_minutes), 0) as avg_duration
            FROM trades
        """)
        row = cur.fetchone()
        cur.close()

        if not row or row[0] == 0:
            return {}

        total = row[0]
        return {
            "total": total,
            "wins": row[1],
            "losses": row[2],
            "breakeven": row[3],
            "total_pnl": round(float(row[4]), 2),
            "avg_pnl": round(float(row[5]), 2),
            "best_trade": round(float(row[6]), 2) if row[6] else 0,
            "worst_trade": round(float(row[7]), 2) if row[7] else 0,
            "win_rate": round(row[1] / total * 100, 1) if total > 0 else 0,
            "avg_duration_min": round(float(row[8])),
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
                   total_margin, realized_pnl, pnl_pct_margin, max_dca_reached,
                   tp1_hit, close_reason, opened_at, closed_at, duration_minutes,
                   is_win, leverage
            FROM trades
            ORDER BY closed_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()

        return [
            {
                "trade_id": r[0], "symbol": r[1], "side": r[2],
                "entry": float(r[3]) if r[3] else 0,
                "avg": float(r[4]) if r[4] else 0,
                "close": float(r[5]) if r[5] else 0,
                "margin": f"${float(r[6]):.2f}" if r[6] else "$0",
                "pnl": f"${float(r[7]):+.2f}" if r[7] else "$0",
                "pnl_pct": f"{float(r[8]):+.1f}%" if r[8] else "0%",
                "dca": r[9], "tp1": r[10], "reason": r[11],
                "duration": f"{r[14]}min" if r[14] else "?",
                "is_win": r[15], "leverage": r[16],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_recent_trades failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════
# ▌ DAILY EQUITY
# ══════════════════════════════════════════════════════════════════════════

def save_daily_equity(equity: float, daily_pnl: float = 0,
                      trades_count: int = 0, wins_count: int = 0,
                      losses_count: int = 0) -> bool:
    """Save today's equity snapshot."""
    conn = get_connection()
    if not conn:
        return False

    try:
        today = date.today()
        daily_pnl_pct = (daily_pnl / (equity - daily_pnl) * 100) if equity > daily_pnl and equity > 0 else 0

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO daily_equity (date, equity, daily_pnl, daily_pnl_pct,
                                      trades_count, wins_count, losses_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (date) DO UPDATE SET
                equity=%s, daily_pnl=%s, daily_pnl_pct=%s,
                trades_count=%s, wins_count=%s, losses_count=%s
        """, (today, equity, daily_pnl, daily_pnl_pct,
              trades_count, wins_count, losses_count,
              equity, daily_pnl, daily_pnl_pct,
              trades_count, wins_count, losses_count))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB save_daily_equity failed: {e}")
        return False


def get_equity_history(days: int = 90) -> list[dict]:
    """Get equity history for chart."""
    conn = get_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date, equity, daily_pnl, daily_pnl_pct,
                   trades_count, wins_count, losses_count
            FROM daily_equity
            ORDER BY date DESC
            LIMIT %s
        """, (days,))
        rows = cur.fetchall()
        cur.close()

        return [
            {
                "date": r[0].isoformat(),
                "equity": float(r[1]),
                "pnl": float(r[2]) if r[2] else 0,
                "pnl_pct": float(r[3]) if r[3] else 0,
                "trades": r[4], "wins": r[5], "losses": r[6],
            }
            for r in reversed(rows)  # oldest first for chart
        ]
    except Exception as e:
        logger.error(f"DB get_equity_history failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════
# ▌ ACTIVE TRADE PERSISTENCE (crash recovery)
# ══════════════════════════════════════════════════════════════════════════

def save_active_trade(trade_id: str, symbol: str, side: str,
                      status: str, state_json: dict) -> bool:
    """Persist active trade state to DB. Upserts on trade_id."""
    conn = get_connection()
    if not conn:
        return False

    try:
        import json
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO active_trades (trade_id, symbol, side, status, state_json, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (trade_id) DO UPDATE SET
                status=%s, state_json=%s, updated_at=NOW()
        """, (trade_id, symbol, side, status, json.dumps(state_json),
              status, json.dumps(state_json)))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB save_active_trade failed for {trade_id}: {e}")
        return False


def delete_active_trade(trade_id: str) -> bool:
    """Remove a trade from active_trades (trade closed)."""
    conn = get_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM active_trades WHERE trade_id = %s", (trade_id,))
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB delete_active_trade failed for {trade_id}: {e}")
        return False


def get_all_active_trades() -> list[dict]:
    """Load all active trades from DB (for startup recovery)."""
    conn = get_connection()
    if not conn:
        return []

    try:
        import json
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_id, symbol, side, status, state_json "
            "FROM active_trades ORDER BY created_at"
        )
        rows = cur.fetchall()
        cur.close()

        results = []
        for r in rows:
            state = r[4] if isinstance(r[4], dict) else json.loads(r[4])
            results.append({
                "trade_id": r[0],
                "symbol": r[1],
                "side": r[2],
                "status": r[3],
                "state": state,
            })
        return results
    except Exception as e:
        logger.error(f"DB get_all_active_trades failed: {e}")
        return []


def clear_all_active_trades() -> bool:
    """Clear all active trades (emergency reset)."""
    conn = get_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM active_trades")
        cur.close()
        logger.info("All active trades cleared from DB")
        return True
    except Exception as e:
        logger.error(f"DB clear_all_active_trades failed: {e}")
        return False


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

        z = get_zone("TESTUSDT")
        print(f"get_zone: {z}")

        all_z = get_all_zones()
        print(f"get_all_zones: {len(all_z)} rows")

        # Test trade save
        ok = save_trade(
            "TEST_123", "TESTUSDT", "long", 100.0, 99.5, 101.0,
            10.0, 50.0, 15.0, 1, True, "TP1+trail",
            time.time() - 3600, time.time(), 50,
            equity_at_entry=2400, equity_at_close=2415, leverage=20
        )
        print(f"save_trade: {ok}")

        stats = get_trade_stats()
        print(f"trade_stats: {stats}")

        trades = get_recent_trades(5)
        print(f"recent_trades: {len(trades)}")

        # Test daily equity
        ok = save_daily_equity(2415.0, 15.0, 3, 2, 1)
        print(f"save_daily_equity: {ok}")

        history = get_equity_history(30)
        print(f"equity_history: {len(history)} days")

        # Cleanup
        cur = conn.cursor()
        cur.execute("DELETE FROM coin_zones WHERE symbol = 'TESTUSDT'")
        cur.execute("DELETE FROM trades WHERE trade_id = 'TEST_123'")
        cur.execute("DELETE FROM daily_equity WHERE date = CURRENT_DATE")
        cur.close()
        print("Cleanup done")
    else:
        print("No DATABASE_URL - module works in no-op mode")
        print("This is expected for local dev. Set DATABASE_URL to enable persistence.")
