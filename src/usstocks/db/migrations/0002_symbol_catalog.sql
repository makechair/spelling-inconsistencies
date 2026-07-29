-- Local copy of the provider's ticker universe.
--
-- Symbol search used to call the provider on every distinct query, spending
-- the same 50 calls/hour the collector needs for backfill (spec-review A-2).
-- Autocomplete could consume the hour's allowance on its own.
--
-- This is not a second data source. It is Tiingo's own supported_tickers file,
-- published as a static download rather than an API endpoint, so importing it
-- costs no requests and cannot disagree with what the price endpoints serve --
-- the failure mode of bolting on a different provider's ticker list.
--
-- Kept apart from `symbols`, which holds the watchlist and its per-symbol state
-- (spec 3.1). This table is disposable: it is rebuilt from the download, and
-- losing it degrades search rather than losing anything the user entered.
CREATE TABLE IF NOT EXISTS symbol_catalog (
    symbol         TEXT PRIMARY KEY,
    name           TEXT,
    exchange       TEXT,
    asset_type     TEXT,
    price_currency TEXT,
    -- Coverage bounds from the provider. end_date is what tells a live listing
    -- apart from one that stopped trading years ago; the file keeps both.
    start_date     TEXT,
    end_date       TEXT,
    -- Stamped per import so a run that dies partway leaves the previous rows
    -- searchable instead of an empty table.
    refreshed_at   TEXT NOT NULL
);

-- Prefix search on the ticker. Company-name matching scans, which is
-- acceptable at this size and avoids carrying an FTS index on a 1 GB host.
CREATE INDEX IF NOT EXISTS idx_symbol_catalog_name
    ON symbol_catalog (name);

CREATE INDEX IF NOT EXISTS idx_symbol_catalog_refreshed
    ON symbol_catalog (refreshed_at);
