-- Turn archived XBRL facts into comparable per-period metrics.
--
-- Three filters come first, and each of them exists because production data
-- showed what happens without it (docs/earnings-spec.md 9).

-- Periodic reports only. 6-K has to be in: it is where foreign private
-- issuers put their quarters, and ARM had 336 of 444 rows there. 8-K
-- pre-announces figures the 10-Q repeats, and DEF 14A's share count is for
-- voting, not for an EPS denominator.
CREATE OR REPLACE TEMP TABLE periodic AS
SELECT *
FROM fundamentals_input
WHERE regexp_replace(form, '/A$', '') IN ('10-K', '10-Q', '20-F', '40-F', '6-K');

-- A 10-K carries last year's figures as comparatives, so the same period
-- arrives once per filing that mentions it. The newest filing wins: a
-- restatement is the figure that stands.
CREATE OR REPLACE TEMP TABLE deduped AS
SELECT * EXCLUDE (rank)
FROM (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, concept, period_start, period_end
            ORDER BY filed DESC NULLS LAST, accession DESC
        ) AS rank
    FROM periodic
)
WHERE rank = 1;

-- Duration facts, labelled by how long they cover. Anything that is not
-- recognisably a quarter, half or year is dropped rather than guessed at.
CREATE OR REPLACE TEMP TABLE flows AS
SELECT
    symbol,
    concept,
    unit,
    value,
    period_start,
    period_end,
    date_diff('day', period_start, period_end) + 1 AS period_days,
    CASE
        WHEN date_diff('day', period_start, period_end) BETWEEN 300 AND 400 THEN 'annual'
        WHEN date_diff('day', period_start, period_end) BETWEEN 150 AND 220 THEN 'semi'
        WHEN date_diff('day', period_start, period_end) BETWEEN 60 AND 120 THEN 'quarter'
    END AS period_type
FROM deduped
WHERE period_start IS NOT NULL;

-- Balance-sheet facts carry no start date; they attach to the closing date.
CREATE OR REPLACE TEMP TABLE stocks AS
SELECT symbol, concept, unit, value, period_end
FROM deduped
WHERE period_start IS NULL;

CREATE OR REPLACE TEMP TABLE flow_wide AS
SELECT
    symbol,
    period_type,
    period_start,
    period_end,
    any_value(period_days) AS period_days,
    max(value) FILTER (concept = 'revenue') AS revenue,
    any_value(unit) FILTER (concept = 'revenue') AS revenue_unit,
    max(value) FILTER (concept = 'cost_of_revenue') AS cost_of_revenue,
    any_value(unit) FILTER (concept = 'cost_of_revenue') AS cost_of_revenue_unit,
    max(value) FILTER (concept = 'gross_profit') AS gross_profit,
    any_value(unit) FILTER (concept = 'gross_profit') AS gross_profit_unit,
    max(value) FILTER (concept = 'operating_income') AS operating_income,
    any_value(unit) FILTER (concept = 'operating_income') AS operating_income_unit,
    max(value) FILTER (concept = 'net_income') AS net_income,
    any_value(unit) FILTER (concept = 'net_income') AS net_income_unit,
    max(value) FILTER (concept = 'research_development') AS research_development,
    any_value(unit) FILTER (concept = 'research_development') AS research_development_unit,
    max(value) FILTER (concept = 'operating_cash_flow') AS operating_cash_flow,
    any_value(unit) FILTER (concept = 'operating_cash_flow') AS operating_cash_flow_unit,
    max(value) FILTER (concept = 'capex') AS capex,
    any_value(unit) FILTER (concept = 'capex') AS capex_unit
FROM flows
WHERE period_type IS NOT NULL
GROUP BY symbol, period_type, period_start, period_end;

CREATE OR REPLACE TEMP TABLE stock_wide AS
SELECT
    symbol,
    period_end,
    max(value) FILTER (concept = 'inventory') AS inventory,
    any_value(unit) FILTER (concept = 'inventory') AS inventory_unit,
    max(value) FILTER (concept = 'assets') AS assets,
    any_value(unit) FILTER (concept = 'assets') AS assets_unit,
    max(value) FILTER (concept = 'equity') AS equity,
    any_value(unit) FILTER (concept = 'equity') AS equity_unit,
    max(value) FILTER (concept = 'cash_and_equivalents') AS cash_and_equivalents,
    max(value) FILTER (concept = 'shares_outstanding') AS shares_outstanding
FROM stocks
GROUP BY symbol, period_end;

CREATE OR REPLACE TEMP TABLE combined AS
SELECT
    flow_wide.*,
    stock_wide.inventory,
    stock_wide.inventory_unit,
    stock_wide.assets,
    stock_wide.assets_unit,
    stock_wide.equity,
    stock_wide.equity_unit,
    stock_wide.cash_and_equivalents,
    stock_wide.shares_outstanding
FROM flow_wide
LEFT JOIN stock_wide USING (symbol, period_end);

