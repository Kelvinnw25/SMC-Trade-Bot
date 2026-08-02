"""Segmentation — a dimension registry, not a pile of report code (Task 3).

REPLAY_CONTRACT.md §12 "Segmentations", which asks for *each metric × each
dimension* with per-segment `n` and bootstrap CIs.

## The shape of the solution

A **dimension** is a name plus a function from `(record, labels)` to a key. That is
the whole abstraction. `group_by` applies one dimension and returns
`{key: MetricSet}`; `segment_all` applies every registered dimension. Adding a
dimension is one entry in a registry — never a new branch in a report, which is
what "no hardcoded report logic" means in practice.

Three properties the registry buys, all of which a hand-rolled report loses:

- **the report cannot forget a dimension.** It iterates the registry.
- **a dimension can be unavailable.** When labels are missing, its extractor
  returns `None` for every record and the report says the dimension was
  unavailable, instead of showing an empty table that reads like "no trades had a
  regime".
- **multi-valued dimensions work.** A `notes` token dimension returns a *tuple* of
  keys, so one trade contributes to several groups. Those dimensions do not
  partition the population and are marked `partitions=False`, because a reader who
  sums their segments and gets more than the total deserves to have been warned.

## Where a dimension's data comes from

`source="outcome"` reads the `OutcomeRecord`. `source="label"` reads the labeled
dataset's attribute mapping (§11 step 4) — every strategy-context dimension §12
asks for lives there, because `OutcomeRecord` carries none of them. The metrics
engine never learns where labels came from; it receives a plain dict.

## Bucketing

Continuous quantities are bucketed with **fixed, declared edges**. Quantile buckets
would move with the data and make two baselines incomparable — which is precisely
what a frozen baseline exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence, Union

from backtest.metrics.bootstrap import (
    BOOTSTRAP_SEED, MIN_SEGMENT_N, MULTIPLE_COMPARISON_THRESHOLD,
)
from backtest.metrics.core import MetricSet, compute_metrics
from backtest.outcome.record import OutcomeRecord

__all__ = [
    "DIMENSIONS",
    "DIMENSIONS_BY_NAME",
    "Dimension",
    "SegmentSet",
    "Segmentation",
    "group_by",
    "register",
    "segment_all",
]

Key = Optional[Union[str, tuple[str, ...]]]
Extractor = Callable[[OutcomeRecord, dict[str, Any]], Key]


@dataclass(frozen=True)
class Dimension:
    """One way of slicing the population."""

    name: str
    extract: Extractor
    source: str = "outcome"          # "outcome" | "label"
    description: str = ""
    partitions: bool = True          # False for multi-valued dimensions
    clause: str = "§12"

    def keys_for(self, record: OutcomeRecord,
                 labels: dict[str, Any]) -> tuple[str, ...]:
        """The keys this record belongs to under this dimension.

        `None` means "this record has no value for this dimension" and excludes it
        from the dimension entirely — deliberately not bucketed as `"unknown"`,
        which would silently mix "the field was absent" with "the field said
        unknown". The report states the excluded count instead.
        """
        value = self.extract(record, labels)
        if value is None:
            return ()
        if isinstance(value, tuple):
            return value
        return (value,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "partitions": self.partitions,
            "clause": self.clause,
        }


@dataclass(frozen=True)
class SegmentSet:
    """Every segment of one dimension."""

    dimension: Dimension
    segments: dict[str, MetricSet]
    excluded: int = 0
    available: bool = True

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def inferential_segments(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, metrics in self.segments.items()
                            if metrics.inferential))

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.to_dict(),
            "available": self.available,
            "excluded_records": self.excluded,
            "segment_count": len(self.segments),
            "inferential_segments": list(self.inferential_segments),
            "segments": {
                key: metrics.to_dict()
                for key, metrics in sorted(self.segments.items())
            },
        }


@dataclass(frozen=True)
class Segmentation:
    """The whole segmented view, plus §12's mandatory cautions."""

    dimensions: dict[str, SegmentSet] = field(default_factory=dict)

    @property
    def total_segments(self) -> int:
        return sum(len(entry) for entry in self.dimensions.values())

    @property
    def multiple_comparison_caution(self) -> bool:
        """§12: mandatory whenever more than 20 segments are reported."""
        return self.total_segments > MULTIPLE_COMPARISON_THRESHOLD

    @property
    def unavailable(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, entry in self.dimensions.items()
                            if not entry.available))

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_segments": self.total_segments,
            "multiple_comparison_caution": self.multiple_comparison_caution,
            "multiple_comparison_note": (
                f"{self.total_segments} segments are reported; with this many "
                f"comparisons some will look significant by chance. A segment may "
                f"motivate a hypothesis but never justify a change on its own "
                f"(§12)." if self.multiple_comparison_caution else None),
            "unavailable_dimensions": list(self.unavailable),
            "dimensions": {
                name: entry.to_dict()
                for name, entry in sorted(self.dimensions.items())
            },
        }


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def group_by(
    dimension: Dimension,
    records: Iterable[OutcomeRecord],
    *,
    labels: Optional[dict] = None,
    label_lookup: Optional[Callable[[OutcomeRecord], dict[str, Any]]] = None,
    with_intervals: bool = True,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    min_segment_n: int = MIN_SEGMENT_N,
    n_resamples: Optional[int] = None,
) -> SegmentSet:
    """Apply one dimension and aggregate each group."""
    records = tuple(records)
    lookup = label_lookup or _lookup_from(labels)
    buckets: dict[str, list[OutcomeRecord]] = {}
    excluded = 0
    for record in records:
        keys = dimension.keys_for(record, lookup(record))
        if not keys:
            excluded += 1
            continue
        for key in keys:
            buckets.setdefault(str(key), []).append(record)
    return SegmentSet(
        dimension=dimension,
        segments={
            key: compute_metrics(
                group, with_intervals=with_intervals,
                bootstrap_seed=bootstrap_seed, min_segment_n=min_segment_n,
                n_resamples=n_resamples)
            for key, group in buckets.items()
        },
        excluded=excluded,
        # A dimension every record was excluded from is *unavailable*, not empty —
        # the usual cause is an outcomes-only dataset with no ledger to label it.
        available=bool(buckets) or not records,
    )


