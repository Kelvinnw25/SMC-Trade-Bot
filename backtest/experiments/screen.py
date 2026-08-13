"""Tier-0 screen of a scoring variant: does its book differ, and why?

Phase B0. **This is not a replay and cannot promote anything.** It re-decides
admission over trades the Outcome Engine already resolved, under a variant's
score weights and its anchored thresholds, and reports what changed. Read
`tier0.LIMITATIONS` before quoting any number.

## What it actually does

1. Join outcomes to their ledger rows (`dataset.LabeledDataset`, C-9.4 keys).
2. Decompose each row's recorded score into its note contributions
   (`rescore.decompose`) and re-score it under the variant's weight table.
3. Apply the variant's **anchored thresholds** to get a variant decision tier.
4. Every row whose variant decision is `SKIP` becomes a rejected canonical key.
5. Hand that key set to `tier0.simulate`, which frees the slots those rejections
   vacate and lets suppressed `CAPPED` candidates compete for them.

Step 5 is the whole reason this exists rather than a `groupby`. The slot cap binds
51% of cycles, so a filter does not merely delete trades — it changes which
suppressed candidate got in, and a naive subset overstated one screen by ~3x.

## The control, and why a variant is never scored alone

Each variant is run beside a **volume-matched control**: reject the same number of
trades, chosen by lowest baseline score. That is the cheapest rule with no
hypothesised skill, and it is the comparison Phase A found to be the only one with
teeth — with a -0.0087 R baseline, any rule that simply trades less improves net R
mechanically. A variant that does not beat its control has demonstrated nothing
except that it trades less.

## Separating quality from exposure

A variant changes two things at once and they must not be reported as one number.
Given baseline `(n_b, E_b)` and variant `(n_v, E_v)`:

    delta_total_r = n_v*E_v - n_b*E_b
                  = (n_v - n_b)*E_b        <- exposure: same quality, more/less of it
                  + n_v*(E_v - E_b)        <- quality: same book size, better trades

The split is exact, not an approximation. `refilled` R is reported separately on
top, because that term exists only because of the slot cap and would not appear in
any analysis that ignored capacity.

The anchoring is imperfect by up to 9pp of admitted volume (see
`scoring_v2/derivation/`), so the exposure term is expected to be non-zero. That
is precisely why it is separated rather than absorbed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from backtest.data.repository import canonical_json
from backtest.dataset import CanonicalKey, LabeledDataset
from backtest.experiments.config import ExperimentConfig, load_config
from backtest.experiments.rescore import decompose
from backtest.experiments.scoring_variant import scoring_config_from_inputs
from backtest.experiments.tier0 import (
    AdmissionRule,
    LIMITATIONS,
    Occupancy,
    Tier0Result,
    key_of,
    load_occupancy,
    simulate,
)
from backtest.outcome.record import OutcomeRecord
from v2.analysis.scoring_config import DEFAULT_SCORING_CONFIG, ScoringConfig

__all__ = [
    "Coverage",
    "Screen",
    "VariantBook",
    "coverage_for",
    "decisions_for",
    "drawdown",
    "screen_variant",
    "write_screen",
]


# ---------------------------------------------------------------------------
# re-scoring a resolved outcome
# ---------------------------------------------------------------------------

def _ledger_view(record: OutcomeRecord, labels: Mapping[str, Any]) -> dict:
    """The fields `rescore.decompose` needs, rebuilt from an outcome + its labels.

    The levels come from the **outcome record**, not the labels: `_exact_rr` needs
    unrounded `entry_price`/`sl`/`tp` to place a row in the right rr band, and
    `LABEL_FIELDS` carries only the rounded `rr`. Getting that wrong mis-weighted
    378 of 16,297 rows the last time it was done from the note text.
    """
    return {
        "entry_mode": record.entry_mode,
        "score": labels.get("score"),
        "notes": labels.get("notes") or (),
        "reason": labels.get("reason"),
        "rr": labels.get("rr"),
        "entry_price": record.entry_price,
        "sl": record.sl,
        "tp": record.tp,
    }


def _variant_score(decomposition, variant: ScoringConfig,
                   base: ScoringConfig) -> int:
    """The variant's raw score. Only notes that fired can move."""
    new_table = variant.weights_for(decomposition.mode)
    old_table = base.weights_for(decomposition.mode)
    score = decomposition.raw_score
    for note in decomposition.contributions:
        if note == "rr_band":
            continue                       # weight lives in the band, not a table
        new, old = new_table.get(note), old_table.get(note)
        if new is None or old is None or new == old:
            continue
        score += new - old
    return score


