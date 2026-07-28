-- Initial schema.
--
-- Time is stored as ISO-8601 UTC text ("2026-07-28T13:30:00+00:00"). SQLite
-- compares these lexicographically in the same order as chronologically, so
-- range scans on timestamp_utc work without a conversion function.
--
-- bars_1m.timestamp_utc is the START of the interval (spec-review B-2).

CREATE TABLE IF NOT EXISTS symbols (
    symbol        TEXT PRIMARY KEY,
    name          TEXT,
    exchange      TEXT,
    asset_type    TEXT,
    is_watched    INTEGER NOT NULL DEFAULT 0,
    is_held       INTEGER NOT NULL DEFAULT 0,
    -- NULL until the collector has proven the provider serves this symbol
    -- (spec 3.1 requires surfacing unsupported symbols; spec-review D-6).
    supported     INTEGER,
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_symbols_watched
    ON symbols (is_watched, is_held);

CREATE TABLE IF NOT EXISTS bars_1m (
    symbol        TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,      -- interval start, ISO-8601 UTC
    session       TEXT NOT NULL,      -- pre | regular | post | closed
    open          REAL NOT NULL,
    high          REAL NOT NULL,
    low           REAL NOT NULL,
    close         REAL NOT NULL,
    volume        INTEGER NOT NULL DEFAULT 0,
    vwap          REAL,
    trade_count   INTEGER,
    source        TEXT NOT NULL,
    is_final      INTEGER NOT NULL DEFAULT 0,
    received_at   TEXT NOT NULL,
    PRIMARY KEY (symbol, timestamp_utc, source)
) WITHOUT ROWID;

-- Serves the chart query: one symbol, a time range, newest sources resolved
-- in the application layer by configured priority (spec-review B-1).
CREATE INDEX IF NOT EXISTS idx_bars_symbol_time
    ON bars_1m (symbol, timestamp_utc DESC);

-- Optional second-level snapshots. Empty unless tick_retention_days > 0
-- (spec 10.2 listed three options without choosing; default is "do not store").
CREATE TABLE IF NOT EXISTS ticks (
    symbol        TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    price         REAL NOT NULL,
    size          INTEGER,
    source        TEXT NOT NULL,
    PRIMARY KEY (symbol, timestamp_utc, source)
) WITHOUT ROWID;

-- Persistent REST budget accounting. Survives restarts on purpose: a crash
-- loop must not reset the hourly allowance (spec-review A-2).
CREATE TABLE IF NOT EXISTS api_usage (
    source        TEXT NOT NULL,
    window_kind   TEXT NOT NULL,      -- hour | day | month
    window_start  TEXT NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, window_kind, window_start)
);

-- Last successfully stored bar per (symbol, source); drives gap detection.
CREATE TABLE IF NOT EXISTS collector_state (
    symbol            TEXT NOT NULL,
    source            TEXT NOT NULL,
    last_bar_utc      TEXT,
    last_trade_utc    TEXT,
    last_backfill_utc TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (symbol, source)
);

-- Unscheduled market closures / early closes that cannot be derived
-- (spec-review B-4).
CREATE TABLE IF NOT EXISTS market_calendar_overrides (
    day       TEXT PRIMARY KEY,       -- YYYY-MM-DD, Eastern date
    kind      TEXT NOT NULL,          -- closed | early_close
    reason    TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
