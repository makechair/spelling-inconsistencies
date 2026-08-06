CREATE OR REPLACE TEMP TABLE daily_indexed AS
SELECT
    symbol,
    date,
    "adjClose" AS adj_close,
    "adjVolume" AS adj_volume,
    median("adjVolume") OVER (
        PARTITION BY symbol
        ORDER BY date
        ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
    ) AS median_volume_60d,
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
    event.ticker_origin,
    event.ticker_evidence,
    event.event_date,
    event.published_at,
    event.candidate_date,
    event.timing_quality,
    event.timing_bucket,
    event.headline,
    event.summary_ja,
    event.my_take,
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
        WHEN pre_5d.adj_close IS NULL OR pre_5d.adj_close = 0 OR base.adj_close IS NULL
        THEN NULL
        ELSE base.adj_close / pre_5d.adj_close - 1
    END AS pre_event_return_5d,
    CASE
        WHEN pre_20d.adj_close IS NULL OR pre_20d.adj_close = 0 OR base.adj_close IS NULL
        THEN NULL
        ELSE base.adj_close / pre_20d.adj_close - 1
    END AS pre_event_return_20d,
    CASE
        WHEN reaction.median_volume_60d IS NULL OR reaction.median_volume_60d = 0
        THEN NULL
        ELSE reaction.adj_volume / reaction.median_volume_60d
    END AS reaction_volume_ratio_60d,
    CASE
        WHEN base.adj_close IS NULL
          OR base.adj_close = 0
          OR endpoint.adj_close IS NULL
        THEN NULL
        ELSE endpoint.adj_close / base.adj_close - 1
    END AS raw_return
FROM aligned_decorated AS event
CROSS JOIN (VALUES (0), (1), (2), (5), (20)) AS horizon(horizon)
LEFT JOIN daily_indexed AS reaction
    ON reaction.symbol = event.symbol
   AND reaction.trading_index = event.reaction_index
LEFT JOIN daily_indexed AS base
    ON base.symbol = event.symbol
   AND base.trading_index = event.reaction_index - 1
LEFT JOIN daily_indexed AS endpoint
    ON endpoint.symbol = event.symbol
   AND endpoint.trading_index = event.reaction_index + horizon.horizon
LEFT JOIN daily_indexed AS pre_5d
    ON pre_5d.symbol = event.symbol
   AND pre_5d.trading_index = event.reaction_index - 6
LEFT JOIN daily_indexed AS pre_20d
    ON pre_20d.symbol = event.symbol
   AND pre_20d.trading_index = event.reaction_index - 21;

CREATE OR REPLACE TEMP TABLE historical_percentiles AS
SELECT
    subject.event_key,
    subject.horizon,
    count(*) AS historical_observations,
    avg(CAST(
        historical_endpoint.adj_close / historical_base.adj_close - 1
            <= subject.raw_return
        AS INTEGER
    )) AS historical_percentile
FROM event_subject_returns AS subject
INNER JOIN daily_indexed AS historical_reaction
    ON historical_reaction.symbol = subject.symbol
   AND historical_reaction.date < subject.reaction_date
INNER JOIN daily_indexed AS historical_base
    ON historical_base.symbol = historical_reaction.symbol
   AND historical_base.trading_index = historical_reaction.trading_index - 1
INNER JOIN daily_indexed AS historical_endpoint
    ON historical_endpoint.symbol = historical_reaction.symbol
   AND historical_endpoint.trading_index
       = historical_reaction.trading_index + subject.horizon
WHERE historical_base.adj_close <> 0
  AND subject.raw_return IS NOT NULL
GROUP BY subject.event_key, subject.horizon;

-- Daily returns, and the equal-weight return of a symbol's peers on the same
-- day. The peer average excludes the symbol itself: leaving it in would make
-- every symbol partly its own benchmark, and most of all in the small
-- subsectors where the effect is largest.
CREATE OR REPLACE TEMP TABLE symbol_daily_returns AS
SELECT
    daily.symbol,
    daily.date,
    daily.trading_index,
    sector.subsector,
    daily.adj_close / previous.adj_close - 1 AS symbol_return
FROM daily_indexed AS daily
INNER JOIN daily_indexed AS previous
    ON previous.symbol = daily.symbol
   AND previous.trading_index = daily.trading_index - 1
INNER JOIN sectors_input AS sector
    ON sector.symbol = daily.symbol
WHERE previous.adj_close <> 0;

CREATE OR REPLACE TEMP TABLE peer_daily_returns AS
SELECT
    member.symbol,
    member.date,
    member.trading_index,
    member.symbol_return,
    (sector_totals.return_sum - member.symbol_return)
        / nullif(sector_totals.member_count - 1, 0) AS peer_return
FROM symbol_daily_returns AS member
INNER JOIN (
    SELECT subsector, date, sum(symbol_return) AS return_sum, count(*) AS member_count
    FROM symbol_daily_returns
    GROUP BY subsector, date
) AS sector_totals
    ON sector_totals.subsector = member.subsector
   AND sector_totals.date = member.date;

-- How much of its sector's move a symbol usually takes, estimated on the days
-- before the event and never including it.
--
-- Subtracting the peer average outright assumes every member moves one for
-- one with its sector. They do not: a high-beta name rises more than the
-- group on an up day, so a flat difference reports abnormal strength every
-- time the sector rises and abnormal weakness every time it falls. That
-- pattern is an artefact of the assumption, not news.
CREATE OR REPLACE TEMP TABLE symbol_betas AS
SELECT
    symbol,
    date,
    trading_index,
    regr_slope(symbol_return, peer_return) OVER window_before AS raw_beta,
    regr_count(symbol_return, peer_return) OVER window_before AS beta_observations
