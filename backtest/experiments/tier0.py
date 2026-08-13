"""Tier-0 — a slot-aware counterfactual over outcomes that are already resolved.

Phase 3.5C. **This is not a replay.** It re-decides *admission* over trades whose
outcomes the Outcome Engine has already resolved, and nothing else. Read
`LIMITATIONS` before quoting any number it produces.

## The problem it exists to solve

Subsetting realized trades is the obvious way to screen an EXP-1-style filter, and
it is biased. The official run holds **41,625 `CAPPED` signals** — decisions whose
insertion the slot cap suppressed (C-2.2), resolved anyway by the Outcome Engine.
A filter therefore does not merely delete its targets: it frees slots, and a
suppressed candidate takes each one. Measured on the official bundle the cap binds
in **15,627 of 30,600 cycles (51%)**, so substitution is a dominant term, not a
correction — ignoring it overstated a "drop LONG-in-premium" screen by roughly 3x.

## Occupancy is read, not modeled

The first version of this module inferred residency from the trade's own life
(`boundary` -> `exit_T`) and **failed its own validation**: under a rule that
rejects nothing it displaced 32,520 observed residents and promoted 7,031
candidates into slots that were really full. The cause is a category error — a
memory slot is released when the *thesis* is evicted (TTL, `miss_count`, kill,
replace), which has nothing to do with when the *trade* exits. Median residency is
3 cycles; `max_holding_h1` is 168.

So residency is not inferred at all. `memory_snapshots.jsonl` records the resident
set at every cycle, and that is the ground truth this module reads: 69,916 theses,
each with a contiguous cycle interval (measured: zero non-contiguous resumptions),
peak concurrency exactly `SLOT_LIMIT`. Under the identity rule nothing is rejected,
so occupancy is the observed occupancy and no promotion is possible — `slack` is 0
by construction rather than by tuning, and any non-zero value is a real
inconsistency in the bundle worth investigating.

## The one assumption

A promoted candidate's residency is the single quantity that cannot be observed:
how long it would have held the slot depends on TTL and `miss_count` dynamics
driven by prices after an admission that never happened. It is therefore a
declared constant — `PROMOTED_RESIDENCY_CYCLES`, the observed **median** residency
— and it is a sensitivity knob, not a fact. `Tier0Result` records the value used,
so no number can be quoted without it.

## Two orderings, and why they differ

`c98_order_key` is **exit** order (`metrics.equity`: "the order is by exit, not by
instantiation"). It is right for accounting an equity curve and wrong for
simulating admission — a slot decision cannot be taken in the order trades finish.
Admission is walked in **arrival** order (cycle, then `universe_rank`, then
symbol), the processing order the lifecycle saw (C-16.1: those fields "restore the
intra-cycle ordering the lifecycle depends on — the processing order decides the
slot cap"). Net R is then accounted in `c98_order_key` order, where that key
belongs. Both orders are total, so both are deterministic.

## Which population occupies a slot, and which refills one

| `shadow_kind` | what happened | role here |
|---|---|---|
| `None` | inserted and alerted | occupied a slot |
| `UNALERTED_INSERT` | **inserted**, not alerted (`outcome/builder.py:367`) | occupied a slot |
| `CAPPED` | insertion **suppressed** by the cap (C-2.2) | refill pool |

`UNALERTED_INSERT` was resident — it appears in the snapshots. Treating it as a
refill candidate would let one thesis hold a slot and also compete for it.
`KILL_COOLDOWN` shadows are excluded from the pool for a related reason: freeing a
slot does not lift a cooldown. (The official bundle contains none.)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from backtest.dataset import CanonicalKey, canonical_key
from backtest.metrics.equity import c98_order_key
from backtest.outcome.record import OutcomeRecord

__all__ = [
    "LIMITATIONS",
    "MEMORY_SNAPSHOTS_FILENAME",
    "key_of",
    "PROMOTED_RESIDENCY_CYCLES",
    "REFILL_KINDS",
    "RESIDENT_KINDS",
    "SLOT_LIMIT",
    "AdmissionRule",
    "Clause",
    "Occupancy",
    "Thesis",
    "Tier0Error",
    "Tier0Result",
    "load_occupancy",
    "simulate",
]

MEMORY_SNAPSHOTS_FILENAME = "memory_snapshots.jsonl"

#: `LifecycleConfig.max_open_positions` (v2/lifecycle/config.py:36), and the value
#: stamped into the official manifest's `core.lifecycle_config`. Not invented here.
SLOT_LIMIT = 10

#: How long a promoted candidate is assumed to hold its slot: the observed median
#: residency on the official bundle (mean 3.84, max 39). A declared assumption,
#: exposed as a parameter, reported with every result.
PROMOTED_RESIDENCY_CYCLES = 3

RESIDENT_KINDS: tuple[Optional[str], ...] = (None, "UNALERTED_INSERT")
REFILL_KINDS: tuple[str, ...] = ("CAPPED",)

LIMITATIONS = [
    "NOT A REPLAY. Scores, entries, stops and targets are those the baseline "
    "produced; nothing is re-analyzed and no price is re-walked.",
    "Admission only. A rule that changes a score would move decision tiers and so "
    "change which rows exist at all — invisible here; that needs tier 2.",
    "A promoted candidate's residency is assumed, not observed "
    "(PROMOTED_RESIDENCY_CYCLES). Promotion counts and refilled R scale with it.",
    "A promoted candidate keeps the levels and resolved outcome of its own "
    "boundary. Sound — resolution does not depend on admission — but it assumes "
    "the freed slot is taken at that candidate's own arrival cycle.",
    "Second-order lifecycle effects are not simulated: a promoted thesis would "
    "itself have produced kill cooldowns, REPLACE decisions and miss_count "
    "history, changing later admissions in ways no bundle can record.",
    "Rejecting a thesis frees its whole observed interval. If it held the slot "
    "that long because of a REPLACE the rule also removes, the vacancy is "
    "overstated.",
    "Burn-in trades are excluded from reported R exactly as the metrics engine "
    "excludes them, but they still occupy slots, because they did.",
    "The result is a bound, not a point estimate.",
]


class Tier0Error(Exception):
    """The simulation cannot be run over the supplied population."""


# ---------------------------------------------------------------------------
# admission rules — EXP-1 shaped, deliberately not general
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Clause:
    """One rejection clause: a conjunction over direction and two HTF labels.

    A field left `None` is a wildcard, so `Clause(direction="LONG")` rejects every
    long and `Clause(direction="LONG", location="premium")` only longs in premium.
    Three fields is not an oversight — EXP-1 is a statement about direction and
    location, and a general predicate language would be an abstraction built
    before its second use.
    """

    direction: Optional[str] = None
    location: Optional[str] = None
    d1_bias: Optional[str] = None

    def __post_init__(self) -> None:
        if self.direction is None and self.location is None and self.d1_bias is None:
            raise Tier0Error(
                "an empty clause rejects every trade, which is never the intended "
                "rule; state at least one condition")

    def matches(self, direction: Optional[str], labels: dict[str, Any]) -> bool:
        if self.direction is not None and direction != self.direction:
            return False
        if self.location is not None and labels.get("location") != self.location:
            return False
        if self.d1_bias is not None and labels.get("d1_bias") != self.d1_bias:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, "location": self.location,
                "d1_bias": self.d1_bias}


@dataclass(frozen=True)
class AdmissionRule:
    """A named rule. A trade is rejected when **any** clause matches it.

    Two ways to say what a rule rejects, and they answer different questions.
    `reject` holds `Clause`es — a small conjunction over direction and two HTF
    labels, which is what an EXP-1-style hypothesis is made of. `reject_keys`
    holds **precomputed canonical row keys** (C-9.4), for a rule whose predicate
    this module has no business knowing: a re-scored decision tier depends on the
    weight table, the threshold ladder and the choppy quirk, and teaching a slot
    simulator to evaluate that would put scoring logic in the one place that must
    stay a pure capacity model.

    A key set is also the more auditable of the two. It is a value, so it hashes:
    `to_dict` records its size and digest, and a result can be checked against the
    decision set that produced it without re-deriving anything.
    """

    name: str
    reject: tuple[Clause, ...] = ()
    reject_keys: frozenset[CanonicalKey] = frozenset()

    @property
    def is_identity(self) -> bool:
        """True for the rule that changes nothing — the validation rule."""
        return not self.reject and not self.reject_keys

    def rejects(self, direction: Optional[str], labels: dict[str, Any]) -> bool:
        return any(clause.matches(direction, labels) for clause in self.reject)

    def rejects_record(
        self, record: OutcomeRecord, labels: dict[str, Any]
    ) -> bool:
        """Whether this rule rejects `record`. Keys first — they are a lookup."""
        if self.reject_keys and key_of(record) in self.reject_keys:
            return True
        return self.rejects(record.direction, labels)

    @property
    def reject_keys_digest(self) -> Optional[str]:
        if not self.reject_keys:
            return None
        # Sorted on the stringified key so the digest cannot depend on set
        # iteration order, and stringified because a key mixes str and
        # Optional[int] and is therefore not orderable as a tuple.
        payload = "\n".join(
            "|".join(str(part) for part in key)
            for key in sorted(self.reject_keys, key=lambda k: tuple(map(str, k))))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "reject": [c.to_dict() for c in self.reject],
                "reject_keys": len(self.reject_keys),
                "reject_keys_digest": self.reject_keys_digest,
                "is_identity": self.is_identity}


def key_of(record: OutcomeRecord) -> CanonicalKey:
    """The canonical row key of the ledger row this outcome came from (C-9.4).

    `cycle_index` is deliberately **not** passed, matching `dataset._key_of`. C-9.4
    states the first three components are already unique at replay cadence, and the
    ledger index in `dataset` is built the same way — including the fourth here
    would produce keys that never match a label, which reads as a total join
    failure rather than as a bug.
    """
    return canonical_key(
        str(record.run_id), str(record.symbol), float(record.h1_last_ts))


# ---------------------------------------------------------------------------
# occupancy ground truth
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Thesis:
    """One resident thesis and the contiguous cycle interval it held a slot."""

    signal_id: str
    symbol: str
    first_cycle: int
    last_cycle: int

    @property
    def cycles(self) -> int:
        return self.last_cycle - self.first_cycle + 1


@dataclass(frozen=True)
class Occupancy:
    """Observed residency, read from `memory_snapshots.jsonl`.

    `full_cycles` is reported because it is the reason this module exists: when
    the cap binds, a slot a filter frees is immediately worth something.
    """

    theses: tuple[Thesis, ...]
    cycles: int
    full_cycles: int
    slot_limit: int
    #: Theses whose snapshot interval was not contiguous. Expected zero; non-zero
    #: means the interval model does not fit this bundle.
    non_contiguous: int
    #: Observed resident count per cycle. A promotion is allowed only where the
    #: counterfactual count is *below* this, i.e. only into a slot the rule
    #: demonstrably freed. That is what makes the identity rule promote nothing
    #: without needing the snapshot to resolve intra-cycle ordering: a `CAPPED`
    #: candidate's own existence is proof the cap bound at its arrival, even in a
    #: cycle whose end-of-cycle snapshot shows a vacancy.
    active_counts: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "theses": len(self.theses),
            "cycles": self.cycles,
            "cycles_at_slot_limit": self.full_cycles,
            "share_of_cycles_at_limit": (
                self.full_cycles / self.cycles if self.cycles else None),
            "slot_limit": self.slot_limit,
            "non_contiguous_residencies": self.non_contiguous,
        }


def load_occupancy(
    run_dir: Path | str, *, slot_limit: int = SLOT_LIMIT
) -> Occupancy:
    """Read the per-cycle resident set and reduce it to intervals.

    Read-only and streaming: the file is ~124 MB on the official bundle.
    """
    path = Path(run_dir) / MEMORY_SNAPSHOTS_FILENAME
    if not path.exists():
        raise Tier0Error(
            f"no {MEMORY_SNAPSHOTS_FILENAME} in {run_dir}. Occupancy is read from "
            f"the snapshots, never inferred from a trade's own life — a slot is "
            f"released when the thesis is evicted, not when the trade exits.")

    spans: dict[str, list] = {}
    observed: dict[int, int] = {}
    cycles = 0
    full = 0
    non_contiguous = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            snapshot = json.loads(line)
            cycle = int(snapshot.get("cycle_index") or 0)
            active = snapshot.get("active") or {}
            cycles += 1
            observed[cycle] = len(active)
            if len(active) >= slot_limit:
                full += 1
            for symbol, thesis in active.items():
                signal_id = thesis.get("signal_id")
                if not signal_id:
                    continue
                span = spans.get(signal_id)
                if span is None:
                    spans[signal_id] = [symbol, cycle, cycle]
                else:
                    if cycle != span[2] + 1:
                        non_contiguous += 1
                    span[2] = cycle

    theses = tuple(sorted(
        (Thesis(signal_id=sid, symbol=span[0], first_cycle=span[1],
                last_cycle=span[2]) for sid, span in spans.items()),
        key=lambda t: (t.first_cycle, t.symbol, t.signal_id)))
    highest = max(observed) if observed else -1
    counts = tuple(observed.get(cycle, 0) for cycle in range(highest + 1))
    return Occupancy(theses=theses, cycles=cycles, full_cycles=full,
                     slot_limit=slot_limit, non_contiguous=non_contiguous,
                     active_counts=counts)


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------

def _summary(values: list[float]) -> dict[str, Any]:
    n = len(values)
    net = sum(values)
    wins = sum(1 for v in values if v > 0)
    return {"n": n, "net_r": net, "avg_r": (net / n) if n else None,
            "win_rate": (wins / n) if n else None}


@dataclass
class Tier0Result:
    """What the rule admitted, what it rejected, and what refilled the gaps."""

    rule: AdmissionRule
    run_id: Optional[str] = None
    slot_limit: int = SLOT_LIMIT
    promoted_residency_cycles: int = PROMOTED_RESIDENCY_CYCLES
    kept: list[float] = field(default_factory=list)
    rejected: list[float] = field(default_factory=list)
    refilled: list[float] = field(default_factory=list)
    baseline: list[float] = field(default_factory=list)
    promotions: int = 0
    #: Promotions under the identity rule. Must be 0 — those slots were full.
    slack: int = 0
    still_capped: int = 0
    symbol_busy: int = 0
    resident_theses: int = 0
    #: Reported rather than dropped silently: a join that quietly loses rows
    #: understates every population it feeds.
    unjoined_theses: int = 0
    unjoined_outcomes: int = 0
    occupancy: Optional[Occupancy] = None

    @property
    def counterfactual_net_r(self) -> float:
        return sum(self.kept) + sum(self.refilled)

    @property
    def baseline_net_r(self) -> float:
        return sum(self.baseline)

    @property
    def delta_net_r(self) -> float:
        return self.counterfactual_net_r - self.baseline_net_r

    @property
    def naive_delta_net_r(self) -> float:
        """The delta a subset estimate reports — no substitution.

        The gap between this and `delta_net_r` is the bias this module removes.
        """
        return -sum(self.rejected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "derived",
            "analysis": "tier0_slot_counterfactual",
            "is_replay": False,
            "run_id": self.run_id,
            "rule": self.rule.to_dict(),
            "slot_limit": self.slot_limit,
            "promoted_residency_cycles": self.promoted_residency_cycles,
            "occupancy": None if self.occupancy is None
            else self.occupancy.to_dict(),
            "populations": {
                "baseline_resident": _summary(self.baseline),
                "kept": _summary(self.kept),
                "rejected": _summary(self.rejected),
                "refilled": _summary(self.refilled),
            },
            "counts": {
                "resident_theses": self.resident_theses,
                "promotions": self.promotions,
                "still_capped": self.still_capped,
                "symbol_busy": self.symbol_busy,
                "unjoined_theses": self.unjoined_theses,
                "unjoined_outcomes": self.unjoined_outcomes,
            },
            "net_r": {
                "baseline": self.baseline_net_r,
                "counterfactual": self.counterfactual_net_r,
                "delta": self.delta_net_r,
                "delta_if_substitution_ignored": self.naive_delta_net_r,
                "substitution_cost": self.delta_net_r - self.naive_delta_net_r,
            },
            "validation": {
                "slack": self.slack,
                "slack_meaning": "promotions under an identity rule. Must be 0: a "
                                 "CAPPED candidate exists only at a cycle where "
                                 "the cap was binding, so with nothing rejected "
                                 "there is no free slot. Non-zero means the "
                                 "reconstructed occupancy disagrees with the "
                                 "bundle.",
            },
            "limitations": LIMITATIONS,
        }


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------

def _scored_r(record: OutcomeRecord) -> Optional[float]:
    if record.in_burn_in or record.realized_r is None:
        return None
    return float(record.realized_r)


def simulate(
    records: Iterable[OutcomeRecord],
    occupancy: Occupancy,
    rule: AdmissionRule,
    *,
    labels_for: Optional[Callable[[OutcomeRecord], dict[str, Any]]] = None,
    slot_limit: int = SLOT_LIMIT,
    promoted_residency_cycles: int = PROMOTED_RESIDENCY_CYCLES,
) -> Tier0Result:
    """Re-decide admission under `rule`, against observed occupancy.

    `labels_for` maps a record to its label dict — pass `LabeledDataset.labels_for`.
    The join rule lives in `dataset` and is deliberately not duplicated here.

    A rule that reads a label is refused when no label source was supplied, rather
    than silently seeing `{}` for every trade and therefore rejecting nothing: a
    filter that quietly becomes the identity rule reports a delta of zero and
    looks like a finding.
    """
    if slot_limit < 1:
        raise Tier0Error(f"slot_limit must be >= 1, got {slot_limit}")
    if promoted_residency_cycles < 1:
        raise Tier0Error(
            f"promoted_residency_cycles must be >= 1, got "
            f"{promoted_residency_cycles}")

    needs_labels = any(c.location is not None or c.d1_bias is not None
                       for c in rule.reject)
    if needs_labels and labels_for is None:
        raise Tier0Error(
            f"rule {rule.name!r} conditions on a ledger label (location / "
            f"d1_bias) but no `labels_for` was supplied, so every label would "
            f"read empty and the rule would reject nothing. Pass "
            f"LabeledDataset.labels_for.")

    def labels_of(record: OutcomeRecord) -> dict[str, Any]:
        return {} if labels_for is None else (labels_for(record) or {})

    result = Tier0Result(
        rule=rule, slot_limit=slot_limit,
        promoted_residency_cycles=promoted_residency_cycles,
        occupancy=occupancy)
    result.resident_theses = len(occupancy.theses)

    # -- index the outcome records ------------------------------------------
    by_signal_id: dict[str, OutcomeRecord] = {}
    refills: list[OutcomeRecord] = []
    for record in records:
        if result.run_id is None:
            result.run_id = record.run_id
        kind = record.shadow_kind
        if kind in RESIDENT_KINDS:
            if record.signal_id:
                # First writer wins: a signal_id reused across a partial refresh
                # describes one residency, and the earliest record is the one the
                # snapshot interval opened with.
                by_signal_id.setdefault(str(record.signal_id), record)
            else:
                result.unjoined_outcomes += 1
            r = _scored_r(record)
            if r is not None:
                result.baseline.append(r)
        elif kind in REFILL_KINDS:
            refills.append(record)

    # -- which observed residencies survive the rule ------------------------
    counts = [0] * (max(occupancy.cycles, 1) + promoted_residency_cycles + 1)
    busy: list[set[str]] = [set() for _ in range(len(counts))]
    observed_counts = occupancy.active_counts
    survivors: list[OutcomeRecord] = []

    def hold(symbol: str, first: int, last: int) -> None:
        for cycle in range(first, last + 1):
            if 0 <= cycle < len(counts):
                counts[cycle] += 1
                busy[cycle].add(symbol)

    for thesis in occupancy.theses:
        record = by_signal_id.get(thesis.signal_id)
        if record is None:
            # No outcome joined, so the rule cannot be evaluated on it. It held
            # the slot, so it keeps holding it — dropping it would invent
            # capacity that never existed.
            result.unjoined_theses += 1
            hold(thesis.symbol, thesis.first_cycle, thesis.last_cycle)
            continue

        if rule.rejects_record(record, labels_of(record)):
            r = _scored_r(record)
            if r is not None:
                result.rejected.append(r)
            continue

        survivors.append(record)
        hold(thesis.symbol, thesis.first_cycle, thesis.last_cycle)

    # -- let suppressed candidates compete for what the rule freed ----------
    arrivals: dict[int, list[OutcomeRecord]] = {}
    for record in refills:
        if rule.rejects_record(record, labels_of(record)):
            continue
        cycle = -1 if record.cycle_index is None else int(record.cycle_index)
        arrivals.setdefault(cycle, []).append(record)

    promoted: list[OutcomeRecord] = []
    for cycle in sorted(arrivals):
        if not 0 <= cycle < len(counts):
            result.still_capped += len(arrivals[cycle])
            continue
        # Arrival order within a cycle is the universe rank: the processing order
        # that decided the cap in the first place (C-16.1).
        for record in sorted(
            arrivals[cycle],
            key=lambda r: (-1 if r.universe_rank is None
                           else int(r.universe_rank), r.symbol),
        ):
            if record.symbol in busy[cycle]:
                result.symbol_busy += 1
                continue
            # Two gates, and the second is the one that matters. A `CAPPED`
            # candidate exists only because the cap bound at its arrival, so it
            # may be promoted only into a vacancy the *rule* created: the
            # counterfactual count must be below what was observed at that cycle.
            # Without this, ~7.4% of candidates would be promoted into cycles
            # whose end-of-cycle snapshot shows a vacancy that did not exist when
            # the symbol was processed — an intra-cycle ordering artifact worth
            # 3,094 spurious promotions on the official bundle.
            if counts[cycle] >= slot_limit:
                result.still_capped += 1
                continue
            if cycle < len(observed_counts) and counts[cycle] >= observed_counts[cycle]:
                result.still_capped += 1
                continue
            promoted.append(record)
            result.promotions += 1
            if rule.is_identity:
                result.slack += 1
            hold(record.symbol, cycle, cycle + promoted_residency_cycles - 1)

    # -- account R in C-9.8 order, where that key belongs -------------------
    for record in sorted(survivors, key=c98_order_key):
        r = _scored_r(record)
        if r is not None:
            result.kept.append(r)
    for record in sorted(promoted, key=c98_order_key):
        r = _scored_r(record)
        if r is not None:
            result.refilled.append(r)

    return result
