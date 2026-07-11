from __future__ import annotations

from .adapters.base import VenueAdapter


def adapter_factories() -> list[type[VenueAdapter]]:
    """Import adapters lazily so one optional venue cannot break the API."""
    factories: list[type[VenueAdapter]] = []
    try:
        from .adapters.cex import (
            BinanceAdapter,
            BitgetAdapter,
            BybitAdapter,
            GateAdapter,
            KrakenAdapter,
            OkxAdapter,
        )

        factories.extend(
            [
                BinanceAdapter,
                BitgetAdapter,
                BybitAdapter,
                GateAdapter,
                KrakenAdapter,
                OkxAdapter,
            ]
        )
    except ImportError:
        pass
    try:
        from .adapters.dex import (
            ExtendedAdapter,
            HotstuffAdapter,
            LighterAdapter,
            OrderlyAdapter,
            XyzAdapter,
        )

        factories.extend(
            [LighterAdapter, ExtendedAdapter, XyzAdapter, HotstuffAdapter, OrderlyAdapter]
        )
    except ImportError:
        pass
    return factories
