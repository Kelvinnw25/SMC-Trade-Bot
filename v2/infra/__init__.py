"""V2 infrastructure layer — the I/O boundary (Layer 1).

Architecture ref: 02-software-architecture.md §1 (Infrastructure) and §3
(module boundaries). Everything that touches the outside world lives here;
this layer knows nothing about SMC concepts. It is the only layer permitted to
wrap V1 code (`src/`) and to perform filesystem/network I/O — the
anti-corruption boundary for the migration.

Built so far:
- `infra.clock`       — injectable time source (Phase 2.1).
- `infra.market_data` — the sole V2 entry point to exchange data (Phase 2.2).
- `infra.persistence` — the sole V2 entry point to durable storage (Phase 2.3).

Pending (later phase): `infra.notifier`.
"""
from v2.infra.clock import Clock, FakeClock, SystemClock
from v2.infra.market_data import MarketDataAdapter, MarketDataError, SymbolVolume
from v2.infra.persistence import PersistenceAdapter, PersistenceError

__all__ = [
    "Clock",
    "SystemClock",
    "FakeClock",
    "MarketDataAdapter",
    "MarketDataError",
    "SymbolVolume",
    "PersistenceAdapter",
    "PersistenceError",
]