@dataclass
class Decisions:
    """Per-record variant decisions, and the census that qualifies them."""

    rejected_keys: set[CanonicalKey] = field(default_factory=set)
    #: key -> (baseline_score, variant_score) for the rows we could evaluate.
    scores: dict[CanonicalKey, tuple[int, int]] = field(default_factory=dict)
    evaluated: int = 0
    unjoined: int = 0
    unverified: int = 0
    early_exit: int = 0
    baseline_tiers: dict[str, int] = field(default_factory=dict)
    variant_tiers: dict[str, int] = field(default_factory=dict)


def decisions_for(
    dataset: LabeledDataset,
    variant: ScoringConfig,
    *,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> Decisions:
    """Which resolved trades the variant would not have admitted.

    A row this cannot evaluate — no joined ledger label, a failed decomposition
    identity, or an early exit whose decision is fixed by its `reason` — is
    **not** rejected. Rejecting the unevaluable would let a join failure look like
    a filter, and the counts are reported so the reader can see how many there
    were.
    """
    out = Decisions()
    for record in dataset.records:
        key = key_of(record)
        labels = dataset.labels.get(key)
        if not labels:
            out.unjoined += 1
            continue

        decomposition = decompose(_ledger_view(record, labels))
        if not decomposition.verified:
            out.unverified += 1
            continue
        if decomposition.early_exit:
            out.early_exit += 1
            continue
        if decomposition.mode not in base.weights:
            out.unjoined += 1
            continue

        variant_score = _variant_score(decomposition, variant, base)
        displacement = bool(labels.get("displacement"))
        before = base.decide(decomposition.mode, decomposition.raw_score,
                             displacement)
        after = variant.decide(decomposition.mode, variant_score, displacement)

        out.evaluated += 1
        out.scores[key] = (decomposition.raw_score, variant_score)
        out.baseline_tiers[before] = out.baseline_tiers.get(before, 0) + 1
        out.variant_tiers[after] = out.variant_tiers.get(after, 0) + 1
        if after == "SKIP":
            out.rejected_keys.add(key)
    return out


# ---------------------------------------------------------------------------
# what the screen cannot see
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Coverage:
    """How much of a variant's change tier 0 can observe at all.

    A tier-0 screen can only ever **remove** trades. A row the baseline scored
    below its `watch` threshold was never inserted, so the Outcome Engine never
    resolved it, so no realized R exists for it — and a variant that admits that
    row cannot be credited or debited for it here at any price.

    That makes the ratio below the single most important qualifier on a screen
    result. When `skip_to_admit` exceeds `admit_to_skip`, the screen is measuring
    the minority of the change and its sign carries no information about the whole.
    """

    scored_rows: int
    skip_to_admit: int        # invisible: never inserted, never resolved
    admit_to_skip: int        # visible: this is what the screen simulates
    unchanged: int
    unverified: int

    @property
    def net_admission_change(self) -> int:
        return self.skip_to_admit - self.admit_to_skip

    @property
    def observable_fraction(self) -> Optional[float]:
        moved = self.skip_to_admit + self.admit_to_skip
        return (self.admit_to_skip / moved) if moved else None

    def to_dict(self) -> dict[str, Any]:
        fraction = self.observable_fraction
        return {
            "scored_ledger_rows": self.scored_rows,
            "admit_to_skip_observable": self.admit_to_skip,
            "skip_to_admit_invisible": self.skip_to_admit,
            "unchanged": self.unchanged,
            "unverified": self.unverified,
            "net_admission_change": self.net_admission_change,
            "observable_fraction_of_moved_rows": None if fraction is None
            else round(fraction, 6),
            "meaning": "a row the baseline SKIPped was never inserted and so was "
                       "never resolved; no realized R exists for it. Tier 0 can "
                       "therefore only simulate removals. When "
                       "skip_to_admit_invisible is the larger number, this screen "
                       "measures the minority of the variant's change and its "
                       "sign does not generalise to the whole.",
        }


def coverage_for(
    ledger_path: Path | str,
    variant: ScoringConfig,
    *,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> Coverage:
    """Count admission changes in both directions over the whole scored ledger.

    Streams the ledger rather than reusing `dataset.labels` because the exact rr
    band needs the unrounded `entry_price`/`sl`/`tp`, and `LABEL_FIELDS` carries
    only the rounded `rr`.
    """
    scored = skip_to_admit = admit_to_skip = unchanged = unverified = 0
    with Path(ledger_path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            decomposition = decompose(row)
            if not decomposition.notes and decomposition.recorded_score == 0:
                continue
            if not decomposition.verified:
                unverified += 1
                continue
            if decomposition.early_exit or decomposition.mode not in base.weights:
                continue

            scored += 1
            displacement = bool(row.get("displacement"))
            before = base.decide(decomposition.mode, decomposition.raw_score,
                                 displacement)
            after = variant.decide(
                decomposition.mode,
                _variant_score(decomposition, variant, base), displacement)
            if before == "SKIP" and after != "SKIP":
                skip_to_admit += 1
            elif before != "SKIP" and after == "SKIP":
                admit_to_skip += 1
            else:
                unchanged += 1

    return Coverage(scored_rows=scored, skip_to_admit=skip_to_admit,
                    admit_to_skip=admit_to_skip, unchanged=unchanged,
                    unverified=unverified)


# ---------------------------------------------------------------------------
# book metrics
# ---------------------------------------------------------------------------

def drawdown(series: Sequence[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative R curve, as a positive R.

    `series` must already be in exit order — `tier0.simulate` accounts in
    `c98_order_key` order (C-9.8), which is what makes this meaningful rather
    than an artifact of list construction.
    """
    peak = 0.0
    running = 0.0
    worst = 0.0
    for value in series:
        running += value
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


@dataclass(frozen=True)
class VariantBook:
    """One admitted book, summarised. Every field is R-denominated or a count."""

    label: str
    n: int
    total_r: float
    expectancy: Optional[float]
    win_rate: Optional[float]
    max_drawdown_r: float
    recovery_factor: Optional[float]

    @classmethod
    def of(cls, label: str, series: Sequence[float]) -> "VariantBook":
        n = len(series)
        total = sum(series)
        dd = drawdown(series)
        return cls(
            label=label, n=n, total_r=total,
            expectancy=(total / n) if n else None,
            win_rate=(sum(1 for v in series if v > 0) / n) if n else None,
            max_drawdown_r=dd,
            recovery_factor=(total / dd) if dd > 0 else None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "n": self.n,
            "total_r": round(self.total_r, 4),
            "expectancy": None if self.expectancy is None
            else round(self.expectancy, 6),
            "win_rate": None if self.win_rate is None
            else round(self.win_rate, 6),
            "max_drawdown_r": round(self.max_drawdown_r, 4),
            "recovery_factor": None if self.recovery_factor is None
            else round(self.recovery_factor, 4),
        }


def _attribution(baseline: VariantBook, variant: VariantBook) -> dict[str, Any]:
    """The exact split of delta total R into exposure and quality."""
    if baseline.expectancy is None or variant.expectancy is None:
        return {"available": False}
    exposure = (variant.n - baseline.n) * baseline.expectancy
    quality = variant.n * (variant.expectancy - baseline.expectancy)
    total = variant.total_r - baseline.total_r
    return {
        "available": True,
        "delta_total_r": round(total, 4),
        "exposure_term_r": round(exposure, 4),
        "quality_term_r": round(quality, 4),
        "residual_r": round(total - exposure - quality, 6),
        "delta_trades": variant.n - baseline.n,
        "delta_expectancy": round(variant.expectancy - baseline.expectancy, 6),
        "reading": "exposure = (n_v - n_b) * E_b, i.e. what the change in book "
                   "size alone is worth at baseline quality. quality = n_v * "
                   "(E_v - E_b), i.e. what the change in per-trade edge is worth "
                   "at the variant's book size. The two sum to delta_total_r "
                   "exactly; residual_r is float noise.",
    }


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Screen:
    """A variant's tier-0 screen, its control, and the attribution."""

    slug: str
    variant_id: str
    decisions: Decisions
    variant_result: Tier0Result
    control_result: Tier0Result
    identity_result: Tier0Result
    baseline: VariantBook
    variant: VariantBook
    control: VariantBook
    coverage: Coverage
    identity: Mapping[str, Any]

    @property
    def beats_control(self) -> Optional[bool]:
        if self.variant.expectancy is None or self.control.expectancy is None:
            return None
        return self.variant.expectancy > self.control.expectancy

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "derived",
            "analysis": "tier0_scoring_screen",
            "is_replay": False,
            "cannot_promote": "Tier 0 re-decides admission over already-resolved "
                              "outcomes. It cannot observe an exit that a changed "
                              "decision would have produced, and the residency of "
                              "a promoted candidate is a declared constant. No "
                              "variant may be shipped on this evidence.",
            "slug": self.slug,
            "variant_id": self.variant_id,
            "decisions": {
                "evaluated": self.decisions.evaluated,
                "rejected": len(self.decisions.rejected_keys),
                "unjoined": self.decisions.unjoined,
                "unverified": self.decisions.unverified,
                "early_exit": self.decisions.early_exit,
                "baseline_tiers": dict(sorted(self.decisions.baseline_tiers.items())),
                "variant_tiers": dict(sorted(self.decisions.variant_tiers.items())),
            },
            "books": {
                "baseline": self.baseline.to_dict(),
                "variant": self.variant.to_dict(),
                "volume_matched_control": self.control.to_dict(),
            },
            "attribution": {
                "variant_vs_baseline": _attribution(self.baseline, self.variant),
                "control_vs_baseline": _attribution(self.baseline, self.control),
                "variant_vs_control": _attribution(self.control, self.variant),
            },
            "slot_pressure": {
                "variant": {
                    "promotions": self.variant_result.promotions,
                    "still_capped": self.variant_result.still_capped,
                    "symbol_busy": self.variant_result.symbol_busy,
                    "refilled_r": round(sum(self.variant_result.refilled), 4),
                    "refilled_n": len(self.variant_result.refilled),
                },
                "control": {
                    "promotions": self.control_result.promotions,
                    "still_capped": self.control_result.still_capped,
                    "refilled_r": round(sum(self.control_result.refilled), 4),
                    "refilled_n": len(self.control_result.refilled),
                },
                "reading": "refilled R is earned by suppressed CAPPED candidates "
                           "that the rejections let in. It exists only because "
                           "the cap binds 51% of cycles and is invisible to any "
                           "analysis that subsets realized trades.",
            },
            "substitution": {
                "delta_net_r": round(self.variant_result.delta_net_r, 4),
                "delta_if_substitution_ignored":
                    round(self.variant_result.naive_delta_net_r, 4),
                "substitution_cost": round(
                    self.variant_result.delta_net_r
                    - self.variant_result.naive_delta_net_r, 4),
            },
            "validation": {
                "identity_rule_slack": self.identity_result.slack,
                "identity_rule_promotions": self.identity_result.promotions,
                "identity_net_r_matches_baseline": abs(
                    self.identity_result.counterfactual_net_r
                    - self.identity_result.baseline_net_r) < 1e-9,
                "meaning": "the identity rule rejects nothing, so it must promote "
                           "nothing and reproduce the baseline book exactly. A "
                           "failure here invalidates every other number.",
            },
            "coverage": self.coverage.to_dict(),
            "verdict": {
                "beats_volume_matched_control_on_expectancy": self.beats_control,
                "screen_observes_majority_of_change": (
                    self.coverage.admit_to_skip >= self.coverage.skip_to_admit),
                "promotion_authority": "none (tier 0)",
            },
            "identity": dict(self.identity),
            "limitations": LIMITATIONS,
        }


def _control_rule(
    dataset: LabeledDataset, decisions: Decisions, count: int
) -> AdmissionRule:
    """Reject `count` trades by lowest baseline score — the no-skill benchmark.

    Ties are broken by the canonical key so the control is deterministic; without
    that, two runs could reject different trades at the same score and the
    benchmark would move under the variant it is judging.
    """
    ranked = sorted(
        decisions.scores.items(),
        key=lambda item: (item[1][0], tuple(str(part) for part in item[0])))
    return AdmissionRule(
        name=f"volume_matched_control_{count}",
        reject_keys=frozenset(key for key, _ in ranked[:count]))


def screen_variant(
    run_dir: Path | str,
    config: ExperimentConfig,
    *,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
    occupancy: Optional[Occupancy] = None,
    dataset: Optional[LabeledDataset] = None,
) -> Screen:
    """Screen one variant at tier 0, beside its volume-matched control."""
    run = Path(run_dir)
    variant_config = scoring_config_from_inputs(config.strategy_inputs, base=base)

    if dataset is None:
        dataset = LabeledDataset.from_run(run)
    if occupancy is None:
        occupancy = load_occupancy(run)

    decisions = decisions_for(dataset, variant_config, base=base)

    identity_result = simulate(
        dataset.records, occupancy, AdmissionRule(name="identity"),
        labels_for=dataset.labels_for)
    variant_result = simulate(
        dataset.records, occupancy,
        AdmissionRule(name=f"scoring_variant_{config.slug}",
                      reject_keys=frozenset(decisions.rejected_keys)),
        labels_for=dataset.labels_for)
    control_result = simulate(
        dataset.records, occupancy,
        _control_rule(dataset, decisions, len(decisions.rejected_keys)),
        labels_for=dataset.labels_for)

    return Screen(
        slug=config.slug,
        variant_id=config.variant_id,
        decisions=decisions,
        variant_result=variant_result,
        control_result=control_result,
        identity_result=identity_result,
        coverage=coverage_for(run / "signal_ledger.jsonl", variant_config,
                              base=base),
        baseline=VariantBook.of("baseline", variant_result.baseline),
        variant=VariantBook.of(
            "variant", list(variant_result.kept) + list(variant_result.refilled)),
        control=VariantBook.of(
            "control", list(control_result.kept) + list(control_result.refilled)),
        identity=_identity(run, config),
    )


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _identity(run_dir: Path, config: ExperimentConfig) -> dict[str, Any]:
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
        "variant_version": config.variant_version,
        "data_snapshot_id": core.get("data_snapshot_id"),
        "source_scope": {
            "tier": 0,
            "population": "all resolved outcomes incl. slot-suppressed shadows",
            "partition": "FULL WINDOW (TRAIN + DEV). Tier 0 screens direction; "
                         "the promotion test is per-quarter over TRAIN and "
                         "belongs to the replay stage.",
            "dev_has_promotion_authority": False,
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_screen(screen: Screen, out_dir: Path | str) -> Path:
    """Write one screen. Deterministic apart from `generated_at`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"screen_{screen.slug}.json"
    path.write_text(canonical_json(screen.to_dict()) + "\n", encoding="utf-8")
    return path