FROM peer_daily_returns
WHERE peer_return IS NOT NULL
WINDOW window_before AS (
    PARTITION BY symbol
    ORDER BY trading_index
    -- Ends the day before, so the event being measured is never part of the
    -- estimate that measures it.
    ROWS BETWEEN 120 PRECEDING AND 1 PRECEDING
);

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
    historical.historical_observations,
    historical.historical_percentile,
    peer.benchmark_return AS exploratory_benchmark_return,
    CASE
        WHEN subject.raw_return IS NOT NULL
         AND coalesce(peer.peer_count, 0) >= 1
        THEN subject.raw_return - peer.benchmark_return
        ELSE NULL
    END AS exploratory_relative_return,
    CASE
        WHEN coalesce(peer.peer_count, 0) >= parameters.min_peers
        THEN peer.benchmark_return
        ELSE NULL
    END AS benchmark_return,
    -- An estimate is only used where there is enough of it, and only inside a
    -- range a real sector member occupies. An outlier slope from sixty noisy
    -- days would move the headline figure further than the assumption it
    -- replaces, so anything outside falls back to one and says so.
    coalesce(beta.beta, 1.0) AS beta,
    coalesce(beta.beta_observations, 0) AS beta_observations,
    CASE WHEN beta.beta IS NULL THEN 'assumed' ELSE 'estimated' END AS beta_source,
    CASE
        WHEN coalesce(peer.peer_count, 0) >= parameters.min_peers
        THEN peer.benchmark_return * coalesce(beta.beta, 1.0)
        ELSE NULL
    END AS sector_attributed_return,
    CASE
        WHEN subject.raw_return IS NOT NULL
         AND coalesce(peer.peer_count, 0) >= parameters.min_peers
        THEN subject.raw_return - peer.benchmark_return * coalesce(beta.beta, 1.0)
        ELSE NULL
    END AS abnormal_return,
    -- The previous definition, kept so a changed number can be traced to the
    -- change rather than to the day's news.
    CASE
        WHEN subject.raw_return IS NOT NULL
         AND coalesce(peer.peer_count, 0) >= parameters.min_peers
        THEN subject.raw_return - peer.benchmark_return
        ELSE NULL
    END AS equal_weight_abnormal_return
FROM event_subject_returns AS subject
LEFT JOIN peer_benchmarks AS peer
    ON peer.event_key = subject.event_key
   AND peer.horizon = subject.horizon
LEFT JOIN (
    SELECT
        symbol,
        date,
        CASE
            WHEN beta_observations >= 60 AND raw_beta BETWEEN 0.2 AND 3.0
            THEN raw_beta
        END AS beta,
        beta_observations
    FROM symbol_betas
) AS beta
    ON beta.symbol = subject.symbol
   AND beta.date = subject.reaction_date
LEFT JOIN historical_percentiles AS historical
    ON historical.event_key = subject.event_key
   AND historical.horizon = subject.horizon
CROSS JOIN analysis_parameters AS parameters;

CREATE OR REPLACE TEMP TABLE event_returns AS
SELECT
    event_key,
    page_id,
    symbol,
    ticker_origin,
    ticker_evidence,
    event_date,
    published_at,
    candidate_date,
    reaction_date,
    timing_quality,
    timing_bucket,
    headline,
    summary_ja,
    my_take,
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
    max(pre_event_return_5d) AS pre_event_return_5d,
    max(pre_event_return_20d) AS pre_event_return_20d,
    max(reaction_volume_ratio_60d) AS reaction_volume_ratio_60d,
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
    -- Beta is a property of the symbol on the day, not of the horizon.
    any_value(beta) AS beta,
    any_value(beta_observations) AS beta_observations,
    any_value(beta_source) AS beta_source,
    max(CASE WHEN horizon = 1 THEN sector_attributed_return END)
        AS sector_attributed_return_1d,
    max(CASE WHEN horizon = 5 THEN sector_attributed_return END)
        AS sector_attributed_return_5d,
    max(CASE WHEN horizon = 1 THEN equal_weight_abnormal_return END)
        AS equal_weight_abnormal_return_1d,
    max(CASE WHEN horizon = 5 THEN equal_weight_abnormal_return END)
        AS equal_weight_abnormal_return_5d,
    max(CASE WHEN horizon = 0 THEN exploratory_benchmark_return END)
        AS exploratory_benchmark_return_0d,
    max(CASE WHEN horizon = 1 THEN exploratory_benchmark_return END)
        AS exploratory_benchmark_return_1d,
    max(CASE WHEN horizon = 2 THEN exploratory_benchmark_return END)
        AS exploratory_benchmark_return_2d,
    max(CASE WHEN horizon = 5 THEN exploratory_benchmark_return END)
        AS exploratory_benchmark_return_5d,
    max(CASE WHEN horizon = 20 THEN exploratory_benchmark_return END)
        AS exploratory_benchmark_return_20d,
    max(CASE WHEN horizon = 0 THEN exploratory_relative_return END)
        AS exploratory_relative_return_0d,
    max(CASE WHEN horizon = 1 THEN exploratory_relative_return END)
        AS exploratory_relative_return_1d,
    max(CASE WHEN horizon = 2 THEN exploratory_relative_return END)
        AS exploratory_relative_return_2d,
    max(CASE WHEN horizon = 5 THEN exploratory_relative_return END)
        AS exploratory_relative_return_5d,
    max(CASE WHEN horizon = 20 THEN exploratory_relative_return END)
        AS exploratory_relative_return_20d,
    max(CASE WHEN horizon = 0 THEN historical_percentile END)
        AS historical_percentile_0d,
    max(CASE WHEN horizon = 1 THEN historical_percentile END)
        AS historical_percentile_1d,
    max(CASE WHEN horizon = 2 THEN historical_percentile END)
        AS historical_percentile_2d,
    max(CASE WHEN horizon = 5 THEN historical_percentile END)
        AS historical_percentile_5d,
    max(CASE WHEN horizon = 20 THEN historical_percentile END)
        AS historical_percentile_20d,
    max(CASE WHEN horizon = 0 THEN historical_observations END)
        AS historical_observations_0d,
    max(CASE WHEN horizon = 1 THEN historical_observations END)
        AS historical_observations_1d,
    max(CASE WHEN horizon = 2 THEN historical_observations END)
        AS historical_observations_2d,
    max(CASE WHEN horizon = 5 THEN historical_observations END)
        AS historical_observations_5d,
    max(CASE WHEN horizon = 20 THEN historical_observations END)
        AS historical_observations_20d,
    max(CASE WHEN horizon = 0 THEN peer_count END) AS peer_count_0d,
    max(CASE WHEN horizon = 1 THEN peer_count END) AS peer_count_1d,
    max(CASE WHEN horizon = 2 THEN peer_count END) AS peer_count_2d,
    max(CASE WHEN horizon = 5 THEN peer_count END) AS peer_count_5d,
    max(CASE WHEN horizon = 20 THEN peer_count END) AS peer_count_20d
