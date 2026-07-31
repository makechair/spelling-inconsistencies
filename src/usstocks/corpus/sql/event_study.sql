CREATE OR REPLACE TEMP TABLE daily_indexed AS
SELECT
    symbol,
    date,
    "adjClose" AS adj_close,
    CAST(
        row_number() OVER (PARTITION BY symbol ORDER BY date) - 1
        AS BIGINT
    ) AS trading_index
FROM daily_input
WHERE "adjClose" IS NOT NULL;

CREATE OR REPLACE TEMP TABLE aligned_events AS
WITH candidates AS (
    SELECT
        event.*,
        daily.date AS reaction_date,
        daily.trading_index AS reaction_index,
        sector.subsector,
        row_number() OVER (
            PARTITION BY event.event_key
            ORDER BY daily.date
        ) AS candidate_rank
    FROM events_timed_input AS event
    INNER JOIN daily_indexed AS daily
        ON daily.symbol = event.symbol
       AND daily.date >= event.candidate_date
    LEFT JOIN sectors_input AS sector
        ON sector.symbol = event.symbol
)
SELECT * EXCLUDE (candidate_rank)
FROM candidates
WHERE candidate_rank = 1;

CREATE OR REPLACE TEMP TABLE event_unmatched AS
SELECT
    event.event_key,
    event.page_id,
    event.symbol,
    event.event_date,
    event.published_at,
    event.candidate_date,
    event.timing_quality,
    event.timing_bucket,
    event.headline,
    event.event_type,
    event.sentiment,
    event.confidence,
    event.importance,
    CASE
        WHEN symbol_coverage.symbol IS NULL THEN 'symbol_not_in_daily_corpus'
        ELSE 'no_session_on_or_after_candidate'
    END AS reason
FROM events_timed_input AS event
LEFT JOIN aligned_events AS aligned
    ON aligned.event_key = event.event_key
LEFT JOIN (
    SELECT DISTINCT symbol
    FROM daily_indexed
) AS symbol_coverage
    ON symbol_coverage.symbol = event.symbol
WHERE aligned.event_key IS NULL;

CREATE OR REPLACE TEMP TABLE aligned_decorated AS
WITH grouped AS (
    SELECT
        aligned.*,
        count(*) OVER (
            PARTITION BY
                symbol,
                reaction_date,
                coalesce(event_type, '__unknown__')
        ) AS event_group_size
    FROM aligned_events AS aligned
),
overlap_counts AS (
    SELECT
        left_event.event_key,
        count(
            DISTINCT
            CAST(right_event.reaction_date AS VARCHAR)
            || ':'
            || coalesce(right_event.event_type, '__unknown__')
        ) AS overlap_count
    FROM aligned_events AS left_event
    LEFT JOIN aligned_events AS right_event
        ON right_event.symbol = left_event.symbol
       AND abs(right_event.reaction_index - left_event.reaction_index) <= 20
       AND NOT (
            right_event.reaction_date = left_event.reaction_date
        AND coalesce(right_event.event_type, '__unknown__')
            = coalesce(left_event.event_type, '__unknown__')
       )
    GROUP BY left_event.event_key
)
SELECT
    grouped.*,
    CAST(1.0 / event_group_size AS DOUBLE) AS event_weight,
    CAST(coalesce(overlap_counts.overlap_count, 0) AS BIGINT) AS overlap_count
FROM grouped
LEFT JOIN overlap_counts USING (event_key);

CREATE OR REPLACE TEMP TABLE event_subject_returns AS
SELECT
    event.*,
    horizon.horizon,
    base.adj_close AS base_adj_close,
    endpoint.adj_close AS end_adj_close,
    CASE
        WHEN base.adj_close IS NULL
          OR base.adj_close = 0
          OR endpoint.adj_close IS NULL
        THEN NULL
        ELSE endpoint.adj_close / base.adj_close - 1
    END AS raw_return
FROM aligned_decorated AS event
CROSS JOIN (VALUES (0), (1), (2), (5), (20)) AS horizon(horizon)
LEFT JOIN daily_indexed AS base
    ON base.symbol = event.symbol
   AND base.trading_index = event.reaction_index - 1
