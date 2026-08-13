"""Quantile-anchored decision thresholds, derived offline from TRAIN.

A variant that changes score *weights* invalidates the thresholds those weights
were calibrated against. Zeroing the four measured-uninformative components moves
186,670 of 373,723 scored rows across a tier boundary; correcting
`momentum_weakening` moves another 78,892. The integers 14/11/8 and 15/12/9 no
longer denote what they denoted, so a variant that keeps them is not testing its
hypothesis — it is testing its hypothesis plus an accidental change in
selectivity.

This module re-derives them. It reads a finished run's ledger and writes integers
into an experiment config. It never replays, never touches lifecycle or outcomes,
and never reads an R value.

## The rule

Per entry mode, over the TRAIN partition's scored rows:

1. Measure the baseline's three **cumulative** tier rates from the recorded
   pre-choppy score under 2.2.0's thresholds — `P(!= SKIP)`, `P(in {VALID, A+})`,
   `P(== A+)`.
2. Re-score the same rows under the variant's weights.
3. For each tier, pick the integer `t` minimising `|P(variant >= t) - rate|`.
   Ties resolve **upward**: the higher threshold admits fewer rows, so a tie
   breaks toward the baseline's selectivity rather than away from it.
4. Clamp `a_plus >= valid >= watch`, which the search can violate when a tier's
   rate is nearly unreachable.

The `A+` predicate is the mode's own. RETRACEMENT awards `A+` only with
displacement (a score above the threshold without it publishes VALID), so its
rate is measured over `score >= t AND displacement` — anchoring it on score alone
would target a rate the mode cannot produce.

## Why volume and not expectancy

Anchoring on admitted volume is a deliberate refusal to optimise. Two reasons:

- It makes every variant **volume-matched to the baseline by construction**, and
  Phase A established that the volume-matched comparison is the only one with
  teeth: with a -0.0087 R baseline and a slot cap binding 51% of cycles, any rule
  that simply trades less improves net R mechanically.
- It has no free parameter. Fitting six integers to maximise TRAIN expectancy
  across ten quarters is where this programme would overfit fastest.

The derivation is still selection on TRAIN and is declared as such — it is the
largest overfitting exposure in the scoring_v2 family, and it is covered by the
holdout rule.

## Determinism

Integer scores, integer thresholds, a full scan of the candidate range in
ascending order, and an explicit tie rule. No sampling, no floating-point
comparison of near-equal rates beyond `abs()`, no dict iteration order
dependence. Two runs over the same ledger produce the same integers, and
`generated_at` is the only field that moves.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from backtest.data.repository import canonical_json
from backtest.experiments.config import (
    CONFIG_FILENAME,
    ExperimentConfig,
    ExperimentConfigError,
    load_config,
)
from backtest.experiments.partition import (
    DEV_END,
    DEV_START,
    HOLDOUT_START,
    TRAIN_END,
    TRAIN_START,
    role_of,
)
from backtest.experiments.rescore import decompose
from v2.analysis.scoring_config import (
    DEFAULT_SCORING_CONFIG,
    ModeThresholds,
    ScoringConfig,
)

__all__ = [
    "TIERS",
    "AnchorReport",
    "ModeAnchor",
    "Scored",
    "apply_to_config",
    "collect",
    "derive",
    "pick_threshold",
    "variant_weights",
    "derive_thresholds",
    "write_report",
]

#: Cumulative tiers, coarsest first. `watch` is "admitted at all".
TIERS = ("watch", "valid", "a_plus")

_MODES = ("CONTINUATION", "RETRACEMENT", "WATCH_ONLY")


@dataclass(frozen=True)
class Scored:
    """One TRAIN row, scored both ways. No R, no outcome, deliberately."""

    mode: str
    baseline_score: int
    variant_score: int
    displacement: bool


def variant_weights(
    weight_overrides: Optional[Mapping[str, Mapping[str, int]]],
    *,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> ScoringConfig:
    """`base` with the variant's weight overrides applied and nothing else.

    Thresholds are deliberately not applied: they are the output of this module,
    so feeding a variant's declared thresholds back in would make the derivation
    depend on the value it is deriving.
    """
    if not weight_overrides:
        return base
    return base.with_overrides(weight_overrides=weight_overrides)


def _rescore(decomposition, variant: ScoringConfig, base: ScoringConfig) -> int:
    """The variant's raw score for a decomposed row.

    Only notes that actually fired can move: a weight change is worth nothing on
    a component that was absent. `contributions` already holds the baseline weight
    of each note that fired, so the delta is a per-note difference and never a
    re-run of the engine.
    """
    mode = decomposition.mode
    new_table = variant.weights_for(mode)
    old_table = base.weights_for(mode)
    score = decomposition.raw_score
    for note in decomposition.contributions:
        if note == "rr_band":
            # The rr weight lives in the band, not the table; no variant in this
            # family touches it, and silently mis-mapping it would be worse than
            # refusing to.
            continue
        new = new_table.get(note)
        old = old_table.get(note)
        if new is None or old is None or new == old:
            continue
        score += new - old
    return score


def collect(
    run_dir: Path | str,
    variant: ScoringConfig,
    *,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
    role: str = "TRAIN",
    ledger_filename: str = "signal_ledger.jsonl",
) -> tuple[list[Scored], dict[str, int], int]:
    """Stream the ledger and score every in-partition row both ways.

    Returns the rows, a role census (so a caller can see what was excluded), and
    the count of rows whose note weights did not sum to their recorded score.
    A non-zero unverified count invalidates the derivation rather than qualifying
    it — the caller raises on it.
    """
    path = Path(run_dir) / ledger_filename
    if not path.exists():
        raise FileNotFoundError(f"no {ledger_filename} in {run_dir}")

    rows: list[Scored] = []
    roles: dict[str, int] = {}
    unverified = 0

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            decomposition = decompose(row)

            if not decomposition.notes and decomposition.recorded_score == 0:
                continue                      # never reached the score engine
            if not decomposition.verified:
                unverified += 1
                continue
            if decomposition.early_exit:
                continue                      # decision fixed by its reason
            if decomposition.mode not in _MODES:
                continue

            # The ledger records the last closed H1; the scan boundary is one
            # candle later (verified against cycle_index: T_0 + i*3600).
            boundary = int(row.get("h1_last_ts") or 0) + 3600
            row_role = role_of(boundary)
            roles[row_role] = roles.get(row_role, 0) + 1
            if row_role != role:
                continue

            rows.append(Scored(
                mode=decomposition.mode,
                baseline_score=decomposition.raw_score,
                variant_score=_rescore(decomposition, variant, base),
                displacement=bool(row.get("displacement")),
            ))

    return rows, roles, unverified


def _rate(scores: Sequence[int], threshold: int,
          gate: Optional[Sequence[bool]] = None) -> float:
    if not scores:
        return 0.0
    if gate is None:
        hits = sum(1 for s in scores if s >= threshold)
    else:
        hits = sum(1 for s, g in zip(scores, gate) if s >= threshold and g)
    return hits / len(scores)


def pick_threshold(
    scores: Sequence[int],
    target: float,
    *,
    gate: Optional[Sequence[bool]] = None,
) -> Optional[int]:
    """The integer threshold whose admitted rate is closest to `target`.

    Ties resolve upward — the higher threshold admits fewer rows, so a tie breaks
    toward the baseline's selectivity. The scan is ascending and the comparison is
    `<=`, so a later (higher) candidate always displaces an equal earlier one;
    that is the whole tie rule, with no special case to get wrong.
    """
    if not scores:
        return None
    lo, hi = min(scores), max(scores)
    best: Optional[int] = None
    best_error: Optional[float] = None
    for candidate in range(lo, hi + 2):        # +2: hi+1 admits nothing
        error = abs(_rate(scores, candidate, gate) - target)
        if best_error is None or error <= best_error:
            best, best_error = candidate, error
    return best


@dataclass(frozen=True)
class ModeAnchor:
    """One entry mode's derived thresholds, with the evidence for them."""

    mode: str
    n: int
    baseline_thresholds: ModeThresholds
    derived_thresholds: ModeThresholds
    baseline_rates: Mapping[str, float]
    variant_rates: Mapping[str, float]
    baseline_tiers: Mapping[str, int]
    variant_tiers: Mapping[str, int]
    unchanged: bool
    #: tier -> {threshold: admitted rate} for the chosen integer and its two
    #: neighbours. This is what makes a large residual reviewable instead of
    #: mysterious: an integer score is lumpy, and when one score value holds a
    #: large share of the mass, no integer threshold lands near the target. The
    #: bracket shows the alternatives that were available and rejected.
    brackets: Mapping[str, Mapping[str, float]] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "n": self.n,
            "baseline_thresholds": self.baseline_thresholds.to_dict(),
            "derived_thresholds": self.derived_thresholds.to_dict(),
            "cumulative_rates": {
                tier: {
                    "baseline": round(self.baseline_rates[tier], 6),
                    "variant": round(self.variant_rates[tier], 6),
                    "delta": round(
                        self.variant_rates[tier] - self.baseline_rates[tier], 6),
                }
                for tier in TIERS
            },
            "bracket": {
                tier: {k: round(v, 6) for k, v in sorted(
                    (self.brackets or {}).get(tier, {}).items(),
                    key=lambda kv: int(kv[0]))}
                for tier in TIERS
            },
            "tier_counts": {
                "baseline": dict(sorted(self.baseline_tiers.items())),
                "variant": dict(sorted(self.variant_tiers.items())),
            },
            "thresholds_unchanged": self.unchanged,
        }