FROM event_returns_long
GROUP BY ALL;

CREATE OR REPLACE TEMP TABLE event_similar_moves AS
SELECT
    event.event_key,
    CASE WHEN event.raw_return_0d < 0 THEN 'down' ELSE 'up' END AS move_direction,
    history.symbol,
    history.date,
    history.trading_index,
    history.adj_close
FROM event_returns AS event
INNER JOIN daily_indexed AS history
    ON history.symbol = event.symbol
   AND history.date < event.reaction_date
INNER JOIN daily_indexed AS history_base
    ON history_base.symbol = history.symbol
   AND history_base.trading_index = history.trading_index - 1
WHERE event.raw_return_0d IS NOT NULL
  AND history_base.adj_close <> 0
  AND (
        (
            event.raw_return_0d < 0
        AND history.adj_close / history_base.adj_close - 1 <= event.raw_return_0d
        )
        OR
        (
            event.raw_return_0d >= 0
        AND history.adj_close / history_base.adj_close - 1 >= event.raw_return_0d
        )
  );

CREATE OR REPLACE TEMP TABLE event_similar_move_counts AS
SELECT
    event_key,
    move_direction,
    count(*) AS similar_move_count
FROM event_similar_moves
GROUP BY event_key, move_direction;

CREATE OR REPLACE TEMP TABLE event_case_context_long AS
SELECT
    move.event_key,
    move.move_direction,
    horizon.horizon,
    count(*) AS forward_observations,
    avg(endpoint.adj_close / move.adj_close - 1) AS forward_mean,
    median(endpoint.adj_close / move.adj_close - 1) AS forward_median,
    stddev_samp(endpoint.adj_close / move.adj_close - 1) AS forward_stddev,
    quantile_cont(endpoint.adj_close / move.adj_close - 1, 0.25) AS forward_q1,
    quantile_cont(endpoint.adj_close / move.adj_close - 1, 0.75) AS forward_q3,
    avg(CAST(endpoint.adj_close / move.adj_close - 1 > 0 AS INTEGER))
        AS forward_win_rate
FROM event_similar_moves AS move
CROSS JOIN range(1, 21) AS horizon(horizon)
INNER JOIN daily_indexed AS endpoint
    ON endpoint.symbol = move.symbol
   AND endpoint.trading_index = move.trading_index + horizon.horizon
WHERE move.adj_close <> 0
GROUP BY move.event_key, move.move_direction, horizon.horizon;

CREATE OR REPLACE TEMP TABLE event_case_context AS
SELECT
    counts.event_key,
    counts.move_direction,
    counts.similar_move_count,
    max(context.forward_mean) FILTER (WHERE context.horizon = 1) AS forward_mean_1d,
    max(context.forward_median) FILTER (WHERE context.horizon = 1) AS forward_median_1d,
    max(context.forward_win_rate) FILTER (WHERE context.horizon = 1)
        AS forward_win_rate_1d,
    max(context.forward_mean) FILTER (WHERE context.horizon = 5) AS forward_mean_5d,
    max(context.forward_median) FILTER (WHERE context.horizon = 5) AS forward_median_5d,
    max(context.forward_win_rate) FILTER (WHERE context.horizon = 5)
        AS forward_win_rate_5d,
    max(context.forward_mean) FILTER (WHERE context.horizon = 20) AS forward_mean_20d,
    max(context.forward_median) FILTER (WHERE context.horizon = 20)
        AS forward_median_20d,
    max(context.forward_win_rate) FILTER (WHERE context.horizon = 20)
        AS forward_win_rate_20d,
    list(
        struct_pack(
            horizon := context.horizon,
            observations := context.forward_observations,
            mean := context.forward_mean,
            median := context.forward_median,
            stddev := context.forward_stddev,
            q1 := context.forward_q1,
            q3 := context.forward_q3,
            win_rate := context.forward_win_rate
        )
        ORDER BY context.horizon
    ) AS forward_path
FROM event_similar_move_counts AS counts
INNER JOIN event_case_context_long AS context USING (event_key, move_direction)
GROUP BY counts.event_key, counts.move_direction, counts.similar_move_count;

-- A two-dimensional response surface for planning after a price move.  Decile
-- buckets are used instead of arbitrary round percentage bands so every
-- symbol has comparable sample support even when its volatility is different.
CREATE OR REPLACE TEMP TABLE daily_move_buckets AS
WITH moves AS (
    SELECT
        reaction.symbol,
        reaction.trading_index,
        reaction.date,
        reaction.adj_close,
        reaction.adj_close / base.adj_close - 1 AS move_return
    FROM daily_indexed AS reaction
    INNER JOIN daily_indexed AS base
        ON base.symbol = reaction.symbol
       AND base.trading_index = reaction.trading_index - 1
    INNER JOIN (SELECT DISTINCT symbol FROM events_timed_input) AS relevant
        ON relevant.symbol = reaction.symbol
    WHERE base.adj_close <> 0
)
SELECT
    *,
    ntile(10) OVER (PARTITION BY symbol ORDER BY move_return) AS move_bucket
FROM moves;

CREATE OR REPLACE TEMP TABLE return_surface AS
SELECT
    move.symbol,
    move.move_bucket,
    min(move.move_return) AS move_min,
    max(move.move_return) AS move_max,
    avg(move.move_return) AS move_mean,
    horizon.horizon,
    count(*) AS observations,
    avg(endpoint.adj_close / move.adj_close - 1) AS forward_mean,
    median(endpoint.adj_close / move.adj_close - 1) AS forward_median,
    stddev_samp(endpoint.adj_close / move.adj_close - 1) AS forward_stddev,
    avg(CAST(endpoint.adj_close / move.adj_close - 1 > 0 AS INTEGER))
        AS forward_win_rate
