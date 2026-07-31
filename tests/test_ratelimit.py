"""REST budget and bandwidth meter (spec-review A-1, A-2)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from usstocks.collector.ratelimit import BandwidthMeter, RestBudget
from usstocks.db.repository import Repository


def budget(repo: Repository, **kwargs) -> RestBudget:
    options = {"per_hour": 5, "per_day": 10, "monthly_bandwidth_bytes": 1_000}
    options.update(kwargs)
    return RestBudget(repo, "tiingo", **options)


def test_hourly_limit_blocks_further_calls(repo: Repository):
    bucket = budget(repo)
    now = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    assert all(bucket.try_acquire(now=now) for _ in range(5))
    assert bucket.try_acquire(now=now) is False


def test_hour_rolls_over_but_day_keeps_counting(repo: Repository):
    bucket = budget(repo)
    first = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    for _ in range(5):
        bucket.try_acquire(now=first)

    second = first + timedelta(hours=1)
    assert bucket.try_acquire(now=second) is True

    snapshot = bucket.snapshot(second)
    assert snapshot.calls_hour == 1
    assert snapshot.calls_day == 6


def test_daily_limit_is_enforced(repo: Repository):
    bucket = budget(repo, per_hour=100, per_day=3)
    now = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    assert sum(bucket.try_acquire(now=now) for _ in range(5)) == 3


def test_consumption_survives_a_restart(db_path):
    """The whole reason the counter lives in SQLite: a crash loop must not
    reset the allowance (spec-review A-2)."""
    now = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    with Repository(db_path) as repo:
        first = budget(repo)
        for _ in range(5):
            first.try_acquire(now=now)

    with Repository(db_path) as repo:
        second = budget(repo)
        assert second.try_acquire(now=now) is False
        assert second.snapshot(now).calls_hour == 5


def test_separate_process_buckets_cannot_spend_the_same_final_token(repo):
    """SQLite serialises the check-and-bump across process-like connections."""
    now = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    buckets = [budget(repo, per_hour=1, per_day=1) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=len(buckets)) as pool:
        results = list(pool.map(lambda bucket: bucket.try_acquire(now=now), buckets))

    assert sum(results) == 1


def test_bandwidth_accumulates_month_to_date(repo: Repository):
    bucket = budget(repo)
    now = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    bucket.record_bytes(400, now=now)
    bucket.record_bytes(350, now=now + timedelta(days=1))

    assert bucket.bytes_this_month(now) == 750
    assert bucket.bytes_today(now) == 400
    assert bucket.snapshot(now).bandwidth_ratio == 0.75


def test_bandwidth_meter_converts_cumulative_totals_to_deltas(repo: Repository):
    bucket = budget(repo)
    meter = BandwidthMeter(bucket, warn_ratio=0.8)

    meter.update(100)
    meter.update(250)
    meter.update(250)  # no change

    assert bucket.bytes_this_month() == 250


def test_bandwidth_meter_warns_once_past_the_threshold(repo: Repository, caplog):
    bucket = budget(repo)
    meter = BandwidthMeter(bucket, warn_ratio=0.8)

    with caplog.at_level("WARNING"):
        meter.update(900)
        meter.update(950)

    warnings = [record for record in caplog.records if "monthly ingress" in record.message]
    assert len(warnings) == 1