LEFT JOIN daily_indexed AS endpoint
    ON endpoint.symbol = event.symbol
   AND endpoint.trading_index = event.reaction_index + horizon.horizon;

CREATE OR REPLACE TEMP TABLE peer_benchmarks AS
SELECT
    subject.event_key,
    subject.horizon,
    count(*) AS peer_count,
    avg(peer_endpoint.adj_close / peer_base.adj_close - 1) AS benchmark_return
FROM event_subject_returns AS subject
INNER JOIN sectors_input AS peer_sector
    ON peer_sector.subsector = subject.subsector
   AND peer_sector.symbol <> subject.symbol
INNER JOIN daily_indexed AS peer_reaction
    ON peer_reaction.symbol = peer_sector.symbol
   AND peer_reaction.date = subject.reaction_date
INNER JOIN daily_indexed AS peer_base
    ON peer_base.symbol = peer_sector.symbol
   AND peer_base.trading_index = peer_reaction.trading_index - 1
INNER JOIN daily_indexed AS peer_endpoint
    ON peer_endpoint.symbol = peer_sector.symbol
   AND peer_endpoint.trading_index = peer_reaction.trading_index + subject.horizon
WHERE peer_base.adj_close <> 0
GROUP BY subject.event_key, subject.horizon;

CREATE OR REPLACE TEMP TABLE event_returns_long AS
SELECT
    subject.*,
    coalesce(peer.peer_count, 0) AS peer_count,
    CASE
        WHEN coalesce(peer.peer_count, 0) >= parameters.min_peers
        THEN peer.benchmark_return
        ELSE NULL
    END AS benchmark_return,
    CASE
        WHEN subject.raw_return IS NOT NULL
         AND coalesce(peer.peer_count, 0) >= parameters.min_peers
        THEN subject.raw_return - peer.benchmark_return
        ELSE NULL
    END AS abnormal_return
FROM event_subject_returns AS subject
LEFT JOIN peer_benchmarks AS peer
    ON peer.event_key = subject.event_key
   AND peer.horizon = subject.horizon
CROSS JOIN analysis_parameters AS parameters;

CREATE OR REPLACE TEMP TABLE event_returns AS
SELECT
    event_key,
    page_id,
    symbol,
    event_date,
    published_at,
    candidate_date,
    reaction_date,
    timing_quality,
    timing_bucket,
    headline,
    event_type,
    sentiment,
    confidence,
    importance,
    category,
    source,
    url,
    notion_url,
    subsector,
    event_group_size,
    event_weight,
    overlap_count,
    max(CASE WHEN horizon = 0 THEN raw_return END) AS raw_return_0d,
    max(CASE WHEN horizon = 1 THEN raw_return END) AS raw_return_1d,
    max(CASE WHEN horizon = 2 THEN raw_return END) AS raw_return_2d,
    max(CASE WHEN horizon = 5 THEN raw_return END) AS raw_return_5d,
    max(CASE WHEN horizon = 20 THEN raw_return END) AS raw_return_20d,
    max(CASE WHEN horizon = 0 THEN abnormal_return END) AS abnormal_return_0d,
    max(CASE WHEN horizon = 1 THEN abnormal_return END) AS abnormal_return_1d,
    max(CASE WHEN horizon = 2 THEN abnormal_return END) AS abnormal_return_2d,
    max(CASE WHEN horizon = 5 THEN abnormal_return END) AS abnormal_return_5d,
    max(CASE WHEN horizon = 20 THEN abnormal_return END) AS abnormal_return_20d,
    max(CASE WHEN horizon = 0 THEN peer_count END) AS peer_count_0d,
    max(CASE WHEN horizon = 1 THEN peer_count END) AS peer_count_1d,
    max(CASE WHEN horizon = 2 THEN peer_count END) AS peer_count_2d,
    max(CASE WHEN horizon = 5 THEN peer_count END) AS peer_count_5d,
    max(CASE WHEN horizon = 20 THEN peer_count END) AS peer_count_20d
FROM event_returns_long
GROUP BY ALL;