FROM daily_move_buckets AS move
CROSS JOIN range(1, 21) AS horizon(horizon)
INNER JOIN daily_indexed AS endpoint
    ON endpoint.symbol = move.symbol
   AND endpoint.trading_index = move.trading_index + horizon.horizon
WHERE move.adj_close <> 0
GROUP BY move.symbol, move.move_bucket, horizon.horizon;

-- Continuous move-rate grid. The inner 90% is kernel-smoothed so adjacent
-- returns share information instead of changing abruptly at a decile border.
-- The outer 5% tails remain explicit crash/spike bands and are allowed to be
-- blank when their effective support is too small.
CREATE OR REPLACE TEMP TABLE return_surface_grid AS
WITH stats AS (
    SELECT
        symbol,
        min(move_return) AS move_minimum,
        max(move_return) AS move_maximum,
        quantile_cont(move_return, 0.05) AS move_q05,
        quantile_cont(move_return, 0.25) AS move_q25,
        quantile_cont(move_return, 0.75) AS move_q75,
        quantile_cont(move_return, 0.95) AS move_q95,
        greatest(
            0.003,
            0.9 * least(
                stddev_samp(move_return),
                (quantile_cont(move_return, 0.75)
                    - quantile_cont(move_return, 0.25)) / 1.34
            ) * pow(count(*)::DOUBLE, -0.2)
        ) AS bandwidth
    FROM daily_move_buckets
    GROUP BY symbol
), inner_grid AS (
    SELECT
        stats.*,
        grid.grid_index,
        stats.move_q05
            + (stats.move_q95 - stats.move_q05)
            * (grid.grid_index - 2)::DOUBLE / 16 AS target_move
    FROM stats
    CROSS JOIN range(2, 19) AS grid(grid_index)
)
SELECT
    symbol,
    1 AS move_bucket,
    'lower_tail' AS surface_method,
    move_q05 AS target_move,
    move_minimum AS move_min,
    move_q05 AS move_max,
    bandwidth
FROM stats
UNION ALL
SELECT
    symbol,
    grid_index AS move_bucket,
    'kernel' AS surface_method,
    target_move,
    target_move - bandwidth AS move_min,
    target_move + bandwidth AS move_max,
    bandwidth
FROM inner_grid
UNION ALL
SELECT
    symbol,
    19 AS move_bucket,
    'upper_tail' AS surface_method,
    move_q95 AS target_move,
    move_q95 AS move_min,
    move_maximum AS move_max,
    bandwidth
FROM stats;

CREATE OR REPLACE TEMP TABLE return_surface_baseline AS
SELECT
    move.symbol,
    horizon.horizon,
    avg(endpoint.adj_close / move.adj_close - 1) AS baseline_mean
FROM daily_move_buckets AS move
CROSS JOIN range(1, 21) AS horizon(horizon)
INNER JOIN daily_indexed AS endpoint
    ON endpoint.symbol = move.symbol
   AND endpoint.trading_index = move.trading_index + horizon.horizon
WHERE move.adj_close <> 0
GROUP BY move.symbol, horizon.horizon;

CREATE OR REPLACE TEMP TABLE return_surface_smoothed AS
WITH weighted AS (
    SELECT
        grid.symbol,
        grid.move_bucket,
        grid.surface_method,
        grid.target_move,
        grid.move_min,
        grid.move_max,
        grid.bandwidth,
        horizon.horizon,
        endpoint.adj_close / move.adj_close - 1 AS forward_return,
        CASE
            WHEN grid.surface_method = 'lower_tail'
                THEN CAST(move.move_return <= grid.target_move AS DOUBLE)
            WHEN grid.surface_method = 'upper_tail'
                THEN CAST(move.move_return >= grid.target_move AS DOUBLE)
            ELSE exp(
                -0.5 * pow(
                    (move.move_return - grid.target_move) / grid.bandwidth,
                    2
                )
            )
        END AS kernel_weight
    FROM return_surface_grid AS grid
    INNER JOIN daily_move_buckets AS move USING (symbol)
    CROSS JOIN range(1, 21) AS horizon(horizon)
    INNER JOIN daily_indexed AS endpoint
        ON endpoint.symbol = move.symbol
       AND endpoint.trading_index = move.trading_index + horizon.horizon
    WHERE move.adj_close <> 0
      AND (
            grid.surface_method <> 'kernel'
         OR abs(move.move_return - grid.target_move) <= 3 * grid.bandwidth
      )
), aggregated AS (
    SELECT
        symbol,
        move_bucket,
        surface_method,
        target_move,
        move_min,
        move_max,
        bandwidth,
        horizon,
        count(*) FILTER (WHERE kernel_weight > 0) AS observations,
        sum(kernel_weight) AS weight_sum,
        pow(sum(kernel_weight), 2) / nullif(sum(pow(kernel_weight, 2)), 0)
            AS local_effective_observations,
        sum(kernel_weight * forward_return) / nullif(sum(kernel_weight), 0)
            AS forward_mean,
        sum(kernel_weight * pow(forward_return, 2)) / nullif(sum(kernel_weight), 0)
            AS forward_second_moment,
        sum(kernel_weight * CAST(forward_return > 0 AS INTEGER))
            / nullif(sum(kernel_weight), 0) AS forward_win_rate
    FROM weighted
    WHERE kernel_weight > 0
    GROUP BY
        symbol,
        move_bucket,
        surface_method,
        target_move,
        move_min,
        move_max,
        bandwidth,
        horizon
)
SELECT
    aggregated.* EXCLUDE (forward_second_moment),
    sqrt(greatest(
        forward_second_moment - pow(forward_mean, 2),
        0
    )) AS forward_stddev,
    CAST(NULL AS DOUBLE) AS forward_median,
    baseline.baseline_mean,
    forward_mean - baseline.baseline_mean AS conditional_edge,
    local_effective_observations / horizon AS effective_observations
