"""ScoringConfig — every literal the score engine reads, in one immutable object.

`score_signal` was a verbatim port of V1's method, with its weights, thresholds,
RR bands and regime sets written inline. This module lifts those literals into a
frozen configuration object so an experiment can vary them without editing
`scoring.py`.

## What this module is NOT

It is not a rules engine, a plugin system, or a DSL. `score_signal` keeps its
exact control flow — same branches, same order, same `notes` sequence — and only
its *literals* are read from here. That constraint is not stylistic: `notes` is a
recorded ledger field, and the acceptance test for this seam is that the default
configuration reproduces 886,954 recorded rows with zero mismatches. Any
restructuring that changed when a note is appended would fail it.

There is no config file discovery at runtime either. A `ScoringConfig` is a plain
argument; loading one from an experiment directory is the backtest layer's job
(`backtest.experiments.scoring_variant`), because analysis performs no I/O.

## Weight 0 keeps the note; an absent key removes it

The single mechanism this seam adds is `ScoreAccumulator.award(note)`:

- the note's weight is **0** → the score is unchanged and the note is still
  appended. The component fired and the audit trail says so;
- the note is **absent from the table** → neither score nor note. The component
  is gone from the vocabulary.

That distinction is the point. Phase A found five components that fire 0 times in
373,723 scored rows — but "never observed on this corpus" is not "unreachable"
(`neutral_penalty` needs a neutral regime; `no_ob` needs a RETRACEMENT without a
fresh OB). Zeroing their weights is bit-identical on this corpus *and* leaves a
future firing visible in `notes` instead of silently subtracting 2. Deleting them
would not.

The convention already exists in V1: the `rr_1.5` band appends a note worth 0.

## Reasons and note names are schema, not parameters

Early-exit reasons (`bad_rr`, `no_continuation_displacement`, …) and note names
stay module constants. They identify rows in the recorded ledger and in every
downstream analytic; making them configurable would let a variant rename a field
that other tools join on. Only *values* are configurable.

## Immutability

The dataclass is frozen and every nested mapping is wrapped in a
`MappingProxyType` at construction, so neither rebinding a field nor mutating a
weight table in place is possible. Overrides are produced by `with_overrides`,
which returns a new object.

Purity: no I/O, no clock, no state. This module imports only the standard
library — it does not even depend on `v2.domain`.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "ALL_MODES",
    "COMPONENT_ARMS",
    "DEFAULT_SCORING_CONFIG",
    "ModeThresholds",
    "ScoreAccumulator",
    "ScoringConfig",
    "ScoringConfigError",
]


class ScoringConfigError(Exception):
    """A scoring configuration is not usable as a scoring configuration."""


# The three entry modes, and the mode an unrecognised one normalises to
# (scoring.py / V1 strategy.py:790).
CONTINUATION = "CONTINUATION"
RETRACEMENT = "RETRACEMENT"
WATCH_ONLY = "WATCH_ONLY"
ALL_MODES = (CONTINUATION, RETRACEMENT, WATCH_ONLY)


#: Components with an else-branch: each arm is an independent note, so an
#: override may touch one without the other. That is legal and almost always a
#: mistake — zeroing `sweep` while keeping `weak_sweep` leaves a penalty with no
#: bonus. Consulted by tooling and by reviewers; the engine does not read it.
COMPONENT_ARMS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "sweep": ("sweep", "weak_sweep"),
    "fresh_ob": ("fresh_ob", "no_ob"),
    "volume_spike": ("weak_volume",),
})


def _freeze_weights(
    weights: Mapping[str, Mapping[str, int]]
) -> Mapping[str, Mapping[str, int]]:
    frozen: dict[str, Mapping[str, int]] = {}
    for mode, table in weights.items():
        if not isinstance(table, Mapping):
            raise ScoringConfigError(
                f"weights[{mode!r}] must be a mapping of note -> int, got "
                f"{type(table).__name__}")
        checked: dict[str, int] = {}
        for note, weight in table.items():
            if isinstance(weight, bool) or not isinstance(weight, int):
                # bool is an int subclass and would silently score 0/1.
                raise ScoringConfigError(
                    f"weights[{mode!r}][{note!r}] must be an int, got "
                    f"{weight!r} ({type(weight).__name__})")
            checked[str(note)] = int(weight)
        frozen[str(mode)] = MappingProxyType(checked)
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class ModeThresholds:
    """The decision tiers for one entry mode.

    `None` means the mode does not award that tier — WATCH_ONLY has no `A+` and
    no `VALID`, which is a property of the mode, not a threshold of infinity.

    `a_plus_requires_displacement` carries V1's RETRACEMENT quirk: a score at or
    above the `A+` threshold *without* displacement is published as VALID, not
    demoted to the `valid` band (strategy.py:1043-1046).
    """

    a_plus: Optional[int] = None
    valid: Optional[int] = None
    watch: Optional[int] = None
    a_plus_requires_displacement: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "a_plus": self.a_plus,
            "valid": self.valid,
            "watch": self.watch,
            "a_plus_requires_displacement": self.a_plus_requires_displacement,
        }


@dataclass(frozen=True)
class ScoringConfig:
    """Every value `score_signal` reads that is not market data."""

    #: entry_mode -> note -> score contribution. A note IS a component name: the
    #: engine appends exactly one note per term, which is what makes a recorded
    #: `notes` list decomposable back into its terms.
    weights: Mapping[str, Mapping[str, int]]
    thresholds: Mapping[str, ModeThresholds]

    #: direction -> the HTF bias and price location that count as aligned.
    alignment: Mapping[str, Mapping[str, str]]

    #: Regime label sets, in the engine's own if/elif order.
    expansion_regimes: tuple[str, ...]
    squeeze_regimes: tuple[str, ...]
    penalty_regimes: tuple[str, ...]

    #: RR engine. Below `rr_floor` the engine returns SKIP/`bad_rr` before any
    #: tier is assigned; `rr_bands` is scanned in order, first match wins, and no
    #: match appends no note at all.
    rr_floor: float
    rr_bands: tuple[tuple[float, int], ...]

    #: Applied by `ScoringStage` after the tier has been chosen — a V1 ordering
    #: quirk, preserved because the recorded tiers were produced under it.
    choppy_penalty: int

    #: Gates and vocabularies.
    entry_modes: tuple[str, ...] = ALL_MODES
    default_entry_mode: str = RETRACEMENT
    directions: tuple[str, ...] = ("LONG", "SHORT")
    directional_biases: tuple[str, ...] = ("BULLISH", "BEARISH")
    continuation_entry_source: str = "continuation_micro_pullback"

    def __post_init__(self) -> None:
        object.__setattr__(self, "weights", _freeze_weights(self.weights))
        object.__setattr__(
            self, "thresholds", MappingProxyType(dict(self.thresholds)))
        object.__setattr__(self, "alignment", MappingProxyType({
            str(d): MappingProxyType(dict(v)) for d, v in self.alignment.items()}))

        for mode in self.entry_modes:
            if mode not in self.weights:
                raise ScoringConfigError(f"no weight table for entry mode {mode!r}")
            if mode not in self.thresholds:
                raise ScoringConfigError(f"no thresholds for entry mode {mode!r}")
            if not isinstance(self.thresholds[mode], ModeThresholds):
                raise ScoringConfigError(
                    f"thresholds[{mode!r}] must be a ModeThresholds, got "
                    f"{type(self.thresholds[mode]).__name__}")
        if self.default_entry_mode not in self.entry_modes:
            raise ScoringConfigError(
                f"default_entry_mode {self.default_entry_mode!r} is not one of "
                f"{self.entry_modes}")
        for direction in self.directions:
            if direction not in self.alignment:
                raise ScoringConfigError(f"no alignment entry for {direction!r}")
        for edge, weight in self.rr_bands:
            if isinstance(weight, bool) or not isinstance(weight, int):
                raise ScoringConfigError(
                    f"rr band weight for edge {edge} must be an int, got {weight!r}")

    # -- lookups the engine uses -------------------------------------------

    def weights_for(self, entry_mode: str) -> Mapping[str, int]:
        return self.weights.get(entry_mode, {})

    def rr_band_weight(self, rr: float) -> Optional[int]:
        """First matching band's weight, or None when no band matches.

        None means "append no note", which is V1's behaviour below 1.5 — distinct
        from a band worth 0, which appends the note and adds nothing.
        """
        for edge, weight in self.rr_bands:
            if rr >= edge:
                return weight
        return None

    def decide(self, entry_mode: str, score: int, displacement_present: bool) -> str:
        """The decision tier, in the engine's own branch order."""
        tiers = self.thresholds[entry_mode]
        if tiers.a_plus is not None and score >= tiers.a_plus:
            if not tiers.a_plus_requires_displacement or displacement_present:
                return "A+"
            return "VALID"
        if tiers.valid is not None and score >= tiers.valid:
            return "VALID"
        if tiers.watch is not None and score >= tiers.watch:
            return "WATCH"
        return "SKIP"

    # -- overrides ----------------------------------------------------------

    def known_notes(self) -> frozenset[str]:
        return frozenset(n for table in self.weights.values() for n in table)

    def with_overrides(
        self,
        *,
        weight_overrides: Optional[Mapping[str, Mapping[str, int]]] = None,
        thresholds: Optional[Mapping[str, Mapping[str, Any]]] = None,
        drop_components: Optional[Iterable[str]] = None,
    ) -> "ScoringConfig":
        """A new config with the given changes merged onto this one.

        Merged, never substituted: a variant states its *change*, so a component
        it does not mention keeps its baseline weight rather than silently
        becoming zero.

        `weight_overrides` is keyed by scope — `"ALL"` or an entry mode. `"ALL"`
        is applied first, so a mode-specific entry wins over it.

        Every referenced note must already exist in the table it is applied to. A
        typo must fail loudly; the alternative is a variant that silently scores
        nothing and looks like a null result.

        `drop_components` removes notes from the vocabulary entirely (no score,
        no note) — the deletion that `weight_overrides: 0` deliberately is not.
        """
        tables = {mode: dict(table) for mode, table in self.weights.items()}

        for scope, table in (weight_overrides or {}).items():
            if scope == "ALL":
                targets = [m for m in tables]
            elif scope in tables:
                targets = [scope]
            else:
                raise ScoringConfigError(
                    f"weight_overrides scope {scope!r} is not 'ALL' nor one of "
                    f"{tuple(tables)}")
            for note, weight in table.items():
                applied = [m for m in targets if note in tables[m]]
                if not applied:
                    raise ScoringConfigError(
                        f"weight_overrides[{scope!r}] names unknown component "
                        f"{note!r}; known components for that scope are "
                        f"{sorted(self.known_notes())}")
                for mode in applied:
                    tables[mode][note] = weight

        for note in drop_components or ():
            present = [m for m in tables if note in tables[m]]
            if not present:
                raise ScoringConfigError(
                    f"drop_components names unknown component {note!r}")
            for mode in present:
                del tables[mode][note]

        tiers = dict(self.thresholds)
        for mode, spec in (thresholds or {}).items():
            if mode not in tiers:
                raise ScoringConfigError(
                    f"thresholds names unknown entry mode {mode!r}")
            unknown = set(spec) - {
                "a_plus", "valid", "watch", "a_plus_requires_displacement"}
            if unknown:
                raise ScoringConfigError(
                    f"thresholds[{mode!r}] has unknown keys {sorted(unknown)}")
            current = tiers[mode]
            tiers[mode] = ModeThresholds(
                a_plus=spec.get("a_plus", current.a_plus),
                valid=spec.get("valid", current.valid),
                watch=spec.get("watch", current.watch),
                a_plus_requires_displacement=bool(spec.get(
                    "a_plus_requires_displacement",
                    current.a_plus_requires_displacement)),
            )

        return ScoringConfig(
            weights=tables,
            thresholds=tiers,
            alignment={d: dict(v) for d, v in self.alignment.items()},
            expansion_regimes=self.expansion_regimes,
            squeeze_regimes=self.squeeze_regimes,
            penalty_regimes=self.penalty_regimes,
            rr_floor=self.rr_floor,
            rr_bands=self.rr_bands,
            choppy_penalty=self.choppy_penalty,
            entry_modes=self.entry_modes,
            default_entry_mode=self.default_entry_mode,
            directions=self.directions,
            directional_biases=self.directional_biases,
            continuation_entry_source=self.continuation_entry_source,
        )

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON view, for recording which config produced a run."""
        return {
            "weights": {m: dict(t) for m, t in self.weights.items()},
            "thresholds": {m: t.to_dict() for m, t in self.thresholds.items()},
            "alignment": {d: dict(v) for d, v in self.alignment.items()},
            "expansion_regimes": list(self.expansion_regimes),
            "squeeze_regimes": list(self.squeeze_regimes),
            "penalty_regimes": list(self.penalty_regimes),
            "rr_floor": self.rr_floor,
            "rr_bands": [list(b) for b in self.rr_bands],
            "choppy_penalty": self.choppy_penalty,
            "entry_modes": list(self.entry_modes),
            "default_entry_mode": self.default_entry_mode,
            "directions": list(self.directions),
            "directional_biases": list(self.directional_biases),
            "continuation_entry_source": self.continuation_entry_source,
        }


# ==========================================================================
# The 2.2.0 defaults — read from v2/analysis/scoring.py:127-369 as it stood
# before this seam existed. Cross-checked against
# experiments/v2_core_rebuild/scoring_v2/baseline_scoring.json.
# ==========================================================================

_SHARED_WEIGHTS: dict[str, int] = {
    # HTF alignment (scoring.py:178-204 before the seam)
    "weekly_aligned": 2,
    "d1_aligned": 2,
    "h4_aligned": 1,
    "discount": 2,
    "premium": 2,
    # structure quality (253-262)
    "target_clear": 1,
    "session_active": 1,
    "htf_context_clean": 1,
    # regime engine (264-273)
    "expansion": 1,
    "squeeze": 1,
    "neutral_penalty": -2,
    # momentum quality (313-316)
    "momentum_weakening": -1,
}

DEFAULT_SCORING_CONFIG = ScoringConfig(
    weights={
        CONTINUATION: {
            **_SHARED_WEIGHTS,
            "weak_volume": -2,
            "sweep": 1,
            "fvg": 1,
            "displacement": 4,
            "fresh_ob": 1,
            "m5_continuation_confirm": 1,
        },
        RETRACEMENT: {
            **_SHARED_WEIGHTS,
            "sweep": 2,
            "weak_sweep": -1,
            "fvg": 1,
            "displacement": 1,
            "fresh_ob": 2,
            "no_ob": -1,
            "m5_choch": 2,
        },
        WATCH_ONLY: {
            **_SHARED_WEIGHTS,
            "sweep": 1,
            "fvg": 1,
            "displacement": 1,
            "fresh_ob": 1,
            "m5_watch_confirm": 1,
        },
    },
    thresholds={
        CONTINUATION: ModeThresholds(a_plus=14, valid=11, watch=8),
        RETRACEMENT: ModeThresholds(
            a_plus=15, valid=12, watch=9, a_plus_requires_displacement=True),
        WATCH_ONLY: ModeThresholds(watch=12),
    },
    alignment={
        "LONG": {"bias": "BULLISH", "location": "discount"},
        "SHORT": {"bias": "BEARISH", "location": "premium"},
    },
    expansion_regimes=("expansion_up", "expansion_down"),
    squeeze_regimes=("accumulation", "distribution"),
    penalty_regimes=("neutral", "compression", "unknown"),
    rr_floor=1.2,
    rr_bands=((3.0, 2), (2.0, 1), (1.5, 0)),
    choppy_penalty=2,
)


class ScoreAccumulator:
    """Score-and-note accumulator. The one mechanism the seam adds.

    `award` is the direct replacement for V1's `score += N; notes.append("x")`
    pair, with the weight read from the table instead of written inline. `add`
    exists for the RR bands, whose note text carries a value and whose weight
    therefore comes from the band rather than from the table.
    """

    __slots__ = ("_weights", "score", "notes")

    def __init__(self, weights: Mapping[str, int]) -> None:
        self._weights = weights
        self.score: int = 0
        self.notes: list[str] = []

    def award(self, note: str) -> None:
        weight = self._weights.get(note)
        if weight is None:
            return
        self.score += weight
        self.notes.append(note)

    def add(self, weight: int, note: str) -> None:
        self.score += weight
        self.notes.append(note)