-- Ratios, each guarded on both sides sharing a unit. TSM reports partly in
-- USD and partly in TWD, so an unguarded margin would divide one currency by
-- another and produce a number that looks plausible and means nothing.
CREATE OR REPLACE TEMP TABLE base_metrics AS
SELECT
    symbol,
    period_type,
    period_start,
    period_end,
    period_days,
    revenue,
    revenue_unit,
    CASE
        WHEN revenue IS NOT NULL AND revenue <> 0
             AND gross_profit IS NOT NULL AND gross_profit_unit = revenue_unit
        THEN gross_profit / revenue
        WHEN revenue IS NOT NULL AND revenue <> 0
             AND cost_of_revenue IS NOT NULL AND cost_of_revenue_unit = revenue_unit
        THEN (revenue - cost_of_revenue) / revenue
    END AS gross_margin,
    CASE
        WHEN revenue IS NOT NULL AND revenue <> 0
             AND operating_income IS NOT NULL AND operating_income_unit = revenue_unit
        THEN operating_income / revenue
    END AS operating_margin,
    CASE
        WHEN revenue IS NOT NULL AND revenue <> 0
             AND net_income IS NOT NULL AND net_income_unit = revenue_unit
        THEN net_income / revenue
    END AS net_margin,
    -- The cycle indicator: inventory builds lead the downturn.
    CASE
        WHEN cost_of_revenue IS NOT NULL AND cost_of_revenue <> 0
             AND inventory IS NOT NULL AND inventory_unit = cost_of_revenue_unit
        THEN inventory / cost_of_revenue * period_days
    END AS inventory_days,
    CASE
        WHEN revenue IS NOT NULL AND revenue <> 0
             AND capex IS NOT NULL AND capex_unit = revenue_unit
        THEN capex / revenue
    END AS capex_intensity,
    CASE
        WHEN revenue IS NOT NULL AND revenue <> 0
             AND research_development IS NOT NULL
             AND research_development_unit = revenue_unit
        THEN research_development / revenue
    END AS rd_intensity,
    CASE
        WHEN operating_cash_flow IS NOT NULL AND capex IS NOT NULL
             AND operating_cash_flow_unit = capex_unit
        THEN operating_cash_flow - capex
    END AS free_cash_flow,
    CASE
        WHEN revenue IS NOT NULL AND revenue <> 0
             AND operating_cash_flow IS NOT NULL AND capex IS NOT NULL
             AND operating_cash_flow_unit = revenue_unit AND capex_unit = revenue_unit
        THEN (operating_cash_flow - capex) / revenue
    END AS free_cash_flow_margin,
    CASE
        WHEN assets IS NOT NULL AND assets <> 0
             AND equity IS NOT NULL AND equity_unit = assets_unit
        THEN equity / assets
    END AS equity_ratio,
    shares_outstanding
FROM combined;

-- Year on year, matched by closing date rather than fiscal labels: fiscal
-- years drift by a few days and are numbered inconsistently across filers.
CREATE OR REPLACE TEMP TABLE metrics AS
SELECT
    current_period.*,
    prior.revenue AS prior_revenue,
    CASE
        WHEN prior.revenue IS NOT NULL AND prior.revenue <> 0
             AND prior.revenue_unit = current_period.revenue_unit
        THEN current_period.revenue / prior.revenue - 1
    END AS revenue_yoy,
    CASE
        WHEN prior.gross_margin IS NOT NULL
        THEN current_period.gross_margin - prior.gross_margin
    END AS gross_margin_yoy_change,
    CASE
        WHEN prior.operating_margin IS NOT NULL
        THEN current_period.operating_margin - prior.operating_margin
    END AS operating_margin_yoy_change,
    CASE
        WHEN prior.inventory_days IS NOT NULL
        THEN current_period.inventory_days - prior.inventory_days
    END AS inventory_days_yoy_change,
    CASE
        WHEN prior.capex_intensity IS NOT NULL
        THEN current_period.capex_intensity - prior.capex_intensity
    END AS capex_intensity_yoy_change
FROM base_metrics AS current_period
LEFT JOIN base_metrics AS prior
    ON prior.symbol = current_period.symbol
   AND prior.period_type = current_period.period_type
   AND date_diff('day', prior.period_end, current_period.period_end) BETWEEN 330 AND 400;

-- The acceleration term. A single growth rate says how fast; the change in it
-- says whether the trend is turning, which is what the report is for.
CREATE OR REPLACE TEMP TABLE fundamentals_metrics AS
SELECT
    metrics.*,
    sectors_input.subsector,
    CASE
        WHEN previous.revenue_yoy IS NOT NULL
        THEN metrics.revenue_yoy - previous.revenue_yoy
    END AS revenue_yoy_change
FROM metrics
LEFT JOIN sectors_input ON sectors_input.symbol = metrics.symbol
LEFT JOIN metrics AS previous
    ON previous.symbol = metrics.symbol
   AND previous.period_type = metrics.period_type
   AND date_diff('day', previous.period_end, metrics.period_end) BETWEEN 330 AND 400;