FROM aggregated
INNER JOIN return_surface_baseline AS baseline USING (symbol, horizon);

-- A trade plan must preserve the order of the decision: wait first, buy, and
-- only then sell.  Computing the return between those two endpoints directly
-- also lets us report a real win rate and downside quantile, neither can be
-- reconstructed from two independently aggregated surface cells.
CREATE OR REPLACE TEMP TABLE return_trade_observations AS
SELECT
    move.symbol,
    move.move_bucket,
    move.date AS signal_date,
    move.trading_index AS signal_index,
    move.move_return,
    buy_day.buy_day,
    sell_day.sell_day,
    sell_day.sell_day - buy_day.buy_day AS holding_days,
    sell_price.adj_close / buy_price.adj_close - 1 AS trade_return
FROM daily_move_buckets AS move
CROSS JOIN range(1, 20) AS buy_day(buy_day)
CROSS JOIN range(buy_day.buy_day + 1, 21) AS sell_day(sell_day)
INNER JOIN daily_indexed AS buy_price
    ON buy_price.symbol = move.symbol
   AND buy_price.trading_index = move.trading_index + buy_day.buy_day
INNER JOIN daily_indexed AS sell_price
    ON sell_price.symbol = move.symbol
   AND sell_price.trading_index = move.trading_index + sell_day.sell_day
WHERE buy_price.adj_close <> 0;

CREATE OR REPLACE TEMP TABLE return_trade_peer_observations AS
SELECT
    subject.symbol,
    subject.move_bucket,
    subject.signal_date,
    subject.buy_day,
    subject.sell_day,
    count(*) AS peer_count,
    avg(peer_sell.adj_close / peer_buy.adj_close - 1) AS peer_return
FROM return_trade_observations AS subject
INNER JOIN sectors_input AS subject_sector
    ON subject_sector.symbol = subject.symbol
INNER JOIN sectors_input AS peer_sector
    ON peer_sector.subsector = subject_sector.subsector
   AND peer_sector.symbol <> subject.symbol
INNER JOIN daily_indexed AS peer_signal
    ON peer_signal.symbol = peer_sector.symbol
   AND peer_signal.date = subject.signal_date
INNER JOIN daily_indexed AS peer_buy
    ON peer_buy.symbol = peer_signal.symbol
   AND peer_buy.trading_index = peer_signal.trading_index + subject.buy_day
INNER JOIN daily_indexed AS peer_sell
    ON peer_sell.symbol = peer_signal.symbol
   AND peer_sell.trading_index = peer_signal.trading_index + subject.sell_day
WHERE peer_buy.adj_close <> 0
GROUP BY
    subject.symbol,
    subject.move_bucket,
    subject.signal_date,
    subject.buy_day,
    subject.sell_day;

CREATE OR REPLACE TEMP TABLE return_trade_candidates AS
WITH aggregated AS (
    SELECT
        trade.symbol,
        trade.move_bucket,
        min(bucket.move_return) AS move_min,
        max(bucket.move_return) AS move_max,
        avg(bucket.move_return) AS move_mean,
        trade.buy_day,
        trade.sell_day,
        trade.holding_days,
        count(*) AS observations,
        count(*) / trade.holding_days::DOUBLE AS effective_observations,
        avg(trade.trade_return) AS expected_return,
        median(trade.trade_return) AS median_return,
        stddev_samp(trade.trade_return) AS return_stddev,
        quantile_cont(trade.trade_return, 0.10) AS downside_p10,
        avg(CAST(trade.trade_return > 0 AS INTEGER)) AS win_rate,
        avg(
            trade.trade_return - peer.peer_return
        ) FILTER (
            WHERE peer.peer_count >= parameters.min_peers
        ) AS sector_excess_return,
        count(*) FILTER (
            WHERE peer.peer_count >= parameters.min_peers
        ) AS sector_observations
    FROM return_trade_observations AS trade
    INNER JOIN daily_move_buckets AS bucket
        ON bucket.symbol = trade.symbol
       AND bucket.move_bucket = trade.move_bucket
       AND bucket.date = trade.signal_date
    LEFT JOIN return_trade_peer_observations AS peer
        ON peer.symbol = trade.symbol
       AND peer.move_bucket = trade.move_bucket
       AND peer.signal_date = trade.signal_date
       AND peer.buy_day = trade.buy_day
       AND peer.sell_day = trade.sell_day
    CROSS JOIN analysis_parameters AS parameters
    GROUP BY
        trade.symbol,
        trade.move_bucket,
        trade.buy_day,
        trade.sell_day,
        trade.holding_days
)
SELECT
    *,
    expected_return - 0.001 AS expected_return_after_cost,
    downside_p10 - 0.001 AS downside_p10_after_cost,
    expected_return - 0.001
        - 1.2816 * return_stddev / sqrt(greatest(effective_observations, 1))
        AS conservative_return
FROM aggregated;

CREATE OR REPLACE TEMP TABLE return_trade_plan AS
SELECT
    *,
    row_number() OVER (
        PARTITION BY symbol, move_bucket
        ORDER BY
            conservative_return DESC NULLS LAST,
            expected_return_after_cost DESC,
            holding_days ASC
    ) AS plan_rank,
    sell_day = 20 AS sell_at_window_boundary,
    CASE
        WHEN effective_observations < 10 THEN 'insufficient'
        WHEN conservative_return > 0 THEN 'strong'
        WHEN expected_return_after_cost > 0 AND win_rate >= 0.5 THEN 'moderate'
        ELSE 'weak'
    END AS evidence_level
FROM return_trade_candidates
WHERE effective_observations >= 10
QUALIFY row_number() OVER (
    PARTITION BY symbol, move_bucket
    ORDER BY
        conservative_return DESC NULLS LAST,
        expected_return_after_cost DESC,
        holding_days ASC
) = 1;