def _tier_counts(
    rows: Sequence[Scored], thresholds: ModeThresholds, *, variant: bool
) -> dict[str, int]:
    counts = {"SKIP": 0, "WATCH": 0, "VALID": 0, "A+": 0}
    for row in rows:
        score = row.variant_score if variant else row.baseline_score
        counts[_decide(thresholds, score, row.displacement)] += 1
    return counts


def _decide(thresholds: ModeThresholds, score: int, displacement: bool) -> str:
    """`ScoringConfig.decide` for one mode's thresholds, branch order preserved."""
    if thresholds.a_plus is not None and score >= thresholds.a_plus:
        if not thresholds.a_plus_requires_displacement or displacement:
            return "A+"
        return "VALID"
    if thresholds.valid is not None and score >= thresholds.valid:
        return "VALID"
    if thresholds.watch is not None and score >= thresholds.watch:
        return "WATCH"
    return "SKIP"


def _cumulative(counts: Mapping[str, int], n: int) -> dict[str, float]:
    if n == 0:
        return {tier: 0.0 for tier in TIERS}
    return {
        "watch": (counts["WATCH"] + counts["VALID"] + counts["A+"]) / n,
        "valid": (counts["VALID"] + counts["A+"]) / n,
        "a_plus": counts["A+"] / n,
    }


