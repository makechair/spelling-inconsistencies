-- When a symbol was last looked at.
--
-- The provider's free tiers stopped delivering websocket data (spec-review
-- A-6), leaving REST polling as the only live path at 50 calls an hour. Spread
-- evenly across ten symbols that is one refresh every twelve minutes; spent on
-- the one symbol actually on screen it is one every seventy-two seconds.
--
-- Deferring the others costs no data. The REST endpoint returns every minute
-- between the last stored bar and now, so a symbol left alone for hours is
-- filled completely by a single call when it is next opened -- late, not
-- missing. What it does cost is freshness for a chart nobody is reading.
--
-- Written by the API when bars or a live stream are requested, read by the
-- collector, which already polls this table every few seconds for subscription
-- changes. Reusing that channel avoids adding an IPC path between the two
-- processes (spec-review A-3).
ALTER TABLE symbols ADD COLUMN last_viewed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_symbols_last_viewed
    ON symbols (last_viewed_at);
