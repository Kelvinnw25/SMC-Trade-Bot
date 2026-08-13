"""Replay configuration and run identity.

Everything that decides *what* a replay run is, in one immutable object, plus the
deterministic `run_id` derived from it (C-16.2).

## Why the run_id is a hash

`run_id = "replay-" + sha256(canonical_json(core))[:16]`, where `core` names every
input that can change a result: the contract, the strategy, the ledger schema, the
dataset snapshot, the window, the burn-in, the universe policy, the lifecycle
config, and the environment. Two runs with the same id were produced from the same
inputs; any drift — a rebuilt dataset, a changed threshold, a pandas upgrade —
surfaces immediately as a different id rather than as two runs that look
comparable and are not (C-5.7.2).

`env` uses `importlib.metadata.version` rather than module `__version__`
attributes, because `pandas_ta.__version__` is empty in this environment and
reading it would silently drop the version from the lineage hash (C-16.2).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from backtest.data.repository import canonical_json
from backtest.lineage import STRATEGY_VERSION, environment
from v2.analysis.scoring_config import DEFAULT_SCORING_CONFIG, ScoringConfig
from v2.app.ledger import CONTRACT_VERSION
from v2.domain.signal_ledger import LEDGER_SCHEMA_VERSION
from v2.lifecycle.rules import LifecycleConfig

__all__ = [
    "BURN_IN_H1_DEFAULT",
    "ConfigError",
    "H1_SECONDS",
    "ReplayConfig",
    "STRATEGY_VERSION",
    "UNIVERSE_MODES",
    "environment",
]

H1_SECONDS = 3600

#: C-5.7.1: 14 days. Trades instantiated before `T_0 + burn_in_h1*3600` are
#: emitted and stored but excluded from headline metrics, because lifecycle state
#: carries across cycles and therefore depends on where the run started.
BURN_IN_H1_DEFAULT = 336

#: C-6.9: the ranked universe, or a pinned set for a controlled experiment.
UNIVERSE_MODES = ("volume", "static")

class ConfigError(ValueError):
    """The replay configuration cannot describe a valid run."""


@dataclass(frozen=True)
class ReplayConfig:
    """One replay run, fully specified.

    `start` and `end` are the first and last **scan boundaries** `T_0` and `T_n`
    in epoch seconds, both inclusive and both on an hour (C-5.0/C-5.1). They are
    explicit run configuration and are never inferred from the data: a window
    derived from whatever the repository happens to hold would change silently
    every time the dataset grew.
    """

    repository_root: Path
    output_root: Path
    start: int
    end: int
    burn_in_h1: int = BURN_IN_H1_DEFAULT
    top_coins_limit: int = 30
    universe_mode: str = "volume"
    static_universe: Optional[tuple[str, ...]] = None
    lifecycle_config: LifecycleConfig = field(default_factory=LifecycleConfig)
    strategy_version: str = STRATEGY_VERSION
    #: Reserved by C-16.2 for the Outcome Engine's configuration hash. The engine
    #: does not exist yet, so it is empty; when it lands it will supply a value
    #: and `run_id`s will change — which is exactly what the field is for.
    outcome_config_hash: str = ""
    capture_console: bool = False
    #: C-15.8: the run **fails** if either swallowed-exception count exceeds this.
    #: Deliberately absent from `core()`: it is an operational tolerance, not an
    #: input that can change a result, and C-16.2's `core` is a closed list.
    max_swallowed_errors: int = 0
    #: M13: the Universe Definition membership set, resolved by
    #: `backtest.universe` and injected. `None` means unfiltered.
    eligible: Optional[frozenset[str]] = None
    #: The identity of that resolution, recorded in the run manifest.
    #:
    #: **Not in `core()`.** C-16.2 defines `core` as a closed list and this
    #: module does not get to extend a frozen contract clause on its own. The
    #: consequence is real and is recorded rather than hidden: two runs that
    #: differ only in `universe_id` produce the same `run_id` and therefore the
    #: same output directory. Adding it to `core` requires a C-16.2 amendment.
    universe_id: str = ""
    #: C-8.4: memoize the analysis pipeline, removing the duplicate evaluation at
    #: [orchestrator.py:265-268](v2/app/orchestrator.py#L265). Measured ~2x.
    #:
    #: **Deliberately absent from `core()`**, exactly as `universe_id` is, and for
    #: the stronger reason: the whole claim being made is that memoization cannot
    #: change a result. Putting it in `core` would assert the opposite — that two
    #: runs differing only in this flag are not comparable — and C-16.2's `core`
    #: is a closed list this module may not extend. The consequence is the same
    #: one `universe_id` carries and is recorded rather than hidden: the two arms
    #: share a `run_id` and therefore an output directory, so a cached-vs-uncached
    #: comparison must direct them at different `output_root`s.
    caching_analyzer: bool = False
    #: The scoring seam (`v2.analysis.scoring_config`). Defaults to 2.2.0's own
    #: table, so an unmodified config replays the baseline.
    #:
    #: **Deliberately absent from `core()`**, which C-16.2 defines as a closed
    #: list this module may not extend. The variant's identity reaches `run_id`
    #: through `strategy_version` instead — already a `core` member, and exactly
    #: what `experiments.config.variant_version` produces. `__post_init__` refuses
    #: the combination that would make that indirection unsound.
    scoring_config: ScoringConfig = DEFAULT_SCORING_CONFIG

    def __post_init__(self) -> None:
        if (self.scoring_config is not DEFAULT_SCORING_CONFIG
                and self.strategy_version == STRATEGY_VERSION):
            # A variant that keeps the base version hashes to the BASELINE's
            # run_id, so it resolves to the baseline's output directory. Without
            # `overwrite` the driver refuses (driver.py:554); with it, the driver
            # unlinks the existing sinks (driver.py:561) — i.e. this mistake
            # deletes the frozen baseline's ledger, lifecycle events and memory
            # snapshots. Refusing here is the only place it is still cheap.
            raise ConfigError(
                f"a non-default scoring_config was supplied with the base "
                f"strategy_version {self.strategy_version!r}. `scoring_config` is "
                f"not in `core()` (C-16.2 is a closed list), so this run would "
                f"hash to the baseline's run_id and write into the baseline's "
                f"directory. Set strategy_version to the variant's version — "
                f"experiments.config.variant_version(base, slug, variant_id) — "
                f"which is what makes the variant a distinct run.")
        if self.start % H1_SECONDS != 0:
            raise ConfigError(
                f"T_0 ({self.start}) is not on an hour: T mod 3600 must be 0 (C-5.1)"
            )
        if self.end % H1_SECONDS != 0:
            raise ConfigError(
                f"T_n ({self.end}) is not on an hour: T mod 3600 must be 0 (C-5.1)"
            )
        if self.end < self.start:
            raise ConfigError(f"T_n ({self.end}) precedes T_0 ({self.start})")
        if self.burn_in_h1 < 0:
            raise ConfigError(f"burn_in_h1 must be >= 0, got {self.burn_in_h1}")
        if self.max_swallowed_errors < 0:
            raise ConfigError(
                f"max_swallowed_errors must be >= 0, got "
                f"{self.max_swallowed_errors}"
            )
        if self.top_coins_limit < 1:
            raise ConfigError(
                f"top_coins_limit must be >= 1, got {self.top_coins_limit}"
            )
        if self.universe_mode not in UNIVERSE_MODES:
            raise ConfigError(
                f"unknown universe_mode {self.universe_mode!r}; "
                f"expected one of {UNIVERSE_MODES}"
            )
        if self.universe_mode == "static" and not self.static_universe:
            raise ConfigError(
                "universe_mode='static' requires a non-empty static_universe "
                "(C-6.9: a pinned universe must declare its own bias)"
            )
        if self.universe_mode == "volume" and self.static_universe:
            raise ConfigError(
                "static_universe is set but universe_mode is 'volume'; the "
                "universe policy must be stated once, not implied twice"
            )

    # -- the window --------------------------------------------------------

    @property
    def boundaries(self) -> tuple[int, ...]:
        """Every scan boundary, strictly increasing with step 3600 and **no gaps**
        (C-5.1). Both ends are inclusive."""
        return tuple(range(self.start, self.end + H1_SECONDS, H1_SECONDS))

    @property
    def cycle_count(self) -> int:
        return (self.end - self.start) // H1_SECONDS + 1

    @property
    def simulated_seconds(self) -> int:
        """Simulated time spanned by the run: `T_n - T_0`."""
        return self.end - self.start

    @property
    def burn_in_until(self) -> int:
        """The first boundary at which a trade is no longer in burn-in."""
        return self.start + self.burn_in_h1 * H1_SECONDS

    # -- identity ----------------------------------------------------------

    def core(self, *, data_snapshot_id: str) -> dict[str, Any]:
        """The C-16.2 `core`: every input that can change a result."""
        return {
            "contract_version": CONTRACT_VERSION,
            "strategy_version": self.strategy_version,
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "data_snapshot_id": data_snapshot_id,
            "T_0": self.start,
            "T_n": self.end,
            "burn_in_h1": self.burn_in_h1,
            "universe_mode": self.universe_mode,
            "static_universe": list(self.static_universe or ()),
            "top_coins_limit": self.top_coins_limit,
            "lifecycle_config": asdict(self.lifecycle_config),
            "outcome_config_hash": self.outcome_config_hash,
            "env": environment(),
        }

    def run_id(self, *, data_snapshot_id: str) -> str:
        """`"replay-" + sha256(canonical_json(core))[:16]` (C-16.2).

        Deterministic by construction, so re-running the same specification is
        idempotent and lands in the same output directory.
        """
        payload = canonical_json(self.core(data_snapshot_id=data_snapshot_id))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"replay-{digest}"
