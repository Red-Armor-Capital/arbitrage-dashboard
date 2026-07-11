from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

import httpx

from ..models import (
    AdapterResult,
    FundingRate,
    HistoryBatchResult,
    HistoryFetchOutcome,
    Instrument,
)


class VenueAdapter(ABC):
    venue: str
    history_mode: Literal["embedded", "per_symbol"] = "embedded"
    history_concurrency = 8

    def __init__(self, client: httpx.AsyncClient, underlyings: set[str]) -> None:
        self.client = client
        self.underlyings = underlyings

    @abstractmethod
    async def collect(self, history_since: datetime, include_history: bool = True) -> AdapterResult:
        """Fetch instruments, current market state, and settled funding history."""

    def history_instruments(self, instruments: list[Instrument]) -> list[Instrument]:
        """Select instruments whose history should be fetched separately.

        Per-symbol adapters may override this hook when a venue exposes history
        for only part of its live catalogue.
        """

        return [instrument for instrument in instruments if instrument.active]

    async def collect_history(
        self,
        instruments: list[Instrument],
        history_since: datetime,
    ) -> HistoryBatchResult:
        """Fetch settled history per symbol while isolating individual failures."""

        semaphore = asyncio.Semaphore(max(1, self.history_concurrency))

        async def fetch(instrument: Instrument) -> HistoryFetchOutcome:
            try:
                async with semaphore:
                    funding = await self._history(instrument, history_since)
                return HistoryFetchOutcome(
                    instrument=instrument,
                    success=True,
                    funding=funding,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}".rstrip()
                return HistoryFetchOutcome(
                    instrument=instrument,
                    success=False,
                    error=error[:240],
                )

        outcomes = await asyncio.gather(*(fetch(instrument) for instrument in instruments))
        return HistoryBatchResult(outcomes=list(outcomes))

    async def _history(
        self,
        instrument: Instrument,
        since: datetime,
    ) -> list[FundingRate]:
        raise NotImplementedError(f"{self.venue} does not implement per-symbol history")

    @staticmethod
    def as_float(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def match_underlying(self, symbol: str) -> str | None:
        normalized = symbol.upper().replace("-", "").replace("_", "")
        for underlying in sorted(self.underlyings, key=len, reverse=True):
            if normalized.startswith(underlying):
                return underlying
        return None
