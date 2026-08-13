"""Phase A — tier-0 offline studies. No replay, no lifecycle mutation, no writes.

Eight studies, each answering one question the V2 Core Rebuild programme must
settle before a single hour of replay is spent. Two families:

- **A1-A3 score counterfactuals** — arithmetic over the recorded ledger via
  `rescore`, which decomposes each score into the terms that produced it and
  verifies the decomposition against the recorded value.
- **A4-A8 population studies** — over the 92,627 resolved outcomes (admitted plus
  slot-suppressed), sliced into TRAIN quarters and judged by sign test.

## Why the resolved population and not the admitted one

The admitted set is 31,209 trades; the resolved set adds the 41,625 `CAPPED` and
28,473 `UNALERTED_INSERT` shadows the Outcome Engine resolved anyway. Those are
signals the *strategy* produced and the *lifecycle* suppressed, so for judging
signal quality they are the right population — three times the n, free of admission
selection, and sensitive to 0.031 R where the admitted set resolves only 0.053 R.

Book-level impact is a different question and needs `tier0.simulate`, because
removing a trade frees a slot and the cap binds in 51% of cycles.

## Volume-matched controls

With a −0.0087 R baseline and 41,625 suppressed candidates, **any** rule that
trades less improves net R mechanically. So every gate study also removes the same
number of trades by lowest score — a rule with no hypothesised skill — and reports
the margin. A gate that does not beat its own volume-matched control measured
volume, not edge, and `beats_control` says so.

## What Phase A cannot do

It cannot see re-scoring effects on admission (a changed score moves decision tiers,
which moves what enters memory), it cannot re-resolve an exit, and it cannot model
second-order lifecycle behavior. Those are tier 1 and tier 2. Every payload carries
`scope` saying so.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from backtest.experiments.partition import (
    DEV_END, DEV_START, HOLDOUT_START, TRAIN_END, TRAIN_START,
    Slice, role_of, sign_test, slice_quarters,
)
from backtest.experiments.rescore import (
    ALWAYS_FIRES, NEVER_FIRES, UNINFORMATIVE, Counterfactual, census,
)

__all__ = ["STUDIES", "Study", "run_phase_a", "write_phase_a"]

RESULTS_DIRNAME = "results"

#: What every study does and does not establish.
SCOPE = {
    "tier": 0,
    "is_replay": False,
    "population": "resolved outcomes (admitted + slot-suppressed shadows), "
                  "burn-in excluded",
    "cannot_see": [
        "admission effects of a changed score (tier moves change what enters memory)",
        "re-resolved exits (a different target produces a different outcome)",
        "second-order lifecycle behavior: kill cooldowns, miss_count, TTL, REPLACE",
    ],
    "escalate_to": "tier 1 for exits, tier 2 for the joint design",
}


# ---------------------------------------------------------------------------
# study container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Study:
    name: str
    title: str
    variant_slug: Optional[str]
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# shared statistics
# ---------------------------------------------------------------------------

def _drawdown(pairs: Sequence[tuple[Any, float]]) -> float:
    """Max drawdown in R over C-9.8 (exit) order."""
    equity = peak = worst = 0.0
    for _key, value in sorted(pairs, key=lambda item: item[0]):
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _summary(rows: Sequence["_Row"]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "net_r": 0.0, "expectancy": None, "win_rate": None,
                "max_drawdown_r": 0.0, "recovery_factor": None}
    values = [r.realized_r for r in rows]
    n = len(values)
    net = math.fsum(values)
    dd = _drawdown([(r.exit_key, r.realized_r) for r in rows])
    return {
        "n": n,
        "net_r": net,
        "expectancy": net / n,
        "win_rate": sum(1 for v in values if v > 0) / n,
        "max_drawdown_r": dd,
        "recovery_factor": (net / dd) if dd > 0 else None,
    }


@dataclass(frozen=True)
class _Row:
    """The projection of an outcome record these studies need."""

    boundary: int
    exit_key: tuple
    realized_r: float
    direction: Optional[str]
    entry_mode: Optional[str]
    score: Optional[int]
    labels: dict[str, Any]

    @property
    def role(self) -> str:
        return role_of(self.boundary)


def _train_sign_test(rows: Sequence[_Row]) -> dict[str, Any]:
    slices = slice_quarters(((r.boundary, r.realized_r) for r in rows),
                            role="TRAIN")
    return sign_test(slices).to_dict()


def _quarter_means(rows: Sequence[_Row], *, min_n: int = 30
                   ) -> dict[str, float]:
    slices = slice_quarters(((r.boundary, r.realized_r) for r in rows),
                            role="TRAIN", min_n=min_n)
    return {s.label: s.mean_r for s in slices if s.mean_r is not None}


def _paired_sign_test(
    rows: Sequence[_Row], reference: Sequence[_Row], *, min_n: int = 30,
) -> dict[str, Any]:
    """Sign test on the per-quarter *difference* in mean R.

    The absolute test — "is the kept book profitable this quarter" — is nearly
    uninformative here, because the baseline loses money in most quarters, so a
    genuinely improving gate still fails it. The paired test asks the question that
    decides promotion: **in how many quarters did this beat its comparator?**

    Against the *volume-matched control* this has teeth. Against the ungated
    baseline it does not: any rule that drops the worst trades raises the mean of
    what remains, so a pruning rule passes trivially. Both are reported, and the
    weakness of the second is stated in the payload rather than left to be
    discovered.
    """
    left, right = _quarter_means(rows, min_n=min_n), _quarter_means(
        reference, min_n=min_n)
    shared = sorted(set(left) & set(right))
    slices = [Slice(label=label, role="TRAIN",
                    values=(left[label] - right[label],))
              for label in shared]
    return sign_test(slices).to_dict()


def _gate(
    rows: Sequence[_Row], keep: Callable[[_Row], bool], *, name: str,
    question: str,
) -> dict[str, Any]:
    """A gate study with its volume-matched control.

    The control removes the same *count* of trades by lowest score, which is the
    cheapest rule with no hypothesised skill. Any gate that cannot beat it is
    measuring volume reduction.
    """
    kept = [r for r in rows if keep(r)]
    removed = [r for r in rows if not keep(r)]

    scored = [r for r in rows if r.score is not None]
    unscored = [r for r in rows if r.score is None]
    ranked = sorted(scored, key=lambda r: (r.score, r.boundary, r.exit_key))
    control_kept = ranked[len(removed):] + unscored if scored else list(rows)

    kept_stats = _summary(kept)
    control_stats = _summary(control_kept)
    margin = (None if kept_stats["expectancy"] is None
              or control_stats["expectancy"] is None
              else kept_stats["expectancy"] - control_stats["expectancy"])

    return {
        "study": name,
        "question": question,
        "baseline": _summary(rows),
        "kept": kept_stats,
        "removed": _summary(removed),
        "volume_matched_control": {
            **control_stats,
            "rule": "remove the same trade count by lowest score",
            "removed_n": len(removed),
        },
        "expectancy_margin_over_control": margin,
        "beats_control": (margin is not None and margin > 0),
        "train_sign_test_absolute": _train_sign_test(kept),
        "train_sign_test_vs_control": _paired_sign_test(kept, control_kept),
        "train_sign_test_vs_baseline": _paired_sign_test(kept, rows),
        "which_sign_test_decides": (
            "vs_control. The absolute test asks whether the kept book is "
            "profitable each quarter and the baseline loses money in most of "
            "them, so a real improvement can still fail it. The vs_baseline test "
            "is passed trivially by any rule that drops the worst trades. Only "
            "vs_control isolates whether the GATE carries skill."),
        "kept_share": (len(kept) / len(rows)) if rows else None,
        "reading": "a gate that does not beat its volume-matched control removed "
                   "trades rather than removed losses; expectancy rising while "
                   "net R falls is PRUNING, not edge",
        "scope": SCOPE,
    }


# ---------------------------------------------------------------------------
# A1-A3 — score counterfactuals
# ---------------------------------------------------------------------------

def _a1(run_dir: Path, _rows: Sequence[_Row]) -> Study:
    never = census(run_dir, Counterfactual(
        name="remove_never_fires", removed=NEVER_FIRES))
    always = census(run_dir, Counterfactual(
        name="remove_always_fires", removed=ALWAYS_FIRES))
    payload = {
        "study": "a1_dead_score_components",
        "question": "which 'dead' score components can be removed without "
                    "changing any decision?",
        "correction": (
            "An earlier version of the programme spec claimed all eight "
            "zero-information components were bit-neutral to remove. They are "
            "not. Only components that NEVER fire are; a component that ALWAYS "
            "fires is a constant, and deleting it shifts every score downward, "
            "moving rows a tier. The two classes are measured separately here."),
        "never_fires": {
            "components": list(NEVER_FIRES),
            "expected": "score_neutral and tier_neutral, provably",
            **never.to_dict(),
        },
        "always_fires": {
            "components": list(ALWAYS_FIRES),
            "expected": "a uniform negative score shift; tier-neutral only with "
                        "a compensating threshold shift",
            **always.to_dict(),
        },
        "verdict": {
            "never_fires_removable_bit_neutrally": (
                never.score_neutral and never.tier_neutral),
            "always_fires_requires_threshold_shift": not always.tier_neutral,
        },
        "scope": SCOPE,
    }
    return Study("a1_dead_score_components", "A1 · Dead score components",
                 "r1-core-cleanup", payload)


def _a2(run_dir: Path, _rows: Sequence[_Row]) -> Study:
    combined = census(run_dir, Counterfactual(
        name="remove_uninformative", removed=UNINFORMATIVE))
    individual = {
        name: census(run_dir, Counterfactual(
            name=f"remove_{name}", removed=(name,))).to_dict()
        for name in UNINFORMATIVE
    }
    payload = {
        "study": "a2_uninformative_removal",
        "question": "how many rows change decision tier when the measured-"
                    "uninformative components are removed?",
        "components": list(UNINFORMATIVE),
        "evidence_for_removal": {
            "sweep": "-0.0143 R, p_adj 1.0000, 2/4 years",
            "target_clear": "-0.0185 R, not significant, 96.8% true",
            "session_active": "-0.0213 R, 1/4 years",
            "htf_context_clean": "-0.0301 R, 1/4 years",
        },
        "combined": combined.to_dict(),
        "individual": individual,
        "scope": SCOPE,
    }
    return Study("a2_uninformative_removal", "A2 · Uninformative inputs",
                 "r1-core-cleanup", payload)


def _a3(run_dir: Path, _rows: Sequence[_Row]) -> Study:
    flipped = census(run_dir, Counterfactual(
        name="momentum_weakening_sign", reweighted={"momentum_weakening": 1}))
    zeroed = census(run_dir, Counterfactual(
        name="momentum_weakening_zero", reweighted={"momentum_weakening": 0}))
    payload = {
        "study": "a3_momentum_weakening_sign",
        "question": "how many rows change tier when momentum_weakening's weight "
                    "is corrected from -1?",
        "evidence": "+0.0617 R, CI [+0.0375, +0.0859], p_adj 0.0000, 4/4 years — "
                    "the component is scored as a penalty and measures as a "
                    "benefit",
        "flip_to_plus_one": flipped.to_dict(),
        "zero_it": zeroed.to_dict(),
        "scope": SCOPE,
    }
    return Study("a3_momentum_weakening_sign", "A3 · momentum_weakening sign",
                 "r1-core-cleanup", payload)


# ---------------------------------------------------------------------------
# A4-A8 — population studies
# ---------------------------------------------------------------------------

_RR_BANDS = ((0.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 4.0),
             (4.0, float("inf")))


def _a4(_run_dir: Path, rows: Sequence[_Row]) -> Study:
    def rr_of(row: _Row) -> Optional[float]:
        value = row.labels.get("tp_rr")
        return None if value is None else float(value)

    bands = []
    for low, high in _RR_BANDS:
        band = [r for r in rows if rr_of(r) is not None and low <= rr_of(r) < high]
        bands.append({"band": f"{low}-{high}", **_summary(band),
                      "train_sign_test_absolute": _train_sign_test(band),
                      "train_sign_test_vs_all": _paired_sign_test(band, rows)})

    capped = [r for r in rows if rr_of(r) is not None and rr_of(r) <= 2.5]
    wide = [r for r in rows if rr_of(r) is not None and rr_of(r) > 2.5]
    capped_stats, wide_stats = _summary(capped), _summary(wide)

    # The substitution estimate. Re-pricing is not a filter: the trade still
    # happens, with a different target, so its outcome must be *re-resolved* —
    # which tier 0 cannot do. Assuming the re-priced trades earn the mean of the
    # already-tight band is the least-worst stand-in, and it is an assumption.
    estimate = None
    if capped_stats["expectancy"] is not None and wide_stats["n"]:
        estimate = (wide_stats["n"] * capped_stats["expectancy"]
                    - wide_stats["net_r"])

    payload = {
        "study": "a4_tp_cap_counterfactual",
        "question": "what does capping the structural target at 2.5 RR do?",
        "bands": bands,
        "capped_le_2_5": capped_stats,
        "wide_gt_2_5": wide_stats,
        "substitution_estimate_net_r_gain": estimate,
        "estimate_assumption": (
            "re-priced trades are assumed to earn the mean R of the <=2.5 "
            "population. Re-pricing changes the outcome, which tier 0 cannot "
            "re-resolve — tier 1 (B1) measures this properly, paired, and B1 also "
            "chooses between 2.0 / 2.5 / 3.0 rather than accepting a band edge."),
        "monotonicity": "expectancy must fall monotonically across bands for the "
                        "attainment mechanism to hold",
        "scope": SCOPE,
    }
    return Study("a4_tp_cap_counterfactual", "A4 · TP cap", "r1-core-cleanup",
                 payload)


def _a5(_run_dir: Path, rows: Sequence[_Row]) -> Study:
    short_only = _gate(rows, lambda r: r.direction == "SHORT",
                       name="short_only",
                       question="what is the ceiling of a directional rule?")
    asym = _gate(
        rows,
        lambda r: (r.direction == "SHORT"
                   or (r.labels.get("volume_spike") is True
                       and r.labels.get("ob_candidate_count") is not None
                       and int(r.labels["ob_candidate_count"]) <= 3)),
        name="short_asymmetry",
        question="does requiring LONG to clear extra evidence capture the "
                 "direction advantage while keeping a long book?")
    payload = {
        "study": "a5_short_asymmetry",
        "question": "is the direction effect an asymmetry in required evidence, "
                    "or a reason to drop a side?",
        "evidence": "SHORT vs LONG +0.1230 R, CI [+0.0830, +0.1631], p 0.0000, "
                    "positive in 4/4 years and strongest in the most recent",
        "note": "direction IS weekly_bias — the joint distribution over 92,627 "
                "resolved trades has exactly two cells, (LONG, BULLISH) and "
                "(SHORT, BEARISH), with zero exceptions. There is no separate "
                "'weekly authority' to grant.",
        "short_only": short_only,
        "asymmetric_long_qualification": asym,
        "falsifier": "if short_only dominates the asymmetric variant on recovery "
                     "factor, the asymmetry hypothesis is refuted and the long "
                     "side has no rescue",
        "scope": SCOPE,
    }
    return Study("a5_short_asymmetry", "A5 · SHORT asymmetry",
                 "r3-short-asymmetry", payload)


def _a6(_run_dir: Path, rows: Sequence[_Row]) -> Study:
    payload = {
        "study": "a6_fvg_mandatory",
        "question": "does requiring an unfilled FVG remove losses or just trades?",
        "evidence": "+0.0842 R, CI [+0.0610, +0.1074], p_adj 0.0000, 4/4 years — "
                    "the strongest confirmation, currently worth +1",
        "caution": "weakest in the most recent year (+0.0189), which is DEV and "
                   "therefore has no promotion authority but is worth seeing",
        **_gate(rows, lambda r: r.labels.get("fvg") is True, name="fvg_mandatory",
                question="does requiring an unfilled FVG remove losses or just "
                         "trades?"),
    }
    return Study("a6_fvg_mandatory", "A6 · FVG mandatory",
                 "r2-selective-continuation", payload)


def _a7(_run_dir: Path, rows: Sequence[_Row]) -> Study:
    payload = {
        "study": "a7_continuation_only",
        "question": "does restricting to CONTINUATION improve edge?",
        "evidence": "+0.0874 R over RETRACEMENT; but 3/4 years, and the failing "
                    "year is the most recent (-0.0027)",
        "note": "mode and regime are the same variable — regime.py maps "
                "expansion->CONTINUATION and rotational->RETRACEMENT, and the two "
                "populations are identical in size and net R",
        "caution": "largest volume cut in the programme and the change DEV "
                   "contradicts; sequenced last and isolated in its own replay",
        **_gate(rows, lambda r: r.entry_mode == "CONTINUATION",
                name="continuation_only",
                question="does restricting to CONTINUATION improve edge?"),
    }
    return Study("a7_continuation_only", "A7 · Continuation only",
                 "r3-short-asymmetry", payload)


def _a8(_run_dir: Path, rows: Sequence[_Row]) -> Study:
    base = [r for r in rows
            if r.entry_mode == "CONTINUATION" and r.labels.get("fvg") is True
            and r.labels.get("tp_rr") is not None
            and float(r.labels["tp_rr"]) <= 2.5]
    filtered = _gate(
        base,
        lambda r: (r.labels.get("ob_candidate_count") is not None
                   and int(r.labels["ob_candidate_count"]) <= 3
                   and r.labels.get("volume_spike") is True),
        name="quality_filters",
        question="do max_ob_candidates=3 and require_volume_spike add edge or "
                 "merely prune?")
    base_stats = _summary(base)
    kept_stats = filtered["kept"]
    is_pruning = (
        kept_stats["expectancy"] is not None
        and base_stats["expectancy"] is not None
        and kept_stats["expectancy"] > base_stats["expectancy"]
        and kept_stats["net_r"] < base_stats["net_r"])
    payload = {
        "study": "a8_quality_filter_falsification",
        "question": "are the proposed quality filters edge or pruning?",
        "preregistered_prediction": "expectancy rises while total R FALLS — "
                                    "i.e. pruning, not edge",
        "r2_gate_baseline": base_stats,
        **filtered,
        "pruning_confirmed": is_pruning,
        "verdict": ("PRUNING — excluded from every replay bundle" if is_pruning
                    else "NOT PRUNING — the prediction is refuted and "
                         "ob_candidate_count belongs in the corrections bundle "
                         "as a weight"),
        "scope": SCOPE,
    }
    return Study("a8_quality_filter_falsification", "A8 · Quality filters",
                 "r3q-quality-filters", payload)


STUDIES: tuple[tuple[str, Callable[[Path, Sequence[_Row]], Study]], ...] = (
    ("a1_dead_score_components", _a1),
    ("a2_uninformative_removal", _a2),
    ("a3_momentum_weakening_sign", _a3),
    ("a4_tp_cap_counterfactual", _a4),
    ("a5_short_asymmetry", _a5),
    ("a6_fvg_mandatory", _a6),
    ("a7_continuation_only", _a7),
    ("a8_quality_filter_falsification", _a8),
)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def _load_rows(run_dir: Path) -> tuple[list[_Row], dict[str, Any]]:
    from backtest.dataset import LabeledDataset
    from backtest.metrics.equity import c98_order_key

    dataset = LabeledDataset.from_run(run_dir)
    rows: list[_Row] = []
    for record in dataset.records:
        if record.in_burn_in or record.realized_r is None:
            continue
        rows.append(_Row(
            boundary=int(record.boundary),
            exit_key=c98_order_key(record),
            realized_r=float(record.realized_r),
            direction=record.direction,
            entry_mode=record.entry_mode,
            score=record.score,
            labels=dataset.labels_for(record) or {},
        ))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.role] = counts.get(row.role, 0) + 1
    return rows, counts


def _variant_ids(experiments_root: Optional[Path]) -> dict[str, str]:
    if experiments_root is None:
        return {}
    from backtest.experiments.config import ExperimentConfigError, load_config

    ids: dict[str, str] = {}
    for directory in sorted(p for p in Path(experiments_root).iterdir()
                            if p.is_dir()):
        try:
            ids[directory.name] = load_config(
                directory / "config.json").variant_id
        except (ExperimentConfigError, OSError):
            continue
    return ids


def run_phase_a(
    run_dir: Path | str, *,
    experiments_root: Optional[Path | str] = None,
) -> dict[str, Study]:
    """Run every Phase A study. Reads the bundle; writes nothing."""
    run_dir = Path(run_dir)
    rows, role_counts = _load_rows(run_dir)
    ids = _variant_ids(None if experiments_root is None
                       else Path(experiments_root))

    identity = _identity(run_dir, role_counts)
    studies: dict[str, Study] = {}
    for name, function in STUDIES:
        study = function(run_dir, rows)
        payload = dict(study.payload)
        payload["identity"] = {
            **identity,
            "variant_id": ids.get(study.variant_slug or "", None),
            "variant_slug": study.variant_slug,
        }
        studies[name] = Study(study.name, study.title, study.variant_slug,
                              payload)
    return studies


def _identity(run_dir: Path, role_counts: dict[str, int]) -> dict[str, Any]:
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
            "population": SCOPE["population"],
            "partition": {
                "TRAIN": [TRAIN_START, TRAIN_END],
                "DEV": [DEV_START, DEV_END],
                "HOLDOUT": [HOLDOUT_START, None],
            },
            "role_counts": dict(sorted(role_counts.items())),
            "dev_has_promotion_authority": False,
        },
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_phase_a(studies: dict[str, Study], out_dir: Path | str) -> list[Path]:
    """Write one JSON per study. Deterministic apart from `generated_at`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(studies):
        path = out / f"{name}.json"
        path.write_text(
            json.dumps(studies[name].payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        written.append(path)
    return written