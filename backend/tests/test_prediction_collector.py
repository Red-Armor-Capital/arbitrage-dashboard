from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models import MarketSnapshot
from backend.app.prediction import MinutePredictionCollector, PredictionMinute


UTC = timezone.utc
MINUTE = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
TARGET = datetime(2026, 7, 11, 11, 0, tzinfo=UTC)


class FakePredictionStore:
    def __init__(self) -> None:
        self.observations: list[tuple[str, int, int, datetime]] = []
        self.failures: list[tuple[str, str, int, datetime | None]] = []
        self.flush_attempts: list[
            tuple[str, datetime, list[PredictionMinute], int, int]
        ] = []
        self.flushes: list[
            tuple[str, datetime, list[PredictionMinute], int, int]
        ] = []
        self.flush_windows: list[int] = []
        self.observation_error: Exception | None = None
        self.flush_errors_remaining = 0
        self.accept_flush = True

    def record_prediction_observation(
        self,
        venue: str,
        expected_symbols: int,
        sampled_symbols: int,
        observed_at: datetime,
    ) -> None:
        if self.observation_error is not None:
            raise self.observation_error
        self.observations.append(
            (venue, expected_symbols, sampled_symbols, observed_at)
        )

    def record_prediction_failure(
        self,
        venue: str,
        error: str,
        expected_symbols: int = 0,
        observed_at: datetime | None = None,
    ) -> None:
        self.failures.append((venue, error, expected_symbols, observed_at))

    def flush_prediction_minutes(
        self,
        venue: str,
        minute_at: datetime,
        items: list[PredictionMinute],
        expected_symbols: int,
        sampled_symbols: int,
        status_window_minutes: int = 60,
    ) -> bool:
        attempt = (venue, minute_at, items, expected_symbols, sampled_symbols)
        self.flush_attempts.append(attempt)
        self.flush_windows.append(status_window_minutes)
        if self.flush_errors_remaining:
            self.flush_errors_remaining -= 1
            raise RuntimeError("temporary write failure")
        if self.accept_flush:
            self.flushes.append(attempt)
        return self.accept_flush


def snapshot(
    *,
    second: int,
    rate: float | None,
    target: datetime = TARGET,
    symbol: str = "NVDA-USD",
    venue: str = "lighter",
    raw_rate: float | None = None,
    interval: float = 1,
    source_tenor: float | None = 1,
    mark: float | None = 100,
    index: float | None = 99,
    observed_at: datetime | None = None,
) -> MarketSnapshot:
    observed = observed_at or MINUTE + timedelta(seconds=second)
    return MarketSnapshot(
        venue=venue,
        symbol=symbol,
        underlying="NVDA",
        observed_at=observed,
        source_observed_at=(
            observed - timedelta(seconds=1) if observed.tzinfo is not None else None
        ),
        mark_price=mark,
        index_price=index,
        funding_rate=rate,
        funding_interval_hours=interval,
        next_funding_at=target,
        target_source="api",
        raw_funding_rate=raw_rate if raw_rate is not None else rate,
        raw_rate_unit="percent",
        source_tenor_hours=source_tenor,
        transform_version="lighter-percent-hourly-v2",
    )


def flushed_items(store: FakePredictionStore) -> list[PredictionMinute]:
    assert len(store.flushes) == 1
    return store.flushes[0][2]


def test_aggregates_minute_ohlc_and_close_provenance() -> None:
    store = FakePredictionStore()
    collector = MinutePredictionCollector(store, status_window_minutes=30)
    rows = [
        snapshot(second=5, rate=0.001, raw_rate=0.1, mark=101, index=100),
        snapshot(second=25, rate=0.003, raw_rate=0.3, mark=103, index=102),
        snapshot(second=45, rate=0.002, raw_rate=0.2, mark=102, index=101),
    ]

    assert collector.ingest("lighter", rows, expected_symbols=5) == 3
    assert store.observations == [("lighter", 5, 1, MINUTE + timedelta(seconds=45))]
    assert collector.flush_completed(MINUTE + timedelta(minutes=1)) == 1

    item = flushed_items(store)[0]
    assert item.minute_at == MINUTE
    assert item.first_observed_at == MINUTE + timedelta(seconds=5)
    assert item.last_observed_at == MINUTE + timedelta(seconds=45)
    assert item.source_observed_at == MINUTE + timedelta(seconds=44)
    assert item.target_funding_at == TARGET
    assert item.normalized_rate_open == pytest.approx(0.001)
    assert item.normalized_rate_high == pytest.approx(0.003)
    assert item.normalized_rate_low == pytest.approx(0.001)
    assert item.normalized_rate_close == pytest.approx(0.002)
    assert item.raw_rate_close == pytest.approx(0.2)
    assert item.sample_count == 3
    assert item.mark_price_close == pytest.approx(102)
    assert item.index_price_close == pytest.approx(101)
    assert store.flushes[0][3:] == (5, 1)
    assert store.flush_windows == [30]


def test_target_change_splits_buckets_without_double_counting_symbol_coverage() -> None:
    store = FakePredictionStore()
    collector = MinutePredictionCollector(store)
    later_target = TARGET + timedelta(hours=1)

    collector.ingest(
        "lighter",
        [
            snapshot(second=10, rate=0.001, target=TARGET),
            snapshot(second=40, rate=0.002, target=later_target),
        ],
        expected_symbols=4,
    )
    assert collector.flush_completed(MINUTE + timedelta(minutes=1)) == 2

    items = flushed_items(store)
    assert [item.target_funding_at for item in items] == [TARGET, later_target]
    assert [item.sample_count for item in items] == [1, 1]
    assert store.flushes[0][3:] == (4, 1)