CREATE OR REPLACE TEMP TABLE return_trade_plan_smoothed AS
WITH weighted AS (
    SELECT
        grid.symbol,
        grid.move_bucket,
        grid.surface_method,
        grid.target_move,
        grid.move_min,
        grid.move_max,
        grid.bandwidth,
        trade.buy_day,
        trade.sell_day,
        trade.holding_days,
        trade.trade_return,
        peer.peer_return,
        peer.peer_count,
        parameters.min_peers,
        CASE
            WHEN grid.surface_method = 'lower_tail'
                THEN CAST(move.move_return <= grid.target_move AS DOUBLE)
            WHEN grid.surface_method = 'upper_tail'
                THEN CAST(move.move_return >= grid.target_move AS DOUBLE)
            ELSE exp(
                -0.5 * pow(
                    (move.move_return - grid.target_move) / grid.bandwidth,
                    2
                )
            )
        END AS kernel_weight
    FROM return_surface_grid AS grid
    INNER JOIN return_trade_observations AS trade USING (symbol)
    INNER JOIN daily_move_buckets AS move
        ON move.symbol = trade.symbol
       AND move.date = trade.signal_date
    LEFT JOIN return_trade_peer_observations AS peer
        ON peer.symbol = trade.symbol
       AND peer.move_bucket = trade.move_bucket
       AND peer.signal_date = trade.signal_date
       AND peer.buy_day = trade.buy_day
       AND peer.sell_day = trade.sell_day
    CROSS JOIN analysis_parameters AS parameters
    WHERE grid.surface_method <> 'kernel'
       OR abs(move.move_return - grid.target_move) <= 3 * grid.bandwidth
), aggregated AS (
    SELECT
        symbol,
        move_bucket,
        surface_method,
        target_move,
        move_min,
        move_max,
        bandwidth,
        buy_day,
        sell_day,
        holding_days,
        count(*) FILTER (WHERE kernel_weight > 0) AS observations,
        pow(sum(kernel_weight), 2) / nullif(sum(pow(kernel_weight, 2)), 0)
            AS local_effective_observations,
        sum(kernel_weight * trade_return) / nullif(sum(kernel_weight), 0)
            AS expected_return,
        sum(kernel_weight * pow(trade_return, 2)) / nullif(sum(kernel_weight), 0)
            AS return_second_moment,
        sum(kernel_weight * CAST(trade_return > 0 AS INTEGER))
            / nullif(sum(kernel_weight), 0) AS win_rate,
        sum(kernel_weight * (trade_return - peer_return)) FILTER (
            WHERE peer_count >= min_peers
        ) / nullif(sum(kernel_weight) FILTER (
            WHERE peer_count >= min_peers
        ), 0) AS sector_excess_return,
        count(*) FILTER (WHERE peer_count >= min_peers AND kernel_weight > 0)
            AS sector_observations
    FROM weighted
    WHERE kernel_weight > 0
    GROUP BY
        symbol,
        move_bucket,
        surface_method,
        target_move,
        move_min,
        move_max,
        bandwidth,
        buy_day,
        sell_day,
        holding_days
), scored AS (
    SELECT
        *,
        sqrt(greatest(
            return_second_moment - pow(expected_return, 2),
            0
        )) AS return_stddev,
        local_effective_observations / holding_days AS effective_observations
    FROM aggregated
), candidates AS (
    SELECT
        *,
        expected_return - 0.001 AS expected_return_after_cost,
        expected_return - 0.001 - 1.2816 * return_stddev
            AS downside_p10_after_cost,
        expected_return - 0.001
            - 1.2816 * return_stddev
                / sqrt(greatest(effective_observations, 1))
            AS conservative_return
    FROM scored
)
SELECT
    *,
    row_number() OVER (
        PARTITION BY symbol, move_bucket
        ORDER BY
            conservative_return DESC NULLS LAST,
            expected_return_after_cost DESC,
            holding_days ASC
    ) AS plan_rank,
    sell_day = 20 AS sell_at_window_boundary,
    'normal_approximation' AS downside_method,
    CASE
        WHEN conservative_return > 0 THEN 'strong'
        WHEN expected_return_after_cost > 0 AND win_rate >= 0.5 THEN 'moderate'
        ELSE 'weak'
    END AS evidence_level
FROM candidates
WHERE effective_observations >= 10
QUALIFY plan_rank <= 3;

-- Out-of-sample validation for a representative subset of the response
-- surface.  Each fold learns its move thresholds and B/S pair from data that
-- ends before the validation window, then applies that frozen rule to the next
-- chronological block.  This prevents future returns from selecting their own
-- rule.  Five move regimes keep the nightly query bounded while covering both
-- tails and the centre of the distribution.
CREATE OR REPLACE TEMP TABLE return_validation_folds AS
WITH bounds AS (
    SELECT
        symbol,
        max(trading_index) - 20 AS last_signal_index
    FROM daily_indexed
    GROUP BY symbol
), fractions(fold, test_start_fraction, test_end_fraction) AS (
    VALUES
        (1, 0.55, 0.70),
        (2, 0.70, 0.85),
        (3, 0.85, 1.00)
)
SELECT
    bounds.symbol,
    fractions.fold,
    CAST(floor(last_signal_index * test_start_fraction) AS BIGINT)
        AS test_start_index,
    CAST(floor(last_signal_index * test_end_fraction) AS BIGINT)
        AS test_end_index
FROM bounds
CROSS JOIN fractions
WHERE last_signal_index >= 120;

