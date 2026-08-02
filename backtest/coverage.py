"""Stage 1 coverage analysis — evidence for the replay-window decision.

The Stage 1 corpus answers one question the platform cannot answer by reasoning:
**how far back does usable history actually go, and for which symbols?** Every
downstream decision — the replay window, the holdout length, the Stage 2 symbol
list, the shakedown universe — depends on the answer, and none of them can be
made honestly from a symbol count alone.

## This module decides nothing

It emits measurements. It never selects a start date, never picks `top_coins_limit`,
never names a "recommended" window. That restraint is not politeness: the corpus
window is the most consequential parameter in the project, and a report that
chooses it makes the choice a side effect of a default rather than a recorded
human decision with a reason attached.

What it *does* do is evaluate each candidate window against **thresholds declared
in advance** and report PASS / WARN / FAIL. A threshold fixed before the data is
seen is a protocol; a recommendation computed after the data is seen is the report
deciding. The difference is the entire scientific content of this distinction.

## The universe reconstruction must match replay exactly

The top-K union computed here determines which symbols Stage 2 acquires. If it
disagrees with what replay will actually rank, Stage 2 acquires the wrong symbols
and the error is invisible until the official run.

So the trailing-volume semantics are reproduced from
`HistoricalMarketDataAdapter.get_top_volume_symbols` **exactly**, and
`tests/backtest/test_coverage.py` asserts agreement against the real adapter
rather than trusting this comment:

* rank by quote volume summed over the trailing 24 rows (`UNIVERSE_TRAILING_ROWS`);
* those are the last 24 **stored** rows before `T`, not the last 24 hours — a
  symbol with gaps sums across them, which is what `KlineRepository.window` does;
* a symbol with fewer than 24 stored rows at `T` is ineligible, so new listings
  enter naturally rather than depending on when the window starts;
* ties break by **name ascending**, then by descending volume, stably.

The implementation is vectorised (one `searchsorted` per symbol over all
boundaries, against a prefix sum) because the per-boundary loop the adapter uses
would be ~36 million window calls at five-year scale. The semantics are identical;
only the arithmetic order differs.

## What this module does not do

No acquisition, no repair, no ranking policy of its own, no strategy, and no
writes anywhere near a run directory or a manifest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from backtest.data.repository import KlineRepository, RepositoryError
from backtest.universe import UniverseResolution

if TYPE_CHECKING:                                           # pragma: no cover
    from backtest.observe import Progress

__all__ = [
    "COVERAGE_SCHEMA_VERSION",
    "COVERAGE_REPORT_NAME",
    "CoverageError",
    "CoverageReport",
    "CoverageThresholds",
    "InstrumentClass",
    "SymbolCoverage",
    "UniverseEvidence",
    "WindowCandidate",
    "analyze_coverage",
]

#: 3 (M13.1): `union_min_completeness`, `gap_ratio` and `worst_gap_hours` are
#: measured **inside the candidate window**. Until this version they were
#: lifetime aggregates, so a hole outside a window still decided its verdict —
#: the 2023-01-01 candidate failed on a gap that ended in September 2022. The
#: field names and types are unchanged, which is precisely why the version must
#: move: a reader comparing a v2 value to a v3 value would be comparing two
#: different measurements under one name.
#:
#: 2 (M13): `universe_id` added; every instrument carries the `source` of its
#: classification; `delisted_in_union` changed meaning from "has no exchangeInfo
#: entry" to "classified from the curated backfill table"; the inventory reports
#: `from_snapshot`/`from_table` instead of `unclassified`, which can no longer
#: occur because an unclassifiable instrument now fails the run. A reader of a
#: version-1 report would misinterpret `delisted_in_union`, so the version moves.
COVERAGE_SCHEMA_VERSION = 3
COVERAGE_REPORT_NAME = "coverage.json"

H1_MS = 3_600_000

#: C-6.9. Imported by value rather than from the adapter so this module does not
#: drag the replay stack in; the equivalence test pins them together.
UNIVERSE_TRAILING_ROWS = 24


class CoverageError(Exception):
    """The Stage 1 corpus cannot be analysed."""


# ---------------------------------------------------------------------------
# Declared-in-advance acceptance thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageThresholds:
    """The quality bar, fixed **before** the corpus is looked at.

    Defaults encode the bar agreed during the methodology review. They are
    arguments, not constants, precisely so that changing them is a visible act
    recorded in the report rather than an edit to a source file.
    """

    #: Every hour of the window must be able to fill the universe, or
    #: `top_coins_limit` is silently not achieved.
    min_rankable_symbols: int = 30
    #: Completeness of a union symbol over its own listed life.
    min_union_completeness: float = 0.995
    #: An unattributed hole longer than this, in a symbol while it was ranked,
    #: is a period the strategy would have been trading blind.
    max_gap_hours: int = 24
    #: Share of the window's symbol-hours that are missing.
    max_gap_ratio: float = 0.01

    def to_dict(self) -> dict[str, Any]:
        return {"min_rankable_symbols": self.min_rankable_symbols,
                "min_union_completeness": self.min_union_completeness,
                "max_gap_hours": self.max_gap_hours,
                "max_gap_ratio": self.max_gap_ratio}


# ---------------------------------------------------------------------------
# Per-symbol evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstrumentClass:
    """One instrument's classification, as reported by `backtest.universe`.

    This module performs **no classification of its own**. Every field here is
    copied from a `Classification` the resolver produced, and `is_crypto_perpetual`
    is the resolver's `eligible` verdict — which is `v2.domain.universe.is_eligible`,
    the same function live applies. Recomputing the rule here would create a
    second rule free to drift from the first.

    `source` records whether the metadata came from a committed `exchangeInfo`
    snapshot or from the curated backfill table, so a reader can see at a glance
    how much of a union rests on human curation.
    """

    symbol: str
    contract_type: Optional[str] = None
    underlying_type: Optional[str] = None
    quote_asset: Optional[str] = None
    source: Optional[str] = None
    eligible: bool = False

    @property
    def known(self) -> bool:
        return self.underlying_type is not None

    @property
    def is_crypto_perpetual(self) -> bool:
        """The canonical Replay/Live universe rule, as resolved. Never recomputed."""
        return self.eligible

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "contract_type": self.contract_type,
                "underlying_type": self.underlying_type,
                "quote_asset": self.quote_asset, "source": self.source,
                "is_crypto_perpetual": self.is_crypto_perpetual,
                "classified": self.known}


@dataclass(frozen=True)
class SymbolCoverage:
    """What the corpus holds for one symbol, and how complete it is."""

    symbol: str
    rows: int
    first_open_ms: int
    last_open_ms: int
    expected_rows: int
    gap_count: int
    gap_hours: int
    longest_gap_hours: int
    zero_volume_hours: int
    flat_candle_hours: int

    @property
    def completeness(self) -> float:
        """Rows held over rows expected across the symbol's own span.

        Measured over `[first, last]`, not over the corpus window: a symbol
        listed in 2024 is not "70% incomplete" for a window starting in 2021.
        Availability across the window is the timeline's job, not this ratio's.
        """
        return round(self.rows / self.expected_rows, 6) if self.expected_rows else 0.0

    @property
    def lifespan_hours(self) -> int:
        return self.expected_rows

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "rows": self.rows,
                "first_open_ms": self.first_open_ms,
                "last_open_ms": self.last_open_ms,
                "first_open": _iso(self.first_open_ms),
                "last_open": _iso(self.last_open_ms),
                "expected_rows": self.expected_rows,
                "completeness": self.completeness,
                "gap_count": self.gap_count, "gap_hours": self.gap_hours,
                "longest_gap_hours": self.longest_gap_hours,
                "zero_volume_hours": self.zero_volume_hours,
                "flat_candle_hours": self.flat_candle_hours,
                "lifespan_hours": self.lifespan_hours}


# ---------------------------------------------------------------------------
# Window and universe evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowCandidate:
    """One candidate replay window, with the evidence for and against it."""

    start_ms: int
    end_ms: int
    hours: int
    symbols_available: int
    union_size: int
    hours_below_min_rank: int
    min_rankable: int
    median_rankable: float
    gap_ratio: float
    union_min_completeness: float
    worst_gap_hours: int
    verdict: str                      # PASS | WARN | FAIL
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms,
                "start": _iso(self.start_ms), "end": _iso(self.end_ms),
                "hours": self.hours,
                "symbols_available": self.symbols_available,
                "union_size": self.union_size,
                "hours_below_min_rank": self.hours_below_min_rank,
                "min_rankable": self.min_rankable,
                "median_rankable": self.median_rankable,
                "gap_ratio": self.gap_ratio,
                "union_min_completeness": self.union_min_completeness,
                "worst_gap_hours": self.worst_gap_hours,
                "verdict": self.verdict, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class UniverseEvidence:
    """Top-K union evidence — the Stage 2 sizing decision, quantified.

    `promotion_rate` is the number that settles Top-30 versus Top-50: of the
    symbol-hours spent in ranks `K+1..K2`, the share whose symbol reaches the
    top-K at any later hour in the window. A high rate means the 31–50 band is a
    genuine feeder into the traded universe and pruning it would remove symbols
    the strategy later trades; a low rate means it is a tail that never arrives.
    """

    top_k: int
    wide_k: int
    union_top_k: tuple[str, ...]
    union_wide: tuple[str, ...]
    marginal_symbols: tuple[str, ...]
    promotion_rate: Optional[float]
    promoted_symbols: tuple[str, ...]
    #: Union members whose metadata came from the curated table rather than a
    #: committed snapshot. These are the rows a reviewer must trust by hand, so
    #: the report names them rather than reporting a count.
    delisted_in_union: tuple[str, ...]
    union_hours: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"top_k": self.top_k, "wide_k": self.wide_k,
                "union_top_k_size": len(self.union_top_k),
                "union_wide_size": len(self.union_wide),
                "union_top_k": list(self.union_top_k),
                "union_wide": list(self.union_wide),
                "marginal_symbols": list(self.marginal_symbols),
                "marginal_count": len(self.marginal_symbols),
                "promotion_rate": self.promotion_rate,
                "promoted_symbols": list(self.promoted_symbols),
                "delisted_in_union": list(self.delisted_in_union),
                "union_hours": dict(sorted(self.union_hours.items()))}


@dataclass(frozen=True)
class CoverageReport:
    """The Stage 1 artifact. Descriptive plus declared thresholds; never a choice."""

    schema_version: int
    corpus_id: Optional[str]
    data_snapshot_id: str
    #: The identity of the classification that produced the ranked candidate
    #: set. `corpus_id` alone does not determine it, because `_corpus_id`
    #: excludes discovery artifacts — see `backtest.universe._universe_id`.
    universe_id: str
    timeframe: str
    generated_at: str
    thresholds: CoverageThresholds
    symbols: tuple[SymbolCoverage, ...]
    instruments: tuple[InstrumentClass, ...]
    candidates: tuple[WindowCandidate, ...]
    universe: Optional[UniverseEvidence]
    shakedown: tuple[str, ...]
    shakedown_reasons: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    # -- inventory summaries ----------------------------------------------

    @property
    def earliest_open_ms(self) -> Optional[int]:
        return min((s.first_open_ms for s in self.symbols), default=None)

    @property
    def latest_open_ms(self) -> Optional[int]:
        return max((s.last_open_ms for s in self.symbols), default=None)

    def to_dict(self) -> dict[str, Any]:
        classified = [i for i in self.instruments if i.known]
        return {
            "schema_version": self.schema_version,
            "corpus_id": self.corpus_id,
            "data_snapshot_id": self.data_snapshot_id,
            "universe_id": self.universe_id,
            "timeframe": self.timeframe,
            "generated_at": self.generated_at,
            "thresholds": self.thresholds.to_dict(),
            "inventory": {
                "symbols": len(self.symbols),
                "classified": len(classified),
                "unclassified": len(self.instruments) - len(classified),
                "crypto_perpetual": sum(
                    1 for i in self.instruments if i.is_crypto_perpetual),
                "excluded_by_universe_rule": sum(
                    1 for i in classified if not i.is_crypto_perpetual),
                "from_snapshot": sum(
                    1 for i in self.instruments if i.source == "snapshot"),
                "from_table": sum(
                    1 for i in self.instruments if i.source == "table"),
                "by_underlying_type": _tally(
                    i.underlying_type or "unknown" for i in self.instruments),
                "by_contract_type": _tally(
                    i.contract_type or "unknown" for i in self.instruments),
                "earliest_open": _iso(self.earliest_open_ms),
                "latest_open": _iso(self.latest_open_ms),
            },
            "integrity": {
                "total_rows": sum(s.rows for s in self.symbols),
                "total_gap_hours": sum(s.gap_hours for s in self.symbols),
                "symbols_with_gaps": sum(
                    1 for s in self.symbols if s.gap_count),
                "symbols_fully_complete": sum(
                    1 for s in self.symbols if s.completeness >= 1.0),
                "worst_gap_hours": max(
                    (s.longest_gap_hours for s in self.symbols), default=0),
            },
            "symbols": [s.to_dict() for s in self.symbols],
            "instruments": [i.to_dict() for i in self.instruments],
            "window_candidates": [c.to_dict() for c in self.candidates],
            "universe": self.universe.to_dict() if self.universe else None,
            "shakedown_candidates": list(self.shakedown),
            "shakedown_reasons": {k: list(v) for k, v
                                  in sorted(self.shakedown_reasons.items())},
            "notes": list(self.notes),
        }

    def write(self, out_dir: Path | str, *, overwrite: bool = False) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / COVERAGE_REPORT_NAME
        if path.exists() and not overwrite:
            raise CoverageError(
                f"{path} exists; a coverage report is evidence for a decision "
                f"that may already have been taken. Pass overwrite=True to "
                f"replace it deliberately.")
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        (out / "coverage.md").write_text(self.render(), encoding="utf-8")
        return path

    def render(self) -> str:
        """Human-readable summary. Evidence only — no recommendation."""
        data = self.to_dict()
        inv, integrity = data["inventory"], data["integrity"]
        out = [
            "# Stage 1 Coverage Report", "",
            f"`data_snapshot_id` **{self.data_snapshot_id}** · corpus "
            f"`{self.corpus_id}` · universe `{self.universe_id}` · generated "
            f"{self.generated_at}", "",
            "> This report chooses nothing. It measures the corpus and scores "
            "each candidate window against thresholds declared in advance. "
            "The replay window is a human decision, recorded with its reason.",
            "", "## Inventory", "",
            f"- symbols: **{inv['symbols']}**",
            f"- classified from a committed snapshot: {inv['from_snapshot']} · "
            f"from the curated backfill table: {inv['from_table']}",
            f"- crypto perpetuals (PERPETUAL / COIN / USDT): "
            f"**{inv['crypto_perpetual']}** · excluded by the universe rule: "
            f"{inv['excluded_by_universe_rule']}",
            f"- underlying types: {inv['by_underlying_type']}",
            f"- span: {inv['earliest_open']} -> {inv['latest_open']}",
            "", "## Integrity", "",
            f"- rows: {integrity['total_rows']:,}",
            f"- symbols with gaps: {integrity['symbols_with_gaps']} of "
            f"{inv['symbols']} · total gap hours: "
            f"{integrity['total_gap_hours']:,}",
            f"- longest single gap: {integrity['worst_gap_hours']} h",
            "", "## Window candidates", "",
            "| start | hours | symbols | union | h<min rank | gap ratio | "
            "union min completeness | verdict |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for candidate in data["window_candidates"]:
            out.append(
                f"| {candidate['start'][:10]} | {candidate['hours']:,} | "
                f"{candidate['symbols_available']} | {candidate['union_size']} | "
                f"{candidate['hours_below_min_rank']:,} | "
                f"{candidate['gap_ratio']:.4f} | "
                f"{candidate['union_min_completeness']:.4f} | "
                f"**{candidate['verdict']}** |")
        for candidate in data["window_candidates"]:
            if candidate["reasons"]:
                out.append("")
                out.append(f"- **{candidate['start'][:10]}** — "
                           + "; ".join(candidate["reasons"]))

        universe = data["universe"]
        if universe:
            rate = universe["promotion_rate"]
            out += [
                "", f"## Universe — top-{universe['top_k']} vs "
                    f"top-{universe['wide_k']}", "",
                f"- union at top-{universe['top_k']}: "
                f"**{universe['union_top_k_size']}** symbols",
                f"- union at top-{universe['wide_k']}: "
                f"**{universe['union_wide_size']}** symbols "
                f"(+{universe['marginal_count']} marginal)",
                f"- promotion rate of ranks {universe['top_k'] + 1}"
                f"-{universe['wide_k']} into the top-{universe['top_k']}: "
                + ("insufficient data" if rate is None else f"**{rate:.4f}**"),
                f"- union members classified from the curated table: "
                f"{len(universe['delisted_in_union'])}",
            ]
        out += ["", "## Shakedown candidates", "",
                f"{len(self.shakedown)} symbols selected as the union of the "
                f"extremes of each pathology dimension, plus liquid controls.", ""]
        for symbol in self.shakedown:
            reasons = ", ".join(self.shakedown_reasons.get(symbol, ()))
            out.append(f"- `{symbol}` — {reasons}")
        if self.notes:
            out += ["", "## Notes", ""] + [f"- {note}" for note in self.notes]
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _tally(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _instrument_classes(
    resolution: UniverseResolution, symbols: Sequence[str]
) -> tuple[InstrumentClass, ...]:
    """Copy the resolver's classifications for `symbols`, in order.

    A pure projection. No metadata is read, no rule is applied and no default is
    invented here: `resolve()` has already failed loudly if any symbol could not
    be classified, so a `KeyError` below would be a genuine bug rather than a
    condition to paper over.
    """
    by_symbol = resolution.by_symbol()
    return tuple(
        InstrumentClass(
            symbol=symbol,
            contract_type=by_symbol[symbol].contract_type,
            underlying_type=by_symbol[symbol].underlying_type,
            quote_asset=by_symbol[symbol].quote_asset,
            source=by_symbol[symbol].source,
            eligible=by_symbol[symbol].eligible,
        )
        for symbol in symbols
    )


def _symbol_coverage(symbol: str, rows: np.ndarray) -> SymbolCoverage:
    times = rows["timestamp"].astype(np.int64)
    first, last = int(times[0]), int(times[-1])
    expected = (last - first) // H1_MS + 1
    deltas = np.diff(times)
    missing = (deltas // H1_MS) - 1
    holes = missing[missing > 0]
    highs, lows = rows["high"], rows["low"]
    return SymbolCoverage(
        symbol=symbol, rows=int(len(times)),
        first_open_ms=first, last_open_ms=last, expected_rows=int(expected),
        gap_count=int(holes.size), gap_hours=int(holes.sum()) if holes.size else 0,
        longest_gap_hours=int(holes.max()) if holes.size else 0,
        zero_volume_hours=int(np.count_nonzero(rows["v"] <= 0)),
        flat_candle_hours=int(np.count_nonzero(highs <= lows)),
    )


# ---------------------------------------------------------------------------
# Universe reconstruction — vectorised, semantics identical to C-6.9
# ---------------------------------------------------------------------------

def _trailing_volume(
    boundaries_ms: np.ndarray, times: np.ndarray, quote_volume: np.ndarray
) -> np.ndarray:
    """Quote volume over the last 24 **stored** rows before each boundary.

    `NaN` where the symbol has fewer than 24 stored rows, which is C-6.9's
    eligibility rule. Uses a prefix sum plus one `searchsorted`, matching
    `KlineRepository.window(count=24)` — which also takes the last 24 stored
    rows and does not require them to be contiguous.
    """
    prefix = np.concatenate(([0.0], np.cumsum(quote_volume.astype(np.float64))))
    index = np.searchsorted(times, boundaries_ms, side="left")
    out = np.full(boundaries_ms.shape, np.nan, dtype=np.float64)
    eligible = index >= UNIVERSE_TRAILING_ROWS
    if np.any(eligible):
        hi = index[eligible]
        out[eligible] = prefix[hi] - prefix[hi - UNIVERSE_TRAILING_ROWS]
    return out


def _rank_matrix(volumes: np.ndarray, limit: int) -> np.ndarray:
    """Per-boundary top-`limit` symbol indices; `-1` pads a short universe.

    Tie-break reproduces the adapter: candidates ordered by name ascending, then
    stably sorted by descending volume. `np.lexsort` takes the *last* key as
    primary, so `(name_index, -volume)` gives volume-major, name-minor — the
    same ordering, and equally a pure function of the data.
    """
    n_boundaries, n_symbols = volumes.shape
    take = min(limit, n_symbols)
    out = np.full((n_boundaries, take), -1, dtype=np.int64)
    names = np.arange(n_symbols)
    for row in range(n_boundaries):
        values = volumes[row]
        eligible = np.flatnonzero(~np.isnan(values))
        if eligible.size == 0:
            continue
        order = np.lexsort((names[eligible], -values[eligible]))
        chosen = eligible[order][:take]
        out[row, :chosen.size] = chosen
    return out


# ---------------------------------------------------------------------------
# Shakedown selection — union of per-dimension extremes
# ---------------------------------------------------------------------------

#: Each entry is `(name, key, want_max)`. Selecting the top-K on *each*
#: dimension independently and unioning beats a single weighted score: a scalar
#: needs arbitrary weights, and whichever component has the widest numeric range
#: silently dominates, producing K symbols that share one pathology while the
#: others go untested. The union also makes the selection self-documenting —
#: every symbol is present *because of a named property*.
_SHAKEDOWN_DIMENSIONS: tuple[tuple[str, Any, bool], ...] = (
    ("most_gaps", lambda s: s.gap_count, True),
    ("longest_gap", lambda s: s.longest_gap_hours, True),
    ("most_gap_hours", lambda s: s.gap_hours, True),
    ("least_complete", lambda s: s.completeness, False),
    ("most_zero_volume", lambda s: s.zero_volume_hours, True),
    ("most_flat_candles", lambda s: s.flat_candle_hours, True),
    ("shortest_lifespan", lambda s: s.lifespan_hours, False),
    ("fewest_rows", lambda s: s.rows, False),
)


def _shakedown(
    symbols: Sequence[SymbolCoverage], *, per_dimension: int, controls: int,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    reasons: dict[str, list[str]] = {}
    for name, key, want_max in _SHAKEDOWN_DIMENSIONS:
        ranked = sorted(symbols, key=lambda s: (key(s), s.symbol),
                        reverse=want_max)
        for entry in ranked[:per_dimension]:
            reasons.setdefault(entry.symbol, []).append(name)
    # Controls: the most complete, longest-lived symbols. Without them a crash
    # cannot be attributed — a pathology-specific bug and a general regression
    # look identical.
    liquid = sorted(symbols, key=lambda s: (-s.rows, s.symbol))[:controls]
    for entry in liquid:
        reasons.setdefault(entry.symbol, []).append("control")
    ordered = tuple(sorted(reasons))
    return ordered, {k: tuple(v) for k, v in reasons.items()}


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------

def analyze_coverage(
    corpus_root: Path | str,
    *,
    resolution: UniverseResolution,
    timeframe: str = "1h",
    candidate_starts: Sequence[int] = (),
    end_ms: Optional[int] = None,
    thresholds: Optional[CoverageThresholds] = None,
    top_k: int = 30,
    wide_k: int = 50,
    shakedown_per_dimension: int = 3,
    shakedown_controls: int = 3,
    observer: Optional["Progress"] = None,
    universe_stride: int = 1,
) -> CoverageReport:
    """Measure a Stage 1 corpus and score candidate windows against `thresholds`.

    `resolution` is **injected, never computed here**. This module measures a
    corpus; deciding what an instrument *is* belongs to `backtest.universe`, and
    duplicating that decision would create a second eligibility rule. Only
    symbols the resolution marks eligible take part in the universe
    reconstruction, so the ranked candidate set is the same one replay will rank
    and the same predicate live applies.

    `candidate_starts` are epoch **seconds** on an hour boundary. Supplying none
    scores nothing — the caller chooses which windows to evaluate, because
    enumerating them here would be this function proposing answers.

    `universe_stride` samples every Nth boundary for the universe reconstruction.
    The union is a set over time and converges quickly, so a stride bounds the
    cost of a five-year × 800-symbol reconstruction. It is recorded in the notes,
    because a sampled union is a slightly *smaller* union and the reader must
    know the direction of the approximation.
    """
    root = Path(corpus_root)
    thresholds = thresholds or CoverageThresholds()
    try:
        repository = KlineRepository(root / "canonical")
    except RepositoryError as exc:
        raise CoverageError(f"cannot open {root / 'canonical'}: {exc}") from exc

    names = sorted(repository.symbols(timeframe))
    if not names:
        raise CoverageError(
            f"the corpus at {root} holds no {timeframe} datasets; Stage 1 must "
            f"complete before coverage can be analysed")

    if observer is not None:
        observer.set_phase("coverage/symbols", total=len(names),
                           unit="symbols")

    # -- 1. per-symbol coverage -------------------------------------------
    coverages: list[SymbolCoverage] = []
    series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name in names:
        rows = repository.range(name, timeframe, start_ms=0,
                                end_ms=np.iinfo(np.int64).max)
        if len(rows) == 0:
            if observer is not None:
                observer.advance(1, detail=name)
            continue
        coverages.append(_symbol_coverage(name, rows))
        series[name] = (rows["timestamp"].astype(np.int64),
                        rows["qv"].astype(np.float64))
        if observer is not None:
            observer.advance(1, detail=name)

    instruments = _instrument_classes(resolution, [c.symbol for c in coverages])
    latest = max(c.last_open_ms for c in coverages)
    horizon = int(end_ms) if end_ms is not None else latest + H1_MS

    # -- 2. universe reconstruction ---------------------------------------
    universe: Optional[UniverseEvidence] = None
    notes: list[str] = []
    candidates: list[WindowCandidate] = []

    if candidate_starts:
        earliest = min(int(s) * 1000 for s in candidate_starts)
        grid = np.arange(earliest, horizon, H1_MS * max(1, universe_stride),
                         dtype=np.int64)
        if observer is not None:
            observer.set_phase("coverage/universe", total=len(series),
                               unit="symbols")

        # C-6.9: rank only what live would rank. Before this filter existed the
        # reconstruction ranked every dataset in the corpus, which on the Stage 1
        # inventory admitted 121 tokenised equities, commodities and index
        # products; they took a mean of 4.5 and a maximum of 13 of the 30
        # universe slots through 2026. Availability is still decided by the data
        # (a symbol with no rows cannot rank), so delisted crypto stays.
        eligible = resolution.eligible_symbols
        ordered_names = [c.symbol for c in coverages if c.symbol in eligible]
        if not ordered_names:
            raise CoverageError(
                "no eligible instrument remains after the universe rule; a "
                "corpus that can rank nothing cannot evidence a window")
        volumes = np.full((grid.size, len(ordered_names)), np.nan,
                          dtype=np.float64)
        for column, name in enumerate(ordered_names):
            times, quote_volume = series[name]
            volumes[:, column] = _trailing_volume(grid, times, quote_volume)
            if observer is not None:
                observer.advance(1, detail=name)

        if observer is not None:
            observer.set_phase("coverage/windows",
                               total=len(candidate_starts),
                               unit="candidates")

        rankable = np.count_nonzero(~np.isnan(volumes), axis=1)
        wide_ranks = _rank_matrix(volumes, wide_k)
        # Window scoring measures the population that can actually be traded:
        # averaging gap ratios over instruments the universe rule excludes would
        # score a window on data no run will ever read.
        eligible_coverages = [c for c in coverages if c.symbol in eligible]
        # The stored open times, which is what window-scoped measurement needs.
        # `SymbolCoverage` holds lifetime aggregates and cannot answer "what was
        # missing between these two instants" — relying on it is exactly the
        # M13.1 defect.
        window_series = {name: series[name][0] for name in eligible
                         if name in series}
        universe = _universe_evidence(
            grid, wide_ranks, ordered_names, eligible_coverages, instruments,
            top_k=top_k, wide_k=wide_k)
        if universe_stride > 1:
            notes.append(
                f"universe reconstructed on every {universe_stride}th hour; a "
                f"sampled union is a lower bound on the true union")

        for start in candidate_starts:
            candidates.append(_score_window(
                start_ms=int(start) * 1000, end_ms=horizon, grid=grid,
                rankable=rankable, ranks=wide_ranks, names=ordered_names,
                coverages=eligible_coverages, series=window_series,
                thresholds=thresholds, top_k=top_k))
            if observer is not None:
                observer.advance(1)

    # A shakedown rehearses a real run, so it draws only from instruments a real
    # run could trade.
    eligible_all = resolution.eligible_symbols
    shakedown, reasons = _shakedown(
        [c for c in coverages if c.symbol in eligible_all],
        per_dimension=shakedown_per_dimension,
        controls=shakedown_controls)

    from_table = sum(1 for i in instruments if i.source == "table")
    if from_table:
        notes.append(
            f"{from_table} instrument(s) were classified from the curated "
            f"backfill table rather than a committed exchangeInfo snapshot, "
            f"because they were delisted before snapshot capture began; see "
            f"reference/universe_table.json (table_version "
            f"{resolution.table_version})")
    excluded = sum(1 for i in instruments if not i.is_crypto_perpetual)
    if excluded:
        notes.append(
            f"{excluded} instrument(s) in the corpus are not crypto perpetuals "
            f"and take no part in the universe reconstruction")

    return CoverageReport(
        schema_version=COVERAGE_SCHEMA_VERSION,
        corpus_id=_corpus_id(root),
        data_snapshot_id=repository.data_snapshot_id,
        universe_id=resolution.universe_id,
        timeframe=timeframe,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        thresholds=thresholds,
        symbols=tuple(coverages), instruments=instruments,
        candidates=tuple(candidates), universe=universe,
        shakedown=shakedown, shakedown_reasons=reasons, notes=tuple(notes),
    )


def _corpus_id(root: Path) -> Optional[str]:
    path = root / "corpus.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("corpus_id")
    except (OSError, ValueError):
        return None


def _universe_evidence(
    grid: np.ndarray, ranks: np.ndarray, names: Sequence[str],
    coverages: Sequence[SymbolCoverage],
    instruments: Sequence[InstrumentClass],
    *, top_k: int, wide_k: int,
) -> UniverseEvidence:
    inner = ranks[:, :top_k]
    union_inner_idx = np.unique(inner[inner >= 0])
    union_wide_idx = np.unique(ranks[ranks >= 0])
    union_inner = {names[i] for i in union_inner_idx.tolist()}
    union_wide = {names[i] for i in union_wide_idx.tolist()}

    hours: dict[str, int] = {}
    for index in union_inner_idx.tolist():
        hours[names[index]] = int(np.count_nonzero(inner == index))

    # Promotion: of the symbols that ever occupy ranks top_k+1..wide_k, how many
    # later reach the top-K at any hour in the window.
    band = ranks[:, top_k:wide_k]
    promoted: list[str] = []
    band_symbols = np.unique(band[band >= 0])
    for index in band_symbols.tolist():
        first_band = int(np.argmax(np.any(band == index, axis=1)))
        inner_hits = np.flatnonzero(np.any(inner == index, axis=1))
        if inner_hits.size and int(inner_hits[-1]) > first_band:
            promoted.append(names[index])
    rate = (round(len(promoted) / band_symbols.size, 6)
            if band_symbols.size else None)

    # Union members whose metadata came from the curated backfill table rather
    # than a committed exchangeInfo snapshot. Named, not counted: these are
    # exactly the rows whose correctness rests on a human, and a reviewer must
    # be able to see which union members depend on them.
    classified = {i.symbol: i for i in instruments}
    delisted = tuple(sorted(
        s for s in union_wide if classified[s].source == "table"))

    return UniverseEvidence(
        top_k=top_k, wide_k=wide_k,
        union_top_k=tuple(sorted(union_inner)),
        union_wide=tuple(sorted(union_wide)),
        marginal_symbols=tuple(sorted(union_wide - union_inner)),
        promotion_rate=rate, promoted_symbols=tuple(sorted(promoted)),
        delisted_in_union=delisted, union_hours=hours,
    )


@dataclass(frozen=True)
class _WindowStats:
    """One symbol measured **strictly inside** one candidate window.

    Every field is computed from the rows the symbol holds in `[lo, hi)`, where
    `lo = max(window_start, first_row)` and `hi = min(window_end, last_row + 1h)`.
    That intersection is what makes the measurement both window-scoped and fair:

    - clipping to the window means a hole outside it cannot reach the verdict;
    - clipping to the symbol's own life means "not listed yet" and "delisted
      already" are not counted as missing data, which is the reason the original
      implementation measured over the lifespan in the first place.

    Taking only one of the two clips is what produced the M13.1 defect: the
    lifespan clip without the window clip failed the 2023 candidate on a gap that
    ended in September 2022.
    """

    rows: int
    expected: int
    gap_hours: int
    longest_gap_hours: int
    gap_count: int

    @property
    def completeness(self) -> float:
        return round(self.rows / self.expected, 6) if self.expected else 0.0


def _window_coverage(
    times: np.ndarray, start_ms: int, end_ms: int
) -> Optional[_WindowStats]:
    """Measure one symbol inside `[start_ms, end_ms)`, or `None` if absent.

    `times` are the symbol's stored H1 open timestamps, ascending. Half-open at
    both ends, matching `_score_window`'s grid mask, so an hour belongs to
    exactly one window and no hour is counted twice.

    A gap that *straddles* a window edge is counted for the part inside the
    window and only that part: `lo` is clamped to the window start using the
    symbol's **first row overall**, so a symbol that was already listed before
    the window and whose data resumes late is charged for the missing hours,
    while a symbol that simply had not listed yet is not.
    """
    if times.size == 0:
        return None
    lo = max(int(start_ms), int(times[0]))
    hi = min(int(end_ms), int(times[-1]) + H1_MS)
    if hi <= lo:
        return None
    selected = times[(times >= lo) & (times < hi)]
    expected = (hi - lo) // H1_MS
    rows = int(selected.size)

    holes: list[int] = []
    if rows:
        if int(selected[0]) > lo:
            holes.append((int(selected[0]) - lo) // H1_MS)
        interior = (np.diff(selected) // H1_MS) - 1
        holes.extend(int(h) for h in interior[interior > 0])
        tail = hi - (int(selected[-1]) + H1_MS)
        if tail > 0:
            holes.append(tail // H1_MS)
    return _WindowStats(
        rows=rows, expected=int(expected),
        gap_hours=int(expected - rows),
        longest_gap_hours=max(holes) if holes else 0,
        gap_count=len(holes),
    )


def _score_window(
    *, start_ms: int, end_ms: int, grid: np.ndarray, rankable: np.ndarray,
    ranks: np.ndarray, names: Sequence[str],
    coverages: Sequence[SymbolCoverage],
    series: Mapping[str, np.ndarray],
    thresholds: CoverageThresholds, top_k: int,
) -> WindowCandidate:
    """Score one candidate window.

    Every metric below is measured inside `[start_ms, end_ms)`. Nothing about a
    symbol's history outside the window reaches the verdict — that is the whole
    of the M13.1 correction, and `coverages` is now used only to decide *which*
    symbols overlap the window, never to measure them.
    """
    mask = (grid >= start_ms) & (grid < end_ms)
    in_window = rankable[mask]
    inner = ranks[mask][:, :top_k]
    union_idx = np.unique(inner[inner >= 0])
    union = {names[i] for i in union_idx.tolist()}

    available = [c for c in coverages
                 if c.first_open_ms < end_ms and c.last_open_ms >= start_ms]
    stats: dict[str, _WindowStats] = {}
    for entry in available:
        measured = _window_coverage(series[entry.symbol], start_ms, end_ms)
        if measured is not None:
            stats[entry.symbol] = measured

    # Missing symbol-hours over *expected* symbol-hours, both inside the window.
    # The denominator moved with the numerator: charging every available symbol
    # for the whole span would count "not listed yet" as missing data, which is
    # the same conflation the completeness fix removes.
    expected_hours = sum(s.expected for s in stats.values())
    gap_hours = sum(s.gap_hours for s in stats.values())
    gap_ratio = round(gap_hours / expected_hours, 6) if expected_hours else 0.0

    union_stats = {s: stats[s] for s in union if s in stats}
    worst_symbol = min(
        union_stats, key=lambda s: (union_stats[s].completeness, s),
        default="")
    min_completeness = round(
        union_stats[worst_symbol].completeness, 6) if worst_symbol else 0.0
    gap_symbol = max(
        union_stats, key=lambda s: (union_stats[s].longest_gap_hours, s),
        default="")
    worst_gap = union_stats[gap_symbol].longest_gap_hours if gap_symbol else 0
    span_hours = max(1, (end_ms - start_ms) // H1_MS)
    below = int(np.count_nonzero(in_window < thresholds.min_rankable_symbols))

    reasons: list[str] = []
    verdict = "PASS"
    if below:
        verdict = "FAIL"
        reasons.append(
            f"{below:,} hour(s) have fewer than "
            f"{thresholds.min_rankable_symbols} rankable symbols, so "
            f"top_coins_limit is not achieved there")
    if min_completeness < thresholds.min_union_completeness:
        verdict = "FAIL"
        # Naming the symbol is not a methodology change; it is the audit trail
        # that would have exposed M13.1 on the first read instead of requiring a
        # census of every gap in the corpus.
        reasons.append(
            f"union symbol {worst_symbol} is only {min_completeness:.4f} "
            f"complete inside this window "
            f"(< {thresholds.min_union_completeness})")
    if gap_ratio > thresholds.max_gap_ratio:
        verdict = "FAIL"
        reasons.append(f"gap ratio {gap_ratio:.4f} exceeds "
                       f"{thresholds.max_gap_ratio}")
    if verdict == "PASS" and worst_gap > thresholds.max_gap_hours:
        verdict = "WARN"
        reasons.append(
            f"longest gap inside this window is {worst_gap} h, in union symbol "
            f"{gap_symbol} (> {thresholds.max_gap_hours}); attribute it before "
            f"accepting")

    return WindowCandidate(
        start_ms=start_ms, end_ms=end_ms, hours=int(span_hours),
        symbols_available=len(available), union_size=len(union),
        hours_below_min_rank=below,
        min_rankable=int(in_window.min()) if in_window.size else 0,
        median_rankable=float(np.median(in_window)) if in_window.size else 0.0,
        gap_ratio=gap_ratio, union_min_completeness=min_completeness,
        worst_gap_hours=int(worst_gap), verdict=verdict,
        reasons=tuple(reasons),
    )
