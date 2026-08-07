"""Bundle audit — does a finished run's output describe what the run did?

REPLAY_CONTRACT.md C-15.3 … C-15.8, C-16.2, C-16.4, C-5.6.4, C-5.6.5.

## Why this module exists

Every audit of a replay bundle up to this point was an ad-hoc script: the
Shakedown A/A2/B reviews and the official-replay checks were written inline,
run once, and discarded. A finding produced that way is not evidence — it
cannot be re-derived, it is not covered by a test, and it disappears with the
shell history. This module is the same set of checks, owned by the repository
and pinned by `tests/backtest/test_audit.py`.

## What it is not

It does not replay, re-analyse, re-score or re-rank anything. It opens no
repository and reads no kline. Every check is a statement about a bundle's
**internal** consistency plus the arithmetic the contract fixes, so an audit
is a pure function of the files in `run_dir` — which is exactly what makes it
reproducible. The one exception is deliberately non-fatal: `I4` compares the
manifest's `strategy_version` against the version installed *now*, and an
archived bundle is expected to disagree, so it is reported as a warning.

## Where it sits

`§10` fixes the dependency direction. The capture layer writes the bundle and
`backtest.capture_schema` is the leaf both writer and reader share — created
for precisely this purpose ("the writer depends on it, the reader depends on
it, and neither depends on the other"). This module is a **reader**: it
imports the schema, `backtest.lineage` and two filename constants from
`backtest.dataset`, and nothing from `backtest.replay`. Auditing a run must
not require the machinery that produced it, or a broken driver would take its
own audit down with it.

## Streaming

The official bundle is 2.6 GB — `signal_ledger.jsonl` alone is 1.27 GB. Every
file is read line by line and nothing is retained whole. Two structures are
held across a pass, both bounded by the run rather than by the file size: the
per-cycle ledger index (`cycle -> {symbol: h1_last_ts}`, which serves both the
uniqueness check and the alert-orphan check) and the reconstructed active set
(bounded by `max_open_positions`).

## Severity

`error` means the bundle contradicts itself or the contract; `warning` means
something a reader must know that is not in itself a defect. The exit status
is driven by errors alone. There is no flag that downgrades a check — §10's
"no `--force`, no `--fix`" applies here as it does everywhere else in the CLI.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from backtest.capture_schema import (
    ALERTS_FILENAME,
    ATTRIBUTIONS,
    EVENT_KINDS,
    KIND_ALERT,
    KIND_KILLED_THESIS,
    KIND_MUTATION,
    LIFECYCLE_EVENTS_FILENAME,
    MEMORY_SNAPSHOTS_FILENAME,
    MUTATION_TYPES,
    PHASES,
    PHASE_END_OF_CYCLE,
)
from backtest.data.repository import canonical_json
from backtest.dataset import HEALTH_FILENAME, LEDGER_FILENAME, canonical_key
from backtest.lineage import (
    MANIFEST_FILENAME,
    STRATEGY_VERSION,
    LineageError,
    read_run_lineage,
)

__all__ = [
    "ACTIVE_POIS_FILENAME",
    "AuditError",
    "AuditReport",
    "Finding",
    "KILLED_THESIS_FILENAME",
    "OB_AUDIT_FILENAME",
    "audit_run",
]

H1_SECONDS = 3600

OB_AUDIT_FILENAME = "retracement_ob_audit.jsonl"

#: The two lifecycle state files. Written by `v2.infra.persistence`
#: ([persistence.py:55-56](v2/infra/persistence.py#L55)), which this module does
#: not import: a reader that had to load the production persistence adapter to
#: learn two filenames would couple the audit to the writer it audits. The names
#: are pinned against their source by `test_state_filenames_match_persistence`,
#: so the duplication cannot drift silently.
ACTIVE_POIS_FILENAME = "active_pois.json"
KILLED_THESIS_FILENAME = "killed_thesis.json"

#: C-16.4's manifest. Absent keys are an error: a manifest is the run's identity
#: document and a partial one cannot be reasoned about.
_MANIFEST_KEYS = frozenset({
    "run_id", "core", "universe_id", "universe_size", "optimizations",
    "builder_version", "repository_root", "cycles", "cycles_processed",
    "simulated_seconds", "ledger_rows", "ob_audit_rows", "alerts", "warmup",
    "burn_in_until", "declared_biases", "git_commit", "generated_at",
    "wall_seconds",
})

#: C-16.2's `core` — a closed list. Both directions are checked: a missing key
#: means the identity is under-determined, an extra key means `run_id` now hashes
#: something the contract does not sanction.
_CORE_KEYS = frozenset({
    "contract_version", "strategy_version", "ledger_schema_version",
    "data_snapshot_id", "T_0", "T_n", "burn_in_h1", "universe_mode",
    "static_universe", "top_coins_limit", "lifecycle_config",
    "outcome_config_hash", "env",
})

#: The provenance block of C-16.2, recorded outside `core`.
_OPTIMIZATION_KEYS = frozenset({"caching_analyzer", "vectorized_swings"})

_RUN_ID_RE = re.compile(r"^replay-[0-9a-f]{16}$")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")

#: How many offending values one finding carries. A 30,600-cycle bundle can fail
#: a check 30,600 times and a report that prints all of them is unreadable; the
#: count is always exact, the examples are a sample.
_MAX_EXAMPLES = 5


class AuditError(Exception):
    """The bundle cannot be audited at all — not the same thing as failing."""


@dataclass(frozen=True)
class Finding:
    """One thing the bundle says that it should not.

    `check` is the stable identifier (`M9`, `H4`, …) so a finding can be quoted
    across runs and referenced in a report without re-describing it.
    """

    check: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "severity": self.severity,
                "message": self.message, "detail": self.detail}


@dataclass
class AuditReport:
    """The outcome of one audit."""

    run_dir: str
    run_id: str = ""
    checks: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    # -- accumulation -------------------------------------------------------

    def ran(self, check: str) -> None:
        self.checks.append(check)

    def fail(self, check: str, message: str, **detail: Any) -> None:
        self.findings.append(Finding(check, "error", message, detail))

    def warn(self, check: str, message: str, **detail: Any) -> None:
        self.findings.append(Finding(check, "warning", message, detail))

    # -- verdict ------------------------------------------------------------

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        """PASS. Warnings do not fail an audit; they are things a reader must
        know, not things the bundle got wrong."""
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "run_id": self.run_id,
            "result": "PASS" if self.ok else "FAIL",
            "checks_run": len(self.checks),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "stats": self.stats,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _jsonl(path: Path, report: AuditReport, check: str) -> Iterator[dict]:
    """Stream a JSONL file, reporting every structural defect as it is found.

    Parse integrity is not a separate pass: a line that does not parse is
    reported here and skipped, so one traversal both validates the file and
    yields it. A file that does not end in a newline is reported once — that is
    the signature of a run killed mid-write, and it is the only way a *missing*
    line is detectable from the file alone.
    """
    if not path.exists():
        return
    bad = 0
    examples: list[dict[str, Any]] = []
    last_char = ""
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line:
                last_char = line[-1]
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                bad += 1
                if len(examples) < _MAX_EXAMPLES:
                    examples.append({"line": number, "error": str(exc)})
                continue
            if not isinstance(row, dict):
                bad += 1
                if len(examples) < _MAX_EXAMPLES:
                    examples.append({"line": number,
                                     "error": f"not an object: {type(row).__name__}"})
                continue
            yield row
    if bad:
        report.fail(check, f"{path.name}: {bad} line(s) are not a JSON object",
                    file=path.name, bad_lines=bad, examples=examples)
    if last_char and last_char != "\n":
        report.fail(
            check,
            f"{path.name} does not end with a newline; the last line is "
            f"truncated, which is what a run killed mid-write leaves behind",
            file=path.name)


def _read_json(path: Path, report: AuditReport, check: str) -> Optional[Any]:
    if not path.exists():
        report.fail(check, f"{path.name} is missing", file=path.name)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.fail(check, f"{path.name} is not valid JSON: {exc}",
                    file=path.name)
        return None


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

def audit_run(run_dir: Path | str) -> AuditReport:
    """Audit one replay bundle. Never raises for a *failing* bundle — a failure
    is a finding. `AuditError` is reserved for a directory that is not a run."""
    run_dir = Path(run_dir)
    report = AuditReport(run_dir=str(run_dir))
    if not run_dir.is_dir():
        raise AuditError(f"{run_dir} is not a directory")

    manifest = _audit_manifest(run_dir, report)
    if manifest is None:
        raise AuditError(
            f"{run_dir} has no readable {MANIFEST_FILENAME}; without the run's "
            f"identity document there is nothing to audit against (C-16.4)")

    _audit_lineage(run_dir, report)
    health = _audit_health(run_dir, report, manifest)
    ledger = _audit_ledger(run_dir, report, manifest, health)
    _audit_alerts(run_dir, report, manifest, health, ledger)
    snapshots_seen, final_active, final_killed, killed_introduced = \
        _audit_events_and_memory(run_dir, report, manifest, health)
    _audit_state_files(run_dir, report, final_active, final_killed,
                       killed_introduced)
    _audit_ob_audit(run_dir, report, manifest)

    report.stats["snapshots"] = snapshots_seen
    return report


# -- manifest ---------------------------------------------------------------

def _audit_manifest(run_dir: Path, report: AuditReport) -> Optional[dict]:
    """M1-M9 — manifest integrity, and the identity the whole bundle hangs on."""
    report.ran("M")
    manifest = _read_json(run_dir / MANIFEST_FILENAME, report, "M1")
    if not isinstance(manifest, dict):
        return None

    missing = sorted(_MANIFEST_KEYS - set(manifest))
    if missing:
        report.fail("M2", f"manifest is missing {len(missing)} required key(s)",
                    missing=missing)

    core = manifest.get("core")
    if not isinstance(core, dict):
        report.fail("M3", "manifest has no 'core' identity block (C-16.2)")
        return manifest
    core_missing = sorted(_CORE_KEYS - set(core))
    core_extra = sorted(set(core) - _CORE_KEYS)
    if core_missing:
        report.fail("M3", "core is missing C-16.2 determinant(s)",
                    missing=core_missing)
    if core_extra:
        report.fail("M3",
                    "core carries key(s) outside C-16.2's closed list; run_id "
                    "now hashes something the contract does not sanction",
                    extra=core_extra)

    run_id = manifest.get("run_id", "")
    report.run_id = str(run_id)
    if not _RUN_ID_RE.match(str(run_id)):
        report.fail("M4", f"run_id {run_id!r} is not 'replay-' + 16 hex (C-16.2)")

    if run_dir.name != run_id:
        report.fail(
            "M8",
            f"the bundle sits in {run_dir.name!r} but declares run_id "
            f"{run_id!r}; a run directory is named by its identity (C-16.4)",
            directory=run_dir.name, run_id=run_id)

    # -- window arithmetic (C-6.3, C-5.1) --------------------------------
    t_0, t_n = core.get("T_0"), core.get("T_n")
    cycles = manifest.get("cycles")
    if isinstance(t_0, int) and isinstance(t_n, int):
        if t_0 % H1_SECONDS or t_n % H1_SECONDS:
            report.fail("M5", "T_0/T_n are not on hour boundaries (C-5.1)",
                        T_0=t_0, T_n=t_n)
        if t_n < t_0:
            report.fail("M5", "T_n precedes T_0", T_0=t_0, T_n=t_n)
        expected_cycles = (t_n - t_0) // H1_SECONDS + 1
        if cycles != expected_cycles:
            report.fail(
                "M5",
                f"cycles={cycles} but the window spans {expected_cycles} "
                f"boundaries inclusive",
                cycles=cycles, expected=expected_cycles)
        if manifest.get("simulated_seconds") != t_n - t_0:
            report.fail("M6", "simulated_seconds != T_n - T_0",
                        recorded=manifest.get("simulated_seconds"),
                        expected=t_n - t_0)
        burn_in = core.get("burn_in_h1")
        if isinstance(burn_in, int):
            expected_until = t_0 + burn_in * H1_SECONDS
            if manifest.get("burn_in_until") != expected_until:
                report.fail("M7", "burn_in_until != T_0 + burn_in_h1 * 3600",
                            recorded=manifest.get("burn_in_until"),
                            expected=expected_until)

    # -- M9: the run identity must recompute from its own determinants ----
    try:
        digest = hashlib.sha256(
            canonical_json(core).encode("utf-8")).hexdigest()[:16]
    except (TypeError, ValueError) as exc:            # pragma: no cover
        report.fail("M9", f"core is not canonicalisable: {exc}")
    else:
        recomputed = f"replay-{digest}"
        if recomputed != run_id:
            report.fail(
                "M9",
                f"run_id does not recompute from core: the manifest says "
                f"{run_id!r} but sha256(canonical_json(core))[:16] gives "
                f"{recomputed!r}. Either core was edited after the run or the "
                f"bundle's identity is not the one that produced it (C-16.2)",
                declared=run_id, recomputed=recomputed)

    _audit_provenance(manifest, core, report)
    _audit_biases(manifest, report)
    return manifest


def _audit_provenance(manifest: dict, core: dict, report: AuditReport) -> None:
    """O1-O3, I3-I5 — the keys recorded *outside* `core`, and the identities."""
    report.ran("O")
    optimizations = manifest.get("optimizations")
    if not isinstance(optimizations, dict):
        report.fail("O1", "manifest has no 'optimizations' provenance block")
    else:
        if set(optimizations) != _OPTIMIZATION_KEYS:
            report.fail("O1", "optimizations block has the wrong keys",
                        found=sorted(optimizations),
                        expected=sorted(_OPTIMIZATION_KEYS))
        non_bool = sorted(k for k, v in optimizations.items()
                          if not isinstance(v, bool))
        if non_bool:
            report.fail("O1", "optimization flags must be booleans",
                        keys=non_bool)

    leaked = sorted(_OPTIMIZATION_KEYS & set(core)) + \
        (["universe_id"] if "universe_id" in core else [])
    if leaked:
        report.fail(
            "O2",
            "keys recorded outside core (C-16.2) appear inside it; two runs "
            "differing only in them would no longer share a run_id",
            keys=leaked)

    report.ran("I")
    universe_id = manifest.get("universe_id")
    if not isinstance(universe_id, str) or not _HEX16_RE.match(universe_id):
        report.fail("O3", f"universe_id {universe_id!r} is not 16 hex chars")

    declared_version = core.get("strategy_version")
    if declared_version != STRATEGY_VERSION:
        report.warn(
            "I4",
            f"the bundle was produced by strategy_version "
            f"{declared_version!r}; the installed code is {STRATEGY_VERSION!r}. "
            f"Expected for an archived run — recorded so a result is never "
            f"read as if it came from today's code",
            bundle=declared_version, installed=STRATEGY_VERSION)


def _audit_biases(manifest: dict, report: AuditReport) -> None:
    """B1 — C-5.5's declared biases. A run whose manifest does not name the
    fidelity losses it inherits invites the result to be read as live."""
    report.ran("B")
    biases = manifest.get("declared_biases")
    if not isinstance(biases, list) or not biases:
        report.fail("B1", "declared_biases is missing or empty (C-5.5)")
        return
    bad = [i for i, entry in enumerate(biases)
           if not isinstance(entry, str) or not entry.strip()]
    if bad:
        report.fail("B1", "declared_biases contains empty entries",
                    indices=bad[:_MAX_EXAMPLES])


# -- run_health -------------------------------------------------------------

@dataclass
class _Health:
    """What the health file says, indexed for the cross-file checks."""

    rows: int = 0
    processed: int = 0
    ledger_rows: dict[int, int] = field(default_factory=dict)
    decisions: dict[int, int] = field(default_factory=dict)
    events: dict[int, int] = field(default_factory=dict)
    alerts: dict[int, int] = field(default_factory=dict)
    ttl_inferred: dict[int, int] = field(default_factory=dict)
    scan_T: dict[int, int] = field(default_factory=dict)


def _audit_health(run_dir: Path, report: AuditReport,
                  manifest: dict) -> _Health:
    """H1-H10 — completeness, the C-15.8 identities, cycle continuity and
    timestamp progression."""
    report.ran("H")
    # `J` — JSONL parse integrity. Not a pass of its own: `_jsonl` validates
    # every file as it streams it, so the check is declared once here and
    # reported from wherever the defective line is read.
    report.ran("J")
    health = _Health()
    core = manifest.get("core") or {}
    burn_in_until = manifest.get("burn_in_until")

    expected_index = 0
    previous_T: Optional[int] = None
    gaps: list[dict[str, Any]] = []
    identity_failures: list[dict[str, Any]] = []
    swallowed: list[dict[str, Any]] = []
    ttl_mismatch: list[dict[str, Any]] = []
    burn_in_wrong: list[int] = []

    for row in _jsonl(run_dir / HEALTH_FILENAME, report, "J"):
        index = int(row.get("cycle_index", -1))
        scan_T = int(row.get("scan_T", 0))
        health.rows += 1
        if row.get("processed"):
            health.processed += 1
        health.ledger_rows[index] = int(row.get("ledger_rows") or 0)
        health.decisions[index] = int(row.get("decisions") or 0)
        health.events[index] = int(row.get("lifecycle_events") or 0)
        health.alerts[index] = int(row.get("alerts") or 0)
        health.ttl_inferred[index] = int(row.get("ttl_inferred") or 0)
        health.scan_T[index] = scan_T

        # H2 — contiguity. A hole means cycles are missing from the file.
        if index != expected_index and len(gaps) < _MAX_EXAMPLES:
            gaps.append({"expected": expected_index, "found": index})
        expected_index = index + 1

        # H3 — the boundary grid, one hour at a time (C-5.1, C-6.3).
        if previous_T is not None and scan_T - previous_T != H1_SECONDS:
            if len(gaps) < _MAX_EXAMPLES:
                gaps.append({"cycle": index, "scan_T": scan_T,
                             "previous": previous_T,
                             "step": scan_T - previous_T})
        previous_T = scan_T

        # H4/H5 — C-15.8's arithmetic, restated from the row's own fields.
        universe = int(row.get("universe_size") or 0)
        none_served = int(row.get("snapshots_none") or 0)
        guard = int(row.get("guard_skips") or 0)
        expected_analyzed = universe - none_served - guard
        before = int(row.get("swallowed_before_ledger") or 0)
        after = int(row.get("swallowed_after_ledger") or 0)
        if (row.get("expected_analyzed") != expected_analyzed
                or before != expected_analyzed - health.ledger_rows[index]
                or after != health.ledger_rows[index] - health.decisions[index]):
            if len(identity_failures) < _MAX_EXAMPLES:
                identity_failures.append({"cycle": index, "row": row})

        # H6 — a healthy run swallows nothing and reports no error channel.
        if before or after or row.get("universe_error") is not None \
                or int(row.get("data_errors") or 0):
            if len(swallowed) < _MAX_EXAMPLES:
                swallowed.append({
                    "cycle": index, "before": before, "after": after,
                    "universe_error": row.get("universe_error"),
                    "data_errors": row.get("data_errors")})

        # H7 — C-15.5's cross-check, re-evaluated from the file.
        if health.ttl_inferred[index] != int(row.get("ttl_log_lines") or 0):
            if len(ttl_mismatch) < _MAX_EXAMPLES:
                ttl_mismatch.append({
                    "cycle": index, "inferred": health.ttl_inferred[index],
                    "rendered": row.get("ttl_log_lines")})

        # H8 — burn-in is a function of the boundary, not a stored opinion.
        if isinstance(burn_in_until, int):
            if bool(row.get("in_burn_in")) != (scan_T < burn_in_until):
                burn_in_wrong.append(index)

    if gaps:
        report.fail("H2", "cycle indices or scan boundaries are not contiguous",
                    examples=gaps)
    if identity_failures:
        report.fail(
            "H4", "C-15.8's accounting identity does not hold",
            cycles=len(identity_failures), examples=identity_failures)
    if swallowed:
        report.fail(
            "H6",
            "cycles report swallowed exceptions or an error channel; the "
            "dataset understates what the run did (C-15.8)",
            examples=swallowed)
    if ttl_mismatch:
        report.fail("H7", "TTL attribution disagrees with the rendered lines "
                          "(C-15.5)", examples=ttl_mismatch)
    if burn_in_wrong:
        report.fail("H8", "in_burn_in disagrees with scan_T vs burn_in_until",
                    cycles=len(burn_in_wrong),
                    examples=burn_in_wrong[:_MAX_EXAMPLES])

    cycles = manifest.get("cycles")
    if health.rows != cycles:
        report.fail("H1", f"{HEALTH_FILENAME} holds {health.rows} line(s) but "
                          f"the manifest declares {cycles} cycle(s)",
                    lines=health.rows, cycles=cycles)
    if health.processed != manifest.get("cycles_processed"):
        report.fail("H10", "cycles_processed disagrees with the health file",
                    counted=health.processed,
                    manifest=manifest.get("cycles_processed"))

    if health.rows:
        first, last = min(health.scan_T), max(health.scan_T)
        if health.scan_T.get(first) != core.get("T_0"):
            report.fail("H3", "the first cycle does not start at T_0",
                        first=health.scan_T.get(first), T_0=core.get("T_0"))
        if health.scan_T.get(last) != core.get("T_n"):
            report.fail("H3", "the last cycle does not end at T_n",
                        last=health.scan_T.get(last), T_n=core.get("T_n"))

    report.stats["cycles"] = health.rows
    report.stats["cycles_processed"] = health.processed
    return health


# -- ledger -----------------------------------------------------------------

def _audit_ledger(run_dir: Path, report: AuditReport, manifest: dict,
                  health: _Health) -> dict[int, dict[str, float]]:
    """L1-L5 — row counts, C-9.4 key uniqueness, and per-row lineage."""
    report.ran("L")
    core = manifest.get("core") or {}
    run_id = manifest.get("run_id")
    top = core.get("top_coins_limit")
    cycles = manifest.get("cycles") or 0

    by_cycle: dict[int, dict[str, float]] = {}
    rows = 0
    duplicates: list[Any] = []
    lineage_bad: list[dict[str, Any]] = []
    rank_bad: list[dict[str, Any]] = []
    cycle_bad: list[dict[str, Any]] = []

    for row in _jsonl(run_dir / LEDGER_FILENAME, report, "J"):
        rows += 1
        symbol = row.get("symbol")
        cycle = row.get("cycle_index")
        h1 = row.get("h1_last_ts")
        if not isinstance(cycle, int) or not isinstance(symbol, str):
            if len(cycle_bad) < _MAX_EXAMPLES:
                cycle_bad.append({"row": rows, "cycle_index": cycle,
                                  "symbol": symbol})
            continue

        seen = by_cycle.setdefault(cycle, {})
        if symbol in seen:
            if len(duplicates) < _MAX_EXAMPLES:
                # `canonical_key` is the contract's row key (C-9.4); it is built
                # here, for the message, rather than stored for every row — a
                # 887k-row bundle does not need 887k tuples to find a collision.
                duplicates.append(list(canonical_key(
                    str(run_id), symbol, float(h1 or 0), cycle)))
        else:
            seen[symbol] = float(h1 or 0)

        if not (0 <= cycle < cycles):
            if len(cycle_bad) < _MAX_EXAMPLES:
                cycle_bad.append({"row": rows, "cycle_index": cycle,
                                  "cycles": cycles})

        if (row.get("run_id") != run_id
                or row.get("source") != "replay"
                or row.get("schema_version") != core.get("ledger_schema_version")
                or row.get("strategy_version") != core.get("strategy_version")
                or row.get("contract_version") != core.get("contract_version")
                or row.get("data_snapshot_id") != core.get("data_snapshot_id")):
            if len(lineage_bad) < _MAX_EXAMPLES:
                lineage_bad.append({
                    "row": rows, "symbol": symbol, "cycle_index": cycle,
                    "run_id": row.get("run_id"),
                    "source": row.get("source"),
                    "schema_version": row.get("schema_version"),
                    "strategy_version": row.get("strategy_version"),
                    "contract_version": row.get("contract_version"),
                    "data_snapshot_id": row.get("data_snapshot_id")})

        rank = row.get("universe_rank")
        if isinstance(top, int) and not (isinstance(rank, int)
                                         and 1 <= rank <= top):
            if len(rank_bad) < _MAX_EXAMPLES:
                rank_bad.append({"row": rows, "symbol": symbol,
                                 "universe_rank": rank, "top": top})

    if rows != manifest.get("ledger_rows"):
        report.fail("L1", f"{LEDGER_FILENAME} holds {rows} row(s) but the "
                          f"manifest declares {manifest.get('ledger_rows')}",
                    counted=rows, manifest=manifest.get("ledger_rows"))
    if duplicates:
        report.fail("L2", "the C-9.4 canonical key is not unique; a duplicated "
                          "key double-counts a decision in every join",
                    examples=duplicates)
    if lineage_bad:
        report.fail("I1", "ledger rows disagree with the manifest's core "
                          "lineage (C-16.1/C-16.2)", examples=lineage_bad)
    if cycle_bad:
        report.fail("L4", "ledger rows carry an unusable cycle_index",
                    examples=cycle_bad)
    if rank_bad:
        report.fail("L5", "universe_rank outside [1, top_coins_limit]",
                    examples=rank_bad)

    per_cycle = [
        {"cycle": cycle, "ledger": len(by_cycle.get(cycle, {})),
         "health": count}
        for cycle, count in health.ledger_rows.items()
        if len(by_cycle.get(cycle, {})) != count
    ]
    if per_cycle:
        report.fail("L4", "per-cycle ledger counts disagree with run_health",
                    cycles=len(per_cycle), examples=per_cycle[:_MAX_EXAMPLES])

    report.stats["ledger_rows"] = rows
    return by_cycle


# -- alerts -----------------------------------------------------------------

def _audit_alerts(run_dir: Path, report: AuditReport, manifest: dict,
                  health: _Health,
                  ledger: dict[int, dict[str, float]]) -> None:
    """A1-A4 — the C-15.7 cross-check channel, re-checked against the ledger."""
    report.ran("A")
    run_id = manifest.get("run_id")
    total = 0
    per_cycle: dict[int, int] = {}
    orphans: list[dict[str, Any]] = []
    lineage_bad: list[dict[str, Any]] = []

    for row in _jsonl(run_dir / ALERTS_FILENAME, report, "J"):
        total += 1
        cycle = row.get("cycle_index")
        alert = row.get("alert") or {}
        symbol = alert.get("symbol")
        if row.get("run_id") != run_id and len(lineage_bad) < _MAX_EXAMPLES:
            lineage_bad.append({"line": total, "run_id": row.get("run_id")})
        if not isinstance(cycle, int):
            continue
        per_cycle[cycle] = per_cycle.get(cycle, 0) + 1
        # A4 — an alert names a symbol the analysis never wrote a row for. The
        # ledger is the decision stream (C-2.1); an alert outside it describes a
        # signal that, as far as the run's own record goes, was never produced.
        if symbol not in ledger.get(cycle, {}):
            if len(orphans) < _MAX_EXAMPLES:
                orphans.append({"cycle": cycle, "symbol": symbol,
                                "kind": alert.get("kind")})

    if total != manifest.get("alerts"):
        report.fail("A1", f"{ALERTS_FILENAME} holds {total} alert(s) but the "
                          f"manifest declares {manifest.get('alerts')}",
                    counted=total, manifest=manifest.get("alerts"))
    if lineage_bad:
        report.fail("A2", "alert lines carry a foreign run_id",
                    examples=lineage_bad)
    if orphans:
        report.fail(
            "A4",
            "alerts reference a (cycle, symbol) with no ledger row; the "
            "notifier and the decision stream disagree (C-15.7/C-2.1)",
            examples=orphans)

    mismatched = [{"cycle": cycle, "alerts_file": per_cycle.get(cycle, 0),
                   "health": count}
                  for cycle, count in health.alerts.items()
                  if per_cycle.get(cycle, 0) != count]
    if mismatched:
        report.fail("A3", "per-cycle alert counts disagree with run_health",
                    cycles=len(mismatched), examples=mismatched[:_MAX_EXAMPLES])

    report.stats["alerts"] = total


# -- lifecycle events and memory --------------------------------------------

def _apply(active: set[str], row: dict) -> None:
    """One captured mutation's effect on membership (C-15.3).

    Only membership: `PartialRefreshMutation` and `IncrementMissMutation`
    change a record's contents and are correctly no-ops here.
    """
    kind = row.get("mutation_type")
    payload = row.get("mutation_payload") or {}
    symbol = payload.get("symbol")
    if kind == "InsertMutation":
        if isinstance(symbol, str):
            active.add(symbol)
    elif kind == "ReplaceMutation":
        target = payload.get("target_symbol") or row.get("target_symbol")
        if isinstance(target, str):
            active.discard(target)
        if isinstance(symbol, str):
            active.add(symbol)
    elif kind == "EvictMutation":
        if isinstance(symbol, str):
            active.discard(symbol)


def _audit_events_and_memory(
    run_dir: Path, report: AuditReport, manifest: dict, health: _Health,
) -> tuple[int, dict[str, Any], dict[str, float], dict[str, int]]:
    """E1-E7 and S1-S5 — event well-formedness, lifecycle reconstruction, and
    the memory snapshots the reconstruction is checked against.

    Events and snapshots are walked together, one cycle at a time, because that
    is the only way to check them against each other without holding either
    file. The reconstruction applies the captured mutations in `(cycle, seq)`
    order and compares the resulting membership with the cycle's snapshot —
    replaying the order rather than netting the counts, which is the difference
    between a model that agrees and one that appears to disagree on intra-cycle
    churn.
    """
    report.ran("E")
    report.ran("S")
    run_id = manifest.get("run_id")
    core = manifest.get("core") or {}
    max_open = (core.get("lifecycle_config") or {}).get("max_open_positions")

    snapshots = _jsonl(run_dir / MEMORY_SNAPSHOTS_FILENAME, report, "J")
    pending: Optional[dict] = next(snapshots, None)

    active: set[str] = set()
    snapshots_seen = 0
    final_active: dict[str, Any] = {}
    final_killed: dict[str, float] = {}
    killed_introduced: dict[str, int] = {}

    total_events = 0
    vocabulary: list[dict[str, Any]] = []
    ordering: list[dict[str, Any]] = []
    phase_order: list[dict[str, Any]] = []
    reconstruction: list[dict[str, Any]] = []
    overfull: list[dict[str, Any]] = []
    snapshot_lineage: list[dict[str, Any]] = []
    per_cycle_events: dict[int, int] = {}
    per_cycle_alerts: dict[int, int] = {}
    per_cycle_ttl: dict[int, int] = {}
    per_cycle_decisions: dict[int, set[str]] = {}

    def compare(snapshot: dict) -> None:
        """S1-S4 plus E7 — one snapshot against the reconstructed set."""
        nonlocal snapshots_seen, final_active, final_killed
        snapshots_seen += 1
        cycle = snapshot.get("cycle_index")
        if snapshot.get("run_id") != run_id and \
                len(snapshot_lineage) < _MAX_EXAMPLES:
            snapshot_lineage.append({"cycle": cycle,
                                     "run_id": snapshot.get("run_id")})
        recorded = snapshot.get("active") or {}
        final_active = recorded
        final_killed = snapshot.get("killed") or {}
        if isinstance(max_open, int) and len(recorded) > max_open:
            if len(overfull) < _MAX_EXAMPLES:
                overfull.append({"cycle": cycle, "held": len(recorded),
                                 "max_open_positions": max_open})
        if set(recorded) != active:
            if len(reconstruction) < _MAX_EXAMPLES:
                reconstruction.append({
                    "cycle": cycle,
                    "in_snapshot_not_reconstructed":
                        sorted(set(recorded) - active)[:_MAX_EXAMPLES],
                    "reconstructed_not_in_snapshot":
                        sorted(active - set(recorded))[:_MAX_EXAMPLES]})

    current_cycle: Optional[int] = None
    expected_seq = 0
    seen_end_of_cycle = False

    for row in _jsonl(run_dir / LIFECYCLE_EVENTS_FILENAME, report, "J"):
        total_events += 1
        cycle = row.get("cycle_index")
        seq = row.get("seq")

        if cycle != current_cycle:
            if current_cycle is not None and isinstance(cycle, int) \
                    and isinstance(current_cycle, int) and cycle < current_cycle:
                if len(ordering) < _MAX_EXAMPLES:
                    ordering.append({"cycle": cycle, "after": current_cycle})
            # Close the previous cycle: drain every snapshot up to and
            # including it, then open the new one.
            while pending is not None and isinstance(cycle, int) \
                    and int(pending.get("cycle_index", -1)) < cycle:
                compare(pending)
                pending = next(snapshots, None)
            current_cycle = cycle
            expected_seq = 0
            seen_end_of_cycle = False

        # E2 — `seq` restarts at 0 each cycle and is contiguous (C-15.3).
        if seq != expected_seq and len(ordering) < _MAX_EXAMPLES:
            ordering.append({"cycle": cycle, "expected_seq": expected_seq,
                             "found_seq": seq})
        expected_seq = (seq + 1) if isinstance(seq, int) else expected_seq + 1

        # E3 — the closed vocabularies of C-15.3.
        kind = row.get("kind")
        phase = row.get("phase")
        attribution = row.get("attribution")
        mutation_type = row.get("mutation_type")
        if (kind not in EVENT_KINDS or phase not in PHASES
                or attribution not in ATTRIBUTIONS
                or (kind == KIND_MUTATION and mutation_type not in MUTATION_TYPES)):
            if len(vocabulary) < _MAX_EXAMPLES:
                vocabulary.append({"cycle": cycle, "seq": seq, "kind": kind,
                                   "phase": phase, "attribution": attribution,
                                   "mutation_type": mutation_type})

        # E4 — end-of-cycle events sort after every symbol-phase event (C-5.6.4).
        if phase == PHASE_END_OF_CYCLE:
            seen_end_of_cycle = True
        elif seen_end_of_cycle and len(phase_order) < _MAX_EXAMPLES:
            phase_order.append({"cycle": cycle, "seq": seq,
                                "symbol": row.get("symbol")})

        if isinstance(cycle, int):
            per_cycle_events[cycle] = per_cycle_events.get(cycle, 0) + 1
            if kind == KIND_ALERT:
                per_cycle_alerts[cycle] = per_cycle_alerts.get(cycle, 0) + 1
            if attribution == "inferred_ttl":
                per_cycle_ttl[cycle] = per_cycle_ttl.get(cycle, 0) + 1
            elif isinstance(row.get("symbol"), str):
                per_cycle_decisions.setdefault(cycle, set()).add(row["symbol"])
            if kind == KIND_KILLED_THESIS:
                key = (row.get("mutation_payload") or {}).get("thesis_key")
                if isinstance(key, str):
                    killed_introduced.setdefault(key, cycle)

        if kind == KIND_MUTATION:
            _apply(active, row)

    # A snapshot is written at `end_cycle`, after the last event of its cycle,
    # so it is only comparable once that cycle's events are exhausted — which
    # the cycle-change branch above does for every cycle but the last.
    # Drain the tail: the final cycle's snapshot, and any snapshot for a cycle
    # that produced no events at all.
    while pending is not None:
        compare(pending)
        pending = next(snapshots, None)

    if vocabulary:
        report.fail("E3", "events use values outside C-15.3's closed "
                          "vocabularies", examples=vocabulary)
    if ordering:
        report.fail("E2", "(cycle_index, seq) is not ascending and contiguous; "
                          "file order is the ordering key (C-15.3)",
                    examples=ordering)
    if phase_order:
        report.fail("E4", "a symbol-phase event follows an end_of_cycle event "
                          "in the same cycle (C-5.6.4)", examples=phase_order)
    if reconstruction:
        report.fail(
            "E7",
            "replaying the captured mutations does not reproduce the memory "
            "snapshot; the event stream and the recorded state disagree",
            cycles=len(reconstruction), examples=reconstruction)
    if overfull:
        report.fail("S4", "a snapshot holds more signals than "
                          "max_open_positions", examples=overfull)
    if snapshot_lineage:
        report.fail("S3", "memory snapshots carry a foreign run_id",
                    examples=snapshot_lineage)

    event_mismatch = [{"cycle": cycle, "events_file": per_cycle_events.get(cycle, 0),
                       "health": count}
                      for cycle, count in health.events.items()
                      if per_cycle_events.get(cycle, 0) != count]
    if event_mismatch:
        report.fail("E1", "per-cycle event counts disagree with run_health",
                    cycles=len(event_mismatch),
                    examples=event_mismatch[:_MAX_EXAMPLES])

    ttl_mismatch = [{"cycle": cycle, "events_file": per_cycle_ttl.get(cycle, 0),
                     "health": count}
                    for cycle, count in health.ttl_inferred.items()
                    if per_cycle_ttl.get(cycle, 0) != count]
    if ttl_mismatch:
        report.fail("E5", "inferred-TTL event counts disagree with run_health "
                          "(C-15.5)", cycles=len(ttl_mismatch),
                    examples=ttl_mismatch[:_MAX_EXAMPLES])

    decision_mismatch = [
        {"cycle": cycle, "events_file": len(per_cycle_decisions.get(cycle, ())),
         "health": count}
        for cycle, count in health.decisions.items()
        if len(per_cycle_decisions.get(cycle, ())) != count]
    if decision_mismatch:
        report.fail("E6", "reconciler-attributed symbols disagree with the "
                          "decision count in run_health (C-15.8)",
                    cycles=len(decision_mismatch),
                    examples=decision_mismatch[:_MAX_EXAMPLES])

    alert_event_mismatch = [
        {"cycle": cycle, "events_file": per_cycle_alerts.get(cycle, 0),
         "health": count}
        for cycle, count in health.alerts.items()
        if per_cycle_alerts.get(cycle, 0) != count]
    if alert_event_mismatch:
        report.fail("E5", "ALERT events disagree with the alert count in "
                          "run_health (C-15.7)",
                    cycles=len(alert_event_mismatch),
                    examples=alert_event_mismatch[:_MAX_EXAMPLES])

    report.stats["lifecycle_events"] = total_events
    return snapshots_seen, final_active, final_killed, killed_introduced


# -- state files ------------------------------------------------------------

def _audit_state_files(run_dir: Path, report: AuditReport,
                       final_active: dict[str, Any],
                       final_killed: dict[str, float],
                       killed_introduced: dict[str, int]) -> None:
    """S5 and K1-K3 — the two state files against the last snapshot.

    `active_pois.json` is the *last* thing the run saved; the last
    `memory_snapshots` line is the state at the same moment (C-15.4 records the
    snapshot before the write that may raise). They must agree, and a
    disagreement means the run's saved state is not the state its own event
    stream describes.
    """
    active = _read_json(run_dir / ACTIVE_POIS_FILENAME, report, "S5")
    if isinstance(active, dict):
        if set(active) != set(final_active):
            report.fail(
                "S5",
                f"{ACTIVE_POIS_FILENAME} holds a different symbol set from the "
                f"final memory snapshot",
                only_in_state=sorted(set(active) - set(final_active))[:_MAX_EXAMPLES],
                only_in_snapshot=sorted(set(final_active) - set(active))[:_MAX_EXAMPLES])
        else:
            differing = sorted(symbol for symbol in active
                               if active[symbol] != final_active.get(symbol))
            if differing:
                report.fail(
                    "S5",
                    f"{ACTIVE_POIS_FILENAME} and the final snapshot hold the "
                    f"same symbols with different records",
                    symbols=differing[:_MAX_EXAMPLES], count=len(differing))
        report.stats["active_at_end"] = len(active)

    report.ran("K")
    killed = _read_json(run_dir / KILLED_THESIS_FILENAME, report, "K1")
    if not isinstance(killed, dict):
        return
    report.stats["killed_at_end"] = len(killed)

    malformed = sorted(key for key in killed
                       if not isinstance(key, str) or key.count("|") != 2
                       or not isinstance(killed[key], (int, float)))
    if malformed:
        report.fail("K1", "killed_thesis keys are not 'symbol|direction|"
                          "entry_mode' with a numeric timestamp",
                    examples=malformed[:_MAX_EXAMPLES])

    if set(killed) != set(final_killed):
        report.fail(
            "K2",
            f"{KILLED_THESIS_FILENAME} disagrees with the final memory snapshot",
            only_in_state=sorted(set(killed) - set(final_killed))[:_MAX_EXAMPLES],
            only_in_snapshot=sorted(set(final_killed) - set(killed))[:_MAX_EXAMPLES])

    # K3 — one direction only. A key can leave the map through the cooldown
    # prune, so absence proves nothing; a key that was never *introduced* by a
    # KILLED_THESIS event is a record with no captured origin.
    unexplained = sorted(key for key in killed
                         if killed_introduced.get(key, -1) < 0)
    if unexplained:
        report.fail(
            "K3",
            "killed theses have no KILLED_THESIS event to account for them",
            examples=unexplained[:_MAX_EXAMPLES], count=len(unexplained))


def _audit_ob_audit(run_dir: Path, report: AuditReport,
                    manifest: dict) -> None:
    """The OB audit trail — parse integrity and the manifest's own count."""
    rows = sum(1 for _ in _jsonl(run_dir / OB_AUDIT_FILENAME, report, "J"))
    if rows != manifest.get("ob_audit_rows"):
        report.fail("L6", f"{OB_AUDIT_FILENAME} holds {rows} row(s) but the "
                          f"manifest declares {manifest.get('ob_audit_rows')}",
                    counted=rows, manifest=manifest.get("ob_audit_rows"))
    report.stats["ob_audit_rows"] = rows


def _audit_lineage(run_dir: Path, report: AuditReport) -> None:
    """I5 — the existing lineage reader must accept the bundle."""
    try:
        lineage = read_run_lineage(run_dir)
    except LineageError as exc:
        report.fail("I5", f"read_run_lineage refuses the bundle: {exc}")
        return
    if not lineage.declared:
        report.fail(
            "I5",
            "the manifest does not declare both C-5.7.2 preconditions; "
            "`metrics.freeze()` will refuse this run",
            burn_in_h1=lineage.burn_in_h1, universe_mode=lineage.universe_mode)
