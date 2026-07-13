from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from pydantic import BaseModel

from .models import MarketSnapshot


UTC = timezone.utc
MAX_TARGET_HORIZON = timedelta(hours=24)
ONE_MINUTE = timedelta(minutes=1)


class PredictionMinute(BaseModel):
    venue: str
    symbol: str
    underlying: str
    minute_at: datetime
    first_observed_at: datetime
    last_observed_at: datetime
    source_observed_at: datetime | None = None
    target_funding_at: datetime
    target_source: Literal["api", "schedule"]
    raw_rate_close: float
    raw_rate_unit: Literal["decimal", "percent"]
    source_tenor_hours: float
    normalized_rate_open: float
    normalized_rate_high: float
    normalized_rate_low: float
    normalized_rate_close: float
    settlement_interval_hours: float
    sample_count: int
    mark_price_close: float | None = None
    index_price_close: float | None = None
    transform_version: str


class PredictionStore(Protocol):
    def record_prediction_observation(
        self,
        venue: str,
        expected_symbols: int,
        sampled_symbols: int,
        observed_at: datetime,
    ) -> None: ...

    def record_prediction_failure(
        self,
        venue: str,
        error: str,
        expected_symbols: int = 0,
        observed_at: datetime | None = None,
    ) -> None: ...

    def flush_prediction_minutes(
        self,
        venue: str,
        minute_at: datetime,
        items: list[PredictionMinute],
        expected_symbols: int,
        sampled_symbols: int,
        status_window_minutes: int = 60,
    ) -> bool: ...


BucketKey = tuple[str, str, datetime, datetime]
MinuteKey = tuple[str, datetime]


