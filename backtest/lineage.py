"""Shared lineage capture — one definition of "which environment produced this".

REPLAY_CONTRACT.md C-16.2 makes the environment capture **normative**, and both
the replay driver (`run_id`) and the Outcome Engine (`OutcomeRecord.env`) must
record the same thing. §10 forbids `outcome` from depending on `replay`, so the
rule lives here, in a leaf both can import, rather than in either of them.

`importlib.metadata.version` is required rather than a module's `__version__`
attribute: `pandas_ta.__version__` is empty in this environment, and reading the
attribute would silently drop the version from every lineage hash.

## Run lineage (M11, finding N2)

`RunLineage` carries the run-level facts an `OutcomeRecord` must stamp but cannot
derive from a trade: the burn-in and the universe policy (C-5.7.2). It is read
from the run manifest the driver wrote, so the value in a record is the value that
produced it rather than one re-typed at the command line. Reading a JSON file is
not a dependency on `replay` — the manifest is data, and §10's direction
(`outcome → data + v2.domain`) is preserved.
"""
from __future__ import annotations

import importlib.metadata
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "MANIFEST_FILENAME",
    "LineageError",
    "RunLineage",
    "STRATEGY_VERSION",
    "environment",
    "optimizations",
    "read_run_lineage",
]

#: The run manifest of C-16.4. Named here because both the trade builder and the
#: Outcome Engine read it, and neither may import the driver that writes it.
MANIFEST_FILENAME = "manifest.json"

#: The identity of the strategy code being replayed and evaluated (C-16.2).
#:
#: Re-exported from `v2.app.ledger`, which is where the live ledger stamps it and
#: where its history is maintained. It used to be declared here independently, on
#: the belief that nothing else defined one — and the two drifted: run manifests
#: recorded `"v2.0"` while the ledger rows *inside those runs* recorded `"2.0.1"`.
#: A single definition makes that class of disagreement unrepresentable.
#:
#: MUST change whenever analysis, scoring, entry, risk, lifecycle or the Universe
#: Definition changes. See finding R5 (M7) and PHASE0_FREEZE.md.
from v2.app.ledger import STRATEGY_VERSION

#: The swing implementation actually in force, read live rather than bound at
#: import time so the manifest records the value the run used, not the value the
#: module held when it was first imported.
#:
#: Imported *here* and not in `backtest/replay/`: the driver's `v2` imports are a
#: closed allowlist (`test_the_driver_contains_no_strategy_logic`), and widening
#: it to admit an analysis module — to write one provenance boolean — would trade
#: an architectural guarantee for a convenience. This module is already the
#: project's provenance layer and already re-exports `STRATEGY_VERSION` from
#: `v2.app.ledger`, so the dependency is one it holds by definition.
from v2.analysis import structure as _swings

#: The libraries whose version can change a float result (§18).
_TRACKED = ("pandas", "pandas_ta", "numpy")


def optimizations(*, caching_analyzer: bool) -> dict[str, bool]:
    """The optimization provenance block of the run manifest.

    Recorded **outside** C-16.2's `core`, exactly as `universe_id` is: the claim
    each flag makes is that it cannot change a result, and putting it in `core`
    would assert the opposite — that two runs differing only in it are not
    comparable. A cached-vs-uncached or v1-vs-vectorized comparison therefore
    excludes this key along with the two wall-clock keys; it *is* the variable
    under test.

    `caching_analyzer` is run configuration and is passed in. `vectorized_swings`
    is not configuration at all — it is the state of the C-8.5 rollback seam
    (`analysis.structure.SWING_IMPL`), which a run cannot choose and an operator
    changes only by editing the module. It is recorded because a rolled-back
    baseline would otherwise be indistinguishable from a vectorized one in its
    own manifest, and `git_commit` does not close that gap: it reads `.git/HEAD`
    and carries no dirty-tree flag.
    """
    return {
        "caching_analyzer": bool(caching_analyzer),
        "vectorized_swings": _swings.SWING_IMPL == "vectorized",
    }


def environment() -> dict[str, str]:
    """The version lineage a numeric result depends on (C-16.2).

    A patch-level upgrade changes this, and therefore changes `run_id` and every
    outcome record's lineage. That is intended: results are only guaranteed
    identical within one environment.
    """
    versions = {"python": platform.python_version()}
    for name in _TRACKED:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:   # pragma: no cover
            versions[name] = "unknown"
    return versions


class LineageError(Exception):
    """A run's lineage cannot be established from its manifest."""


@dataclass(frozen=True)
class RunLineage:
    """The run-level lineage an `OutcomeRecord` stamps (C-5.7.2, finding N2).

    Every field is `Optional` and `None` means **undeclared**, never a default. A
    guessed burn-in would make two incomparable runs look comparable, which is the
    exact failure C-16.6.4 exists to prevent; an absent value must therefore
    survive all the way to `metrics.freeze()`, which refuses it.
    """

    burn_in_h1: Optional[int] = None
    universe_mode: Optional[str] = None
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    data_snapshot_id: Optional[str] = None

    @property
    def declared(self) -> bool:
        """True when both C-5.7.2 preconditions this class exists to carry are set."""
        return self.burn_in_h1 is not None and self.universe_mode is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "burn_in_h1": self.burn_in_h1,
            "universe_mode": self.universe_mode,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "data_snapshot_id": self.data_snapshot_id,
        }


def read_run_lineage(run_dir: Path | str) -> RunLineage:
    """Read `RunLineage` from a run's `manifest.json` (C-16.4).

    The manifest's `core` is the C-16.2 identity block — the same mapping the
    `run_id` hashes — so the values read here are exactly the ones that produced
    the run. A manifest whose `core` is missing is an error rather than an empty
    lineage: a run directory without an identity block is not a run.
    """
    path = Path(run_dir) / MANIFEST_FILENAME
    if not path.exists():
        raise LineageError(
            f"no {MANIFEST_FILENAME} in {run_dir}. The driver writes it (C-16.4); "
            f"without it the run's burn-in and universe policy are unknowable and "
            f"MUST NOT be guessed.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    core = manifest.get("core")
    if not isinstance(core, dict):
        raise LineageError(f"{path} has no 'core' identity block (C-16.2)")
    return RunLineage(
        burn_in_h1=_opt_int(core.get("burn_in_h1")),
        universe_mode=_opt_str(core.get("universe_mode")),
        window_start=_opt_int(core.get("T_0")),
        window_end=_opt_int(core.get("T_n")),
        data_snapshot_id=_opt_str(core.get("data_snapshot_id")),
    )


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _opt_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)
