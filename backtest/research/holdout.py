"""The permanent holdout — declared once, never optimized against.

REPLAY_CONTRACT.md §11 steps 8–9. Milestone M11, closing finding N12.

## What a holdout is, and what makes it stop being one

§11 step 8 requires "a permanent holdout, declared before the first experiment and
never optimized against". The words that carry the weight are *before* and *never*:

- **Before.** A holdout chosen after seeing results is chosen, however honestly, in
  knowledge of them. Hence `declare` writes a lock and every later attempt to
  declare a *different* holdout fails — the first declaration wins permanently, and
  the only way to change it is to delete the lock deliberately, which is an act
  nobody performs by accident.
- **Never.** A holdout is a **consumable**. Each evaluation against it transfers a
  little information into the researcher's choices, and after enough looks it is
  simply another training set. Nothing can prevent that; what this module does is
  make it *countable*. Every evaluation appends to an audit log, and the log is
  reported. A holdout used fifteen times is not a holdout, and the platform should
  be able to say so out loud rather than leave it to memory.

## The three enforcement points

| Question | Function | Failure |
|---|---|---|
| may a window be optimized on? | `assert_untouched` | `HoldoutViolation` |
| is this evaluation actually against the declared holdout? | `assert_is_holdout` | `HoldoutMismatch` |
| do these walk-forward splits keep clear of it? | `guard_splits` | `HoldoutViolation` |

`HoldoutMismatch` is the one the task list calls for: evaluating against *a
different* holdout than the declared one fails loudly rather than reporting a
number that looks like an out-of-sample result and is not.

## Scope: a holdout binds to a dataset, not to a period

The lock records `data_snapshot_id`. The same calendar window over a rebuilt
corpus is a different holdout — different bytes, different bars, different
outcomes — and treating them as one would let a rebuild quietly launder a used
holdout into a fresh one (C-6.10).

## What this module never does

It does not run replay, does not compute a metric, and does not decide whether a
strategy passed. It answers "is this allowed" and records "this happened".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from backtest.data.repository import canonical_json
from backtest.research.splits import H1_SECONDS, Splits, Window

__all__ = [
    "HOLDOUT_LOCK_NAME",
    "HOLDOUT_SCHEMA_VERSION",
    "HOLDOUT_USES_NAME",
    "Holdout",
    "HoldoutError",
    "HoldoutMismatch",
    "HoldoutUse",
    "HoldoutViolation",
    "assert_is_holdout",
    "assert_untouched",
    "declare",
    "guard_splits",
    "holdout_uses",
    "load",
    "record_use",
]

HOLDOUT_SCHEMA_VERSION = 1

#: The lock. Named `.lock` because that is what it is: a declaration that has
#: already happened and may not happen differently.
HOLDOUT_LOCK_NAME = "holdout.lock.json"

#: Append-only. One line per evaluation against the holdout.
HOLDOUT_USES_NAME = "holdout_uses.jsonl"


class HoldoutError(Exception):
    """The holdout contract was broken."""


class HoldoutViolation(HoldoutError):
    """Something touched the holdout that must not have."""


class HoldoutMismatch(HoldoutError):
    """An evaluation claims a holdout that is not the declared one."""


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Holdout:
    """A declared, immutable out-of-sample period.

    `declared_at` is wall-clock and is the **only** non-reproducible field here.
    That is deliberate and is not the determinism problem it looks like: a
    declaration is an event, its identity (`holdout_id`) excludes the timestamp,
    and being able to say *when* a holdout was fixed relative to the first
    experiment is the entire evidential value of declaring one.
    """

    window: Window
    data_snapshot_id: str
    label: str
    reason: str = ""
    contract_version: str = "1.1"
    declared_at: str = ""
    schema_version: int = HOLDOUT_SCHEMA_VERSION

    @property
    def holdout_id(self) -> str:
        """Identity over *what* is held out, never over when it was declared."""
        payload = {
            "schema_version": self.schema_version,
            "start": self.window.start,
            "end": self.window.end,
            "burn_in_h1": self.window.burn_in_h1,
            "data_snapshot_id": self.data_snapshot_id,
        }
        return hashlib.sha256(
            canonical_json(payload).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "holdout_id": self.holdout_id,
            "label": self.label,
            "reason": self.reason,
            "window": self.window.to_dict(),
            "data_snapshot_id": self.data_snapshot_id,
            "contract_version": self.contract_version,
            "declared_at": self.declared_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Holdout":
        window = payload["window"]
        return cls(
            window=Window(
                start=int(window["start"]), end=int(window["end"]),
                burn_in_h1=int(window.get("burn_in_h1", 0)),
                role=window.get("role") or "holdout"),
            data_snapshot_id=str(payload["data_snapshot_id"]),
            label=str(payload.get("label", "")),
            reason=str(payload.get("reason", "")),
            contract_version=str(payload.get("contract_version", "1.1")),
            declared_at=str(payload.get("declared_at", "")),
            schema_version=int(payload.get("schema_version",
                                           HOLDOUT_SCHEMA_VERSION)),
        )


def declare(
    root: Path | str,
    window: Window,
    *,
    data_snapshot_id: str,
    label: str,
    reason: str = "",
    now: Optional[str] = None,
) -> Holdout:
    """Declare the permanent holdout. Idempotent; never silently replaced.

    Re-declaring the *identical* holdout succeeds and changes nothing, so a build
    script may call this unconditionally. Declaring a *different* one raises: the
    lock is the evidence that the period was fixed in advance, and a lock that can
    be rewritten is evidence of nothing.

    Changing the holdout is possible — delete the lock — and that is the point.
    It must be an explicit, visible act, not a parameter someone edits between two
    runs.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if window.burn_in_h1 >= window.cycles:
        raise HoldoutError(
            "the holdout would be entirely burn-in and could measure nothing")

    holdout = Holdout(
        window=Window(start=window.start, end=window.end,
                      burn_in_h1=window.burn_in_h1, role="holdout"),
        data_snapshot_id=data_snapshot_id,
        label=label,
        reason=reason,
        declared_at=now if now is not None else _now(),
    )

    path = root / HOLDOUT_LOCK_NAME
    if path.exists():
        existing = load(root)
        if existing.holdout_id != holdout.holdout_id:
            raise HoldoutViolation(
                f"a holdout is already declared at {path}:\n"
                f"  existing: {existing.holdout_id} "
                f"[{existing.window.start}, {existing.window.end}] on snapshot "
                f"{existing.data_snapshot_id}\n"
                f"  proposed: {holdout.holdout_id} "
                f"[{holdout.window.start}, {holdout.window.end}] on snapshot "
                f"{holdout.data_snapshot_id}\n"
                f"A holdout is declared once, before the first experiment (§11 "
                f"step 8). Re-declaring it after seeing results is choosing the "
                f"out-of-sample period in knowledge of the results. Delete the "
                f"lock deliberately if the corpus genuinely changed.")
        return existing

    path.write_text(json.dumps(holdout.to_dict(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return holdout


def load(root: Path | str) -> Holdout:
    """Read the declared holdout."""
    path = Path(root) / HOLDOUT_LOCK_NAME
    if not path.exists():
        raise HoldoutError(
            f"no {HOLDOUT_LOCK_NAME} at {root}. Declare the permanent holdout "
            f"before the first experiment (§11 step 8) — a holdout declared "
            f"afterwards is not one.")
    return Holdout.from_dict(json.loads(path.read_text(encoding="utf-8")))


def find(root: Path | str) -> Optional[Holdout]:
    """The declared holdout, or `None`. For callers that must not raise."""
    try:
        return load(root)
    except HoldoutError:
        return None


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def assert_untouched(holdout: Holdout, window: Window, *,
                     purpose: str = "optimization") -> None:
    """Refuse a window that overlaps the holdout by even one boundary.

    This is the guard for everything a decision may be taken on: training,
    validation, parameter selection, threshold sweeps, segment mining. One shared
    hour is enough to refuse, because "mostly out of sample" is not a property that
    survives being averaged.
    """
    if holdout.window.overlaps(window):
        overlap_start = max(holdout.window.start, window.start)
        overlap_end = min(holdout.window.end, window.end)
        hours = (overlap_end - overlap_start) // H1_SECONDS + 1
        raise HoldoutViolation(
            f"{purpose} window [{window.start}, {window.end}] overlaps the "
            f"permanent holdout {holdout.holdout_id} "
            f"[{holdout.window.start}, {holdout.window.end}] by {hours} hour(s). "
            f"The holdout is never optimized against (§11 step 8); once it is, "
            f"no later evaluation on it is out-of-sample.")


def assert_is_holdout(holdout: Holdout, window: Window, *,
                      data_snapshot_id: Optional[str] = None) -> None:
    """The window being evaluated **is** the declared holdout, on its snapshot.

    Fails loudly on a different period, a truncated one, an extended one, or the
    same calendar window over rebuilt data. Each of those produces a number that
    reads like an out-of-sample result while not being one, which is the most
    expensive kind of wrong answer this pipeline can produce.
    """
    if (window.start, window.end) != (holdout.window.start, holdout.window.end):
        raise HoldoutMismatch(
            f"evaluation window [{window.start}, {window.end}] is not the "
            f"declared holdout [{holdout.window.start}, {holdout.window.end}] "
            f"({holdout.holdout_id}). Evaluating a different period and calling "
            f"it the holdout is how an in-sample result gets promoted.")
    if data_snapshot_id is not None and data_snapshot_id != holdout.data_snapshot_id:
        raise HoldoutMismatch(
            f"the holdout was declared on data_snapshot_id "
            f"{holdout.data_snapshot_id} but this evaluation ran on "
            f"{data_snapshot_id}. The same calendar window over rebuilt data is "
            f"a different holdout (C-6.10), and reusing the declaration would "
            f"launder a consumed holdout into a fresh one.")


def guard_splits(holdout: Holdout, splits: Splits) -> None:
    """Every optimization window of a walk-forward plan clears the holdout.

    Test windows are checked too. A walk-forward test window is out-of-sample with
    respect to *its own* training data, but it is still reported repeatedly during
    development, so letting one sit inside the permanent holdout would consume the
    holdout without anyone deciding to.
    """
    for split in splits:
        for window in split.windows:
            assert_untouched(holdout, window,
                             purpose=f"split {split.index} {window.role}")


# ---------------------------------------------------------------------------
# The audit log — how a holdout gets used up
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HoldoutUse:
    """One recorded evaluation against the holdout."""

    holdout_id: str
    run_id: str
    strategy_version: str
    metrics_hash: str
    at: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "holdout_id": self.holdout_id,
            "run_id": self.run_id,
            "strategy_version": self.strategy_version,
            "metrics_hash": self.metrics_hash,
            "at": self.at,
            "note": self.note,
        }


def record_use(
    root: Path | str,
    holdout: Holdout,
    *,
    run_id: str,
    strategy_version: str,
    metrics_hash: str,
    note: str = "",
    now: Optional[str] = None,
) -> HoldoutUse:
    """Append one holdout evaluation to the audit log.

    Append-only, like the signal ledger: a use that can be deleted is a use that
    will be. The log is what makes the holdout's remaining validity a fact rather
    than a recollection.
    """
    use = HoldoutUse(
        holdout_id=holdout.holdout_id, run_id=run_id,
        strategy_version=strategy_version, metrics_hash=metrics_hash,
        at=now if now is not None else _now(), note=note)
    path = Path(root) / HOLDOUT_USES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(use.to_dict(), sort_keys=True) + "\n")
    return use


def holdout_uses(root: Path | str) -> tuple[HoldoutUse, ...]:
    """Every recorded evaluation, oldest first."""
    path = Path(root) / HOLDOUT_USES_NAME
    if not path.exists():
        return ()
    uses: list[HoldoutUse] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            uses.append(HoldoutUse(**json.loads(line)))
    return tuple(uses)


def usage_caution(root: Path | str, holdout: Holdout) -> Optional[str]:
    """A sentence to print when the holdout has been evaluated before.

    Returns `None` on the first use. Deliberately a caution rather than a refusal:
    a second look is sometimes legitimate (a bug fix, a rebuilt corpus), and a
    hard block would only teach people to bypass the recorder. What must not
    happen is a fifth look that nobody notices.
    """
    previous = [use for use in holdout_uses(root)
                if use.holdout_id == holdout.holdout_id]
    if not previous:
        return None
    versions = sorted({use.strategy_version for use in previous})
    return (
        f"holdout {holdout.holdout_id} has already been evaluated "
        f"{len(previous)} time(s) (strategy version(s): {', '.join(versions)}). "
        f"Each evaluation transfers information into the choices that follow it; "
        f"this result is that much less out-of-sample than the first one was.")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
