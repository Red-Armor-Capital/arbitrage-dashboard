from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import duckdb

from .models import FundingRate, Instrument, MarketSnapshot, VenueStatus


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
