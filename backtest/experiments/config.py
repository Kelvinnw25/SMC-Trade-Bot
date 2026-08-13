"""Experiment identity — what a variant is, and what it predicted before it ran.

Phase 3.5B. Two objects, each with one job:

- `ExperimentConfig` — the behavioral definition. Its `variant_id` is a content
  hash of the strategy inputs *only*, so renaming a title or fixing a typo in a
  rationale cannot mint a new variant, and changing a threshold always does.
- `Preregistration` — what was predicted, written before results exist.

## Why the hash covers so little

`variant_id = sha256(canonical_json(strategy_inputs))[:16]`

`strategy_inputs` is the sole hashed member, and it holds nothing but values that
change behavior. Everything else on the config — title, rationale, author, dates,
free text — is metadata and is deliberately outside the hash. The reason is that
the id has to be usable as an *equality test on behavior*: two configs with the
same id must produce the same run, or the id is worse than useless. That property
also makes the control's id a constant (`CONTROL_VARIANT_ID`, the hash of `{}`),
which is what lets a control run be recognised rather than declared.

The version string is where the human-readable identity lives:

    2.2.0+exp.<slug>.<variant_id[:8]>

It carries the base version deliberately. A future 2.3.0 rebase must invalidate
every 2.2.0 variant id loudly rather than let an old override ride along on new
code, and embedding the base is what makes that visible in one glance at a
`strategy_version` field. The form follows the existing `STRATEGY_VERSION`
convention in `v2.app.ledger` and stays PEP 440 local-version-shaped so it sorts
and parses like a version, not like a comment.

## Why preregistration is a separate file with its own digest

31,209 scored trades across 54 registered segment dimensions will always yield
something significant. The only defence is a prediction made before looking, and
the only way to keep that honest without a trusted clock is to hash the
prediction and carry the digest into the result. An edited prediction then fails
to match its own recorded digest — the same mechanism `research.holdout` uses for
its lock, and for the same reason.

Nothing here validates that a prediction was *good*. It records what was claimed.

## No YAML

`requirements.txt` is the C-19 determinism contract, and its own header states
that changing it is a re-baselining event that makes prior baselines
incomparable. Adding a YAML parser to read experiment configs would therefore
invalidate the baseline the experiments are measured against. Configs are
canonical JSON, hashed through the same `canonical_json` every other identity in
this repository uses.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from backtest.data.repository import canonical_json

__all__ = [
    "CONFIG_FILENAME",
    "CONFIG_SCHEMA_VERSION",
    "CONTROL_VARIANT_ID",
    "EVALUATION_TIERS",
    "ExperimentConfig",
    "ExperimentConfigError",
    "PREREGISTRATION_FILENAME",
    "PREDICTED_DIRECTIONS",
    "Preregistration",
    "load_config",
    "load_preregistration",
    "variant_id",
    "variant_version",
]

CONFIG_SCHEMA_VERSION = 1

CONFIG_FILENAME = "config.json"
PREREGISTRATION_FILENAME = "preregistration.json"

#: Which evaluation tier a result is allowed to claim. The tiers differ in what
#: they hold fixed, so a result that does not say which one produced it cannot be
#: interpreted: tier0 freezes scoring and re-decides admission over already
#: resolved outcomes; tier1 freezes selection and re-resolves exits; tier2 is a
#: full replay and is the only tier that holds nothing fixed.
EVALUATION_TIERS = ("tier0", "tier1", "tier2")

#: A prediction must commit to a sign. "none" is a real prediction (the
#: null), not a refusal to make one.
PREDICTED_DIRECTIONS = ("increase", "decrease", "none")


class ExperimentConfigError(Exception):
    """A config or preregistration is not usable as an experiment definition."""


def variant_id(strategy_inputs: Mapping[str, Any]) -> str:
    """`sha256(canonical_json(strategy_inputs))[:16]` — behavior identity only."""
    try:
        payload = canonical_json(dict(strategy_inputs))
    except (TypeError, ValueError) as exc:
        raise ExperimentConfigError(
            f"strategy_inputs is not canonicalisable, so it cannot be hashed "
            f"into a variant identity: {exc}") from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


#: The identity of "no override". A control run carries this id by construction,
#: which is what makes a control recognisable rather than merely labelled.
CONTROL_VARIANT_ID = variant_id({})


def variant_version(base: str, slug: str, vid: str) -> str:
    """`<base>+exp.<slug>.<vid[:8]>` — the string a run stamps as its version."""
    return f"{base}+exp.{slug}.{vid[:8]}"


def _require_str(data: Mapping[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExperimentConfigError(
            f"{where}: {key!r} must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True)
class ExperimentConfig:
    """The behavioral definition of one variant.

    `strategy_inputs` is the only hashed member. `metadata` exists so that the
    human-facing text has a home that provably cannot move the variant id.
    """

    slug: str
    base_strategy_version: str
    strategy_inputs: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.slug or not self.slug.strip():
            raise ExperimentConfigError("slug must be a non-empty string")
        if any(ch in self.slug for ch in "+ /\\.@"):
            # The slug lands inside a version string and inside a path, so the
            # separators of both are refused rather than escaped later.
            raise ExperimentConfigError(
                f"slug {self.slug!r} may not contain '+', '.', '@', whitespace "
                f"or a path separator — it is embedded in both the variant "
                f"version and the results path")
        if not self.base_strategy_version.strip():
            raise ExperimentConfigError("base_strategy_version must be set")
        variant_id(self.strategy_inputs)  # fail now, not at write time

    @property
    def variant_id(self) -> str:
        return variant_id(self.strategy_inputs)

    @property
    def variant_version(self) -> str:
        return variant_version(
            self.base_strategy_version, self.slug, self.variant_id)

    @property
    def is_control(self) -> bool:
        """True when nothing is overridden, whatever the metadata says."""
        return self.variant_id == CONTROL_VARIANT_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "slug": self.slug,
            "base_strategy_version": self.base_strategy_version,
            # Hashed. Keep this key last-but-one so a diff of two configs puts
            # the behavioral change where the eye lands.
            "strategy_inputs": dict(self.strategy_inputs),
            "metadata": dict(self.metadata),
            # Derived, written for readability; recomputed on load and never
            # trusted from the file.
            "variant_id": self.variant_id,
            "variant_version": self.variant_version,
        }

    def write(self, path: Path | str, *, overwrite: bool = False) -> Path:
        target = Path(path)
        if target.exists() and not overwrite:
            raise ExperimentConfigError(
                f"{target} exists; pass overwrite=True to replace a config "
                f"deliberately (an edited config is a different variant)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(canonical_json(self.to_dict()) + "\n", encoding="utf-8")
        return target

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentConfig":
        inputs = data.get("strategy_inputs", {})
        if not isinstance(inputs, dict):
            raise ExperimentConfigError(
                f"strategy_inputs must be a JSON object, got {type(inputs).__name__}")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ExperimentConfigError(
                f"metadata must be a JSON object, got {type(metadata).__name__}")
        config = cls(
            slug=_require_str(data, "slug", "config"),
            base_strategy_version=_require_str(
                data, "base_strategy_version", "config"),
            strategy_inputs=inputs,
            metadata=metadata,
            schema_version=int(data.get("schema_version", CONFIG_SCHEMA_VERSION)),
        )
        # A stale derived field means the file was hand-edited without rehashing,
        # which is exactly the drift the id exists to catch.
        declared = data.get("variant_id")
        if declared is not None and str(declared) != config.variant_id:
            raise ExperimentConfigError(
                f"config declares variant_id {declared!r} but its "
                f"strategy_inputs hash to {config.variant_id!r}. The file was "
                f"edited without rehashing; the inputs are authoritative.")
        return config


def load_config(path: Path | str) -> ExperimentConfig:
    target = Path(path)
    if not target.exists():
        raise ExperimentConfigError(f"no experiment config at {target}")
    return ExperimentConfig.from_dict(
        json.loads(target.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Preregistration:
    """What was predicted, and how the decision will be made.

    Every field is required. An optional prediction is not a prediction, and the
    fields that look like ceremony are the ones that stop a result from being
    reinterpreted after the fact: `primary_metric` prevents metric-shopping,
    `decision_rule` prevents threshold-shopping, and `evaluation_tier` prevents a
    tier-0 screen from being quoted as a replay result.
    """

    hypothesis: str
    predicted_direction: str
    predicted_effect_r: float
    primary_metric: str
    evaluation_tier: str
    decision_rule: str
    control_run_id: str
    comparison_key_digest: str
    registered_at: str
    schema_version: int = CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.predicted_direction not in PREDICTED_DIRECTIONS:
            raise ExperimentConfigError(
                f"predicted_direction must be one of {PREDICTED_DIRECTIONS}, "
                f"got {self.predicted_direction!r}")
        if self.evaluation_tier not in EVALUATION_TIERS:
            raise ExperimentConfigError(
                f"evaluation_tier must be one of {EVALUATION_TIERS}, got "
                f"{self.evaluation_tier!r}")
        for name in ("hypothesis", "primary_metric", "decision_rule",
                     "control_run_id", "comparison_key_digest", "registered_at"):
            if not str(getattr(self, name)).strip():
                raise ExperimentConfigError(f"{name} must be set")
        try:
            float(self.predicted_effect_r)
        except (TypeError, ValueError) as exc:
            raise ExperimentConfigError(
                f"predicted_effect_r must be a number, got "
                f"{self.predicted_effect_r!r}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hypothesis": self.hypothesis,
            "predicted_direction": self.predicted_direction,
            "predicted_effect_r": float(self.predicted_effect_r),
            "primary_metric": self.primary_metric,
            "evaluation_tier": self.evaluation_tier,
            "decision_rule": self.decision_rule,
            "control_run_id": self.control_run_id,
            "comparison_key_digest": self.comparison_key_digest,
            "registered_at": self.registered_at,
        }

    @property
    def digest(self) -> str:
        """Content hash, carried into every result built from this prediction."""
        return hashlib.sha256(
            canonical_json(self.to_dict()).encode("utf-8")).hexdigest()[:16]

    def write(self, path: Path | str, *, overwrite: bool = False) -> Path:
        target = Path(path)
        if target.exists() and not overwrite:
            raise ExperimentConfigError(
                f"{target} exists. A preregistration is written once, before "
                f"results — rewriting it after the fact is the failure it exists "
                f"to prevent. Pass overwrite=True only to correct a file that has "
                f"produced no results.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(canonical_json(self.to_dict()) + "\n", encoding="utf-8")
        return target

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Preregistration":
        try:
            effect = float(data["predicted_effect_r"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExperimentConfigError(
                f"predicted_effect_r is required and must be a number: {exc}"
            ) from exc
        return cls(
            hypothesis=_require_str(data, "hypothesis", "preregistration"),
            predicted_direction=str(data.get("predicted_direction")),
            predicted_effect_r=effect,
            primary_metric=_require_str(data, "primary_metric", "preregistration"),
            evaluation_tier=str(data.get("evaluation_tier")),
            decision_rule=_require_str(data, "decision_rule", "preregistration"),
            control_run_id=_require_str(data, "control_run_id", "preregistration"),
            comparison_key_digest=_require_str(
                data, "comparison_key_digest", "preregistration"),
            registered_at=_require_str(data, "registered_at", "preregistration"),
            schema_version=int(data.get("schema_version", CONFIG_SCHEMA_VERSION)),
        )


def load_preregistration(path: Path | str) -> Preregistration:
    target = Path(path)
    if not target.exists():
        raise ExperimentConfigError(
            f"no preregistration at {target}. Results may not be generated "
            f"before a prediction exists — that ordering is the whole point.")
    return Preregistration.from_dict(
        json.loads(target.read_text(encoding="utf-8")))


def experiment_dir(root: Path | str, slug: str) -> Path:
    """`<root>/<slug>/` — one directory per experiment.

    The inputs of one experiment (config, preregistration, prose) live together
    rather than being scattered across parallel `configs/` and `hypotheses/`
    trees joined by filename convention: a per-experiment directory cannot
    develop a half-renamed pair, and it reviews as one unit.
    """
    return Path(root) / slug


def load_experiment(
    root: Path | str, slug: str
) -> tuple[ExperimentConfig, Optional[Preregistration]]:
    """Load a config and its preregistration if one has been written yet.

    The preregistration is optional *here* and required at result-writing time:
    a config may legitimately exist while its prediction is still being drafted,
    but a result may not.
    """
    directory = experiment_dir(root, slug)
    config = load_config(directory / CONFIG_FILENAME)
    prereg_path = directory / PREREGISTRATION_FILENAME
    prereg = load_preregistration(prereg_path) if prereg_path.exists() else None
    return config, prereg
