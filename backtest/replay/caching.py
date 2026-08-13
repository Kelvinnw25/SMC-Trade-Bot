"""C-8.4 analysis memoization — the duplicate pipeline evaluation, paid once.

REPLAY_CONTRACT.md C-8.4 (permitted memoization), C-8.2 (dependency table),
I-8 (no cache may return a frame built for another `T`), C-16.3.1 (no
production edit). IMPLEMENTATION_PLAN.md Step 4.

## The duplication this removes

[orchestrator.py:265-268](v2/app/orchestrator.py#L265) evaluates the full
analysis chain **twice** per symbol-scan::

    context = self._analyzer.pipeline.run(AnalysisContext.initial(snapshot, ...))
    signal  = self._analyzer.analyze(snapshot, session_active=session_active)

`V2Analyzer.analyze` builds the same initial context and runs the same pipeline
again ([engine.py:93-94](v2/analysis/engine.py#L93)). Measured on the top-30
universe at 2024-07-01: **207.82 ms per symbol** for the pair against
**106.86 ms** for one evaluation — the contract's ~214 ms → ~107 ms.

## Why the pipeline is wrapped and not `analyze`

The orchestrator calls **both** seams, so memoizing `analyze` alone would leave
the first evaluation unpaid. Wrapping `AnalysisPipeline` instead puts the memo
underneath both: the orchestrator's explicit `pipeline.run` populates the entry
and `analyze`'s internal `self._pipeline.run` hits it.

That placement matters for a second reason. `analyze` does more than run the
pipeline — it applies V1's M5 re-score gate (`score >= 8` and an M5 frame
present). Caching at the pipeline boundary leaves that gate in `v2/`, executed
every call, where it belongs. Reimplementing it here to cache `analyze`
directly would put strategy logic in `backtest/` and let it drift.

## The key is deliberately stricter than C-8.4's text

C-8.4 specifies `(symbol, h1_last_ts, session_active)`. That **under-determines
the snapshot**: during an H1 gap `h1_last_ts` freezes for hours while D1, M5 and
`live_price` advance, and the corpus holds 6,204 gap hours across 52 symbols.
Two cycles would collide on that key with genuinely different snapshots and the
cache would serve a stale Signal — silently. `tests/backtest/fixtures/
h1_gap_snapshots.py` builds exactly that condition, and the T4.1 control shows
the C-8.4 key colliding across all three cycles.

The key used here is every component of the snapshot that analysis can read::

    (data_snapshot_id, symbol, h1_last_ts, len(h1), d1_last_ts, len(d1),
     m5_last_ts, len(m5), bit-pattern of live_price, session_active)

`MarketSnapshot` cannot be its own key: `d1`/`h1`/`m5` are declared
`field(compare=False)` ([market.py:39-44](v2/domain/market.py#L39)), so the
determinant must be built explicitly. `AnalysisContext` and `Signal` are both
`@dataclass(frozen=True)`, so handing one cached instance to two callers is safe.

`live_price` enters as its **bit pattern**, not its value: `struct.pack` keeps
`0.0`/`-0.0` distinct and makes NaN compare equal to itself, neither of which
`==` does.

## Bounded, and why that is enough

The two calls are adjacent — nothing else touches the pipeline between them — so
even one entry would hit. `DEFAULT_MAX_ENTRIES` is 128 to cover a whole cycle's
universe with headroom while keeping the cache **bounded**: entries retain their
`AnalysisContext`, which retains the snapshot's DataFrames, and an unbounded
cache over a 30,000-cycle replay would retain every frame the run ever built.

## Transparency

This module adds no branch to any analysis path. A hit returns the identical
frozen context the pipeline produced; a miss runs the unmodified inner pipeline.
`caching_analyzer` is excluded from `ReplayConfig.core()` precisely because the
claim is that it cannot change a result — the same treatment `universe_id` gets.
"""
from __future__ import annotations

import struct
from collections import Counter, OrderedDict
from typing import Any, Optional

from v2.analysis.engine import V2Analyzer
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.scoring_config import DEFAULT_SCORING_CONFIG, ScoringConfig

# `AnalysisContext` is deliberately **not** imported. `test_replay_driver.
# test_the_driver_contains_no_strategy_logic` holds `backtest/replay/` to a closed
# set of `v2` imports, and widening that guard to gain a type annotation would be
# the wrong trade. `from __future__ import annotations` makes every annotation
# below a string that is never evaluated, so the contexts are typed `Any` here and
# named in the docstrings instead.

__all__ = [
    "CacheStats",
    "CachingAnalyzer",
    "CachingPipeline",
    "DEFAULT_MAX_ENTRIES",
    "build_analyzer",
    "c84_key",
    "snapshot_key",
]

#: Entries retained. One would suffice for the adjacent-call pattern; 128 covers
#: a full `top_coins_limit` universe with headroom and still bounds retention.
DEFAULT_MAX_ENTRIES = 128


def _last_timestamp(frame: Any) -> Optional[int]:
    """The last `timestamp` in `frame`, or `None` for an absent/empty frame.

    Handles both the epoch-integer column the canonical repository serves and a
    `pandas.Timestamp`, so the key does not depend on which representation a
    given adapter happens to produce.
    """
    if frame is None or len(frame) == 0:
        return None
    value = frame["timestamp"].iloc[-1]
    inner = getattr(value, "value", None)        # pandas.Timestamp -> epoch ns
    if inner is not None:
        return int(inner)
    return int(value)


