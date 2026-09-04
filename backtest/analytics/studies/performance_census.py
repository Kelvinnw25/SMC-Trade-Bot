"""Performance Census — a descriptive map of one frozen replay.

Opt-in and unregistered: absent from `studies.STUDIES`, absent from the CLI. It
is invoked explicitly, never as part of the standing report.

## What this answers

    Where did realized R accumulate, where did losses accumulate, how
    concentrated and persistent were they, and what was observed under the
    nine pre-declared contrasts?

## What this does not answer

**"Which strategy or filter is better."** The census makes no decision. It
emits no ranking, no candidate list, no filtered index, no pass/fail, no
p-value, no significance claim, and no recommendation. Every row is
`DESCRIPTIVE` and scoped to one in-sample bundle covering roughly 27% of the
strategy's decisions.

Three review rounds established why: every fatal defect found in the two
earlier designs of this tool attached to a component that made a *decision* —
a ranking in the first, a coherence gate in the second. A census with no
decision rule has nothing left to miscalibrate. Adding one back would
reintroduce selection, and with it multiplicity, the winner's curse, and the
need for a calibration this dataset cannot support (ESS ≈ 0.25n at a measured
3.97x variance inflation).

## The cross-mode prohibition

The line is **accounting versus comparison**. Accounting states what R
accumulated and needs no counterfactual. Comparison needs the unfilled
counterfactual, which is unobservable.

Entering unfilled decisions as 0R multiplies each mode's mean by its own fill
rate — measured 0.9747 for CONTINUATION against 0.7850 for RETRACEMENT. So the
decision basis does not make the modes comparable; it applies a mode-specific
shrinkage. Neither basis identifies the counterfactual, and the unfilled
bracket (`-1R` to `structural_rr`) is reported precisely to show that its width
exceeds the effects being discussed.

Therefore no emitted row spans two modes, `_guard_row` refuses any row whose
cell is not one of the four frozen single-mode cells, and the rendered output
is screened against a comparative word list.

## Sign vectors are data, not a test

Each binary contrast carries the sign of its mean-R difference in six strata
(the four cells plus two halves of the window, split at the median boundary).
This is reported because reversal is the most decision-relevant fact about a
contrast, and because intersections are deliberately absent — so a reader has
no other way to see it.

It is **not** a criterion. Six-of-six agreement occurs for roughly
`NULL_SIGN_AGREEMENT_RATE` of contrasts that have no relationship to R at all,
so about one of the nine contrasts is expected to show it by chance. That
constant is a measured value, not a runtime computation; its provenance is
recorded beside it. Nothing in this module filters, ranks, or gates on it.

## Populations

    fill basis      Populations.scored     — every R metric
    decision basis  Populations.resolved   — unfilled enter as 0R; mean only
    bracket         the unfilled subset    — one disclosure number per cell

`resolved` decomposes exactly into `scored` plus the unfilled subset, verified
at run time by `_population_accounting`.

## Reuse

Nothing here recomputes a metric `backtest.metrics` defines. The census reuses
`LabeledDataset` (the sanctioned label join), `Populations`,
`exit_census.robust_summary`, and `exit_census.CELLS`. Concentration and
persistence are new quantities, not present in `metrics.definitions`, so they
are computed here.

## Purity

Reader-only. No import of `backtest.replay`, no `v2` or `src` reference, no
RNG, no clock beyond the emission stamp, and no write outside the caller's
output directory.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Optional, Sequence

from backtest.analytics.studies.exit_census import CELLS, TAIL_ATR_FLOOR, TRIM, robust_summary

__all__ = [
    "CELLS",
    "CLOSED",
    "CONTRASTS",
    "EVIDENCE_CLASS",
    "FEE_BAND_BP",
    "IMBALANCE_THRESHOLD",
    "LEG_CONVENTIONS",
    "MIN_SIDE_N",
    "NULL_SIGN_AGREEMENT_RATE",
    "PROHIBITED_GROUPING_KEYS",
    "CensusError",
    "analyse",
    "csv_rows",
    "render",
    "specification_hash",
    "write_census",
]

CENSUS_DIRNAME = "performance_census"
CENSUS_ENGINE_VERSION = 1

#: The only evidence class this module can emit. There is no code path to any
#: other value, and no mechanism here can promote a row.
EVIDENCE_CLASS = "DESCRIPTIVE"


class CensusError(Exception):
    """The census cannot be assembled, or a frozen rule was violated."""


# ---------------------------------------------------------------------------
# Frozen specification
# ---------------------------------------------------------------------------
#
# Every constant below is hashed into `specification_hash`. Changing any of
# them changes the hash, which is what makes "the specification did not move
# during the run" a checkable claim rather than an assurance. None of them is a
# runtime parameter: there are no knobs, so a floor cannot be tuned until an
# observation appears.

#: Effective-sample floor for one side of a contrast, or one month of a cell.
#:
#: Deliberately **not** `metrics.MIN_SEGMENT_N` (30). That threshold governs
#: whether the metrics engine calls a segment inferential; it was calibrated
#: for reporting, not for research admission. Episode inference measured an
#: effective sample of about a quarter of the nominal count, so n = 30 carries
#: ESS ~ 7. 120 nominal is the smallest round figure that keeps ESS at or above
#: 30 under that deflator. Below it a mean is *marked*, never withheld — the
#: census reports what it measured and says when the measurement is thin.
MIN_SIDE_N = 120

#: Minority share below which a contrast is marked `IMBALANCED` in that cell.
#: A contrast can be balanced in one cell and near-constant in another — `sweep`
#: runs 0.46 in CONTINUATION and 0.947 in RETRACEMENT — so usability is decided
#: per cell, never once for the whole book.
IMBALANCE_THRESHOLD = 0.10

#: Fee tiers in basis points, and whether one leg or a round trip is charged.
#: A band rather than a point: the account's tier is not recorded in the bundle,
#: and one-leg versus round-trip is a factor of two. Emitted as columns so the
#: reader performs the comparison. **No gate is derived from these.**
FEE_BAND_BP = (2.0, 5.0, 10.0)
LEG_CONVENTIONS = (("one_leg", 1), ("round_trip", 2))

#: Calendar buckets read `boundary` (instantiation), matching the segments
#: registry. `exit_T` — when the R was realised, and what the C-9.8 equity path
#: is ordered by — disagrees for any trade spanning a boundary. The census
#: commits to instantiation because that is when the strategy decided, and
#: names the choice here so the two are never silently mixed.
TIME_BASIS = "boundary"

#: The measured rate at which a contrast unrelated to R nonetheless shows the
#: same sign in all six strata. Six strata rather than nine: the symbol-tercile
#: axis was removed because no construction of it is defensible (tenure,
#: volume rank and R contribution are each confounded or circular), and
#: dropping strata makes agreement *easier*, which is why this figure is higher
#: than the nine-stratum one.
#:
#: Provenance: permutation over the frozen official run, random binary labels
#: at marginal frequencies 0.10/0.15/0.30/0.50, N = 6,000 each, each side of
#: each stratum required to hold at least 30 rows. Observed 0.0998-0.1063.
#: A literal, not a computation: calibrating at run time would add an
#: inferential layer to a module that deliberately performs no inference.
NULL_SIGN_AGREEMENT_RATE = 0.103

#: Keys that may never group a contrast. The first five are unknowable at entry
#: — conditioning on them describes the outcome with itself. `state` covers
#: WIN/LOSS, which **is** permitted as the gross-accounting partition (the
#: positive and negative ledgers are exactly that split) but never as a
#: condition.
PROHIBITED_GROUPING_KEYS = frozenset({
    "state", "exit_reason", "fill_kind", "holding_candles", "cancel_reason",
    "realized_r", "realized_r_net", "mae_r", "mfe_r", "exit_price", "exit_T",
})

#: Provenance classes, and what each may support.
A_PRIORI = "A_PRIORI"
POST_HOC_DERIVED = "POST_HOC_DERIVED"

PROVENANCE_CLAIMS = {
    A_PRIORI: (
        "The frozen strategy documentation asserts this as an intended source "
        "of edge, independently of any measurement on this bundle. Supports an "
        "observation, and a strategy-coherence reading: whether the mechanism "
        "the strategy claims co-varies with R here. It does not confer "
        "out-of-sample status — this bundle has been examined repeatedly, so "
        "the measurement remains in-sample."),
    POST_HOC_DERIVED: (
        "Surfaced by prior analysis of THIS bundle. Supports an observation "
        "and nothing further. It may motivate a test on data not used to "
        "surface it; it may not support any claim tested against these same "
        "trades, and it may not be reported as something the census found."),
}

#: Statements the census may not make. Screened against rendered output.
PROHIBITED_WORDS = ("recommend", "best", "should", "outperform")

PROHIBITED_STATEMENTS = (
    "any ordering or ranking of the four cells, or of the two entry modes",
    "any claim that one mode, direction, or contrast side is better than another",
    "any single number combining both modes without both fill rates in the record",
    "any use of WIN/LOSS as a condition rather than as the gross-accounting split",
    "any pass/fail, significance, or edge claim derived from a sign vector",
    "any forward expectation, parameter value, filter, or strategy change",
)


# ---------------------------------------------------------------------------
# Contrast extractors
# ---------------------------------------------------------------------------

def _flag(name: str) -> Callable[[Any], Optional[bool]]:
    def extract(trade: Any) -> Optional[bool]:
        value = trade.labels.get(name)
        return None if value is None else bool(value)
    return extract


def _htf_aligned_vs_partial(trade: Any) -> Optional[bool]:
    """True for ALIGNED, False for PARTIAL, None for anything else.

    OPPOSED is excluded rather than folded in: it holds 490 rows book-wide,
    which leaves under 30 in most cells. NEUTRAL is excluded because neither
    bias is directional, which is a different statement from disagreement.
    """
    d1, h4 = trade.labels.get("d1_bias"), trade.labels.get("h4_bias")
    if d1 is None or h4 is None or trade.record.direction is None:
        return None
    favour = "BULLISH" if trade.record.direction == "LONG" else "BEARISH"
    agree = sum(1 for bias in (d1, h4) if str(bias).upper() == favour)
    if agree == 2:
        return True
    if agree == 1:
        return False
    return None


def _ob_candidates_le3(trade: Any) -> Optional[bool]:
    count = trade.labels.get("ob_candidate_count")
    return None if count is None else int(count) <= 3


#: ATR as a share of price, on the segments registry's declared edges. Absolute
#: ATR is not comparable between a symbol trading at 0.00001 and one at 90,000,
#: which is why the registered `atr_band` dimension is closed and this
#: relative form is used instead.
_ATR_PCT_EDGES = (0.0, 0.005, 0.01, 0.02, 0.05)


def _atr_pct_band(trade: Any) -> Optional[str]:
    atr, price = trade.labels.get("atr_last"), trade.record.entry_price
    if not atr or not price:
        return None
    value = float(atr) / float(price)
    for low, high in zip(_ATR_PCT_EDGES, _ATR_PCT_EDGES[1:]):
        if low <= value < high:
            return f"[{low:g},{high:g})"
    return f">={_ATR_PCT_EDGES[-1]:g}"


@dataclass(frozen=True)
class Contrast:
    """One pre-declared way of splitting a cell.

    `kind` is "binary" (two sides, so a sign is defined) or "multi_level" (a
    banded quantity, where no single sign exists). The distinction is
    structural, not a convenience: a five-bucket band has no binary difference
    to take the sign of, so its sign vector is absent with a stated reason
    rather than filled with something invented.
    """

    name: str
    kind: str
    provenance: str
    extract: Callable[[Any], Any]
    description: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "provenance": self.provenance,
            "provenance_claims": PROVENANCE_CLAIMS[self.provenance],
            "description": self.description,
            "declared_warnings": list(self.warnings),
        }


CONTRASTS: tuple[Contrast, ...] = (
    # -- A_PRIORI: the strategy's own asserted confirmation stack and gate ---
    Contrast("sweep", "binary", A_PRIORI, _flag("sweep"),
             "a liquidity sweep was confirmed"),
    Contrast("fvg", "binary", A_PRIORI, _flag("fvg"),
             "a fair-value gap supported the entry"),
    Contrast("displacement", "binary", A_PRIORI, _flag("displacement"),
             "a displacement leg was validated"),
    Contrast("volume_spike", "binary", A_PRIORI, _flag("volume_spike"),
             "a volume spike accompanied the setup"),
    Contrast("session_active", "binary", A_PRIORI, _flag("session_active"),
             "the SNIPER session gate was open"),

    # -- POST_HOC_DERIVED: surfaced by prior analysis of this same bundle ---
    Contrast("momentum_weakening", "binary", POST_HOC_DERIVED,
             _flag("momentum_weakening"),
             "D1 momentum or range was contracting",
             ("PREVIOUSLY_REJECTED_AS_SCORING_WEIGHT",)),
    Contrast("htf_alignment_ALIGNED_vs_PARTIAL", "binary", POST_HOC_DERIVED,
             _htf_aligned_vs_partial,
             "D1 and H4 both favour the direction, against exactly one",
             ("DIMENSION_A_PRIORI_CONTRAST_POST_HOC", "OPPOSED_AND_NEUTRAL_EXCLUDED")),
    Contrast("atr_pct_band", "multi_level", POST_HOC_DERIVED, _atr_pct_band,
             "ATR as a share of price, on declared edges",
             ("KNOWN_REVERSAL",)),
    Contrast("ob_candidate_count_le3", "binary", POST_HOC_DERIVED,
             _ob_candidates_le3,
             "at most three order blocks qualified"),
)

#: Why each excluded dimension is excluded, and where the exclusion was
#: established. Emitted so that a later reader cannot re-litigate a closed
#: question without first reading the reason it closed.
CLOSED: tuple[tuple[str, str, str], ...] = (
    ("target_clear", "NEAR_INVARIANT",
     "true on 0.965/0.967/0.990/0.989 of the four cells; the false side holds "
     "about 46 rows in RETRACEMENT x SHORT"),
    ("session", "NESTED_IN_GATE",
     "session=ASIA implies session_active=False deterministically (0 of 9,158); "
     "a reporting convention duplicating the gate it is nested in"),
    ("fresh_ob", "NEAR_INVARIANT", "fires on 99.1% of the admitted book"),
    ("htf_context_clean", "STRUCTURALLY_GATED",
     "reachable only at location=equilibrium"),
    ("location", "DIRECTION_RELABELING",
     "discount is 100.00% SHORT (7,431/7,431); premium is 0.00% SHORT"),
    ("weekly_bias", "DIRECTION_RELABELING",
     "identical to direction; zero off-diagonal cells in 31,209 trades"),
    ("regime", "DIRECTION_CONFOUNDED",
     "expansion_up is 9.2% SHORT, expansion_down 92.7% SHORT"),
    ("regime_class", "DIRECTION_CONFOUNDED", "a coarsening of regime"),
    ("htf_margin_band", "REDUNDANT",
     "htf_bull/htf_bear take four values, 75.4% at one, and the 7.0 stratum is "
     "identical to htf_alignment=ALIGNED"),
    ("stop_atr_band", "FRICTION_CIRCULAR",
     "friction per R is a function of stop distance, spanning 37x across this "
     "dimension; it is also the denominator of R"),
    ("rr_bucket", "NULL_CIRCULAR",
     "structural_rr is the input to the geometric null 1/(1+RR)"),
    ("tp_rr_bucket", "NULL_CIRCULAR", "the target-side form of the same quantity"),
    ("score_band", "PREVIOUSLY_REJECTED",
     "rejected as a source of expectancy; A+ against VALID differs by 0.015 R "
     "at roughly 1.2-1.6 sigma"),
    ("alert_tier", "PREVIOUSLY_REJECTED", "the tier form of score_band"),
    ("tp_source", "PREVIOUSLY_REJECTED",
     "liquidity targeting falsified within mode and direction; fallback_2r is "
     "the strongest target source"),
    ("m5_choch", "AVAILABILITY_GATED",
     "present only where M5 enrichment ran, which is itself score-gated"),
    ("sweep_reclaim_band", "AVAILABILITY_GATED",
     "present only where a sweep fired, so conditioning on it conditions on sweep"),
    ("selected_ob_rank", "AVAILABILITY_GATED",
     "present only where an order block was selected"),
    ("selected_ob_fresh", "AVAILABILITY_GATED", "the freshness form of the same gate"),
    ("exit_reason", "HINDSIGHT", "not knowable at entry"),
    ("fill_kind", "HINDSIGHT", "not knowable at entry"),
    ("holding_bucket", "HINDSIGHT",
     "not knowable at entry; conditioning on it is largely geometric tautology"),
    ("cancel_reason", "HINDSIGHT", "not knowable at entry"),
    ("WIN_LOSS_as_condition", "HINDSIGHT",
     "the outcome itself. Permitted as the gross-accounting partition, where it "
     "is exactly the positive/negative split, and never as a condition"),
    ("entry_source", "MODE_PROXY",
     "continuation_micro_pullback is equivalent to entry_mode=CONTINUATION"),
    ("strategy_version", "ZERO_VARIANCE", "single-valued within one run"),
    ("contract_version", "ZERO_VARIANCE", "single-valued within one run"),
    ("outcome_config_hash", "ZERO_VARIANCE", "single-valued within one run"),
    ("data_snapshot_id", "ZERO_VARIANCE", "single-valued within one run"),
    ("source", "ZERO_VARIANCE", "single-valued within one run"),
    ("shadow_kind", "ZERO_VARIANCE", "shadows are excluded from every population"),
    ("symbol", "STRATIFICATION_AXIS_ONLY",
     "used for concentration and breadth, never as a contrast"),
    ("month", "STRATIFICATION_AXIS_ONLY",
     "used for persistence and concentration, never as a contrast"),
)

CLOSED_BY_NAME = {name: (reason, note) for name, reason, note in CLOSED}

#: Limits no analysis of this bundle can remove. Emitted verbatim.
LIMITATIONS = (
    "One bundle, in-sample by construction. Eleven studies have examined these "
    "trades; no unexamined split remains.",
    "Coverage is roughly 27% of decisions: 82,954 shadow records sit outside "
    "every population here.",
    "The counterfactual R of an unfilled decision is unobservable. No "
    "convention identifies it, and non-fill correlates with having been "
    "directionally right.",
    "Costs are imputed. The account's fee tier is not recorded in the bundle, "
    "so friction is arithmetic on a declared band.",
    "The path is unobservable beyond mae_r and mfe_r: no ordering, no timing.",
    "Dependence forbids segment-level inference. Measured variance inflation "
    "is about 3.97x, so effective sample is near a quarter of nominal.",
    "Universe survivorship: 9 of 564 symbols span all 42 months, and "
    "volume-ranked entry over-represents symbols that grew.",
    "Slot contention: max_open_positions = 10, so the admitted set is neither "
    "a random sample of signals nor independent of universe rank.",
    "Interaction reversal is undetectable here. Intersections are absent by "
    "design, so a contrast coherent in all six marginal strata may still "
    "reverse inside a two-way split. A six-of-six sign vector is not evidence "
    "against that.",
    "No causal claim is available. Every contrast is observational and jointly "
    "determined with the others by one pipeline.",
    "Cumulative multiplicity across all studies of this bundle is "
    "unrecoverable, so no global false-discovery rate exists.",
)


def specification_hash() -> str:
    """A digest over every frozen constant.

    Canonical JSON with sorted keys, so the digest depends on the values and
    not on declaration order. If a constant moves, the hash moves with it.
    """
    payload = {
        "engine_version": CENSUS_ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "cells": [list(cell) for cell in CELLS],
        "contrasts": [[c.name, c.kind, c.provenance, list(c.warnings)]
                      for c in CONTRASTS],
        "closed": [list(entry) for entry in CLOSED],
        "fee_band_bp": list(FEE_BAND_BP),
        "leg_conventions": [list(leg) for leg in LEG_CONVENTIONS],
        "min_side_n": MIN_SIDE_N,
        "imbalance_threshold": IMBALANCE_THRESHOLD,
        "sign_strata": _sign_stratum_names(),
        "trim": TRIM,
        "tail_atr_floor": TAIL_ATR_FLOOR,
        "time_basis": TIME_BASIS,
        "prohibited_grouping_keys": sorted(PROHIBITED_GROUPING_KEYS),
        "prohibited_words": list(PROHIBITED_WORDS),
        "atr_pct_edges": list(_ATR_PCT_EDGES),
        "null_sign_agreement_rate": NULL_SIGN_AGREEMENT_RATE,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _sign_stratum_names() -> list[str]:
    return [cell for cell, _mode, _direction in CELLS] + ["half_1", "half_2"]


def guard_grouping_key(name: str) -> str:
    """Refuse a prohibited grouping key. Raised, never warned."""
    if name in PROHIBITED_GROUPING_KEYS:
        reason, note = CLOSED_BY_NAME.get(
            "WIN_LOSS_as_condition" if name == "state" else name,
            ("HINDSIGHT", "not knowable at entry"))
        raise CensusError(
            f"{name!r} may not group a contrast ({reason}): {note}")
    return name


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """One decision, filled or not, with its joined ledger labels."""

    record: Any
    labels: dict[str, Any]

    @property
    def filled(self) -> bool:
        return self.record.realized_r is not None

    @property
    def cell(self) -> Optional[str]:
        for name, mode, direction in CELLS:
            if (self.record.entry_mode == mode
                    and self.record.direction == direction):
                return name
        return None


@dataclass(frozen=True)
class Book:
    """The three populations plus the identity needed to cite them."""

    resolved: tuple[Row, ...]          # decision basis
    scored: tuple[Row, ...]            # fill basis
    unfilled: tuple[Row, ...]          # the bracket subset
    counts: dict[str, int]
    join: dict[str, Any]

    @property
    def denominators(self) -> dict[str, int]:
        """Carried into every emitted block, never the header alone."""
        return {
            "shadow_records_excluded": self.counts["shadows"],
            "unfilled_decisions_excluded_from_fill_basis": len(self.unfilled),
        }


#: Below this share of joined ledger rows the label contrasts are refused
#: outright: a partial join drops rows non-randomly from every label-sourced
#: contrast, and an unavailable contrast is preferable to a biased one.
JOIN_COMPLETENESS_FLOOR = 0.999


def load(run_dir: Path | str) -> Book:
    """Assemble the three populations from a finished bundle."""
    from backtest.dataset import LabeledDataset, canonical_key
    from backtest.metrics import Populations

    dataset = LabeledDataset.from_run(run_dir)
    populations = Populations.of(dataset.records)

    def wrap(records: Iterable[Any]) -> tuple[Row, ...]:
        out = []
        for record in records:
            key = canonical_key(record.run_id, record.symbol, record.h1_last_ts)
            out.append(Row(record, dataset.labels.get(key) or {}))
        return tuple(out)

    resolved = wrap(populations.resolved)
    scored = wrap(populations.scored)
    unfilled = tuple(row for row in resolved if not row.filled)

    joined = len(dataset.records)
    completeness = (dataset.joined / joined) if joined else 0.0
    if completeness < JOIN_COMPLETENESS_FLOOR:
        raise CensusError(
            f"label join covers {completeness:.4f} of {joined} records, below "
            f"the {JOIN_COMPLETENESS_FLOOR} floor. A partial join removes rows "
            f"non-randomly from every label contrast; refusing rather than "
            f"reporting a biased split.")

    counts = dict(populations.to_dict())
    return Book(
        resolved=resolved, scored=scored, unfilled=unfilled, counts=counts,
        join={"records": joined, "joined": dataset.joined,
              "unjoined": dataset.unjoined, "completeness": completeness,
              "floor": JOIN_COMPLETENESS_FLOOR},
    )


# ---------------------------------------------------------------------------
# Block 1 — population accounting
# ---------------------------------------------------------------------------

def _population_accounting(book: Book) -> dict[str, Any]:
    """Every population, per cell, with the identities asserted."""
    scored_n = len(book.scored)
    unfilled_n = len(book.unfilled)
    resolved_n = len(book.resolved)
    if scored_n + unfilled_n != resolved_n:
        raise CensusError(
            f"resolved ({resolved_n}) is not scored ({scored_n}) plus unfilled "
            f"({unfilled_n}); the decision basis is not a partition")

    per_cell = {}
    for name, _mode, _direction in CELLS:
        resolved_cell = [r for r in book.resolved if r.cell == name]
        scored_cell = [r for r in resolved_cell if r.filled]
        unfilled_cell = [r for r in resolved_cell if not r.filled]
        per_cell[name] = {
            "resolved_decisions": len(resolved_cell),
            "scored_trades": len(scored_cell),
            "unfilled_decisions": len(unfilled_cell),
            "fill_rate": (len(scored_cell) / len(resolved_cell)
                          if resolved_cell else None),
        }

    unassigned = sum(1 for r in book.resolved if r.cell is None)
    return {
        "denominators": book.denominators,
        "funnel": book.counts,
        "join": book.join,
        "bases": {
            "fill_basis": {"population": "Populations.scored", "n": scored_n},
            "decision_basis": {"population": "Populations.resolved",
                               "n": resolved_n,
                               "unfilled_enter_as": 0.0},
            "bracket": {"population": "the unfilled subset", "n": unfilled_n},
        },
        "per_cell": per_cell,
        "unassigned_to_any_cell": unassigned,
        "identity": {
            "resolved_equals_scored_plus_unfilled": True,
            "cells_plus_unassigned_equals_resolved":
                sum(c["resolved_decisions"] for c in per_cell.values())
                + unassigned == resolved_n,
        },
    }


# ---------------------------------------------------------------------------
# Block 2 — fill-selection disclosure
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    return (float(sum(values)) / len(values)) if values else None


def _fill_selection(rows: Sequence[Row], denominators: dict[str, int],
                    ) -> dict[str, Any]:
    """Both bases, the shrinkage the decision basis applies, and the bracket.

    The shrinkage factor is emitted beside the decision-basis mean, always. It
    equals the fill rate, which is precisely why the decision basis does not
    make two differently-attenuated populations comparable.

    The bracket replaces each unfilled row with -1R (worst) and with its own
    `structural_rr` (best). Its width is the honest statement about what the
    unfilled subset can do to any mean computed over these decisions.
    """
    filled = [r for r in rows if r.filled]
    unfilled = [r for r in rows if not r.filled]
    fill_values = [float(r.record.realized_r) for r in filled]
    decision_values = [float(r.record.realized_r or 0.0) for r in rows]

    worst = list(fill_values) + [-1.0] * len(unfilled)
    best = list(fill_values) + [
        float(r.record.structural_rr) if r.record.structural_rr is not None
        else 0.0 for r in unfilled]

    fill_mean, decision_mean = _mean(fill_values), _mean(decision_values)
    worst_mean, best_mean = _mean(worst), _mean(best)
    return {
        "denominators": dict(denominators),
        "n_resolved_decisions": len(rows),
        "n_scored_trades": len(filled),
        "n_unfilled_decisions": len(unfilled),
        "fill_rate": (len(filled) / len(rows)) if rows else None,
        "fill_basis_mean_r": fill_mean,
        "decision_basis_mean_r": decision_mean,
        "decision_basis_shrinkage_factor": (
            (decision_mean / fill_mean)
            if (fill_mean not in (None, 0.0) and decision_mean is not None)
            else None),
        "shrinkage_note": (
            "the decision basis multiplies the mean by the fill rate, so it "
            "attenuates each population by its own attrition and does not make "
            "two populations comparable"),
        "bracket_worst_mean_r": worst_mean,
        "bracket_best_mean_r": best_mean,
        "bracket_width_r": (
            None if worst_mean is None or best_mean is None
            else best_mean - worst_mean),
        "bracket_note": (
            "unfilled rows replaced by -1R and by their own structural_rr. "
            "Neither endpoint is an estimate; the width states what the "
            "unfilled subset leaves unidentified"),
    }


# ---------------------------------------------------------------------------
# Block 3 — trade-level gross accounting
# ---------------------------------------------------------------------------

def _gross(values: Sequence[float]) -> tuple[float, float]:
    """Win and loss contributions, summed at the **trade** level.

    Taking the sign per trade rather than per segment is what makes the shares
    invariant: for any partition, the segment win contributions sum to the book
    win contribution exactly, because each trade lands in exactly one segment
    and contributes the same non-negative amount wherever it lands. Taking the
    sign per *segment* instead makes the denominator grow as the partition is
    refined, which is the defect this replaces.
    """
    win = float(sum(v for v in values if v > 0))
    loss = float(sum(v for v in values if v < 0))
    return win, loss


def _gross_record(values: Sequence[float], book_win: float, book_loss: float,
                  denominators: dict[str, int]) -> dict[str, Any]:
    win, loss = _gross(values)
    return {
        "denominators": dict(denominators),
        "n": len(values),
        # `net` first: it is the only figure that answers "what did this
        # contribute", and the shares below locate variance, not profit.
        "net": win + loss,
        "win_contribution": win,
        "loss_contribution": loss,
        "share_of_gross_positive": (win / book_win) if book_win else None,
        "share_of_gross_negative": (loss / book_loss) if book_loss else None,
        "median_r": (float(median(values)) if values else None),
        "share_note": (
            "shares are of the book's gross positive and gross negative R, "
            "computed at trade level and therefore invariant to how finely the "
            "population is partitioned. They locate where wins and losses "
            "accumulated, not where net profit came from. No share is taken "
            "against the net figure: that denominator is sign-unstable and "
            "near zero for several cells, so a share of it is unbounded"),
    }


def _gross_accounting(book: Book) -> dict[str, Any]:
    values = [float(r.record.realized_r) for r in book.scored]
    book_win, book_loss = _gross(values)
    per_cell = {}
    for name, _mode, _direction in CELLS:
        cell_values = [float(r.record.realized_r) for r in book.scored
                       if r.cell == name]
        per_cell[name] = _gross_record(cell_values, book_win, book_loss,
                                       book.denominators)
    # WIN/LOSS is legitimate here and only here: the two ledgers below *are*
    # that split, which is why it needs no separate conditioning treatment.
    return {
        "denominators": book.denominators,
        "population": "fill basis (Populations.scored)",
        "book": {
            "n": len(values),
            "gross_positive": book_win,
            "gross_negative": book_loss,
            "net": book_win + book_loss,
            "win_loss_note": (
                "the positive and negative ledgers are exactly the WIN/LOSS "
                "partition of realized R. That is the one legitimate use of "
                "the outcome as a grouping; it is closed as a condition"),
        },
        "per_cell": per_cell,
        "closure": {
            "cell_win_sums_to_book": abs(
                sum(c["win_contribution"] for c in per_cell.values())
                - book_win) < 1e-6,
            "cell_loss_sums_to_book": abs(
                sum(c["loss_contribution"] for c in per_cell.values())
                - book_loss) < 1e-6,
        },
    }


# ---------------------------------------------------------------------------
# Block 4 — concentration
# ---------------------------------------------------------------------------

_SYMBOL_TOP_K = (1, 5, 10, 25)
_MONTH_TOP_K = (1, 3, 6)


def _month_of(row: Row) -> str:
    moment = datetime.fromtimestamp(int(row.record.boundary), tz=timezone.utc)
    return moment.strftime("%Y-%m")


def _herfindahl(shares: Sequence[float]) -> Optional[float]:
    """Sum of squared shares. `None` when there is nothing to concentrate."""
    total = float(sum(shares))
    if total <= 0:
        return None
    return float(sum((s / total) ** 2 for s in shares))


def _concentration_side(groups: dict[str, float], top_k: Sequence[int],
                        ) -> dict[str, Any]:
    """One side of the ledger. `groups` holds non-negative magnitudes.

    Ranked on `(-magnitude, key)` rather than magnitude alone. Two symbols or
    two months can contribute exactly the same amount, and with a partial
    ordering the tie falls to dict insertion order — which depends on the order
    trades arrived in, so the reported `keys` would change under a permutation
    of an identical population. The key is the tiebreaker because it is unique
    and stable.
    """
    ranked = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))
    total = float(sum(groups.values()))
    return {
        "contributors": len(ranked),
        "total": total,
        "herfindahl": _herfindahl(list(groups.values())),
        "top": {
            f"top_{k}": {
                "magnitude": float(sum(v for _key, v in ranked[:k])),
                "share": (float(sum(v for _key, v in ranked[:k])) / total)
                if total > 0 else None,
                "keys": [key for key, _v in ranked[:k]],
            } for k in top_k
        },
    }


def _concentration(book: Book) -> dict[str, Any]:
    """Where wins and losses concentrated, by symbol and by month.

    The two sides are kept apart throughout. Netting them first would let a
    symbol that both won and lost heavily look small, which is the opposite of
    what a concentration figure is for.
    """
    out: dict[str, Any] = {"denominators": book.denominators,
                           "population": "fill basis (Populations.scored)",
                           "per_cell": {}}
    for name, _mode, _direction in CELLS:
        rows = [r for r in book.scored if r.cell == name]
        wins_by_symbol: dict[str, float] = {}
        losses_by_symbol: dict[str, float] = {}
        wins_by_month: dict[str, float] = {}
        losses_by_month: dict[str, float] = {}
        for row in rows:
            value = float(row.record.realized_r)
            symbol, month = row.record.symbol, _month_of(row)
            if value > 0:
                wins_by_symbol[symbol] = wins_by_symbol.get(symbol, 0.0) + value
                wins_by_month[month] = wins_by_month.get(month, 0.0) + value
            elif value < 0:
                losses_by_symbol[symbol] = (
                    losses_by_symbol.get(symbol, 0.0) - value)
                losses_by_month[month] = losses_by_month.get(month, 0.0) - value

        symbols = {r.record.symbol for r in rows}
        months = {_month_of(r) for r in rows}
        tenure_months = len({_month_of(r) for r in book.scored})
        out["per_cell"][name] = {
            "n": len(rows),
            "symbols_present": len(symbols),
            "symbols_at_min_side_n": sum(
                1 for symbol in symbols
                if sum(1 for r in rows if r.record.symbol == symbol)
                >= MIN_SIDE_N),
            "months_present": len(months),
            "breadth_note": (
                "raw and tenure-normalised breadth are both given; neither is "
                "unbiased. Universe entry is volume-ranked, so tenure is "
                "associated with volatility and therefore with R"),
            "tenure_normalised_symbol_breadth": (
                (len(symbols) / len({r.record.symbol for r in book.scored}))
                if book.scored else None),
            "tenure_normalised_month_breadth": (
                (len(months) / tenure_months) if tenure_months else None),
            "positive": {
                "by_symbol": _concentration_side(wins_by_symbol, _SYMBOL_TOP_K),
                "by_month": _concentration_side(wins_by_month, _MONTH_TOP_K),
            },
            "negative": {
                "by_symbol": _concentration_side(losses_by_symbol, _SYMBOL_TOP_K),
                "by_month": _concentration_side(losses_by_month, _MONTH_TOP_K),
            },
        }
    return out


# ---------------------------------------------------------------------------
# Block 5 — persistence
# ---------------------------------------------------------------------------

def _persistence(book: Book) -> dict[str, Any]:
    """Monthly sign counts and the longest negative run, per cell.

    A month counts as positive when its net R exceeds zero, and enters the
    tally only when it holds at least `MIN_SIDE_N` trades — the same floor used
    for a contrast side, reused rather than a second threshold invented for the
    calendar. Thin months are counted separately, not silently dropped.
    """
    boundaries = sorted(int(r.record.boundary) for r in book.scored)
    midpoint = boundaries[len(boundaries) // 2] if boundaries else 0

    out: dict[str, Any] = {
        "denominators": book.denominators,
        "population": "fill basis (Populations.scored)",
        "time_basis": TIME_BASIS,
        "month_usability_floor": MIN_SIDE_N,
        "half_split_boundary": midpoint,
        "per_cell": {},
    }
    for name, _mode, _direction in CELLS:
        rows = [r for r in book.scored if r.cell == name]
        by_month: dict[str, list[float]] = {}
        for row in rows:
            by_month.setdefault(_month_of(row), []).append(
                float(row.record.realized_r))

        ordered = sorted(by_month.items())
        usable = [(m, v) for m, v in ordered if len(v) >= MIN_SIDE_N]
        thin = [m for m, v in ordered if len(v) < MIN_SIDE_N]
        positives = sum(1 for _m, v in usable if sum(v) > 0)

        longest = current = 0
        for _m, values in usable:
            if sum(values) <= 0:
                current += 1
                longest = max(longest, current)
            else:
                current = 0

        halves = {}
        for label, predicate in (("half_1", lambda b: b <= midpoint),
                                 ("half_2", lambda b: b > midpoint)):
            values = [float(r.record.realized_r) for r in rows
                      if predicate(int(r.record.boundary))]
            win, loss = _gross(values)
            halves[label] = {"n": len(values), "net": win + loss,
                             "mean_r": _mean(values),
                             "below_min_side_n": len(values) < MIN_SIDE_N}

        out["per_cell"][name] = {
            "months_present": len(ordered),
            "months_usable": len(usable),
            "months_thin": thin,
            "months_positive": positives,
            "positive_month_share": (positives / len(usable)) if usable else None,
            "longest_negative_run_months": longest,
            "halves": halves,
        }
    return out


# ---------------------------------------------------------------------------
# Block 7 — friction (descriptive)
# ---------------------------------------------------------------------------

def _notional_per_r(row: Row) -> Optional[float]:
    """`entry_price / |entry_price - sl|` — notional carried per 1R of risk.

    Read from the planned levels rather than the fill, so it is defined for an
    unfilled decision too, and so it describes the geometry the strategy chose
    rather than the one it received.
    """
    entry, stop = row.record.entry_price, row.record.sl
    if entry is None or stop is None:
        return None
    entry, stop = float(entry), float(stop)
    if entry == 0.0 or entry == stop:
        return None
    return abs(entry) / abs(entry - stop)


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1,
                             max(0, int(q * (len(ordered) - 1))))])


def _friction(rows: Sequence[Row], denominators: dict[str, int],
              ) -> dict[str, Any]:
    """Geometry, and the R cost it implies across the declared fee band.

    Emitted as columns. **No threshold, no gate, no pass/fail.** A single hard
    economic bar is unjustified here on three counts: the fee tier is not in
    the bundle, one leg against a round trip is a factor of two, and a segment
    median compared with a mean is a mismatched comparison on a heavy-tailed
    quantity. The reader performs the comparison.
    """
    values = [v for v in (_notional_per_r(r) for r in rows) if v is not None]
    med = float(median(values)) if values else None
    implied = {}
    for basis_points in FEE_BAND_BP:
        for label, legs in LEG_CONVENTIONS:
            implied[f"{basis_points:g}bp_{label}"] = (
                None if med is None
                else med * (basis_points / 10_000.0) * legs)
    return {
        "denominators": dict(denominators),
        "n": len(values),
        "notional_per_r_median": med,
        "notional_per_r_p25": _quantile(values, 0.25),
        "notional_per_r_p75": _quantile(values, 0.75),
        "implied_friction_r": implied,
        "note": (
            "descriptive only. Computed from entry_price / |entry_price - sl| "
            "so it is defined for unfilled decisions. No threshold is derived "
            "from these figures anywhere in this module"),
    }


# ---------------------------------------------------------------------------
# Block 8 — robust excursions
# ---------------------------------------------------------------------------

def _tail_share(rows: Sequence[Row]) -> Optional[float]:
    """Share of rows whose risk unit falls below the ATR floor.

    Reported beside every R total because a vanishing risk unit inflates R
    without any market event behind it. Measured on the frozen run this runs
    2.19-3.93% of gross positive R against 2.58-2.85% of rows in CONTINUATION
    and 0.00% in both RETRACEMENT cells — proportional rather than amplified,
    which is why the ledger stays raw and carries this rate instead.
    """
    usable = 0
    below = 0
    for row in rows:
        atr = row.labels.get("atr_last")
        fill, stop = row.record.fill_price, row.record.sl
        if not atr or fill is None or stop is None:
            continue
        usable += 1
        if abs(float(fill) - float(stop)) / float(atr) < TAIL_ATR_FLOOR:
            below += 1
    return (below / usable) if usable else None


def _excursions(rows: Sequence[Row], denominators: dict[str, int],
                ) -> dict[str, Any]:
    """Trimmed means and quantiles. A bare mean is never emitted."""
    mfe = [float(r.record.mfe_r) for r in rows if r.record.mfe_r is not None]
    mae = [float(r.record.mae_r) for r in rows if r.record.mae_r is not None]
    return {
        "denominators": dict(denominators),
        "mfe_r": robust_summary(mfe),
        "mae_r": robust_summary(mae),
        "tail_contamination_share": _tail_share(rows),
        "note": (
            f"{int(TRIM * 100)}% trimmed mean and quantiles. No untrimmed mean "
            f"is emitted: the book-wide raw means are tail-contaminated"),
    }


# ---------------------------------------------------------------------------
# Block 6 — condition x cell
# ---------------------------------------------------------------------------

def _sign_vector(contrast: Contrast, book: Book) -> dict[str, Any]:
    """The sign of the mean-R difference in each of the six strata.

    Descriptive. There is no gate, no count, no pass/fail and no promotion
    anywhere downstream of this. The null annotation travels with it so that a
    reader cannot informally reconstruct the criterion that three review rounds
    removed.
    """
    if contrast.kind != "binary":
        return {
            "available": False,
            "reason": "MULTI_LEVEL_NO_BINARY_SIDE",
            "note": ("a banded contrast has no two sides, so no difference "
                     "exists to take the sign of"),
            "strata": None,
        }

    boundaries = sorted(int(r.record.boundary) for r in book.scored)
    midpoint = boundaries[len(boundaries) // 2] if boundaries else 0
    strata: list[tuple[str, Callable[[Row], bool]]] = [
        (name, (lambda row, cell=name: row.cell == cell))
        for name, _mode, _direction in CELLS
    ]
    strata.append(("half_1", lambda row: int(row.record.boundary) <= midpoint))
    strata.append(("half_2", lambda row: int(row.record.boundary) > midpoint))

    out: dict[str, Any] = {}
    for label, predicate in strata:
        present, absent = [], []
        for row in book.scored:
            if not predicate(row):
                continue
            side = contrast.extract(row)
            if side is None:
                continue
            (present if bool(side) else absent).append(
                float(row.record.realized_r))
        left, right = _mean(present), _mean(absent)
        if left is None or right is None:
            out[label] = {"sign": None, "n_true": len(present),
                          "n_false": len(absent),
                          "below_min_side_n": True}
            continue
        difference = left - right
        out[label] = {
            "sign": (0 if difference == 0 else (1 if difference > 0 else -1)),
            "difference_r": difference,
            "n_true": len(present),
            "n_false": len(absent),
            "below_min_side_n": (len(present) < MIN_SIDE_N
                                 or len(absent) < MIN_SIDE_N),
        }
    signs = [entry["sign"] for entry in out.values() if entry["sign"] is not None]
    return {
        "available": True,
        "strata": out,
        "strata_count": len(out),
        "all_signs_agree": bool(signs) and len(set(signs)) == 1,
        "null_agreement_rate": NULL_SIGN_AGREEMENT_RATE,
        "null_note": (
            f"six-of-six agreement occurs for about "
            f"{NULL_SIGN_AGREEMENT_RATE:.1%} of contrasts unrelated to R, so "
            f"roughly one of the {len(CONTRASTS)} contrasts is expected to show "
            f"it by chance. This is descriptive: nothing filters or ranks on it"),
    }


def _side_record(rows: Sequence[Row], side: str, book_win: float,
                 book_loss: float, denominators: dict[str, int],
                 cell_n: int) -> dict[str, Any]:
    """One side of a contrast within one cell."""
    values = [float(r.record.realized_r) for r in rows if r.filled]
    record = _gross_record(values, book_win, book_loss, denominators)
    markers = []
    if len(rows) < MIN_SIDE_N:
        markers.append("BELOW_MIN_SIDE_N")
    if cell_n and (len(rows) / cell_n) < IMBALANCE_THRESHOLD:
        markers.append("IMBALANCED")
    record.update({
        "side": side,
        "n_side": len(rows),
        "share_of_cell": (len(rows) / cell_n) if cell_n else None,
        "usability_markers": markers,
        "mean_r_fill_basis": _mean(values),
        "evidence_class": EVIDENCE_CLASS,
    })
    return record


def _conditions(book: Book) -> dict[str, Any]:
    """All contrasts in all cells. Alphabetical, complete, unordered by size.

    Nothing here is filtered or sorted by a metric. A contrast that looks flat
    is emitted exactly like one that does not, because the complete family
    being visible is the whole of the multiplicity protection this module
    relies on.
    """
    all_values = [float(r.record.realized_r) for r in book.scored]
    book_win, book_loss = _gross(all_values)

    rows_out: list[dict[str, Any]] = []
    for contrast in sorted(CONTRASTS, key=lambda c: c.name):
        signs = _sign_vector(contrast, book)
        for cell_name, _mode, _direction in CELLS:
            cell_rows = [r for r in book.resolved if r.cell == cell_name]
            grouped: dict[str, list[Row]] = {}
            for row in cell_rows:
                key = contrast.extract(row)
                if key is None:
                    continue
                if contrast.kind == "binary":
                    label = "true" if bool(key) else "false"
                else:
                    label = str(key)
                grouped.setdefault(label, []).append(row)

            usable_n = sum(len(v) for v in grouped.values())
            sides = []
            for label in sorted(grouped):
                member = grouped[label]
                side = _side_record(member, label, book_win, book_loss,
                                    book.denominators, usable_n)
                side["fill_selection"] = _fill_selection(
                    member, book.denominators)
                side["friction"] = _friction(member, book.denominators)
                side["excursions"] = _excursions(
                    [r for r in member if r.filled], book.denominators)
                if contrast.kind == "multi_level" and len(member) < MIN_SIDE_N:
                    side["usability_markers"] = sorted(
                        set(side["usability_markers"]) | {"THIN_BUCKET"})
                sides.append(side)

            warnings = list(contrast.warnings)
            if any("THIN_BUCKET" in s["usability_markers"] for s in sides):
                warnings.append("THIN_BUCKET_PRESENT")
            if any("IMBALANCED" in s["usability_markers"] for s in sides):
                warnings.append("IMBALANCED_PRESENT")
            if any("BELOW_MIN_SIDE_N" in s["usability_markers"] for s in sides):
                warnings.append("BELOW_MIN_SIDE_N_PRESENT")

            row_out = {
                "cell": cell_name,
                "contrast": contrast.name,
                "kind": contrast.kind,
                "provenance": contrast.provenance,
                "provenance_claims": PROVENANCE_CLAIMS[contrast.provenance],
                "evidence_class": EVIDENCE_CLASS,
                "n_cell_resolved": len(cell_rows),
                "n_usable": usable_n,
                "n_excluded_no_value": len(cell_rows) - usable_n,
                "sides": sides,
                "sign_vector": signs,
                "warnings": sorted(set(warnings)),
                "denominators": book.denominators,
            }
            if contrast.provenance == POST_HOC_DERIVED:
                row_out["restriction"] = PROVENANCE_CLAIMS[POST_HOC_DERIVED]
            _guard_row(row_out)
            rows_out.append(row_out)

    return {
        "denominators": book.denominators,
        "contrast_count": len(CONTRASTS),
        "cell_count": len(CELLS),
        "row_count": len(rows_out),
        "ordering": "alphabetical by (contrast, cell); never by any metric",
        "rows": rows_out,
    }


def _guard_row(row: dict[str, Any]) -> dict[str, Any]:
    """Refuse any row that is not scoped to exactly one frozen cell.

    This is the mechanism behind the cross-mode prohibition: a row that spanned
    two modes would have to carry a cell name outside the frozen four, and there
    is no code path that constructs one.
    """
    names = {cell for cell, _mode, _direction in CELLS}
    if row.get("cell") not in names:
        raise CensusError(
            f"row cell {row.get('cell')!r} is not one of the four frozen "
            f"single-mode cells; a row spanning two modes cannot be emitted")
    return row


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def analyse(run_dir: Path | str) -> dict[str, Any]:
    """The whole census as a plain dictionary. Pure: no RNG, no I/O writes."""
    book = load(run_dir)
    denominators = book.denominators

    per_cell_fill = {}
    per_cell_friction = {}
    per_cell_excursions = {}
    for name, _mode, _direction in CELLS:
        resolved_cell = [r for r in book.resolved if r.cell == name]
        per_cell_fill[name] = _fill_selection(resolved_cell, denominators)
        per_cell_friction[name] = _friction(resolved_cell, denominators)
        per_cell_excursions[name] = _excursions(
            [r for r in resolved_cell if r.filled], denominators)

    return {
        "census": "performance_census",
        "engine_version": CENSUS_ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "specification_hash": specification_hash(),
        "governance": {
            "evidence_class": EVIDENCE_CLASS,
            "answers": (
                "where realized R accumulated, where losses accumulated, how "
                "concentrated and persistent they were, and what was observed "
                "under nine pre-declared contrasts"),
            "does_not_answer": (
                "which strategy, filter, mode, or direction is preferable. The "
                "census makes no decision and emits no ranking, candidate, "
                "filtered index, pass/fail, p-value, or significance claim"),
            "scope": (
                "one in-sample bundle covering roughly 27% of the strategy's "
                "decisions; 82,954 shadow records lie outside every population "
                "here. No causal claim and no edge claim is available"),
            "prohibited_statements": list(PROHIBITED_STATEMENTS),
            "prohibited_words_in_output": list(PROHIBITED_WORDS),
            "prohibited_grouping_keys": sorted(PROHIBITED_GROUPING_KEYS),
            "provenance_claims": PROVENANCE_CLAIMS,
            "min_side_n": MIN_SIDE_N,
            "min_side_n_basis": (
                "declared independently of metrics MIN_SEGMENT_N (30); chosen "
                "so nominal 120 keeps effective sample at or above 30 under a "
                "measured variance inflation of about 3.97x"),
            "imbalance_threshold": IMBALANCE_THRESHOLD,
            "time_basis": TIME_BASIS,
            "fee_band_bp": list(FEE_BAND_BP),
            "leg_conventions": [label for label, _legs in LEG_CONVENTIONS],
            "sign_strata": _sign_stratum_names(),
            "null_sign_agreement_rate": NULL_SIGN_AGREEMENT_RATE,
            "no_gate_derived_from_friction": True,
            "registered_in_studies": False,
        },
        "population_accounting": _population_accounting(book),
        "fill_selection": {"denominators": denominators,
                           "per_cell": per_cell_fill},
        "gross_accounting": _gross_accounting(book),
        "concentration": _concentration(book),
        "persistence": _persistence(book),
        "conditions": _conditions(book),
        "friction": {"denominators": denominators,
                     "per_cell": per_cell_friction,
                     "no_gate": ("descriptive only; no threshold is derived "
                                 "from these figures")},
        "excursions": {"denominators": denominators,
                       "per_cell": per_cell_excursions},
        "exclusions": {
            "denominators": denominators,
            "closed_count": len(CLOSED),
            "closed": [{"name": name, "reason": reason, "note": note}
                       for name, reason, note in CLOSED],
        },
        "limitations": list(LIMITATIONS),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.{places}f}"


def _denominator_line(denominators: dict[str, int]) -> str:
    return (f"> Denominators: "
            f"{denominators['shadow_records_excluded']:,} shadow records "
            f"excluded · "
            f"{denominators['unfilled_decisions_excluded_from_fill_basis']:,} "
            f"unfilled decisions excluded from the fill basis.\n\n")


def render(payload: dict[str, Any]) -> str:
    """Markdown. Screened against the prohibited word list by test."""
    out: list[str] = ["# Performance census\n\n"]
    governance = payload["governance"]
    out.append(f"> **Evidence class: {payload['evidence_class']}.** "
               f"{governance['scope']}\n\n")
    out.append(f"> Answers: {governance['answers']}\n\n")
    out.append(f"> Does not answer: {governance['does_not_answer']}\n\n")
    out.append(f"> Specification hash `{payload['specification_hash']}` · "
               f"MIN_SIDE_N {governance['min_side_n']} · time basis "
               f"`{governance['time_basis']}` · fee band "
               f"{governance['fee_band_bp']} bp × "
               f"{governance['leg_conventions']}\n\n")

    accounting = payload["population_accounting"]
    out.append("## 1 · Population accounting\n\n")
    out.append(_denominator_line(accounting["denominators"]))
    out.append("| Cell | resolved decisions | scored trades | unfilled | "
               "fill rate |\n|---|---:|---:|---:|---:|\n")
    for cell, block in accounting["per_cell"].items():
        out.append(f"| `{cell}` | {block['resolved_decisions']:,} | "
                   f"{block['scored_trades']:,} | "
                   f"{block['unfilled_decisions']:,} | "
                   f"{_num(block['fill_rate'])} |\n")
    out.append("\n")

    out.append("## 2 · Fill selection\n\n")
    out.append(_denominator_line(payload["fill_selection"]["denominators"]))
    out.append("| Cell | fill rate | fill basis mean | decision basis mean | "
               "shrinkage | bracket width |\n|---|---:|---:|---:|---:|---:|\n")
    for cell, block in payload["fill_selection"]["per_cell"].items():
        out.append(f"| `{cell}` | {_num(block['fill_rate'])} | "
                   f"{_num(block['fill_basis_mean_r'], 5)} | "
                   f"{_num(block['decision_basis_mean_r'], 5)} | "
                   f"{_num(block['decision_basis_shrinkage_factor'])} | "
                   f"{_num(block['bracket_width_r'], 3)} |\n")
    out.append(f"\n> {payload['fill_selection']['per_cell'][CELLS[0][0]]['shrinkage_note']}.\n\n")

    gross = payload["gross_accounting"]
    out.append("## 3 · Gross accounting\n\n")
    out.append(_denominator_line(gross["denominators"]))
    out.append(f"> Population: {gross['population']}. "
               f"{gross['book']['win_loss_note']}.\n\n")
    out.append("| Cell | n | net R | win contribution | loss contribution | "
               "share of gross+ | share of gross− |\n"
               "|---|---:|---:|---:|---:|---:|---:|\n")
    for cell, block in gross["per_cell"].items():
        out.append(f"| `{cell}` | {block['n']:,} | {_num(block['net'], 1)} | "
                   f"{_num(block['win_contribution'], 1)} | "
                   f"{_num(block['loss_contribution'], 1)} | "
                   f"{_num(block['share_of_gross_positive'])} | "
                   f"{_num(block['share_of_gross_negative'])} |\n")
    out.append("\n")

    out.append("## 4 · Concentration\n\n")
    out.append(_denominator_line(payload["concentration"]["denominators"]))
    out.append("| Cell | symbols | months | top-1 symbol share of gross+ | "
               "top-5 | HHI+ | top-1 symbol share of gross− | HHI− |\n"
               "|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for cell, block in payload["concentration"]["per_cell"].items():
        pos, neg = block["positive"]["by_symbol"], block["negative"]["by_symbol"]
        out.append(f"| `{cell}` | {block['symbols_present']} | "
                   f"{block['months_present']} | "
                   f"{_num(pos['top']['top_1']['share'])} | "
                   f"{_num(pos['top']['top_5']['share'])} | "
                   f"{_num(pos['herfindahl'])} | "
                   f"{_num(neg['top']['top_1']['share'])} | "
                   f"{_num(neg['herfindahl'])} |\n")
    out.append("\n")

    out.append("## 5 · Persistence\n\n")
    out.append(_denominator_line(payload["persistence"]["denominators"]))
    out.append("| Cell | months usable | positive | share | longest negative "
               "run | half 1 net | half 2 net |\n"
               "|---|---:|---:|---:|---:|---:|---:|\n")
    for cell, block in payload["persistence"]["per_cell"].items():
        out.append(f"| `{cell}` | {block['months_usable']} | "
                   f"{block['months_positive']} | "
                   f"{_num(block['positive_month_share'])} | "
                   f"{block['longest_negative_run_months']} | "
                   f"{_num(block['halves']['half_1']['net'], 1)} | "
                   f"{_num(block['halves']['half_2']['net'], 1)} |\n")
    out.append("\n")

    conditions = payload["conditions"]
    out.append("## 6 · Conditions by cell\n\n")
    out.append(_denominator_line(conditions["denominators"]))
    out.append(f"> {conditions['row_count']} rows — "
               f"{conditions['contrast_count']} contrasts × "
               f"{conditions['cell_count']} cells, complete. "
               f"Ordering: {conditions['ordering']}.\n\n")
    for row in conditions["rows"]:
        out.append(f"### `{row['contrast']}` · `{row['cell']}`\n\n")
        out.append(f"*{row['provenance']}* · evidence class "
                   f"{row['evidence_class']} · n usable {row['n_usable']:,} "
                   f"of {row['n_cell_resolved']:,}")
        if row["warnings"]:
            out.append(" · warnings: " + ", ".join(f"`{w}`" for w in row["warnings"]))
        out.append("\n\n")
        out.append(_denominator_line(row["denominators"]))
        if row["provenance"] == POST_HOC_DERIVED:
            out.append(f"> Restriction: {row['restriction']}\n\n")
        out.append("| Side | n | fill rate | mean R | median R | net R | "
                   "share of gross+ | 5bp round trip | markers |\n"
                   "|---|---:|---:|---:|---:|---:|---:|---:|---|\n")
        for side in row["sides"]:
            friction = side["friction"]["implied_friction_r"].get(
                "5bp_round_trip")
            markers = ", ".join(f"`{m}`" for m in side["usability_markers"]) or "—"
            out.append(f"| `{side['side']}` | {side['n_side']:,} | "
                       f"{_num(side['fill_selection']['fill_rate'])} | "
                       f"{_num(side['mean_r_fill_basis'], 5)} | "
                       f"{_num(side['median_r'])} | {_num(side['net'], 1)} | "
                       f"{_num(side['share_of_gross_positive'])} | "
                       f"{_num(friction)} | {markers} |\n")
        signs = row["sign_vector"]
        if signs["available"]:
            vector = " ".join(
                ("+" if entry["sign"] == 1 else
                 "−" if entry["sign"] == -1 else "·")
                for entry in signs["strata"].values())
            out.append(f"\nSign vector ({signs['strata_count']} strata): "
                       f"`{vector}` · all agree: "
                       f"{_num(signs['all_signs_agree'])}\n\n")
            out.append(f"> {signs['null_note']}.\n\n")
        else:
            out.append(f"\nSign vector: unavailable — "
                       f"`{signs['reason']}` ({signs['note']}).\n\n")

    out.append("## 7 · Friction\n\n")
    out.append(_denominator_line(payload["friction"]["denominators"]))
    out.append(f"> {payload['friction']['no_gate']}.\n\n")
    columns = [f"{bp:g}bp_{label}" for bp in FEE_BAND_BP
               for label, _legs in LEG_CONVENTIONS]
    out.append("| Cell | notional/R p25 | median | p75 | "
               + " | ".join(columns) + " |\n")
    out.append("|---|" + "---:|" * (3 + len(columns)) + "\n")
    for cell, block in payload["friction"]["per_cell"].items():
        cells = " | ".join(_num(block["implied_friction_r"][c]) for c in columns)
        out.append(f"| `{cell}` | {_num(block['notional_per_r_p25'], 1)} | "
                   f"{_num(block['notional_per_r_median'], 1)} | "
                   f"{_num(block['notional_per_r_p75'], 1)} | {cells} |\n")
    out.append("\n")

    out.append("## 8 · Robust excursions\n\n")
    out.append(_denominator_line(payload["excursions"]["denominators"]))
    out.append("| Cell | MFE trimmed | MFE median | MFE p90 | MAE trimmed | "
               "MAE median | tail share |\n|---|---:|---:|---:|---:|---:|---:|\n")
    for cell, block in payload["excursions"]["per_cell"].items():
        mfe, mae = block["mfe_r"], block["mae_r"]
        out.append(f"| `{cell}` | {_num(mfe['trimmed_mean_10pct'])} | "
                   f"{_num(mfe['median'])} | {_num(mfe['p90'])} | "
                   f"{_num(mae['trimmed_mean_10pct'])} | "
                   f"{_num(mae['median'])} | "
                   f"{_num(block['tail_contamination_share'])} |\n")
    out.append("\n")

    out.append("## 9 · Exclusions, limitations, prohibited statements\n\n")
    out.append(_denominator_line(payload["exclusions"]["denominators"]))
    out.append("| Excluded | reason | note |\n|---|---|---|\n")
    for entry in payload["exclusions"]["closed"]:
        out.append(f"| `{entry['name']}` | `{entry['reason']}` | "
                   f"{entry['note']} |\n")
    out.append("\n### Provenance classes\n\n")
    for name, claim in sorted(governance["provenance_claims"].items()):
        out.append(f"- **{name}** — {claim}\n")
    out.append("\n### Prohibited statements\n\n")
    for statement in payload["governance"]["prohibited_statements"]:
        out.append(f"- {statement}\n")
    out.append("\n### Irreducible limitations\n\n")
    for limitation in payload["limitations"]:
        out.append(f"- {limitation}\n")
    return "".join(out)


def csv_rows(payload: dict[str, Any]) -> list[list[str]]:
    """One row per (cell, contrast, side)."""
    header = ["cell", "contrast", "kind", "provenance", "side", "n_side",
              "share_of_cell", "fill_rate", "mean_r_fill_basis",
              "decision_basis_mean_r", "decision_basis_shrinkage_factor",
              "median_r", "net", "win_contribution", "loss_contribution",
              "share_of_gross_positive", "share_of_gross_negative",
              "notional_per_r_median", "friction_5bp_round_trip",
              "mfe_trimmed_mean_10pct", "mae_trimmed_mean_10pct",
              "tail_contamination_share", "usability_markers", "warnings",
              "all_signs_agree", "evidence_class",
              "shadow_records_excluded", "unfilled_decisions_excluded"]
    rows = [header]
    for row in payload["conditions"]["rows"]:
        signs = row["sign_vector"]
        for side in row["sides"]:
            fill = side["fill_selection"]
            rows.append([
                row["cell"], row["contrast"], row["kind"], row["provenance"],
                side["side"], str(side["n_side"]),
                _num(side["share_of_cell"]), _num(fill["fill_rate"]),
                _num(side["mean_r_fill_basis"], 6),
                _num(fill["decision_basis_mean_r"], 6),
                _num(fill["decision_basis_shrinkage_factor"]),
                _num(side["median_r"]), _num(side["net"], 3),
                _num(side["win_contribution"], 3),
                _num(side["loss_contribution"], 3),
                _num(side["share_of_gross_positive"]),
                _num(side["share_of_gross_negative"]),
                _num(side["friction"]["notional_per_r_median"], 2),
                _num(side["friction"]["implied_friction_r"]["5bp_round_trip"]),
                _num(side["excursions"]["mfe_r"]["trimmed_mean_10pct"]),
                _num(side["excursions"]["mae_r"]["trimmed_mean_10pct"]),
                _num(side["excursions"]["tail_contamination_share"]),
                ";".join(side["usability_markers"]),
                ";".join(row["warnings"]),
                _num(signs.get("all_signs_agree")) if signs["available"] else "—",
                side["evidence_class"],
                str(row["denominators"]["shadow_records_excluded"]),
                str(row["denominators"]
                    ["unfilled_decisions_excluded_from_fill_basis"]),
            ])
    return rows


def write_census(payload: dict[str, Any], out_dir: Path | str,
                 *, identity: Optional[dict[str, Any]] = None,
                 ) -> dict[str, Path]:
    """Emit the three artifacts into the caller's directory."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["identity"] = dict(identity or {})
    body["generated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    written: dict[str, Path] = {}
    json_path = out / "performance_census.json"
    json_path.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written["performance_census.json"] = json_path

    md_path = out / "performance_census.md"
    md_path.write_text(render(payload), encoding="utf-8")
    written["performance_census.md"] = md_path

    csv_path = out / "performance_census.csv"
    csv_path.write_text(
        "\n".join(",".join(_csv_escape(c) for c in row)
                  for row in csv_rows(payload)) + "\n", encoding="utf-8")
    written["performance_census.csv"] = csv_path
    return written


def _csv_escape(value: str) -> str:
    text = str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Explicit entry point. Deliberately not a CLI verb."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="performance_census",
        description="Descriptive census of a finished replay bundle.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    payload = analyse(run_dir)
    out_dir = (Path(args.out) if args.out
               else run_dir / "analytics" / "research" / CENSUS_DIRNAME)
    written = write_census(payload, out_dir, identity={"run_dir": str(run_dir)})
    for name, path in sorted(written.items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(main())