CREATE OR REPLACE TEMP TABLE return_validation_grid AS
WITH training_stats AS (
    SELECT
        fold.symbol,
        fold.fold,
        fold.test_start_index,
        fold.test_end_index,
        min(move.move_return) AS move_minimum,
        max(move.move_return) AS move_maximum,
        quantile_cont(move.move_return, 0.05) AS move_q05,
        quantile_cont(move.move_return, 0.25) AS move_q25,
        quantile_cont(move.move_return, 0.75) AS move_q75,
        quantile_cont(move.move_return, 0.95) AS move_q95,
        greatest(
            0.003,
            0.9 * least(
                stddev_samp(move.move_return),
                (quantile_cont(move.move_return, 0.75)
                    - quantile_cont(move.move_return, 0.25)) / 1.34
            ) * pow(count(*)::DOUBLE, -0.2)
        ) AS bandwidth,
        count(*) AS training_signals,
        max(move.date) AS training_through
    FROM return_validation_folds AS fold
    INNER JOIN daily_move_buckets AS move
        ON move.symbol = fold.symbol
       AND move.trading_index + 20 < fold.test_start_index
    GROUP BY
        fold.symbol,
        fold.fold,
        fold.test_start_index,
        fold.test_end_index
), selected_buckets(move_bucket) AS (
    VALUES (1), (5), (10), (15), (19)
)
SELECT
    stats.symbol,
    stats.fold,
    stats.test_start_index,
    stats.test_end_index,
    stats.training_signals,
    stats.training_through,
    bucket.move_bucket,
    CASE
        WHEN bucket.move_bucket = 1 THEN 'lower_tail'
        WHEN bucket.move_bucket = 19 THEN 'upper_tail'
        ELSE 'kernel'
    END AS surface_method,
    CASE
        WHEN bucket.move_bucket = 1 THEN stats.move_q05
        WHEN bucket.move_bucket = 19 THEN stats.move_q95
        ELSE stats.move_q05 + (stats.move_q95 - stats.move_q05)
            * (bucket.move_bucket - 2)::DOUBLE / 16
    END AS target_move,
    CASE
        WHEN bucket.move_bucket = 1 THEN stats.move_minimum
        WHEN bucket.move_bucket = 19 THEN stats.move_q95
        ELSE stats.move_q05 + (stats.move_q95 - stats.move_q05)
            * (bucket.move_bucket - 2)::DOUBLE / 16 - stats.bandwidth
    END AS move_min,
    CASE
        WHEN bucket.move_bucket = 1 THEN stats.move_q05
        WHEN bucket.move_bucket = 19 THEN stats.move_maximum
        ELSE stats.move_q05 + (stats.move_q95 - stats.move_q05)
            * (bucket.move_bucket - 2)::DOUBLE / 16 + stats.bandwidth
    END AS move_max,
    stats.bandwidth
FROM training_stats AS stats
CROSS JOIN selected_buckets AS bucket;

CREATE OR REPLACE TEMP TABLE return_validation_training_plan AS
WITH weighted AS (
    SELECT
        grid.*,
        trade.buy_day,
        trade.sell_day,
        trade.holding_days,
        trade.trade_return,
        CASE
            WHEN grid.surface_method = 'lower_tail'
                THEN CAST(trade.move_return <= grid.target_move AS DOUBLE)
            WHEN grid.surface_method = 'upper_tail'
                THEN CAST(trade.move_return >= grid.target_move AS DOUBLE)
            ELSE exp(-0.5 * pow(
                (trade.move_return - grid.target_move) / grid.bandwidth,
                2
            ))
        END AS kernel_weight
    FROM return_validation_grid AS grid
    INNER JOIN return_trade_observations AS trade
        ON trade.symbol = grid.symbol
       AND trade.signal_index + 20 < grid.test_start_index
    WHERE grid.surface_method <> 'kernel'
       OR abs(trade.move_return - grid.target_move) <= 3 * grid.bandwidth
), aggregated AS (
    SELECT
        symbol,
        fold,
        test_start_index,
        test_end_index,
        training_signals,
        training_through,
        move_bucket,
        surface_method,
        target_move,
        move_min,
        move_max,
        bandwidth,
        buy_day,
        sell_day,
        holding_days,
        count(*) FILTER (WHERE kernel_weight > 0) AS training_observations,
        pow(sum(kernel_weight), 2) / nullif(sum(pow(kernel_weight, 2)), 0)
            AS local_effective_observations,
        sum(kernel_weight * trade_return) / nullif(sum(kernel_weight), 0)
            AS expected_return,
        sum(kernel_weight * pow(trade_return, 2)) / nullif(sum(kernel_weight), 0)
            AS return_second_moment,
        sum(kernel_weight * CAST(trade_return - 0.001 > 0 AS INTEGER))
            / nullif(sum(kernel_weight), 0) AS expected_win_rate
    FROM weighted
    WHERE kernel_weight > 0
    GROUP BY ALL
), scored AS (
    SELECT
        * EXCLUDE (return_second_moment),
        local_effective_observations / holding_days AS effective_observations,
        expected_return - 0.001 AS expected_return_after_cost,
        expected_return - 0.001
            - 1.2816 * sqrt(greatest(
                return_second_moment - pow(expected_return, 2),
                0
            )) / sqrt(greatest(
                local_effective_observations / holding_days,
                1
            )) AS conservative_return
    FROM aggregated
)
SELECT *
FROM scored
WHERE effective_observations >= 10
QUALIFY row_number() OVER (
    PARTITION BY symbol, fold, move_bucket
    ORDER BY
        conservative_return DESC NULLS LAST,
        expected_return_after_cost DESC,
        holding_days ASC
) = 1;

CREATE OR REPLACE TEMP TABLE return_validation_test_trades AS
SELECT
    plan.*,
    trade.signal_date,
    trade.move_return AS actual_initial_move,
    trade.trade_return - 0.001 AS actual_return_after_cost,
    CASE
        WHEN plan.surface_method = 'lower_tail'
            THEN CAST(trade.move_return <= plan.target_move AS DOUBLE)
        WHEN plan.surface_method = 'upper_tail'
            THEN CAST(trade.move_return >= plan.target_move AS DOUBLE)
        ELSE exp(-0.5 * pow(
            (trade.move_return - plan.target_move) / plan.bandwidth,
            2
        ))
    END AS validation_weight
FROM return_validation_training_plan AS plan
INNER JOIN return_trade_observations AS trade
    ON trade.symbol = plan.symbol
   AND trade.buy_day = plan.buy_day
   AND trade.sell_day = plan.sell_day
   AND trade.signal_index >= plan.test_start_index
   AND trade.signal_index < plan.test_end_index