CREATE OR REPLACE TEMP TABLE event_summary AS
WITH samples AS (
    SELECT 'all' AS sample, *
    FROM event_returns_long
    UNION ALL
    SELECT 'non_overlapping' AS sample, *
    FROM event_returns_long
    WHERE overlap_count = 0
),
metrics AS (
    SELECT
        sample,
        event_type,
        sentiment,
        subsector,
        timing_bucket,
        confidence,
        importance,
        horizon,
        event_weight,
        'raw_return' AS metric,
        raw_return AS return_value
    FROM samples
    UNION ALL
    SELECT
        sample,
        event_type,
        sentiment,
        subsector,
        timing_bucket,
        confidence,
        importance,
        horizon,
        event_weight,
        'abnormal_return' AS metric,
        abnormal_return AS return_value
    FROM samples
),
dimensions AS (
    SELECT
        sample,
        'all' AS dimension,
        'all' AS group_value,
        metric,
        horizon,
        event_weight,
        return_value
    FROM metrics
    UNION ALL
    SELECT
        sample,
        'event_type',
        coalesce(event_type, 'unknown'),
        metric,
        horizon,
        event_weight,
        return_value
    FROM metrics
    UNION ALL
    SELECT
        sample,
        'sentiment',
        coalesce(sentiment, 'unknown'),
        metric,
        horizon,
        event_weight,
        return_value
    FROM metrics
    UNION ALL
    SELECT
        sample,
        'subsector',
        coalesce(subsector, 'unknown'),
        metric,
        horizon,
        event_weight,
        return_value
    FROM metrics
    UNION ALL
    SELECT
        sample,
        'timing_bucket',
        timing_bucket,
        metric,
        horizon,
        event_weight,
        return_value
    FROM metrics
    UNION ALL
    SELECT
        sample,
        'confidence_band',
        CASE
            WHEN confidence IS NULL THEN 'unknown'
            WHEN confidence < 0.50 THEN '0.00-0.49'
            WHEN confidence < 0.75 THEN '0.50-0.74'
            ELSE '0.75-1.00'
        END,
        metric,
        horizon,
        event_weight,
        return_value
    FROM metrics
    UNION ALL
    SELECT
        sample,
        'importance',
        coalesce(CAST(importance AS VARCHAR), 'unknown'),
        metric,
        horizon,
        event_weight,
        return_value
    FROM metrics
),
aggregated AS (
    SELECT
        sample,
        dimension,
        group_value,
        metric,
        horizon,
        count(*) AS events,
        sum(event_weight) AS weighted_events,
        sum(event_weight * return_value) / sum(event_weight) AS weighted_mean_return,
        sum(event_weight * return_value * return_value) AS sum_wx2,
        quantile_cont(return_value, 0.25) AS q25_return,
        quantile_cont(return_value, 0.50) AS median_return,
        quantile_cont(return_value, 0.75) AS q75_return,
        sum(event_weight * CAST(return_value > 0 AS INTEGER))
            / sum(event_weight) AS weighted_win_rate
    FROM dimensions
    WHERE return_value IS NOT NULL
    GROUP BY sample, dimension, group_value, metric, horizon
),
with_variance AS (
    SELECT
        *,
        weighted_events AS effective_events,
        CASE
            WHEN weighted_events > 1
            THEN
                greatest(
                    (
                        sum_wx2
                        - weighted_events
                          * weighted_mean_return
                          * weighted_mean_return
                    ),
                    0
                )
                / (weighted_events - 1)
            ELSE NULL
        END AS weighted_variance
    FROM aggregated
)
SELECT
    sample,
    dimension,
    group_value,
    metric,
    horizon,
    events,
    weighted_events,
    effective_events,
    weighted_mean_return,
    median_return,
    weighted_win_rate,
    q25_return,
    q75_return,
    CASE
        WHEN weighted_variance IS NOT NULL
        THEN weighted_mean_return - 1.96 * sqrt(weighted_variance / effective_events)
        ELSE NULL
    END AS ci95_low,
    CASE
        WHEN weighted_variance IS NOT NULL
        THEN weighted_mean_return + 1.96 * sqrt(weighted_variance / effective_events)
        ELSE NULL
    END AS ci95_high
FROM with_variance
ORDER BY sample, dimension, group_value, metric, horizon;
