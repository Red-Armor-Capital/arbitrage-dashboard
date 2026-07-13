from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from .models import FundingRate, Instrument, MarketSnapshot, VenueStatus
from .prediction import PredictionMinute


PREDICTION_MIGRATION_VERSION = "20260711_01_prediction_minutes"
HOTSTUFF_HOURLY_MIGRATION_VERSION = "20260712_02_hotstuff_ticker_hourly"
PREDICTION_MINUTE_COLUMNS = (
    "venue",
    "symbol",
    "underlying",
    "minute_at",
    "first_observed_at",
    "last_observed_at",
    "source_observed_at",
    "target_funding_at",
    "target_source",
    "raw_rate_close",
    "raw_rate_unit",
    "source_tenor_hours",
    "normalized_rate_open",
    "normalized_rate_high",
    "normalized_rate_low",
    "normalized_rate_close",
    "settlement_interval_hours",
    "sample_count",
    "mark_price_close",
    "index_price_close",
    "transform_version",
)


class CarryStore:
    _ASSET_CLASSES = {"stock", "etf", "index", "preipo", "basket", "unknown"}

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path))

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    venue VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    underlying VARCHAR NOT NULL,
                    display_name VARCHAR,
                    product_type VARCHAR NOT NULL,
                    quote_currency VARCHAR NOT NULL,
                    funding_interval_hours DOUBLE NOT NULL,
                    maker_fee DOUBLE NOT NULL,
                    taker_fee DOUBLE NOT NULL,
                    active BOOLEAN NOT NULL,
                    metadata_json VARCHAR NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (venue, symbol)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS current_market (
                    venue VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    underlying VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    bid DOUBLE,
                    ask DOUBLE,
                    mark_price DOUBLE,
                    index_price DOUBLE,
                    funding_rate DOUBLE,
                    funding_interval_hours DOUBLE NOT NULL,
                    next_funding_at TIMESTAMPTZ,
                    open_interest DOUBLE,
                    volume_24h DOUBLE,
                    PRIMARY KEY (venue, symbol)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS funding_rates (
                    venue VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    underlying VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL,
                    effective_at TIMESTAMPTZ NOT NULL,
                    rate DOUBLE NOT NULL,
                    interval_hours DOUBLE NOT NULL,
                    kind VARCHAR NOT NULL,
                    PRIMARY KEY (venue, symbol, effective_at, kind)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS venue_status (
                    venue VARCHAR PRIMARY KEY,
                    status VARCHAR NOT NULL,
                    last_success_at TIMESTAMPTZ,
                    last_error VARCHAR,
                    instruments INTEGER NOT NULL,
                    latency_ms DOUBLE
                )
                """
            )
            self._apply_migrations(conn)

    @staticmethod
    def _apply_migrations(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        already_applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (PREDICTION_MIGRATION_VERSION,),
        ).fetchone()
        if not already_applied:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(
                    """
                    CREATE TABLE funding_prediction_minutes (
                        venue VARCHAR NOT NULL,
                        symbol VARCHAR NOT NULL,
                        underlying VARCHAR NOT NULL,
                        minute_at TIMESTAMPTZ NOT NULL,
                        first_observed_at TIMESTAMPTZ NOT NULL,
                        last_observed_at TIMESTAMPTZ NOT NULL,
                        source_observed_at TIMESTAMPTZ,
                        target_funding_at TIMESTAMPTZ NOT NULL,
                        target_source VARCHAR NOT NULL,
                        raw_rate_close DOUBLE NOT NULL,
                        raw_rate_unit VARCHAR NOT NULL,
                        source_tenor_hours DOUBLE NOT NULL,
                        normalized_rate_open DOUBLE NOT NULL,
                        normalized_rate_high DOUBLE NOT NULL,
                        normalized_rate_low DOUBLE NOT NULL,
                        normalized_rate_close DOUBLE NOT NULL,
                        settlement_interval_hours DOUBLE NOT NULL,
                        sample_count INTEGER NOT NULL,
                        mark_price_close DOUBLE,
                        index_price_close DOUBLE,
                        transform_version VARCHAR NOT NULL,
                        CHECK (source_tenor_hours > 0),
                        CHECK (settlement_interval_hours > 0),
                        CHECK (sample_count > 0)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE funding_prediction_collector_status (
                        venue VARCHAR PRIMARY KEY,
                        status VARCHAR NOT NULL,
                        last_success_at TIMESTAMPTZ,
                        last_flushed_minute TIMESTAMPTZ,
                        expected_symbols INTEGER NOT NULL,
                        sampled_symbols_last_minute INTEGER NOT NULL,
                        coverage_60m DOUBLE NOT NULL,
                        missed_minutes_60m INTEGER NOT NULL,
                        hot_rows BIGINT NOT NULL,
                        archived_rows BIGINT NOT NULL,
                        last_error VARCHAR,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE funding_prediction_archives (
                        month_start DATE PRIMARY KEY,
                        file_path VARCHAR NOT NULL,
                        row_count BIGINT NOT NULL,
                        min_minute_at TIMESTAMPTZ NOT NULL,
                        max_minute_at TIMESTAMPTZ NOT NULL,
                        file_size_bytes BIGINT NOT NULL,
                        status VARCHAR NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        validated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    (PREDICTION_MIGRATION_VERSION, datetime.now(timezone.utc)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        hotstuff_migration_applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (HOTSTUFF_HOURLY_MIGRATION_VERSION,),
        ).fetchone()
        if hotstuff_migration_applied:
            return

        conn.execute("BEGIN TRANSACTION")
        try:
            invalid_legacy_rows = conn.execute(
                """
                SELECT COUNT(*)
                FROM funding_prediction_minutes
                WHERE venue = 'hotstuff'
                  AND transform_version = 'eight-hour-to-hourly-v1'
                  AND (
                      source_tenor_hours <> 8
                      OR raw_rate_unit <> 'decimal'
                      OR settlement_interval_hours <> 1
                      OR NOT isfinite(raw_rate_close)
                      OR NOT isfinite(normalized_rate_open)
                      OR NOT isfinite(normalized_rate_high)
                      OR NOT isfinite(normalized_rate_low)
                      OR NOT isfinite(normalized_rate_close)
                      OR abs(normalized_rate_close * 8 - raw_rate_close)
                         > greatest(1e-15, abs(raw_rate_close) * 1e-12)
                  )
                """
            ).fetchone()[0]
            if invalid_legacy_rows:
                raise RuntimeError(
                    "Hotstuff hourly migration found legacy rows with an "
                    "unexpected rate transform"
                )

            conn.execute(
                """
                UPDATE funding_prediction_minutes
                SET source_tenor_hours = 1,
                    normalized_rate_open = normalized_rate_open * 8,
                    normalized_rate_high = normalized_rate_high * 8,
                    normalized_rate_low = normalized_rate_low * 8,
                    normalized_rate_close = raw_rate_close,
                    transform_version = 'identity-v1'
                WHERE venue = 'hotstuff'
                  AND source_tenor_hours = 8
                  AND transform_version = 'eight-hour-to-hourly-v1'
                  AND raw_rate_unit = 'decimal'
                  AND settlement_interval_hours = 1
                """
            )
            conn.execute(
                """
                UPDATE current_market
                SET funding_rate = funding_rate * 8
                WHERE venue = 'hotstuff'
                  AND funding_rate IS NOT NULL
                """
            )
            conn.execute(
                """
                UPDATE funding_rates
                SET rate = rate * 8
                WHERE venue = 'hotstuff'
                  AND kind = 'current'
                """
            )
            conn.execute(
                """
                UPDATE instruments
                SET metadata_json = json_merge_patch(
                    metadata_json,
                    '{"current_rate_tenor_hours":1,"ticker_rate_semantics":"hourly_payment_rate"}'
                )
                WHERE venue = 'hotstuff'
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (HOTSTUFF_HOURLY_MIGRATION_VERSION, datetime.now(timezone.utc)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def upsert_instruments(self, items: list[Instrument]) -> None:
        if not items:
            return
        rows = self._instrument_rows(items)
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO instruments (
                    venue, symbol, underlying, display_name, product_type,
                    quote_currency, funding_interval_hours, maker_fee, taker_fee,
                    active, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def sync_instruments(self, venue: str, items: list[Instrument]) -> None:
        """Replace one venue's active catalog without deleting its historical rows.

        The inactive sweep and current-catalog upsert share one transaction, so a
        failed upsert cannot leave a previously healthy catalog disabled.
        """
        if any(item.venue != venue for item in items):
            raise ValueError("all instruments must belong to the synchronized venue")
        rows = self._instrument_rows(items)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(
                    "UPDATE instruments SET active = FALSE WHERE venue = ?",
                    (venue,),
                )
                if rows:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO instruments (
                            venue, symbol, underlying, display_name, product_type,
                            quote_currency, funding_interval_hours, maker_fee, taker_fee,
                            active, metadata_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _instrument_rows(items: list[Instrument]) -> list[tuple]:
        return [
            (
                item.venue,
                item.symbol,
                item.underlying,
                item.display_name,
                item.product_type,
                item.quote_currency,
                item.funding_interval_hours,
                item.maker_fee,
                item.taker_fee,
                item.active,
                json.dumps(item.metadata, ensure_ascii=False),
                item.updated_at,
            )
            for item in items
        ]

    def upsert_snapshots(self, items: list[MarketSnapshot]) -> None:
        if not items:
            return
        rows = [
            (
                item.venue,
                item.symbol,
                item.underlying,
                item.observed_at,
                item.bid,
                item.ask,
                item.mark_price,
                item.index_price,
                item.funding_rate,
                item.funding_interval_hours,
                item.next_funding_at,
                item.open_interest,
                item.volume_24h,
            )
            for item in items
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO current_market (
                    venue, symbol, underlying, observed_at, bid, ask, mark_price,
                    index_price, funding_rate, funding_interval_hours,
                    next_funding_at, open_interest, volume_24h
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def upsert_funding(self, items: list[FundingRate]) -> None:
        if not items:
            return
        rows = [
            (
                item.venue,
                item.symbol,
                item.underlying,
                item.observed_at,
                item.effective_at,
                item.rate,
                item.interval_hours,
                item.kind,
            )
            for item in items
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO funding_rates (
                    venue, symbol, underlying, observed_at, effective_at,
                    rate, interval_hours, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def upsert_status(self, status: VenueStatus) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO venue_status (
                    venue, status, last_success_at, last_error, instruments, latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    status.venue,
                    status.status,
                    status.last_success_at,
                    status.last_error,
                    status.instruments,
                    status.latency_ms,
                ),
            )

    def get_statuses(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT venue, status, last_success_at, last_error, instruments, latency_ms
                FROM venue_status ORDER BY venue
                """
            ).fetchall()
        keys = ["venue", "status", "last_success_at", "last_error", "instruments", "latency_ms"]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def get_current_rows(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.venue, m.symbol, i.underlying, m.observed_at,
                    m.bid, m.ask, m.mark_price, m.index_price,
                    m.funding_rate, m.funding_interval_hours,
                    m.next_funding_at, m.open_interest, m.volume_24h,
                    i.display_name, i.maker_fee, i.taker_fee, i.metadata_json
                FROM current_market m
                JOIN instruments i USING (venue, symbol)
                WHERE i.active = TRUE
                  AND i.product_type = 'perpetual'
                  AND m.funding_rate IS NOT NULL
                """
            ).fetchall()
        keys = [
            "venue", "symbol", "underlying", "observed_at", "bid", "ask",
            "mark_price", "index_price", "funding_rate", "funding_interval_hours",
            "next_funding_at", "open_interest", "volume_24h", "display_name",
            "maker_fee", "taker_fee", "metadata_json",
        ]
        results: list[dict] = []
        for row in rows:
            result = dict(zip(keys, row, strict=True))
            metadata = self._parse_metadata(result.pop("metadata_json"))
            asset_class = metadata.get("asset_class")
            result["asset_class"] = (
                asset_class if asset_class in self._ASSET_CLASSES else "unknown"
            )
            result["spot_carry_eligible"] = (
                metadata.get("spot_carry_eligible") is True
            )
            results.append(result)
        return results

    @staticmethod
    def _parse_metadata(raw: object) -> dict:
        if not isinstance(raw, str):
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def get_settled_funding(self, since: datetime) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.venue, f.symbol, i.underlying,
                    f.effective_at, f.rate, f.interval_hours
                FROM funding_rates f
                JOIN instruments i USING (venue, symbol)
                WHERE i.active = TRUE
                  AND f.kind = 'settled'
                  AND f.effective_at >= ?
                ORDER BY f.effective_at
                """,
                (since,),
            ).fetchall()
        keys = ["venue", "symbol", "underlying", "effective_at", "rate", "interval_hours"]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def record_prediction_observation(
        self,
        venue: str,
        expected_symbols: int,
        sampled_symbols: int,
        observed_at: datetime,
    ) -> None:
        observed_at = self._as_utc(observed_at)
        expected_symbols = max(0, int(expected_symbols))
        sampled_symbols = max(0, int(sampled_symbols))
        coverage_error = self._prediction_coverage_error(
            expected_symbols, sampled_symbols
        )
        with self._lock, self._connect() as conn:
            previous = self._prediction_status_row(conn, venue)
            if previous is None:
                status = "warming_up" if sampled_symbols else "degraded"
                conn.execute(
                    """
                    INSERT INTO funding_prediction_collector_status VALUES (
                        ?, ?, ?, NULL, ?, ?, 0, 0, 0, 0, ?, ?
                    )
                    """,
                    (
                        venue,
                        status,
                        observed_at if sampled_symbols else None,
                        expected_symbols,
                        sampled_symbols,
                        coverage_error,
                        observed_at,
                    ),
                )
                return

            status = (
                "warming_up"
                if previous["last_flushed_minute"] is None and sampled_symbols
                else "healthy"
                if expected_symbols > 0 and sampled_symbols >= expected_symbols
                else "degraded"
            )
            conn.execute(
                """
                UPDATE funding_prediction_collector_status
                SET status = ?,
                    last_success_at = ?,
                    expected_symbols = ?,
                    sampled_symbols_last_minute = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE venue = ?
                """,
                (
                    status,
                    observed_at if sampled_symbols else previous["last_success_at"],
                    expected_symbols,
                    sampled_symbols,
                    coverage_error,
                    observed_at,
                    venue,
                ),
            )

    def record_prediction_failure(
        self,
        venue: str,
        error: str,
        expected_symbols: int = 0,
        observed_at: datetime | None = None,
    ) -> None:
        observed_at = self._as_utc(observed_at or datetime.now(timezone.utc))
        expected_symbols = max(0, int(expected_symbols))
        with self._lock, self._connect() as conn:
            previous = self._prediction_status_row(conn, venue)
            if previous is None:
                conn.execute(
                    """
                    INSERT INTO funding_prediction_collector_status VALUES (
                        ?, 'offline', NULL, NULL, ?, 0, 0, 0, 0, 0, ?, ?
                    )
                    """,
                    (venue, expected_symbols, error[:500], observed_at),
                )
                return
            conn.execute(
                """
                UPDATE funding_prediction_collector_status
                SET status = 'offline',
                    expected_symbols = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE venue = ?
                """,
                (
                    expected_symbols or previous["expected_symbols"],
                    error[:500],
                    observed_at,
                    venue,
                ),
            )

    def flush_prediction_minutes(
        self,
        venue: str,
        minute_at: datetime,
        items: list[PredictionMinute],
        expected_symbols: int,
        sampled_symbols: int,
        status_window_minutes: int = 60,
    ) -> bool:
        if not items:
            return False
        minute_at = self._as_utc(minute_at)
        if any(item.venue != venue or item.minute_at != minute_at for item in items):
            raise ValueError("prediction minute batch must share venue and minute_at")
        expected_symbols = max(0, int(expected_symbols))
        sampled_symbols = max(0, int(sampled_symbols))
        status_window_minutes = max(1, int(status_window_minutes))
        rows = [self._prediction_minute_row(item) for item in sorted(
            items,
            key=lambda item: (item.minute_at, item.venue, item.symbol, item.target_funding_at),
        )]

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                previous = self._prediction_status_row(conn, venue)
                watermark = previous["last_flushed_minute"] if previous else None
                if watermark is not None and minute_at <= watermark:
                    conn.execute("COMMIT")
                    return False
                conn.executemany(
                    f"""
                    INSERT INTO funding_prediction_minutes (
                        {", ".join(PREDICTION_MINUTE_COLUMNS)}
                    ) VALUES ({", ".join("?" for _ in PREDICTION_MINUTE_COLUMNS)})
                    """,
                    rows,
                )

                window_start = minute_at - timedelta(
                    minutes=status_window_minutes - 1
                )
                coverage_row = conn.execute(
                    """
                    SELECT
                        COUNT(*),
                        COUNT(DISTINCT minute_at),
                        MIN(minute_at)
                    FROM (
                        SELECT DISTINCT minute_at, symbol
                        FROM funding_prediction_minutes
                        WHERE venue = ? AND minute_at BETWEEN ? AND ?
                    )
                    """,
                    (venue, window_start, minute_at),
                ).fetchone()
                distinct_pairs = int(coverage_row[0] or 0)
                distinct_minutes = int(coverage_row[1] or 0)
                first_minute = coverage_row[2]
                observed_slots = (
                    min(
                        status_window_minutes,
                        int((minute_at - first_minute).total_seconds() // 60) + 1,
                    )
                    if first_minute is not None
                    else 0
                )
                denominator = expected_symbols * observed_slots
                coverage = (
                    min(1.0, distinct_pairs / denominator) if denominator else 0.0
                )
                missed_minutes = max(0, observed_slots - distinct_minutes)
                hot_rows = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM funding_prediction_minutes WHERE venue = ?",
                        (venue,),
                    ).fetchone()[0]
                )
                archived_rows = int(previous["archived_rows"] if previous else 0)
                last_success_at = max(item.last_observed_at for item in items)
                status = (
                    "healthy"
                    if expected_symbols > 0 and sampled_symbols >= expected_symbols
                    else "degraded"
                )
                coverage_error = self._prediction_coverage_error(
                    expected_symbols, sampled_symbols
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO funding_prediction_collector_status VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        venue,
                        status,
                        last_success_at,
                        minute_at,
                        expected_symbols,
                        sampled_symbols,
                        coverage,
                        missed_minutes,
                        hot_rows,
                        archived_rows,
                        coverage_error,
                        datetime.now(timezone.utc),
                    ),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_prediction_collector_status(self) -> dict:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT venue, status, last_success_at, last_flushed_minute,
                       expected_symbols, sampled_symbols_last_minute,
                       coverage_60m, missed_minutes_60m, hot_rows,
                       archived_rows, last_error, updated_at
                FROM funding_prediction_collector_status
                ORDER BY venue
                """
            ).fetchall()
            hot_rows = int(
                conn.execute(
                    "SELECT COUNT(*) FROM funding_prediction_minutes"
                ).fetchone()[0]
            )
            archive_summary = conn.execute(
                """
                SELECT COALESCE(SUM(row_count), 0), MAX(month_start)
                FROM funding_prediction_archives
                WHERE status = 'validated'
                """
            ).fetchone()
        keys = [
            "venue",
            "status",
            "last_success_at",
            "last_flushed_minute",
            "expected_symbols",
            "sampled_symbols_last_minute",
            "coverage_60m",
            "missed_minutes_60m",
            "hot_rows",
            "archived_rows",
            "last_error",
            "updated_at",
        ]
        venues = [dict(zip(keys, row, strict=True)) for row in rows]
        for venue in venues:
            for field in (
                "last_success_at",
                "last_flushed_minute",
                "updated_at",
            ):
                if venue[field] is not None:
                    venue[field] = self._as_utc(venue[field])
        return {
            "hot_rows": hot_rows,
            "archived_rows": int(archive_summary[0] or 0),
            "latest_archived_month": archive_summary[1],
            "venues": venues,
        }

    def archive_old_prediction_months(
        self,
        now: datetime,
        hot_days: int,
        archive_dir: Path,
    ) -> list[dict]:
        now = self._as_utc(now)
        if hot_days < 1:
            raise ValueError("prediction hot retention must be at least one day")
        cutoff = now - timedelta(days=hot_days)
        current_month = date(now.year, now.month, 1)
        with self._connect() as conn:
            candidates = conn.execute(
                """
                SELECT CAST(date_trunc('month', minute_at) AS DATE),
                       COUNT(*), MIN(minute_at), MAX(minute_at)
                FROM funding_prediction_minutes
                GROUP BY 1
                ORDER BY 1
                """
            ).fetchall()

        archived: list[dict] = []
        for month_start, row_count, min_minute, max_minute in candidates:
            month_end = self._next_month(month_start)
            if month_start >= current_month or datetime.combine(
                month_end, datetime.min.time(), tzinfo=timezone.utc
            ) > cutoff:
                continue
            archived.append(
                self._archive_prediction_month(
                    month_start=month_start,
                    month_end=month_end,
                    expected_count=int(row_count),
                    expected_min=min_minute,
                    expected_max=max_minute,
                    archive_dir=Path(archive_dir),
                )
            )
        return archived

    def _archive_prediction_month(
        self,
        *,
        month_start: date,
        month_end: date,
        expected_count: int,
        expected_min: datetime,
        expected_max: datetime,
        archive_dir: Path,
    ) -> dict:
        month_dir = archive_dir / f"year={month_start.year:04d}" / f"month={month_start.month:02d}"
        month_dir.mkdir(parents=True, exist_ok=True)
        final_path = month_dir / "funding_predictions.parquet"
        temp_path = month_dir / ".funding_predictions.parquet.tmp"
        month_start_at = datetime.combine(
            month_start, datetime.min.time(), tzinfo=timezone.utc
        )
        month_end_at = datetime.combine(
            month_end, datetime.min.time(), tzinfo=timezone.utc
        )

        with self._lock, self._connect() as conn:
            manifest = conn.execute(
                """
                SELECT file_path, row_count, min_minute_at, max_minute_at, status
                FROM funding_prediction_archives WHERE month_start = ?
                """,
                (month_start,),
            ).fetchone()
        if manifest is not None:
            if manifest[4] != "validated" or not final_path.exists():
                raise RuntimeError(f"invalid archive manifest for {month_start}")
            self._validate_prediction_archive(
                final_path,
                int(manifest[1]),
                manifest[2],
                manifest[3],
            )
            return {
                "month_start": month_start,
                "file_path": str(final_path),
                "row_count": int(manifest[1]),
                "status": "already_validated",
            }

        if final_path.exists():
            self._validate_prediction_archive(
                final_path, expected_count, expected_min, expected_max
            )
        else:
            temp_path.unlink(missing_ok=True)
            escaped_temp = self._sql_path(temp_path)
            with self._connect() as conn:
                conn.execute(
                    f"""
                    COPY (
                        SELECT {", ".join(PREDICTION_MINUTE_COLUMNS)}
                        FROM funding_prediction_minutes
                        WHERE minute_at >= ? AND minute_at < ?
                        ORDER BY minute_at, venue, symbol, target_funding_at
                    ) TO '{escaped_temp}' (FORMAT PARQUET, COMPRESSION ZSTD)
                    """,
                    (month_start_at, month_end_at),
                )
            try:
                self._validate_prediction_archive(
                    temp_path, expected_count, expected_min, expected_max
                )
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
            temp_path.replace(final_path)

        file_size = final_path.stat().st_size
        archived_at = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                live_summary = conn.execute(
                    """
                    SELECT COUNT(*), MIN(minute_at), MAX(minute_at)
                    FROM funding_prediction_minutes
                    WHERE minute_at >= ? AND minute_at < ?
                    """,
                    (month_start_at, month_end_at),
                ).fetchone()
                if (
                    int(live_summary[0]) != expected_count
                    or live_summary[1] != expected_min
                    or live_summary[2] != expected_max
                ):
                    raise RuntimeError(
                        f"hot data changed while archiving {month_start}"
                    )
                venue_counts = conn.execute(
                    """
                    SELECT venue, COUNT(*)
                    FROM funding_prediction_minutes
                    WHERE minute_at >= ? AND minute_at < ?
                    GROUP BY venue
                    """,
                    (month_start_at, month_end_at),
                ).fetchall()
                conn.execute(
                    """
                    INSERT INTO funding_prediction_archives VALUES (
                        ?, ?, ?, ?, ?, ?, 'validated', ?, ?
                    )
                    """,
                    (
                        month_start,
                        str(final_path.resolve()),
                        expected_count,
                        expected_min,
                        expected_max,
                        file_size,
                        archived_at,
                        archived_at,
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM funding_prediction_minutes
                    WHERE minute_at >= ? AND minute_at < ?
                    """,
                    (month_start_at, month_end_at),
                )
                for venue, count in venue_counts:
                    hot_rows = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM funding_prediction_minutes
                            WHERE venue = ?
                            """,
                            (venue,),
                        ).fetchone()[0]
                    )
                    conn.execute(
                        """
                        UPDATE funding_prediction_collector_status
                        SET hot_rows = ?,
                            archived_rows = archived_rows + ?,
                            updated_at = ?
                        WHERE venue = ?
                        """,
                        (hot_rows, int(count), archived_at, venue),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        with self._connect() as conn:
            conn.execute("CHECKPOINT")
        return {
            "month_start": month_start,
            "file_path": str(final_path),
            "row_count": expected_count,
            "status": "validated",
        }

    @staticmethod
    def _validate_prediction_archive(
        path: Path,
        expected_count: int,
        expected_min: datetime,
        expected_max: datetime,
    ) -> None:
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError(f"prediction archive is missing or empty: {path}")
        escaped_path = CarryStore._sql_path(path)
        with duckdb.connect() as conn:
            columns = [
                row[0]
                for row in conn.execute(
                    f"""
                    DESCRIBE SELECT * FROM read_parquet(
                        '{escaped_path}', hive_partitioning = false
                    )
                    """
                ).fetchall()
            ]
            if tuple(columns) != PREDICTION_MINUTE_COLUMNS:
                raise RuntimeError(f"prediction archive schema mismatch: {path}")
            summary = conn.execute(
                f"""
                SELECT COUNT(*), MIN(minute_at), MAX(minute_at)
                FROM read_parquet('{escaped_path}', hive_partitioning = false)
                """
            ).fetchone()
        if (
            int(summary[0]) != expected_count
            or summary[1] != expected_min
            or summary[2] != expected_max
        ):
            raise RuntimeError(f"prediction archive validation failed: {path}")

    @staticmethod
    def _prediction_minute_row(item: PredictionMinute) -> tuple:
        return tuple(getattr(item, column) for column in PREDICTION_MINUTE_COLUMNS)

    @staticmethod
    def _prediction_status_row(
        conn: duckdb.DuckDBPyConnection,
        venue: str,
    ) -> dict | None:
        row = conn.execute(
            """
            SELECT venue, status, last_success_at, last_flushed_minute,
                   expected_symbols, sampled_symbols_last_minute,
                   coverage_60m, missed_minutes_60m, hot_rows,
                   archived_rows, last_error, updated_at
            FROM funding_prediction_collector_status
            WHERE venue = ?
            """,
            (venue,),
        ).fetchone()
        if row is None:
            return None
        keys = [
            "venue",
            "status",
            "last_success_at",
            "last_flushed_minute",
            "expected_symbols",
            "sampled_symbols_last_minute",
            "coverage_60m",
            "missed_minutes_60m",
            "hot_rows",
            "archived_rows",
            "last_error",
            "updated_at",
        ]
        return dict(zip(keys, row, strict=True))

    @staticmethod
    def _prediction_coverage_error(
        expected_symbols: int,
        sampled_symbols: int,
    ) -> str | None:
        if expected_symbols > 0 and sampled_symbols >= expected_symbols:
            return None
        if sampled_symbols <= 0:
            return "No valid prediction snapshots"
        return f"Partial prediction snapshots: {sampled_symbols}/{expected_symbols}"

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _next_month(value: date) -> date:
        if value.month == 12:
            return date(value.year + 1, 1, 1)
        return date(value.year, value.month + 1, 1)

    @staticmethod
    def _sql_path(path: Path) -> str:
        return str(path.resolve()).replace("'", "''")
