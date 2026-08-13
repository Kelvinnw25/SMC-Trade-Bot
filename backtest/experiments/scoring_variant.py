"""Turn a variant's `strategy_inputs` into a `ScoringConfig`.

The seam itself takes a `ScoringConfig` as a plain argument; this module is the
one place that builds one from a file on disk. It lives in `backtest` rather than
in `v2` for two reasons: the analysis layer performs no I/O, and `v2` must not
import `backtest` (rule A8) — the dependency points this way.

## What it will and will not load

`strategy_inputs.scoring` is the only recognised block, and inside it only
`weight_overrides`, `thresholds` and `gates`. Anything else is rejected by name.
Rejection is the point: a mistyped key that were silently ignored would produce a
variant that ran as the control and reported a null result, which is the most
expensive failure mode this programme has.

Two blocks are recognised by the schema but **not yet implementable**, and they
raise `UnsupportedScoringInput` rather than being quietly dropped:

- `gates` (e.g. `require_fvg`) — the seam parameterises literals only; a gate is a
  behaviour change and is explicitly out of scope for this phase.
- `thresholds` declared as `{"mode": "quantile_anchored", ...}` — the anchor is an
  offline derivation whose *output* is a set of integers. Until those integers are
  derived and written into the config, the variant is not runnable. Explicit
  integer thresholds keyed by entry mode load fine.

So of the `scoring_v2` family, `s0-control` and `s1-dead-weights-zero` load today;
the rest name work that has not been done yet and say so when you ask for them.

## Canonical JSON, no YAML

Configs are read through `backtest.experiments.config`, which parses canonical
JSON and re-derives `variant_id` from the content rather than trusting the file's
own declaration. Adding a YAML parser would mean editing `requirements.txt`, which
its own header defines as a re-baselining event that makes prior baselines
incomparable — the baseline these experiments are measured against.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backtest.experiments.config import (
    ExperimentConfig,
    ExperimentConfigError,
    load_config,
    load_experiment,
)
from v2.analysis.scoring_config import (
    DEFAULT_SCORING_CONFIG,
    ScoringConfig,
    ScoringConfigError,
)

__all__ = [
    "SCORING_V2_ROOT",
    "SUPPORTED_SCORING_KEYS",
    "UnsupportedScoringInput",
    "scoring_config_from_inputs",
    "load_scoring_variant",
]

#: Where the scoring_v2 family lives. A default, not a restriction — the loader
#: takes a root so a test can point it at a temporary directory.
SCORING_V2_ROOT = Path("experiments/v2_core_rebuild/scoring_v2")

SUPPORTED_SCORING_KEYS = frozenset({"weight_overrides", "thresholds", "gates"})


class UnsupportedScoringInput(ExperimentConfigError):
    """The config is well-formed but names behaviour the seam does not implement.

    Distinct from `ExperimentConfigError` for a typo, and deliberately not a
    silent no-op: "this variant declares a gate and the seam has no gates" is a
    fact the caller needs, not a detail to swallow.
    """


def _reject_unknown(block: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = set(block) - allowed
    if unknown:
        raise ExperimentConfigError(
            f"{where} has unknown keys {sorted(unknown)}; recognised keys are "
            f"{sorted(allowed)}")


def _thresholds(spec: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    """Explicit integer thresholds, or a refusal explaining what is missing."""
    mode = spec.get("mode")
    if mode is not None:
        raise UnsupportedScoringInput(
            f"thresholds declares mode {mode!r}. A derived anchor is an offline "
            f"computation whose OUTPUT is a set of integers; write those integers "
            f"into the config (thresholds: {{'CONTINUATION': {{'watch': 8, ...}}}}) "
            f"and the variant becomes runnable. The seam does not derive "
            f"thresholds at runtime by design — a variant must be a fixed, "
            f"hashable object.")
    return {k: v for k, v in spec.items()}


def scoring_config_from_inputs(
    strategy_inputs: Mapping[str, Any],
    *,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> ScoringConfig:
    """Apply a variant's `strategy_inputs` to `base`.

    Empty inputs return `base` unchanged — that is what makes the control config
    (`strategy_inputs: {}`) resolve to exactly 2.2.0 behaviour rather than to a
    reconstruction of it.
    """
    _reject_unknown(strategy_inputs, frozenset({"scoring"}), "strategy_inputs")

    scoring = strategy_inputs.get("scoring")
    if scoring is None:
        return base
    if not isinstance(scoring, Mapping):
        raise ExperimentConfigError(
            f"strategy_inputs.scoring must be a JSON object, got "
            f"{type(scoring).__name__}")
    _reject_unknown(scoring, SUPPORTED_SCORING_KEYS, "strategy_inputs.scoring")

    gates = scoring.get("gates")
    if gates:
        raise UnsupportedScoringInput(
            f"strategy_inputs.scoring.gates {dict(gates)!r} is not implemented. "
            f"The seam parameterises the score engine's literals; a gate changes "
            f"which setups are scored at all, which is a behaviour change and is "
            f"out of scope for this phase.")

    weight_overrides = scoring.get("weight_overrides")
    thresholds = scoring.get("thresholds")
    if not weight_overrides and not thresholds:
        # `base` itself, not an equal rebuild. A control that merely *equalled*
        # the baseline would be a second definition of baseline behaviour, and
        # the point of the control is that there is only one.
        return base

    return base.with_overrides(
        weight_overrides=weight_overrides,
        thresholds=_thresholds(thresholds) if thresholds else None,
    )


def load_scoring_variant(
    slug: str,
    *,
    root: Path | str = SCORING_V2_ROOT,
    base: ScoringConfig = DEFAULT_SCORING_CONFIG,
    require_preregistration: bool = False,
) -> tuple[ExperimentConfig, ScoringConfig]:
    """Load `<root>/<slug>/config.json` and resolve it to a `ScoringConfig`.

    Returns the experiment config alongside the scoring config, because the
    caller needs the `variant_id` to stamp on whatever the variant produces —
    a resolved `ScoringConfig` on its own has no identity.
    """
    if require_preregistration:
        config, prereg = load_experiment(root, slug)
        if prereg is None:
            raise ExperimentConfigError(
                f"{slug} has no preregistration. Results may not be generated "
                f"before a prediction exists.")
    else:
        config = load_config(Path(root) / slug / "config.json")

    try:
        resolved = scoring_config_from_inputs(config.strategy_inputs, base=base)
    except UnsupportedScoringInput as exc:
        raise UnsupportedScoringInput(f"{slug}: {exc}") from exc
    except ScoringConfigError as exc:
        # Re-raised with the variant named: "unknown component 'sweeep'" is much
        # less useful than knowing which config on disk contains it.
        raise ExperimentConfigError(f"{slug}: {exc}") from exc
    return config, resolved