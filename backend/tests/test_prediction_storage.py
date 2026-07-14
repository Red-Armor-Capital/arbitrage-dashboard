from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from backend.app.prediction import PredictionMinute
from backend.app.storage import (
    CarryStore,
    HOTSTUFF_HOURLY_MIGRATION_VERSION,
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
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (HOTSTUFF_HOURLY_MIGRATION_VERSION,),
        ).fetchone()[0] == 1
        assert {
            row[0]
            for row in conn.execute("SHOW TABLES").fetchall()
        } >= {
            "funding_prediction_minutes",
            "funding_prediction_collector_status",
            "funding_prediction_archives",
        }


def test_hotstuff_hourly_migration_repairs_predictions_without_touching_settled(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-hotstuff.duckdb"
    store = CarryStore(path)
    minute = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)

    with store._connect() as conn:
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (HOTSTUFF_HOURLY_MIGRATION_VERSION,),
        )
        conn.execute(
            """
            INSERT INTO instruments VALUES (
                'hotstuff', 'AAPL-PERP', 'AAPL', 'AAPL', 'perpetual', 'USDC',
                1, -0.00002, 0.00025, TRUE,
                '{"current_rate_tenor_hours":8,"spot_carry_eligible":true}', ?
            )
            """,
            (minute,),
        )
        conn.execute(
            """
            INSERT INTO current_market (
                venue, symbol, underlying, observed_at, bid, ask, mark_price,
                index_price, funding_rate, funding_interval_hours,
                next_funding_at, open_interest, volume_24h
            ) VALUES (
                'hotstuff', 'AAPL-PERP', 'AAPL', ?, 100, 101, 100.5, 100.4,
                0.00005, 1, ?, 10, 1000
            )
            """,
            (minute, minute + timedelta(hours=1)),
        )
        for kind, rate in (("current", 0.00005), ("settled", 0.0004)):
            conn.execute(
                """
                INSERT INTO funding_rates VALUES (
                    'hotstuff', 'AAPL-PERP', 'AAPL', ?, ?, ?, 1, ?
                )
                """,
                (minute, minute + timedelta(hours=1), rate, kind),
            )
        conn.execute(
            """
            INSERT INTO funding_prediction_minutes VALUES (
                'hotstuff', 'AAPL-PERP', 'AAPL', ?, ?, ?, NULL, ?, 'schedule',
                0.0004, 'decimal', 8,
                0.000025, 0.000075, -0.0000125, 0.00005,
                1, 2, 100.5, 100.4, 'eight-hour-to-hourly-v1'
            )
            """,
            (
                minute,
                minute + timedelta(seconds=5),
                minute + timedelta(seconds=35),
                minute + timedelta(hours=1),
            ),
        )

    CarryStore(path)
    CarryStore(path)

    with duckdb.connect(str(path)) as conn:
        prediction = conn.execute(
            """
            SELECT raw_rate_close, source_tenor_hours,
                   normalized_rate_open, normalized_rate_high,
                   normalized_rate_low, normalized_rate_close,
                   transform_version, raw_rate_unit,
                   settlement_interval_hours, sample_count,
                   minute_at, target_funding_at
            FROM funding_prediction_minutes
            """
        ).fetchone()
        assert prediction[:6] == pytest.approx(
            (0.0004, 1, 0.0002, 0.0006, -0.0001, 0.0004),
            rel=0,
            abs=1e-15,
        )
        assert prediction[6] == "identity-v1"
        assert prediction[7:] == (
            "decimal",
            1,
            2,
            minute,
            minute + timedelta(hours=1),
        )
        assert conn.execute(
            "SELECT funding_rate FROM current_market WHERE venue = 'hotstuff'"
        ).fetchone()[0] == pytest.approx(0.0004)
        assert dict(
            conn.execute(
                "SELECT kind, rate FROM funding_rates WHERE venue = 'hotstuff'"
            ).fetchall()
        ) == {
            "current": pytest.approx(0.0004),
            "settled": pytest.approx(0.0004),
        }
        metadata = json.loads(
            conn.execute(
                "SELECT metadata_json FROM instruments WHERE venue = 'hotstuff'"
            ).fetchone()[0]
        )
        assert metadata["current_rate_tenor_hours"] == 1
        assert metadata["ticker_rate_semantics"] == "hourly_payment_rate"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (HOTSTUFF_HOURLY_MIGRATION_VERSION,),
        ).fetchone()[0] == 1


def test_hotstuff_hourly_migration_rolls_back_unexpected_legacy_transform(
    tmp_path,
) -> None:
    path = tmp_path / "invalid-hotstuff.duckdb"
    store = CarryStore(path)
    minute = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)

    with store._connect() as conn:
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (HOTSTUFF_HOURLY_MIGRATION_VERSION,),
        )
        conn.execute(
            """
            INSERT INTO funding_prediction_minutes VALUES (
                'hotstuff', 'AAPL-PERP', 'AAPL', ?, ?, ?, NULL, ?, 'schedule',
                0.0004, 'decimal', 8,
                0.0002, 0.0002, 0.0002, 0.0002,
                1, 1, 100.5, 100.4, 'eight-hour-to-hourly-v1'
            )
            """,
            (
                minute,
                minute + timedelta(seconds=5),
                minute + timedelta(seconds=5),
                minute + timedelta(hours=1),
            ),
        )

    with pytest.raises(RuntimeError, match="unexpected rate transform"):
        CarryStore(path)

    with duckdb.connect(str(path)) as conn:
        assert conn.execute(
            """
            SELECT source_tenor_hours, normalized_rate_close, transform_version
            FROM funding_prediction_minutes
            """
        ).fetchone() == (8, 0.0002, "eight-hour-to-hourly-v1")
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (HOTSTUFF_HOURLY_MIGRATION_VERSION,),
        ).fetchone()[0] == 0


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