def snapshot_key(
    snapshot: Any, *, session_active: bool, data_snapshot_id: str = "",
) -> tuple:
    """The full determinant of an analysis result. See the module docstring."""
    m5 = snapshot.m5
    return (
        data_snapshot_id,
        snapshot.symbol,
        snapshot.h1_last_ts,
        len(snapshot.h1),
        _last_timestamp(snapshot.d1),
        len(snapshot.d1),
        _last_timestamp(m5),
        0 if m5 is None else len(m5),
        struct.pack("<d", float(snapshot.live_price)),
        bool(session_active),
    )


def c84_key(
    snapshot: Any, *, session_active: bool, data_snapshot_id: str = "",
) -> tuple:
    """The key exactly as C-8.4's text specifies it.

    Present **only** as the T4.1 control: the test shows this key colliding
    across three cycles whose snapshots genuinely differ. Never used to cache.
    """
    return (snapshot.symbol, snapshot.h1_last_ts, bool(session_active))


class CacheStats:
    """Hit/miss counters. Observation only — nothing reads these to decide."""

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.hits_by_symbol: Counter = Counter()
        self.misses_by_symbol: Counter = Counter()
        self.evictions = 0

    def record_hit(self, symbol: str) -> None:
        self.hits += 1
        self.hits_by_symbol[symbol] += 1

    def record_miss(self, symbol: str) -> None:
        self.misses += 1
        self.misses_by_symbol[symbol] += 1

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits, "misses": self.misses,
            "lookups": self.lookups, "hit_rate": round(self.hit_rate, 6),
            "evictions": self.evictions,
            "symbols": len(set(self.hits_by_symbol) | set(self.misses_by_symbol)),
        }


class CachingPipeline:
    """`AnalysisPipeline` by duck typing, memoized on the snapshot determinant.

    Not a subclass: `V2Analyzer` only ever calls `.run(context)` and reads
    `.stage_names` for an error message, and inheriting would invite overriding
    stage execution, which this must never do.
    """

    def __init__(
        self,
        inner: Any,
        *,
        data_snapshot_id: str = "",
        stats: Optional[CacheStats] = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._inner = inner
        self._data_snapshot_id = data_snapshot_id
        self._max_entries = max_entries
        self.stats = stats if stats is not None else CacheStats()
        #: key -> the `AnalysisContext` the inner pipeline produced.
        self._entries: "OrderedDict[tuple, Any]" = OrderedDict()

    # -- the AnalysisPipeline surface --------------------------------------

    @property
    def stages(self) -> tuple:
        return self._inner.stages

    @property
    def stage_names(self) -> tuple[str, ...]:
        return self._inner.stage_names

    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def run(self, context: Any) -> Any:
        """The memoized evaluation. `context` is a `v2.analysis.AnalysisContext`.

        Returns byte-for-byte the inner pipeline's result, or that same frozen
        instance again.
        """
        key = snapshot_key(
            context.snapshot,
            session_active=context.session_active,
            data_snapshot_id=self._data_snapshot_id,
        )
        if key in self._entries:
            self._entries.move_to_end(key)
            self.stats.record_hit(context.snapshot.symbol)
            return self._entries[key]

        result = self._inner.run(context)
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1
        self.stats.record_miss(context.snapshot.symbol)
        return result


class CachingAnalyzer:
    """`V2Analyzer` with the pipeline memoized. Exposes only its two seams.

    Behaviourally identical by construction: the M5 gate, the scoring chain and
    every stage remain the inner `V2Analyzer`'s, unmodified.
    """

    def __init__(
        self,
        inner: Optional[V2Analyzer] = None,
        *,
        data_snapshot_id: str = "",
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        base = inner if inner is not None else V2Analyzer()
        # Kept for the `prior` bypass below: `base.pipeline` is the uncached one.
        self._uncached = base
        self._pipeline = CachingPipeline(
            base.pipeline, data_snapshot_id=data_snapshot_id,
            max_entries=max_entries)
        self._cached = V2Analyzer(pipeline=self._pipeline)

    @property
    def pipeline(self) -> CachingPipeline:
        return self._pipeline

    @property
    def stats(self) -> CacheStats:
        return self._pipeline.stats

    def analyze(
        self,
        snapshot: Any,
        *,
        prior: Any = None,
        session_active: bool = False,
    ):
        """`V2Analyzer.analyze`, served from the memo when the key matches.

        `prior` is documented as intentionally unused by `V2Analyzer`, which
        makes it invisible to the key. Rather than ignore it — and risk serving a
        cached result to a caller who believed `prior` mattered — a non-`None`
        `prior` **bypasses the cache entirely** and runs the uncached analyzer.
        """
        target = self._uncached if prior is not None else self._cached
        return target.analyze(
            snapshot, prior=prior, session_active=session_active)


def build_analyzer(*, caching: bool, data_snapshot_id: str = "",
                   scoring_config: ScoringConfig = DEFAULT_SCORING_CONFIG):
    """The driver's one-line selection, extracted so it is directly testable.

    `scoring_config` is the replay end of the `ScoringConfig` seam. It defaults to
    `DEFAULT_SCORING_CONFIG`, so every existing caller keeps producing 2.2.0.

    The pipeline is built explicitly in both arms rather than only in the caching
    one. `CachingAnalyzer()` with no inner analyzer constructs its own
    `V2Analyzer()` — and therefore its own default pipeline — so passing the
    config to one arm and not the other would make `--caching` silently decide
    whether a variant took effect. That is precisely the class of bug the flag is
    documented as being unable to cause.
    """
    inner = V2Analyzer(AnalysisPipeline.default(scoring_config))
    if caching:
        return CachingAnalyzer(inner, data_snapshot_id=data_snapshot_id)
    return inner