def _anchor_mode(mode: str, rows: Sequence[Scored]) -> ModeAnchor:
    baseline = DEFAULT_SCORING_CONFIG.thresholds[mode]
    n = len(rows)

    baseline_counts = _tier_counts(rows, baseline, variant=False)
    baseline_rates = _cumulative(baseline_counts, n)

    if n == 0:
        # No evidence is not a licence to invent an integer.
        return ModeAnchor(
            mode=mode, n=0, baseline_thresholds=baseline,
            derived_thresholds=baseline,
            baseline_rates=baseline_rates,
            variant_rates=baseline_rates,
            baseline_tiers=baseline_counts,
            variant_tiers=baseline_counts,
            unchanged=True,
            brackets={tier: {} for tier in TIERS})

    scores = [r.variant_score for r in rows]
    displacement = [r.displacement for r in rows]

    watch = (pick_threshold(scores, baseline_rates["watch"])
             if baseline.watch is not None else None)
    valid = (pick_threshold(scores, baseline_rates["valid"])
             if baseline.valid is not None else None)
    a_plus = None
    if baseline.a_plus is not None:
        gate = displacement if baseline.a_plus_requires_displacement else None
        a_plus = pick_threshold(scores, baseline_rates["a_plus"], gate=gate)

    # The search treats each tier independently, so a mode whose A+ rate is
    # nearly unreachable can land below its VALID threshold. Order is a property
    # of the tier ladder, not of the data.
    if valid is not None and watch is not None:
        valid = max(valid, watch)
    if a_plus is not None and valid is not None:
        a_plus = max(a_plus, valid)

    derived = ModeThresholds(
        a_plus=a_plus, valid=valid, watch=watch,
        a_plus_requires_displacement=baseline.a_plus_requires_displacement)
    variant_counts = _tier_counts(rows, derived, variant=True)

    chosen = {"watch": watch, "valid": valid, "a_plus": a_plus}
    gates = {"watch": None, "valid": None,
             "a_plus": displacement if baseline.a_plus_requires_displacement
             else None}
    brackets = {
        tier: ({} if chosen[tier] is None else {
            str(t): _rate(scores, t, gates[tier])
            for t in (chosen[tier] - 1, chosen[tier], chosen[tier] + 1)
        })
        for tier in TIERS
    }

    return ModeAnchor(
        mode=mode, n=n,
        baseline_thresholds=baseline,
        derived_thresholds=derived,
        baseline_rates=baseline_rates,
        variant_rates=_cumulative(variant_counts, n),
        baseline_tiers=baseline_counts,
        variant_tiers=variant_counts,
        unchanged=derived == baseline,
        brackets=brackets)


