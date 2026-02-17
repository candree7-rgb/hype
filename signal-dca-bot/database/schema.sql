-- Signal DCA Bot v2 - PostgreSQL Schema
-- Railway PostgreSQL: auto-created on first startup via database.py
-- Manual setup: psql $DATABASE_URL -f database/schema.sql

-- ══════════════════════════════════════════════════════════════════════════
-- COIN ZONES: LuxAlgo reversal zones (400+ coins, updated every 15min)
-- ══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS coin_zones (
    symbol          VARCHAR(30) PRIMARY KEY,
    s1              DECIMAL(20, 8) DEFAULT 0,   -- Inner support (nearest price)
    s2              DECIMAL(20, 8) DEFAULT 0,   -- Middle support
    s3              DECIMAL(20, 8) DEFAULT 0,   -- Outer support (deepest)
    r1              DECIMAL(20, 8) DEFAULT 0,   -- Inner resistance (nearest price)
    r2              DECIMAL(20, 8) DEFAULT 0,   -- Middle resistance
    r3              DECIMAL(20, 8) DEFAULT 0,   -- Outer resistance (highest)
    source          VARCHAR(20) DEFAULT 'unknown',  -- luxalgo, swing, manual
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_zones_updated ON coin_zones(updated_at);
CREATE INDEX IF NOT EXISTS idx_zones_source ON coin_zones(source);


-- ══════════════════════════════════════════════════════════════════════════
-- TRADES: Every closed trade with full P&L and DCA details
-- ══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trades (
    trade_id            VARCHAR(100) PRIMARY KEY,
    symbol              VARCHAR(30) NOT NULL,
    side                VARCHAR(10) NOT NULL,       -- 'long' or 'short'

    -- Pricing
    entry_price         DECIMAL(20, 8),             -- Signal entry price
    avg_price           DECIMAL(20, 8),             -- Weighted avg after DCAs
    close_price         DECIMAL(20, 8),

    -- Position
    total_qty           DECIMAL(20, 8),
    total_margin        DECIMAL(20, 8),
    leverage            INTEGER DEFAULT 20,

    -- P&L
    realized_pnl        DECIMAL(20, 8) DEFAULT 0,
    pnl_pct_margin      DECIMAL(10, 4),             -- PnL % of margin used
    pnl_pct_equity      DECIMAL(10, 6),             -- PnL % of equity
    equity_at_entry     DECIMAL(12, 2),
    equity_at_close     DECIMAL(12, 2),
    is_win              BOOLEAN,

    -- DCA / Exit details
    max_dca_reached     INTEGER DEFAULT 0,          -- 0 = E1 only, 3 = all DCAs
    tp1_hit             BOOLEAN DEFAULT FALSE,
    tps_hit             INTEGER DEFAULT 0,          -- Total TPs filled (0-4)
    trail_pnl_pct       DECIMAL(10, 4) DEFAULT 0,   -- Trail price-% from avg (universal, ignores size/lev)
    close_reason        VARCHAR(200),               -- 'TP1+trail', 'BE-trail', 'Hard SL', etc.
    signal_leverage     INTEGER DEFAULT 0,          -- Original signal leverage

    -- Config at time of trade
    equity_pct_per_trade DECIMAL(5, 2) DEFAULT 5.0, -- Bot's EQUITY_PCT when trade was opened

    -- Zone snapping
    zone_source         VARCHAR(20),                -- luxalgo, swing, fixed
    zones_used          INTEGER DEFAULT 0,          -- How many DCAs snapped to zones

    -- Timing
    opened_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    duration_minutes    INTEGER,

    -- Metadata
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_is_win ON trades(is_win);
CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side);


-- ══════════════════════════════════════════════════════════════════════════
-- DAILY EQUITY: Snapshot for equity chart in dashboard
-- ══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS daily_equity (
    date            DATE PRIMARY KEY,
    equity          DECIMAL(20, 8) NOT NULL,
    daily_pnl       DECIMAL(20, 8),
    daily_pnl_pct   DECIMAL(10, 4),
    trades_count    INTEGER DEFAULT 0,
    wins_count      INTEGER DEFAULT 0,
    losses_count    INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_equity_date ON daily_equity(date);


-- ══════════════════════════════════════════════════════════════════════════
-- NEO CLOUD TRENDS: Track lead/lag direction per symbol for switch detection
-- ══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS neo_cloud_trends (
    symbol          VARCHAR(30) PRIMARY KEY,
    direction       VARCHAR(10) NOT NULL,       -- 'up' or 'down'
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ══════════════════════════════════════════════════════════════════════════
-- ACTIVE TRADES: Live trade state for crash recovery (survives redeploy)
-- ══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS active_trades (
    trade_id        VARCHAR(100) PRIMARY KEY,
    symbol          VARCHAR(30) NOT NULL,
    side            VARCHAR(10) NOT NULL,
    status          VARCHAR(20) NOT NULL,
    state_json      JSONB NOT NULL,             -- Full Trade object serialized
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_active_trades_symbol ON active_trades(symbol);


-- ══════════════════════════════════════════════════════════════════════════
-- AUTO-UPDATE TRIGGER
-- ══════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Only create trigger if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_trades_updated_at') THEN
        CREATE TRIGGER update_trades_updated_at
            BEFORE UPDATE ON trades
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    END IF;
END
$$;


-- ══════════════════════════════════════════════════════════════════════════
-- MIGRATIONS: Safe ADD COLUMN (idempotent, skips if exists)
-- ══════════════════════════════════════════════════════════════════════════

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='tps_hit') THEN
        ALTER TABLE trades ADD COLUMN tps_hit INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='trail_pnl_pct') THEN
        ALTER TABLE trades ADD COLUMN trail_pnl_pct DECIMAL(10,4) DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trades' AND column_name='equity_pct_per_trade') THEN
        ALTER TABLE trades ADD COLUMN equity_pct_per_trade DECIMAL(5,2) DEFAULT 5.0;
    END IF;
END
$$;
