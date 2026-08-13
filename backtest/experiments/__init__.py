"""Experiment infrastructure — Phase 3.5 foundations.

This package is **reader-only**, in the same sense as `backtest.audit` and
`backtest.analytics`: it opens a finished run bundle, computes, and writes only
where it is told to. It never replays, never resolves outcomes, and never mutates
a bundle.

Three concerns, three modules:

| module | question it answers |
|---|---|
| `identity` | does the installed code still reproduce the frozen baseline's decisions, row for row? |
| `config` | what exactly is this experiment, and what did it predict before it ran? |
| `tier0` | what would an admission rule have done, given what the baseline already resolved? |
| `scoring_variant` | what `ScoringConfig` does this variant's `strategy_inputs` resolve to? |
| `anchor` | which integer thresholds keep this variant's admitted fraction at the baseline's? |

## Why `identity` exists at all

Phase 3.5 exists to make later experiments cheap. The precondition for a cheap
experiment is an *exact* reference: if the control drifts, every delta measured
against it is noise plus drift, and there is no way afterwards to tell which.

The obvious reference check is "replay again and diff", which costs ~7.8 h
(`manifest.wall_seconds` = 28148 on the official run). `identity` gets the same
guarantee for the scoring layer in a couple of minutes by replaying **the scoring
function over the recorded ledger** instead of replaying the market: every input
`score_signal` reads is a recorded ledger field, so the recorded `score` /
`decision` / `notes` / `rr` are a 886,954-row golden file that already exists.

That is a narrower claim than a full replay and the module says so out loud —
see `identity.SCOPE`.

## Boundary

Nothing here may import `backtest.replay`. `identity` and `scoring_variant` do
import `v2.analysis` — the first re-runs the scoring function, the second builds
the config that function takes — and those are the deliberate exceptions;
`config` and `tier0` import no strategy code at all. The dependency only ever
points this way: `v2` never imports `backtest` (rule A8), which is why the loader
for an experiment config lives here and not next to the seam.
"""
from __future__ import annotations

from backtest.experiments.anchor import (
    AnchorReport,
    ModeAnchor,
    derive_thresholds,
    pick_threshold,
)
from backtest.experiments.config import (
    CONFIG_SCHEMA_VERSION,
    ExperimentConfig,
    ExperimentConfigError,
    Preregistration,
    load_config,
    variant_id,
    variant_version,
)
from backtest.experiments.identity import (
    SCOPE,
    BranchCensus,
    IdentityReport,
    RowVerdict,
    rescore_ledger,
)
from backtest.experiments.scoring_variant import (
    SCORING_V2_ROOT,
    UnsupportedScoringInput,
    load_scoring_variant,
    scoring_config_from_inputs,
)
from backtest.experiments.tier0 import (
    AdmissionRule,
    SLOT_LIMIT,
    Tier0Result,
    simulate,
)

__all__ = [
    "AdmissionRule",
    "AnchorReport",
    "BranchCensus",
    "CONFIG_SCHEMA_VERSION",
    "ExperimentConfig",
    "ExperimentConfigError",
    "IdentityReport",
    "ModeAnchor",
    "Preregistration",
    "RowVerdict",
    "SCOPE",
    "SCORING_V2_ROOT",
    "SLOT_LIMIT",
    "Tier0Result",
    "UnsupportedScoringInput",
    "derive_thresholds",
    "load_config",
    "load_scoring_variant",
    "pick_threshold",
    "rescore_ledger",
    "scoring_config_from_inputs",
    "simulate",
    "variant_id",
    "variant_version",
]