@dataclass(frozen=True)
class AnchorReport:
    """The derivation, its inputs, and what it changed."""

    slug: str
    variant_id: str
    modes: tuple[ModeAnchor, ...]
    rows: int
    role_counts: Mapping[str, int]
    unverified: int
    identity: Mapping[str, Any]

    @property
    def thresholds(self) -> dict[str, dict[str, Any]]:
        """The config block this report authorises."""
        return {
            anchor.mode: {
                key: value
                for key, value in (
                    ("a_plus", anchor.derived_thresholds.a_plus),
                    ("valid", anchor.derived_thresholds.valid),
                    ("watch", anchor.derived_thresholds.watch),
                )
                if value is not None
            }
            for anchor in self.modes
        }

    @property
    def max_rate_delta(self) -> float:
        """The worst cumulative-rate deviation from baseline, over every mode."""
        deltas = [
            abs(anchor.variant_rates[tier] - anchor.baseline_rates[tier])
            for anchor in self.modes for tier in TIERS
        ]
        return max(deltas) if deltas else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "derived",
            "analysis": "quantile_anchored_thresholds",
            "is_replay": False,
            "slug": self.slug,
            "variant_id": self.variant_id,
            "method": {
                "anchor": "per-mode cumulative tier rates of the 2.2.0 baseline",
                "objective": "match admitted fraction; expectancy and R are not "
                             "read at any point",
                "tie_rule": "upward (the higher threshold admits fewer rows)",
                "a_plus_predicate": "the mode's own; RETRACEMENT requires "
                                    "displacement",
                "ordering": "clamped to a_plus >= valid >= watch",
                "partition": "TRAIN only",
            },
            "rows_train": self.rows,
            "role_counts": dict(sorted(self.role_counts.items())),
            "unverified_rows": self.unverified,
            "max_cumulative_rate_delta": round(self.max_rate_delta, 6),
            "modes": [anchor.to_dict() for anchor in self.modes],
            "thresholds": self.thresholds,
            "identity": dict(self.identity),
        }


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _identity(run_dir: Path, role_counts: Mapping[str, int]) -> dict[str, Any]:
    manifest = _read_json(run_dir / "manifest.json") or {}
    metrics = _read_json(run_dir / "metrics.json") or {}
    baseline = _read_json(
        Path("data/baselines/v2.2.0-official/baseline.json")) or {}
    core = manifest.get("core") or {}
    return {
        "run_id": manifest.get("run_id"),
        "baseline_id": baseline.get("baseline_id"),
        "metrics_hash": (metrics.get("metrics_hash")
                         or baseline.get("metrics_hash")),
        "comparison_key_digest": baseline.get("comparison_key_digest"),
        "strategy_version": core.get("strategy_version"),
        "data_snapshot_id": core.get("data_snapshot_id"),
        "source_scope": {
            "tier": 0,
            "population": "scored ledger rows that reached the tier logic "
                          "(verified, non-early-exit)",
            "partition": {
                "TRAIN": [TRAIN_START, TRAIN_END],
                "DEV": [DEV_START, DEV_END],
                "HOLDOUT": [HOLDOUT_START, None],
            },
            "role_counts": dict(sorted(role_counts.items())),
            "dev_has_promotion_authority": False,
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def derive(
    run_dir: Path | str,
    config: ExperimentConfig,
    *,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
    role: str = "TRAIN",
) -> AnchorReport:
    """Derive `config`'s thresholds from `run_dir`'s ledger over `role`."""
    scoring = (config.strategy_inputs or {}).get("scoring") or {}
    variant = variant_weights(scoring.get("weight_overrides"), base=base)

    rows, role_counts, unverified = collect(
        run_dir, variant, base=base, role=role)
    if unverified:
        raise ExperimentConfigError(
            f"{unverified} ledger row(s) failed the decomposition identity "
            f"(note weights must sum to the recorded score). The weight table "
            f"disagrees with the run, so any threshold derived from it would be "
            f"wrong in a way nothing downstream could detect.")
    if not rows:
        raise ExperimentConfigError(
            f"no {role} rows in {run_dir}; nothing to anchor to")

    by_mode: dict[str, list[Scored]] = {mode: [] for mode in _MODES}
    for row in rows:
        by_mode[row.mode].append(row)

    return AnchorReport(
        slug=config.slug,
        variant_id=config.variant_id,
        modes=tuple(_anchor_mode(mode, by_mode[mode]) for mode in _MODES),
        rows=len(rows),
        role_counts=role_counts,
        unverified=unverified,
        identity=_identity(Path(run_dir), role_counts),
    )


def apply_to_config(
    root: Path | str, slug: str, report: AnchorReport
) -> tuple[Path, str, str]:
    """Write the derived integers into `<root>/<slug>/config.json`.

    Returns the path and the variant id before and after. The id **must** change:
    thresholds are strategy inputs, so replacing a declared anchor with the
    integers it resolved to is a different variant, and a stable id would mean
    the derivation had not been recorded in the thing that identifies behaviour.
    """
    path = Path(root) / slug / CONFIG_FILENAME
    config = load_config(path)
    if config.variant_id != report.variant_id:
        raise ExperimentConfigError(
            f"{slug}: report was derived for variant {report.variant_id} but the "
            f"config on disk hashes to {config.variant_id}; the config changed "
            f"after the derivation ran")

    inputs = json.loads(canonical_json(dict(config.strategy_inputs)))
    scoring = inputs.setdefault("scoring", {})
    previous = scoring.get("thresholds")
    scoring["thresholds"] = report.thresholds

    metadata = json.loads(canonical_json(dict(config.metadata)))
    metadata["threshold_derivation"] = {
        "method": "quantile_anchored (backtest.experiments.anchor)",
        "replaced": previous,
        "anchor_run_id": report.identity.get("run_id"),
        "baseline_id": report.identity.get("baseline_id"),
        "partition": "TRAIN",
        "rows": report.rows,
        "max_cumulative_rate_delta": round(report.max_rate_delta, 6),
        "predecessor_variant_id": config.variant_id,
    }

    updated = ExperimentConfig(
        slug=config.slug,
        base_strategy_version=config.base_strategy_version,
        strategy_inputs=inputs,
        metadata=metadata,
        schema_version=config.schema_version,
    )
    updated.write(path, overwrite=True)
    return path, config.variant_id, updated.variant_id


def write_report(report: AnchorReport, out_dir: Path | str) -> Path:
    """Write the derivation evidence. Deterministic apart from `generated_at`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"anchor_{report.slug}.json"
    path.write_text(canonical_json(report.to_dict()) + "\n", encoding="utf-8")
    return path


#: Package-level alias. `derive` reads well beside its module (`anchor.derive`)
#: and badly on its own, and the package namespace is where it would appear alone.
derive_thresholds = derive
