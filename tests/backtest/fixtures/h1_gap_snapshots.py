"""The H1-gap condition C-8.4's key cannot distinguish (IMPLEMENTATION_PLAN §5).

Three consecutive replay cycles for one symbol — `T`, `T+3600`, `T+7200` — in
which **no new H1 candle closes**. The H1 frame and `h1_last_ts` are byte-identical
across all three, so C-8.4's `(symbol, h1_last_ts, session_active)` key collides,
while the snapshot genuinely differs:

- `m5` slides forward — `m5_last_ts` advances by 3600 s each cycle;
- `live_price` changes each cycle — the forming candle closes regardless of H1;
- **variant B** additionally advances `d1`, the case of a gap spanning UTC midnight.

## Why this is built from the corpus rather than synthesised

IMPLEMENTATION_PLAN §5 specified a synthetic fixture, on the stated premise that
*"the gapped symbols (ICPUSDT, LUNAUSDT, ZENUSDT) live in `data/stage1`, which has
no M5."* **Gate A removed that premise** — `data/stage1` now carries 1h, 1d and 5m.

The premise mattered. Hand-built OHLC does not reach the `score >= 8` gate that
`enrich_with_m5` sits behind: a synthesised attempt scored 0/SKIP on all three
cycles, which would have made the equality assertions pass vacuously against a
*broken* cache — the exact failure §5 demands the fixture rule out. Real frames
reach the gate and produce genuinely different outcomes, so the assertions bite.

The **gap itself** is still constructed; no corpus exhibits it naturally.

## The verified condition

`DYDX/USDT` at 2023-03-15T12:00Z, `session_active=True`:

    cycle 0 -> score 13, VALID, m5_choch=True,  retracement_long_confirm
    cycle 1 -> score 11, WATCH, m5_choch=False, retracement_long_fail
    cycle 2 -> score 11, WATCH, m5_choch=False, retracement_long_fail

A C-8.4-keyed cache serves cycle 0's `VALID` to cycles 1 and 2. That is the
silent wrong answer this fixture exists to demonstrate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from v2.domain import MarketSnapshot

__all__ = [
    "BOUNDARY",
    "CORPUS_ROOT",
    "SESSION_ACTIVE",
    "SYMBOL",
    "build_cycles",
    "corpus_available",
    "outcomes_of",
]

#: Verified to reach the `score >= 8` M5 gate and to change verdict across the gap.
SYMBOL = "DYDX/USDT"
BOUNDARY = int(datetime(2023, 3, 15, 12, 0, tzinfo=timezone.utc).timestamp())
SESSION_ACTIVE = True
CORPUS_ROOT = Path("data/stage1")

_H1_SECONDS = 3600
_DAY_SECONDS = 86_400


def corpus_available(root: Path | str = CORPUS_ROOT) -> bool:
    return (Path(root) / "canonical" / "manifest.json").is_file()


def _snapshot_at(repository, boundary: int, symbol: str):
    from backtest.replay.adapter import HistoricalMarketDataAdapter

    adapter = HistoricalMarketDataAdapter(repository)
    adapter.set_time(boundary)
    return adapter.get_market_snapshot(symbol, include_m5=True)


def build_cycles(
    *,
    variant: str = "A",
    root: Path | str = CORPUS_ROOT,
    symbol: str = SYMBOL,
    boundary: int = BOUNDARY,
) -> list[MarketSnapshot]:
    """Three snapshots across a constructed H1 gap. `variant` is "A" or "B".

    H1 is pinned to the base cycle in all three — that *is* the gap. M5 and
    `live_price` are the genuine per-cycle values the adapter serves at
    `T`, `T+3600`, `T+7200`. Variant B additionally takes D1 from a boundary one
    day later per cycle, so `d1_last_ts` advances while H1 stays frozen.
    """
    if variant not in ("A", "B"):
        raise ValueError(f"variant must be 'A' or 'B', got {variant!r}")

    from backtest.data.repository import KlineRepository

    repository = KlineRepository(Path(root) / "canonical")
    base = _snapshot_at(repository, boundary, symbol)
    if base is None:
        raise RuntimeError(
            f"{symbol} has no snapshot at {boundary}; the fixture's premise is "
            f"gone and the T4.1 assertions would be vacuous")

    cycles: list[MarketSnapshot] = []
    for index in range(3):
        later = _snapshot_at(repository, boundary + index * _H1_SECONDS, symbol)
        if later is None:
            raise RuntimeError(f"{symbol} has no snapshot at cycle {index}")

        d1 = base.d1
        if variant == "B" and index:
            advanced = _snapshot_at(
                repository, boundary + index * _DAY_SECONDS, symbol)
            if advanced is not None:
                d1 = advanced.d1

        cycles.append(MarketSnapshot(
            symbol=base.symbol,
            d1=d1,
            h1=base.h1,                       # byte-identical across cycles
            h1_last_ts=base.h1_last_ts,       # frozen: no H1 candle closed
            h1_last_ts_dt=base.h1_last_ts_dt,
            live_price=later.live_price,      # advances every cycle
            m5=later.m5,                      # slides every cycle
        ))
    return cycles


def outcomes_of(snapshots, analyzer, *, session_active: bool = SESSION_ACTIVE):
    """`(score, decision, m5_choch, m5_reason)` per cycle, via the real path."""
    return [
        (lambda s: (s.score, s.decision, s.m5_choch, s.m5_reason))(
            analyzer.analyze(snapshot, session_active=session_active))
        for snapshot in snapshots
    ]
