"""LifecycleConfig — the operational constants the lifecycle rules read.

Architecture ref: 02-software-architecture.md §6 ("config holds operational
values only — limits, TTLs, thresholds"). These are *operational* values (not
strategy calibration), so they belong here, injected into the reconciler/store,
rather than being hard-coded in the rules. Defaults reproduce V1's exact values
so the default wiring is behaviorally identical to V1; tests inject shrunk
values to exercise TTL / slot / cooldown edges deterministically.

V1 sources (byte-for-byte):
- ``max_open_positions``      = ``config.MAX_OPEN_POSITIONS`` (10).
- ``miss_count_limit``        = ``main.MISS_COUNT_LIMIT`` (3).
- ``kill_cooldown_seconds``   = ``main.KILL_COOLDOWN_SECONDS`` (3600 = 1 H1).
- ``untapped_ttl_seconds``    = ``config.POI_UNTAPPED_TTL_SECONDS`` (24h).
- ``tapped_ttl_seconds``      = ``config.POI_TAPPED_TTL_SECONDS`` (6h).
- ``manual_action_ttl_seconds`` = ``config.MANUAL_ACTION_TTL_SECONDS`` (10h).

The V1 TTL values are env-overridable (``os.getenv`` in config.py); the numbers
below are V1's shipped defaults. This module reads no clock and imports nothing
from ``src`` (Rule A2): it only carries values.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleConfig:
    """Operational thresholds for the lifecycle layer (V1 defaults)."""

    max_open_positions: int = 10
    miss_count_limit: int = 3
    kill_cooldown_seconds: float = 60.0 * 60.0
    untapped_ttl_seconds: float = 24.0 * 60.0 * 60.0
    tapped_ttl_seconds: float = 6.0 * 60.0 * 60.0
    manual_action_ttl_seconds: float = 10.0 * 60.0 * 60.0