def _as_utc(value: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _minute_start(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _valid_price(value: float | None) -> bool:
    return value is None or (math.isfinite(value) and value > 0)


@dataclass(frozen=True, slots=True)
class _ValidatedSnapshot:
    snapshot: MarketSnapshot
    observed_at: datetime
    source_observed_at: datetime | None
    target_funding_at: datetime
    minute_at: datetime
    raw_rate: float
    normalized_rate: float
    settlement_interval_hours: float
    source_tenor_hours: float


@dataclass(slots=True)
class _MinuteCoverage:
    expected_symbols: int
    sampled_symbols: set[str]


@dataclass(slots=True)
class _PredictionBucket:
    venue: str
    symbol: str
    underlying: str
    minute_at: datetime
    target_funding_at: datetime
    first_observed_at: datetime
    last_observed_at: datetime
    source_observed_at: datetime | None
    target_source: str
    raw_rate_close: float
    raw_rate_unit: str
    source_tenor_hours: float
    normalized_rate_open: float
    normalized_rate_high: float
    normalized_rate_low: float
    normalized_rate_close: float
    settlement_interval_hours: float
    sample_count: int
    mark_price_close: float | None
    index_price_close: float | None
    transform_version: str

    @classmethod
    def from_snapshot(cls, item: _ValidatedSnapshot) -> _PredictionBucket:
        snapshot = item.snapshot
        return cls(
            venue=snapshot.venue,
            symbol=snapshot.symbol,
            underlying=snapshot.underlying,
            minute_at=item.minute_at,
            target_funding_at=item.target_funding_at,
            first_observed_at=item.observed_at,
            last_observed_at=item.observed_at,
            source_observed_at=item.source_observed_at,
            target_source=snapshot.target_source,
            raw_rate_close=item.raw_rate,
            raw_rate_unit=snapshot.raw_rate_unit,
            source_tenor_hours=item.source_tenor_hours,
            normalized_rate_open=item.normalized_rate,
            normalized_rate_high=item.normalized_rate,
            normalized_rate_low=item.normalized_rate,
            normalized_rate_close=item.normalized_rate,
            settlement_interval_hours=item.settlement_interval_hours,
            sample_count=1,
            mark_price_close=snapshot.mark_price,
            index_price_close=snapshot.index_price,
            transform_version=snapshot.transform_version,
        )

    def add(self, item: _ValidatedSnapshot) -> None:
        snapshot = item.snapshot
        rate = item.normalized_rate
        self.sample_count += 1
        self.normalized_rate_high = max(self.normalized_rate_high, rate)
        self.normalized_rate_low = min(self.normalized_rate_low, rate)

        if item.observed_at < self.first_observed_at:
            self.first_observed_at = item.observed_at
            self.normalized_rate_open = rate

        # Equal timestamps deliberately keep the first accepted close. There is
        # no source sequence number with which to order equal-time packets.
        if item.observed_at > self.last_observed_at:
            self.last_observed_at = item.observed_at
            self.source_observed_at = item.source_observed_at
            self.target_source = snapshot.target_source
            self.raw_rate_close = item.raw_rate
            self.raw_rate_unit = snapshot.raw_rate_unit
            self.source_tenor_hours = item.source_tenor_hours
            self.normalized_rate_close = rate
            self.settlement_interval_hours = item.settlement_interval_hours
            self.mark_price_close = snapshot.mark_price
            self.index_price_close = snapshot.index_price
            self.transform_version = snapshot.transform_version

    def to_model(self) -> PredictionMinute:
        return PredictionMinute(
            venue=self.venue,
            symbol=self.symbol,
            underlying=self.underlying,
            minute_at=self.minute_at,
            first_observed_at=self.first_observed_at,
            last_observed_at=self.last_observed_at,
            source_observed_at=self.source_observed_at,
            target_funding_at=self.target_funding_at,
            target_source=self.target_source,
            raw_rate_close=self.raw_rate_close,
            raw_rate_unit=self.raw_rate_unit,
            source_tenor_hours=self.source_tenor_hours,
            normalized_rate_open=self.normalized_rate_open,
            normalized_rate_high=self.normalized_rate_high,
            normalized_rate_low=self.normalized_rate_low,
            normalized_rate_close=self.normalized_rate_close,
            settlement_interval_hours=self.settlement_interval_hours,
            sample_count=self.sample_count,
            mark_price_close=self.mark_price_close,
            index_price_close=self.index_price_close,
            transform_version=self.transform_version,
        )


class MinutePredictionCollector:
    """Aggregate valid indicative funding snapshots into UTC minute bars.

    The collector intentionally does not carry a prior observation into a new
    minute. A row exists only when the venue supplied at least one valid sample
    for that symbol, target settlement and UTC minute.
    """

    def __init__(
        self,
        store: PredictionStore,
        status_window_minutes: int = 60,
    ) -> None:
        if status_window_minutes <= 0:
            raise ValueError("status_window_minutes must be positive")
        self.store = store
        self.status_window_minutes = int(status_window_minutes)
        self._buckets: dict[BucketKey, _PredictionBucket] = {}
        self._coverage: dict[MinuteKey, _MinuteCoverage] = {}
        self._lock = threading.RLock()

    def ingest(
        self,
        venue: str,
        snapshots: list[MarketSnapshot],
        expected_symbols: int,
    ) -> int:
        expected = max(int(expected_symbols), 0)
        valid = [
            item
            for snapshot in snapshots
            if (item := self._validate(venue, snapshot)) is not None
        ]

        sampled = len({item.snapshot.symbol for item in valid})
        observation_at = (
            max(item.observed_at for item in valid)
            if valid
            else datetime.now(UTC)
        )

        # Persist collection coverage before mutating in-memory minute state.
        # If this write fails, the caller can record the failed collection and
        # none of its samples will later be flushed without an observation row.
        self.store.record_prediction_observation(
            venue,
            expected,
            sampled,
            observation_at,
        )

        with self._lock:
            for item in valid:
                minute_key = (venue, item.minute_at)
                coverage = self._coverage.get(minute_key)
                if coverage is None:
                    coverage = _MinuteCoverage(
                        expected_symbols=expected,
                        sampled_symbols=set(),
                    )
                    self._coverage[minute_key] = coverage
                else:
                    coverage.expected_symbols = max(
                        coverage.expected_symbols,
                        expected,
                    )
                coverage.sampled_symbols.add(item.snapshot.symbol)

                key = (
                    venue,
                    item.snapshot.symbol,
                    item.minute_at,
                    item.target_funding_at,
                )
                bucket = self._buckets.get(key)
                if bucket is None:
                    self._buckets[key] = _PredictionBucket.from_snapshot(item)
                else:
                    bucket.add(item)
        return len(valid)

    def flush_completed(self, now: datetime) -> int:
        utc_now = _as_utc(now)
        if utc_now is None:
            raise ValueError("now must be timezone-aware")

        flushed = 0
        with self._lock:
            complete_minutes = sorted(
                {
                    (key[0], key[2])
                    for key in self._buckets
                    if key[2] + ONE_MINUTE <= utc_now
                },
                key=lambda item: (item[1], item[0]),
            )
            for venue, minute_at in complete_minutes:
                bucket_keys = sorted(
                    (
                        key
                        for key in self._buckets
                        if key[0] == venue and key[2] == minute_at
                    ),
                    key=lambda key: (key[1], key[3]),
                )
                items = [self._buckets[key].to_model() for key in bucket_keys]
                coverage = self._coverage.get((venue, minute_at))
                expected = coverage.expected_symbols if coverage else 0
                sampled = (
                    len(coverage.sampled_symbols)
                    if coverage
                    else len({item.symbol for item in items})
                )

                # Only an exception is retryable. False means the store's
                # persisted watermark already covers this minute, so retaining
                # it would create an in-memory retry loop for late packets.
                persisted = self.store.flush_prediction_minutes(
                    venue,
                    minute_at,
                    items,
                    expected,
                    sampled,
                    self.status_window_minutes,
                )
                for key in bucket_keys:
                    del self._buckets[key]
                self._coverage.pop((venue, minute_at), None)
                if persisted:
                    flushed += len(items)
        return flushed

    def note_failure(
        self,
        venue: str,
        error: str,
        expected_symbols: int = 0,
        observed_at: datetime | None = None,
    ) -> None:
        timestamp = observed_at or datetime.now(UTC)
        utc_timestamp = _as_utc(timestamp)
        if utc_timestamp is None:
            raise ValueError("observed_at must be timezone-aware")
        self.store.record_prediction_failure(
            venue,
            error,
            max(int(expected_symbols), 0),
            utc_timestamp,
        )

    @staticmethod
    def _validate(
        venue: str,
        snapshot: MarketSnapshot,
    ) -> _ValidatedSnapshot | None:
        if snapshot.venue != venue or not snapshot.symbol or not snapshot.underlying:
            return None

        observed_at = _as_utc(snapshot.observed_at)
        target_funding_at = (
            _as_utc(snapshot.next_funding_at)
            if snapshot.next_funding_at is not None
            else None
        )
        if observed_at is None or target_funding_at is None:
            return None
        horizon = target_funding_at - observed_at
        if horizon <= timedelta(0) or horizon > MAX_TARGET_HORIZON:
            return None

        normalized_rate = snapshot.funding_rate
        raw_rate = (
            snapshot.raw_funding_rate
            if snapshot.raw_funding_rate is not None
            else normalized_rate
        )
        interval = snapshot.funding_interval_hours
        if (
            not _finite(normalized_rate)
            or not _finite(raw_rate)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            return None

        source_tenor = (
            snapshot.source_tenor_hours
            if snapshot.source_tenor_hours is not None
            else interval
        )
        if not math.isfinite(source_tenor) or source_tenor <= 0:
            return None
        if not _valid_price(snapshot.mark_price) or not _valid_price(
            snapshot.index_price
        ):
            return None

        source_observed_at = (
            _as_utc(snapshot.source_observed_at)
            if snapshot.source_observed_at is not None
            else None
        )
        if snapshot.source_observed_at is not None and source_observed_at is None:
            return None

        return _ValidatedSnapshot(
            snapshot=snapshot,
            observed_at=observed_at,
            source_observed_at=source_observed_at,
            target_funding_at=target_funding_at,
            minute_at=_minute_start(observed_at),
            raw_rate=float(raw_rate),
            normalized_rate=float(normalized_rate),
            settlement_interval_hours=float(interval),
            source_tenor_hours=float(source_tenor),
        )