WHERE plan.surface_method <> 'kernel'
   OR abs(trade.move_return - plan.target_move) <= 3 * plan.bandwidth;

CREATE OR REPLACE TEMP TABLE return_validation_results AS
WITH aggregated AS (
    SELECT
        symbol,
        fold,
        move_bucket,
        surface_method,
        target_move,
        move_min,
        move_max,
        bandwidth,
        training_signals,
        training_through,
        min(signal_date) AS validation_from,
        max(signal_date) AS validation_through,
        buy_day,
        sell_day,
        holding_days,
        training_observations,
        effective_observations AS training_effective_observations,
        expected_return_after_cost,
        expected_win_rate,
        conservative_return,
        count(*) FILTER (WHERE validation_weight > 0) AS validation_observations,
        pow(sum(validation_weight), 2)
            / nullif(sum(pow(validation_weight, 2)), 0) AS validation_local_effective,
        pow(sum(validation_weight), 2)
            / nullif(sum(pow(validation_weight, 2)), 0) / holding_days
            AS validation_effective_observations,
        sum(validation_weight * actual_return_after_cost)
            / nullif(sum(validation_weight), 0) AS actual_mean_return,
        median(actual_return_after_cost) AS actual_median_return,
        quantile_cont(actual_return_after_cost, 0.10) AS actual_downside_p10,
        sum(validation_weight * CAST(actual_return_after_cost > 0 AS INTEGER))
            / nullif(sum(validation_weight), 0) AS actual_win_rate
    FROM return_validation_test_trades
    WHERE validation_weight > 0
    GROUP BY
        symbol,
        fold,
        move_bucket,
        surface_method,
        target_move,
        move_min,
        move_max,
        bandwidth,
        training_signals,
        training_through,
        buy_day,
        sell_day,
        holding_days,
        training_observations,
        effective_observations,
        expected_return_after_cost,
        expected_win_rate,
        conservative_return
)
SELECT
    *,
    actual_mean_return - expected_return_after_cost AS calibration_error,
    sign(actual_mean_return) = sign(expected_return_after_cost)
        AS direction_correct
FROM aggregated
WHERE validation_observations >= 5;

CREATE OR REPLACE TEMP TABLE return_validation_examples AS
SELECT
    symbol,
    fold,
    move_bucket,
    surface_method,
    target_move,
    signal_date,
    actual_initial_move,
    buy_day,
    sell_day,
    actual_return_after_cost,
    validation_weight
FROM return_validation_test_trades
WHERE validation_weight > 0
QUALIFY row_number() OVER (
    PARTITION BY symbol, fold, move_bucket
    ORDER BY validation_weight DESC, signal_date DESC
) <= 3;

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

-- Where the news is, per symbol, and where it stops being usable.
--
-- Two different shortfalls look identical in the totals: a symbol nobody
-- writes about, and a symbol that is written about but whose articles never
-- reach a price series. The first is a crawl problem and the second is a
-- corpus problem, and they are fixed in different places, so they are
-- counted separately here rather than summed into one coverage number.
CREATE OR REPLACE TEMP TABLE symbol_news_coverage AS
WITH universe AS (
    SELECT symbol, subsector FROM sectors_input
    UNION
    SELECT DISTINCT symbol, CAST(NULL AS VARCHAR) AS subsector
    FROM events_timed_input
    WHERE symbol NOT IN (SELECT symbol FROM sectors_input)
),
priced AS (
    SELECT symbol, count(*) AS sessions, min(date) AS first_session, max(date) AS last_session
    FROM daily_indexed
    GROUP BY symbol
),
mentioned AS (
    SELECT symbol, count(*) AS events, min(event_date) AS first_event, max(event_date) AS last_event
    FROM events_timed_input
    GROUP BY symbol
),
connected AS (
    SELECT symbol, count(*) AS matched_events
    FROM aligned_events
    GROUP BY symbol
),
lost AS (
    SELECT
        symbol,
        count(*) AS unmatched_events,
        count(*) FILTER (WHERE reason = 'symbol_not_in_daily_corpus') AS no_price_series,
        count(*) FILTER (WHERE reason = 'no_session_on_or_after_candidate')
            AS no_session_after_event
    FROM event_unmatched
    GROUP BY symbol
)
SELECT
    universe.symbol,
    universe.subsector,
    coalesce(mentioned.events, 0) AS events,
    coalesce(connected.matched_events, 0) AS matched_events,
    coalesce(lost.unmatched_events, 0) AS unmatched_events,
    coalesce(lost.no_price_series, 0) AS no_price_series,
    coalesce(lost.no_session_after_event, 0) AS no_session_after_event,
    coalesce(priced.sessions, 0) AS sessions,
    priced.first_session,
    priced.last_session,
    mentioned.first_event,
    mentioned.last_event,
    CASE
        WHEN coalesce(mentioned.events, 0) = 0 THEN NULL
        ELSE coalesce(connected.matched_events, 0)::DOUBLE / mentioned.events
    END AS attach_rate,
    -- One diagnosis per symbol, chosen in the order the problems have to be
    -- fixed in: a symbol with no price series cannot connect anything, so
    -- that is reported before the article count.
    CASE
        WHEN coalesce(priced.sessions, 0) = 0 THEN 'no_price_series'
        WHEN coalesce(lost.no_price_series, 0) > 0 THEN 'partly_unpriced'
        WHEN coalesce(mentioned.events, 0) = 0 THEN 'no_articles'
        WHEN coalesce(lost.unmatched_events, 0) > 0 THEN 'some_events_dropped'
        -- Below a year of sessions the conditional surfaces stay mostly
        -- blank whatever the news does, because the overlap correction
        -- divides the effective sample by the horizon.
        WHEN coalesce(priced.sessions, 0) < 252 THEN 'short_price_history'
        WHEN coalesce(mentioned.events, 0) < 5 THEN 'few_articles'
        ELSE 'ok'
    END AS diagnosis
FROM universe
LEFT JOIN priced USING (symbol)
LEFT JOIN mentioned USING (symbol)
LEFT JOIN connected USING (symbol)
LEFT JOIN lost USING (symbol);