def segment_all(
    records: Iterable[OutcomeRecord],
    *,
    labels: Optional[dict] = None,
    label_lookup: Optional[Callable[[OutcomeRecord], dict[str, Any]]] = None,
    dimensions: Optional[Sequence[Dimension]] = None,
    with_intervals: bool = True,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    min_segment_n: int = MIN_SEGMENT_N,
    n_resamples: Optional[int] = None,
) -> Segmentation:
    """Apply every registered dimension (or the given subset)."""
    records = tuple(records)
    chosen = tuple(dimensions) if dimensions is not None else DIMENSIONS
    return Segmentation(dimensions={
        dimension.name: group_by(
            dimension, records, labels=labels, label_lookup=label_lookup,
            with_intervals=with_intervals, bootstrap_seed=bootstrap_seed,
            min_segment_n=min_segment_n, n_resamples=n_resamples)
        for dimension in chosen
    })


def _lookup_from(labels: Optional[dict]
                 ) -> Callable[[OutcomeRecord], dict[str, Any]]:
    if not labels:
        return lambda _record: {}

    def lookup(record: OutcomeRecord) -> dict[str, Any]:
        key = (record.run_id, record.symbol, int(record.h1_last_ts), None)
        found = labels.get(key)
        if found is None:
            found = labels.get((record.run_id, record.symbol,
                                int(record.h1_last_ts)))
        return found or {}

    return lookup


# ---------------------------------------------------------------------------
# Bucketing helpers — fixed edges, declared here and nowhere else
# ---------------------------------------------------------------------------

