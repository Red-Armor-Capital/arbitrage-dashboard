from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from backend.app.prediction import PredictionMinute
from backend.app.storage import (
    CarryStore,
    PREDICTION_MIGRATION_VERSION,
    PREDICTION_MINUTE_COLUMNS,
)


UTC = timezone.utc


def prediction_minute(
    minute_at: datetime,
    *,
    symbol: str = "NVDA-USD",
    rate: float = 0.001,
) -> PredictionMinute:
    return PredictionMinute(
        venue="lighter",
        symbol=symbol,
        underlying=symbol.split("-")[0],
        minute_at=minute_at,
        first_observed_at=minute_at + timedelta(seconds=5),
        last_observed_at=minute_at + timedelta(seconds=35),
        source_observed_at=minute_at + timedelta(seconds=34),
        target_funding_at=minute_at.replace(minute=0) + timedelta(hours=1),
        target_source="schedule",
        raw_rate_close=rate * 100,
        raw_rate_unit="percent",
        source_tenor_hours=1,
        normalized_rate_open=rate,
        normalized_rate_high=rate,
        normalized_rate_low=rate,
        normalized_rate_close=rate,
        settlement_interval_hours=1,
        sample_count=2,
        mark_price_close=100,
        index_price_close=99.9,
        transform_version="test-v1",
    )


def row_count(store: CarryStore, table: str) -> int:
    with store._connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_migration_preserves_existing_tables_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE instruments (
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
            INSERT INTO instruments VALUES (
                'test', 'NVDA-USD', 'NVDA', NULL, 'perpetual', 'USD',
                1, 0, 0, TRUE, '{}', ?
            )
            """,
            (datetime(2026, 1, 1, tzinfo=UTC),),
        )

    CarryStore(path)
    CarryStore(path)

    with duckdb.connect(str(path)) as conn:
        assert conn.execute("SELECT symbol FROM instruments").fetchall() == [
            ("NVDA-USD",)
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (PREDICTION_MIGRATION_VERSION,),
        ).fetchone()[0] == 1
        assert {
            row[0]
            for row in conn.execute("SHOW TABLES").fetchall()
        } >= {
            "funding_prediction_minutes",
            "funding_prediction_collector_status",
            "funding_prediction_archives",
        }


def test_flush_and_watermark_are_idempotent(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    minute = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    items = [
        prediction_minute(minute),
        prediction_minute(minute, symbol="AAPL-USD", rate=0.002),
    ]
    store.record_prediction_observation("lighter", 2, 2, minute + timedelta(seconds=35))

    assert store.flush_prediction_minutes("lighter", minute, items, 2, 2) is True
    assert store.flush_prediction_minutes("lighter", minute, items, 2, 2) is False
    assert row_count(store, "funding_prediction_minutes") == 2

    status = store.get_prediction_collector_status()
    assert status["hot_rows"] == 2
    assert status["venues"][0]["last_flushed_minute"] == minute
    assert status["venues"][0]["coverage_60m"] == pytest.approx(1.0)
    assert status["venues"][0]["missed_minutes_60m"] == 0


def test_failed_batch_rolls_back_rows_and_watermark(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    minute = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    good = prediction_minute(minute)
    invalid = prediction_minute(minute, symbol="AAPL-USD").model_copy(
        update={"source_tenor_hours": 0}
    )

    with pytest.raises(Exception):
        store.flush_prediction_minutes("lighter", minute, [good, invalid], 2, 2)

    assert row_count(store, "funding_prediction_minutes") == 0
    assert store.get_prediction_collector_status()["venues"] == []


def test_failure_status_keeps_last_success_and_counts(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    observed = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    store.record_prediction_observation("lighter", 5, 4, observed)
    store.record_prediction_failure(
        "lighter",
        "network timeout",
        observed_at=observed + timedelta(seconds=30),
    )

    status = store.get_prediction_collector_status()["venues"][0]
    assert status["status"] == "offline"
    assert status["last_success_at"] == observed
    assert status["expected_symbols"] == 5
    assert status["last_error"] == "network timeout"


def test_archive_writes_validated_parquet_before_deleting_hot_rows(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    january = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    april = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    store.flush_prediction_minutes(
        "lighter",
        january,
        [prediction_minute(january), prediction_minute(january, symbol="AAPL-USD")],
        2,
        2,
    )
    store.flush_prediction_minutes(
        "lighter", april, [prediction_minute(april)], 2, 1
    )

    archive_dir = tmp_path / "prediction_archive"
    results = store.archive_old_prediction_months(
        datetime(2026, 5, 15, tzinfo=UTC),
        hot_days=90,
        archive_dir=archive_dir,
    )

    assert [item["row_count"] for item in results] == [2]
    parquet = archive_dir / "year=2026" / "month=01" / "funding_predictions.parquet"
    assert parquet.exists()
    with duckdb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", (str(parquet),)
        ).fetchone()[0] == 2
    assert row_count(store, "funding_prediction_minutes") == 1
    status = store.get_prediction_collector_status()
    assert status["archived_rows"] == 2
    assert status["hot_rows"] == 1
    assert status["latest_archived_month"].isoformat() == "2026-01-01"
    assert store.archive_old_prediction_months(
        datetime(2026, 5, 15, tzinfo=UTC), 90, archive_dir
    ) == []


def test_archive_validation_failure_never_deletes_hot_rows(
    tmp_path, monkeypatch
) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    january = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    store.flush_prediction_minutes(
        "lighter", january, [prediction_minute(january)], 1, 1
    )

    def fail_validation(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated validation failure")

    monkeypatch.setattr(
        CarryStore,
        "_validate_prediction_archive",
        staticmethod(fail_validation),
    )
    archive_dir = tmp_path / "prediction_archive"
    with pytest.raises(RuntimeError, match="simulated validation failure"):
        store.archive_old_prediction_months(
            datetime(2026, 5, 15, tzinfo=UTC), 90, archive_dir
        )

    assert row_count(store, "funding_prediction_minutes") == 1
    assert row_count(store, "funding_prediction_archives") == 0
    assert not (
        archive_dir / "year=2026" / "month=01" / "funding_predictions.parquet"
    ).exists()


def test_archive_recovers_final_file_left_before_manifest(tmp_path) -> None:
    store = CarryStore(tmp_path / "carry.duckdb")
    january = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    store.flush_prediction_minutes(
        "lighter", january, [prediction_minute(january)], 1, 1
    )
    final_path = (
        tmp_path
        / "prediction_archive"
        / "year=2026"
        / "month=01"
        / "funding_predictions.parquet"
    )
    final_path.parent.mkdir(parents=True)
    escaped = store._sql_path(final_path)
    with store._connect() as conn:
        conn.execute(
            f"""
            COPY (
                SELECT {", ".join(PREDICTION_MINUTE_COLUMNS)}
                FROM funding_prediction_minutes
                ORDER BY minute_at, venue, symbol, target_funding_at
            ) TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
    original_bytes = final_path.read_bytes()

    results = store.archive_old_prediction_months(
        datetime(2026, 5, 15, tzinfo=UTC),
        90,
        tmp_path / "prediction_archive",
    )

    assert results[0]["status"] == "validated"
    assert final_path.read_bytes() == original_bytes
    assert row_count(store, "funding_prediction_minutes") == 0
    assert row_count(store, "funding_prediction_archives") == 1