def test_incomplete_minute_is_not_flushed_and_empty_poll_does_not_carry() -> None:
    store = FakePredictionStore()
    collector = MinutePredictionCollector(store)
    collector.ingest(
        "lighter",
        [snapshot(second=50, rate=0.001)],
        expected_symbols=1,
    )

    assert collector.flush_completed(MINUTE + timedelta(seconds=59)) == 0
    assert store.flushes == []
    assert collector.flush_completed(MINUTE + timedelta(minutes=1)) == 1

    collector.ingest("lighter", [], expected_symbols=1)
    assert collector.flush_completed(MINUTE + timedelta(minutes=2)) == 0
    assert len(store.flushes) == 1
    assert store.observations[-1][2] == 0


def test_out_of_order_packet_updates_open_but_cannot_overwrite_close() -> None:
    store = FakePredictionStore()
    collector = MinutePredictionCollector(store)
    collector.ingest(
        "lighter",
        [snapshot(second=50, rate=0.003, raw_rate=0.3, mark=130)],
        expected_symbols=1,
    )
    collector.ingest(
        "lighter",
        [snapshot(second=10, rate=0.001, raw_rate=0.1, mark=110)],
        expected_symbols=1,
    )

    collector.flush_completed(MINUTE + timedelta(minutes=1))
    item = flushed_items(store)[0]
    assert item.first_observed_at == MINUTE + timedelta(seconds=10)
    assert item.last_observed_at == MINUTE + timedelta(seconds=50)
    assert item.normalized_rate_open == pytest.approx(0.001)
    assert item.normalized_rate_close == pytest.approx(0.003)
    assert item.raw_rate_close == pytest.approx(0.3)
    assert item.mark_price_close == pytest.approx(130)


def test_invalid_values_are_skipped() -> None:
    store = FakePredictionStore()
    collector = MinutePredictionCollector(store)
    invalid = [
        snapshot(second=1, rate=None),
        snapshot(second=2, rate=float("nan")),
        snapshot(second=3, rate=0.001, raw_rate=float("inf")),
        snapshot(second=4, rate=0.001, interval=0),
        snapshot(second=5, rate=0.001, source_tenor=0),
        snapshot(second=6, rate=0.001, target=MINUTE + timedelta(seconds=6)),
        snapshot(second=7, rate=0.001, target=MINUTE + timedelta(hours=25)),
        snapshot(second=8, rate=0.001, mark=0),
        snapshot(second=9, rate=0.001, index=float("inf")),
        snapshot(second=10, rate=0.001, venue="extended"),
        snapshot(
            second=11,
            rate=0.001,
            observed_at=datetime(2026, 7, 11, 10, 0, 11),
        ),
    ]

    assert collector.ingest("lighter", invalid, expected_symbols=11) == 0
    assert store.observations[0][1:3] == (11, 0)
    assert collector.flush_completed(MINUTE + timedelta(minutes=1)) == 0
    assert store.flush_attempts == []


def test_observation_is_recorded_before_aggregation() -> None:
    store = FakePredictionStore()
    store.observation_error = RuntimeError("coverage write failed")
    collector = MinutePredictionCollector(store)

    with pytest.raises(RuntimeError, match="coverage write failed"):
        collector.ingest(
            "lighter",
            [snapshot(second=5, rate=0.001)],
            expected_symbols=1,
        )

    store.observation_error = None
    assert collector.flush_completed(MINUTE + timedelta(minutes=1)) == 0
    assert store.flush_attempts == []


def test_failed_flush_preserves_complete_bucket_for_retry() -> None:
    store = FakePredictionStore()
    store.flush_errors_remaining = 1
    collector = MinutePredictionCollector(store)
    collector.ingest(
        "lighter",
        [snapshot(second=5, rate=0.001)],
        expected_symbols=1,
    )

    with pytest.raises(RuntimeError, match="temporary write failure"):
        collector.flush_completed(MINUTE + timedelta(minutes=1))

    assert collector.flush_completed(MINUTE + timedelta(minutes=2)) == 1
    assert len(store.flush_attempts) == 2
    assert len(store.flushes) == 1
    assert store.flushes[0][2][0].normalized_rate_close == pytest.approx(0.001)


def test_watermark_rejection_discards_bucket_and_failure_note_is_recorded() -> None:
    store = FakePredictionStore()
    store.accept_flush = False
    collector = MinutePredictionCollector(store)
    collector.ingest(
        "lighter",
        [snapshot(second=5, rate=0.001)],
        expected_symbols=2,
    )

    assert collector.flush_completed(MINUTE + timedelta(minutes=1)) == 0
    store.accept_flush = True
    assert collector.flush_completed(MINUTE + timedelta(minutes=2)) == 0
    assert len(store.flush_attempts) == 1

    failed_at = MINUTE + timedelta(minutes=3)
    collector.note_failure(
        "lighter",
        "network timeout",
        expected_symbols=2,
        observed_at=failed_at,
    )
    assert store.failures == [("lighter", "network timeout", 2, failed_at)]