def bucket(value: Optional[float], edges: Sequence[float],
           *, fmt: str = "g") -> Optional[str]:
    """Label `value` by fixed `edges`. `None` in, `None` out."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:                       # NaN
        return None
    if number < edges[0]:
        return f"<{edges[0]:{fmt}}"
    for low, high in zip(edges, edges[1:]):
        if low <= number < high:
            return f"[{low:{fmt}},{high:{fmt}})"
    return f">={edges[-1]:{fmt}}"


RR_EDGES = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0)
HOLDING_EDGES = (1.0, 2.0, 4.0, 8.0, 24.0, 48.0, 96.0, 168.0)
SCORE_EDGES = (6.0, 8.0, 10.0, 12.0, 14.0, 16.0)
RANK_EDGES = (1.0, 4.0, 11.0, 21.0, 31.0)
ATR_EDGES = (0.0, 0.002, 0.005, 0.01, 0.02, 0.05)
MARGIN_EDGES = (0.0, 0.5, 1.0, 2.0, 4.0)
STRENGTH_EDGES = (0.0, 0.25, 0.5, 0.75, 1.0)
OB_COUNT_EDGES = (1.0, 2.0, 4.0, 8.0)


def _flag(name: str) -> Extractor:
    """A boolean label as a two-key dimension. `None` (absent) stays excluded, so
    "the confirmation was false" is never conflated with "we do not know"."""
    def extract(_record: OutcomeRecord, labels: dict[str, Any]) -> Key:
        value = labels.get(name)
        if value is None:
            return None
        return "true" if bool(value) else "false"
    return extract


def _text(name: str) -> Extractor:
    def extract(_record: OutcomeRecord, labels: dict[str, Any]) -> Key:
        value = labels.get(name)
        if value is None or value == "":
            return None
        return str(value)
    return extract


def _numeric_bucket(name: str, edges: Sequence[float]) -> Extractor:
    def extract(_record: OutcomeRecord, labels: dict[str, Any]) -> Key:
        return bucket(labels.get(name), edges)
    return extract


#: The seven regime labels analysis actually emits ([regime.py:8](v2/domain/regime.py#L8))
#: mapped onto the three coarse classes a report reads more easily.
#:
#: This is a **reporting grouping, not a strategy concept** — nothing in `v2` knows
#: these three words, and `regime` itself is segmented separately and unmapped so
#: the raw label is always available. See the M10 findings: the milestone brief asked
#: for TRENDING / RANGING / CHOPPY, which is not the vocabulary the strategy uses.
REGIME_CLASS = {
    "expansion_up": "TRENDING",
    "expansion_down": "TRENDING",
    "accumulation": "RANGING",
    "distribution": "RANGING",
    "rotational": "RANGING",
    "compression": "CHOPPY",
    "neutral": "CHOPPY",
    "unknown": "CHOPPY",
}

_TRENDING = {"BULLISH", "BEARISH"}


def _htf_alignment(record: OutcomeRecord, labels: dict[str, Any]) -> Key:
    """D1 and H4 bias agreement with the trade's own direction.

    ALIGNED — both biases favour the direction; OPPOSED — both oppose it;
    PARTIAL — exactly one; NEUTRAL — neither is directional. §12 asks for "D1/H4
    bias combinations"; this is the three-way collapse that answers "did the higher
    timeframe agree", with the raw biases still segmented separately.
    """
    d1, h4 = labels.get("d1_bias"), labels.get("h4_bias")
    if d1 is None or h4 is None:
        return None
    favour = "BULLISH" if record.direction == "LONG" else "BEARISH"
    against = "BEARISH" if record.direction == "LONG" else "BULLISH"
    agree = sum(1 for bias in (d1, h4) if str(bias).upper() == favour)
    oppose = sum(1 for bias in (d1, h4) if str(bias).upper() == against)
    if agree == 2:
        return "ALIGNED"
    if oppose == 2:
        return "OPPOSED"
    if agree == 0 and oppose == 0:
        return "NEUTRAL"
    return "PARTIAL"


def _window_truncated(_record: OutcomeRecord, labels: dict[str, Any]) -> Key:
    """§12's window-truncation flag: was the served window short of the full
    119/499 closed rows (C-6.2)? Legitimately different behaviour, not a bug — but
    it must be visible, not inexplicable."""
    d1, h1 = labels.get("d1_rows"), labels.get("h1_rows")
    if d1 is None or h1 is None:
        return None
    return "true" if (int(d1) < 119 or int(h1) < 499) else "false"


def _notes_tokens(_record: OutcomeRecord, labels: dict[str, Any]) -> Key:
    """Every `notes` token, as a multi-valued dimension — §12's per-token lift.

    A trade carries several tokens, so it lands in several groups and the segments
    do not sum to the population. `partitions=False` says so.
    """
    notes = labels.get("notes")
    if not notes:
        return None
    tokens = tuple(sorted({str(token) for token in notes if token}))
    return tokens or None


def _weekday(record: OutcomeRecord, _labels: dict[str, Any]) -> Key:
    return _utc(record.boundary).strftime("%a")


def _hour(record: OutcomeRecord, _labels: dict[str, Any]) -> Key:
    return f"{_utc(record.boundary).hour:02d}"


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def _outcome_field(name: str) -> Extractor:
    def extract(record: OutcomeRecord, _labels: dict[str, Any]) -> Key:
        value = getattr(record, name, None)
        if value is None or value == "":
            return None
        return str(value)
    return extract


DIMENSIONS: tuple[Dimension, ...] = (
    # -- from the OutcomeRecord ------------------------------------------
    Dimension("direction", _outcome_field("direction"),
              description="LONG / SHORT"),
    Dimension("entry_mode", _outcome_field("entry_mode"),
              description="CONTINUATION / RETRACEMENT"),
    Dimension("alert_tier", _outcome_field("alert_tier"),
              description="the tier that was alerted (A+ / VALID / WATCH)"),
    Dimension("symbol", _outcome_field("symbol"), description="per symbol"),
    Dimension("entry_source", _outcome_field("entry_source"),
              description="which entry rule produced the level"),
    Dimension("tp_source", _outcome_field("tp_source"),
              description="which liquidity target was chosen"),
    Dimension("fill_kind", _outcome_field("fill_kind"),
              description="LIMIT_TOUCH / LIMIT_GAP / IMMEDIATE / MARKET_WALK_ORIGIN",
              clause="C-4.1/C-4.2"),
    Dimension("exit_reason", _outcome_field("exit_reason"),
              description="the mechanism that closed the trade", clause="§4"),
    Dimension("cancel_reason", _outcome_field("cancel_reason"),
              description="why a pending trade was cancelled", clause="C-4.8"),
    Dimension("shadow_kind", _outcome_field("shadow_kind"),
              description="research tier; never mixed into headline metrics",
              clause="C-2.2"),
    Dimension("rr_bucket",
              lambda record, _labels: bucket(record.structural_rr, RR_EDGES),
              description="structural reward-to-risk bands", clause="C-9.6"),
    Dimension("holding_bucket",
              lambda record, _labels: bucket(record.holding_candles,
                                             HOLDING_EDGES),
              description="H1 candles held", clause="C-4.10"),
    Dimension("score_band",
              lambda record, _labels: bucket(record.score, SCORE_EDGES),
              description="scoring bands"),
    Dimension("universe_rank_band",
              lambda record, _labels: bucket(record.universe_rank, RANK_EDGES),
              description="liquidity tier by processing rank", clause="C-5.6.2"),
    Dimension("weekday", _weekday, description="UTC weekday of instantiation"),
    Dimension("hour_of_day", _hour, description="UTC hour of instantiation"),
    Dimension("strategy_version", _outcome_field("strategy_version"),
              description="lineage", clause="C-16.6.3"),
    Dimension("contract_version", _outcome_field("contract_version"),
              description="lineage", clause="C-16.6.3"),
    Dimension("outcome_config_hash", _outcome_field("outcome_config_hash"),
              description="lineage; outcomes from different configs are never "
                          "comparable without segmentation",
              clause="C-4.12"),
    Dimension("data_snapshot_id", _outcome_field("data_snapshot_id"),
              description="lineage", clause="C-6.10"),
    Dimension("source", _outcome_field("source"),
              description="replay / live", clause="C-16.6"),

    # -- from the labeled dataset ----------------------------------------
    Dimension("regime", _text("regime"), source="label",
              description="the regime label analysis emitted, unmapped"),
    Dimension("regime_class",
              lambda _record, labels: REGIME_CLASS.get(
                  str(labels.get("regime") or "")) or None,
              source="label",
              description="TRENDING / RANGING / CHOPPY — a reporting grouping "
                          "over the seven real regimes, not a strategy concept"),
    Dimension("htf_alignment", _htf_alignment, source="label",
              description="ALIGNED / PARTIAL / OPPOSED / NEUTRAL, D1+H4 vs the "
                          "trade direction"),
    Dimension("session_active", _flag("session_active"), source="label",
              description="the SNIPER session gate", clause="C-5.4"),
    Dimension("weekly_bias", _text("weekly_bias"), source="label"),
    Dimension("d1_bias", _text("d1_bias"), source="label"),
    Dimension("h4_bias", _text("h4_bias"), source="label"),
    Dimension("location", _text("location"), source="label"),
    Dimension("direction_source", _text("direction_source"), source="label"),
    Dimension("sweep", _flag("sweep"), source="label",
              description="liquidity sweep confirmation"),
    Dimension("fvg", _flag("fvg"), source="label",
              description="fair-value-gap confirmation"),
    Dimension("displacement", _flag("displacement"), source="label"),
    Dimension("fresh_ob", _flag("fresh_ob"), source="label"),
    Dimension("volume_spike", _flag("volume_spike"), source="label"),
    Dimension("target_clear", _flag("target_clear"), source="label"),
    Dimension("htf_context_clean", _flag("htf_context_clean"), source="label"),
    Dimension("m5_choch", _flag("m5_choch"), source="label"),
    Dimension("veto_active", _flag("veto_active"), source="label"),
    Dimension("anchor_override", _flag("anchor_override"), source="label"),
    Dimension("momentum_weakening", _flag("momentum_weakening"), source="label"),
    Dimension("selected_ob_fresh", _flag("selected_ob_fresh"), source="label"),
    Dimension("selected_ob_rank",
              lambda _record, labels: (
                  None if labels.get("selected_ob_rank") is None
                  else str(int(labels["selected_ob_rank"]))),
              source="label", description="rank of the chosen order block"),
    Dimension("ob_candidate_band",
              _numeric_bucket("ob_candidate_count", OB_COUNT_EDGES),
              source="label"),
    Dimension("sweep_reclaim_band",
              _numeric_bucket("sweep_reclaim_strength", STRENGTH_EDGES),
              source="label"),
    Dimension("tp_rr_bucket", _numeric_bucket("tp_rr", RR_EDGES),
              source="label", description="target-side RR bands"),
    Dimension("atr_band", _numeric_bucket("atr_last", ATR_EDGES),
              source="label",
              description="absolute ATR bands. NOTE: not ATR-relative — see the "
                          "M10 findings"),
    Dimension("htf_margin_band",
              lambda _record, labels: bucket(
                  None if labels.get("htf_bull") is None
                  or labels.get("htf_bear") is None
                  else abs(float(labels["htf_bull"]) - float(labels["htf_bear"])),
                  MARGIN_EDGES),
              source="label", description="|htf_bull − htf_bear| margin"),
    Dimension("window_truncated", _window_truncated, source="label",
              description="was the served window short of 119 D1 / 499 H1 rows",
              clause="C-6.2/C-16.1"),
    Dimension("notes_token", _notes_tokens, source="label", partitions=False,
              description="per-token lift; multi-valued, so segments do not sum "
                          "to the population"),
)

DIMENSIONS_BY_NAME = {dimension.name: dimension for dimension in DIMENSIONS}


def register(dimension: Dimension) -> None:
    """Add a dimension at runtime.

    Exists so an exploratory notebook can slice on something new without editing
    this module. A dimension that earns a place in the baseline belongs in
    `DIMENSIONS` above, where it is reviewed and versioned.
    """
    global DIMENSIONS
    if dimension.name in DIMENSIONS_BY_NAME:
        raise ValueError(f"dimension {dimension.name!r} is already registered")
    DIMENSIONS = DIMENSIONS + (dimension,)
    DIMENSIONS_BY_NAME[dimension.name] = dimension
