"""`backtest.audit` — the repository-owned bundle audit.

Every test builds a complete, self-consistent bundle and then breaks exactly one
thing in it. That shape matters: an audit that fails a corrupted bundle proves
nothing unless the same bundle passes before the corruption, so each failure
test is its own control.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backtest.audit import (
    ACTIVE_POIS_FILENAME,
    KILLED_THESIS_FILENAME,
    OB_AUDIT_FILENAME,
    AuditError,
    audit_run,
)
from backtest.capture_schema import (
    ALERTS_FILENAME,
    KIND_ALERT,
    KIND_LOG,
    KIND_MUTATION,
    LIFECYCLE_EVENTS_FILENAME,
    MEMORY_SNAPSHOTS_FILENAME,
    CapturedEvent,
    MemorySnapshot,
)
from backtest.data.repository import canonical_json
from backtest.dataset import HEALTH_FILENAME, LEDGER_FILENAME
from backtest.lineage import MANIFEST_FILENAME, STRATEGY_VERSION

T_0 = 1672531200
H1 = 3600
SYMBOL = "AAAUSDT"
SNAPSHOT_ID = "c8542cb360d13207"


def _core() -> dict:
    """A C-16.2 `core` with every determinant the contract lists."""
    return {
        "contract_version": "1.1",
        "strategy_version": STRATEGY_VERSION,
        "ledger_schema_version": 2,
        "data_snapshot_id": SNAPSHOT_ID,
        "T_0": T_0,
        "T_n": T_0 + H1,
        "burn_in_h1": 0,
        "universe_mode": "volume",
        "static_universe": [],
        "top_coins_limit": 2,
        "lifecycle_config": {"max_open_positions": 10, "miss_count_limit": 3},
        "outcome_config_hash": "",
        "env": {"python": "3.13.5"},
    }


def _run_id(core: dict) -> str:
    digest = hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()
    return f"replay-{digest[:16]}"


def _active_record() -> dict:
    """One `ActiveSignal.to_v1_dict()`-shaped record. Only its identity as a
    dict matters here; the audit compares it, it does not interpret it."""
    return {"direction": "long", "score": 9, "decision": "A+",
            "entry_price": 1.0, "sl": 0.9, "tp": 1.3, "regime": "expansion_up",
            "timestamp": float(T_0), "miss_count": 0, "status": "WATCH"}


def _ledger_row(cycle: int, symbol: str = SYMBOL) -> dict:
    core = _core()
    return {
        "schema_version": 2, "strategy_version": STRATEGY_VERSION,
        "recorded_at": float(T_0 + cycle * H1 + 1), "symbol": symbol,
        "h1_last_ts": float(T_0 + cycle * H1 - H1),
        "decision": "A+", "score": 9, "entry_mode": "CONTINUATION",
        "regime": "expansion_up", "session_active": True, "live_price": 1.0,
        "source": "replay", "run_id": _run_id(core), "cycle_index": cycle,
        "universe_rank": 1, "contract_version": "1.1",
        "data_snapshot_id": SNAPSHOT_ID,
    }


def _health_row(cycle: int, *, ledger_rows: int, decisions: int,
                events: int, alerts: int) -> dict:
    return {
        "cycle_index": cycle, "scan_T": T_0 + cycle * H1, "processed": True,
        "universe_size": 1, "snapshots_served": 1, "snapshots_none": 0,
        "guard_skips": 0, "m5_attached": 0, "m5_guard_failures": 0,
        "data_errors": 0, "universe_error": None, "ledger_rows": ledger_rows,
        "decisions": decisions, "lifecycle_events": events,
        "ttl_inferred": 0, "ttl_log_lines": 0, "alerts": alerts,
        "expected_analyzed": 1,
        "swallowed_before_ledger": 1 - ledger_rows,
        "swallowed_after_ledger": ledger_rows - decisions,
        "unaccounted": 0, "in_burn_in": False,
    }


def _event(cycle: int, seq: int, kind: str, run_id: str, **extra) -> str:
    return CapturedEvent(
        run_id=run_id, source="replay", cycle_index=cycle,
        scan_T=T_0 + cycle * H1, h1_last_ts=float(T_0 + cycle * H1 - H1),
        seq=seq, symbol=SYMBOL, universe_rank=1, phase="symbol", kind=kind,
        **extra
    ).to_json_line()


def build_bundle(root: Path) -> Path:
    """A two-cycle bundle that satisfies every check in `backtest.audit`.

    Cycle 0 inserts one signal and alerts on it; cycle 1 reconciles the same
    symbol and decides nothing, which C-15.3 still records as one LOG line so
    that "reconcile ran and decided nothing" stays distinguishable from
    "reconcile never ran".
    """
    core = _core()
    run_id = _run_id(core)
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "run_id": run_id, "core": core, "universe_id": "7fd26361c467d048",
        "universe_size": 1,
        "optimizations": {"caching_analyzer": True, "vectorized_swings": True},
        "builder_version": "m4.2-1.0.0", "repository_root": "data/stage1/canonical",
        "cycles": 2, "cycles_processed": 2, "simulated_seconds": H1,
        "ledger_rows": 2, "ob_audit_rows": 0, "alerts": 1,
        "warmup": {"symbols_total": 1, "guard_skips_at_T0": 0},
        "burn_in_until": T_0, "declared_biases": ["live_price = close(C_ref)"],
        "git_commit": "0" * 40, "generated_at": "2026-08-07T00:00:00Z",
        "wall_seconds": 1.0,
    }
    (run_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    (run_dir / HEALTH_FILENAME).write_text(
        json.dumps(_health_row(0, ledger_rows=1, decisions=1, events=2,
                               alerts=1), sort_keys=True) + "\n"
        + json.dumps(_health_row(1, ledger_rows=1, decisions=1, events=1,
                                 alerts=0), sort_keys=True) + "\n",
        encoding="utf-8")

    (run_dir / LEDGER_FILENAME).write_text(
        json.dumps(_ledger_row(0), sort_keys=True) + "\n"
        + json.dumps(_ledger_row(1), sort_keys=True) + "\n", encoding="utf-8")

    (run_dir / ALERTS_FILENAME).write_text(
        json.dumps({"run_id": run_id, "cycle_index": 0, "seq": 1,
                    "alert": {"kind": "new", "symbol": SYMBOL,
                              "direction": "long", "entry_price": 1.0,
                              "sl": 0.9, "tp": 1.3, "dedup_key": "A+"}},
                   sort_keys=True) + "\n", encoding="utf-8")

    (run_dir / LIFECYCLE_EVENTS_FILENAME).write_text(
        _event(0, 0, KIND_MUTATION, run_id,
               mutation_type="InsertMutation",
               mutation_payload={"symbol": SYMBOL,
                                 "record": _active_record()})
        + _event(0, 1, KIND_ALERT, run_id, alert_kind="new", alert_tier="A+",
                 alert_payload={"kind": "new", "symbol": SYMBOL})
        + _event(1, 0, KIND_LOG, run_id),
        encoding="utf-8")

    (run_dir / MEMORY_SNAPSHOTS_FILENAME).write_text(
        MemorySnapshot(run_id=run_id, source="replay", cycle_index=0,
                       scan_T=T_0, active={SYMBOL: _active_record()},
                       killed={}).to_json_line()
        + MemorySnapshot(run_id=run_id, source="replay", cycle_index=1,
                         scan_T=T_0 + H1, active={SYMBOL: _active_record()},
                         killed={}).to_json_line(),
        encoding="utf-8")

    (run_dir / ACTIVE_POIS_FILENAME).write_text(
        json.dumps({SYMBOL: _active_record()}, indent=4), encoding="utf-8")
    (run_dir / KILLED_THESIS_FILENAME).write_text("{}", encoding="utf-8")
    (run_dir / OB_AUDIT_FILENAME).write_text("", encoding="utf-8")
    return run_dir


class AuditCase(unittest.TestCase):
    """A fresh valid bundle per test, with helpers to break one thing."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="audit-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run_dir = build_bundle(self.root)

    # -- helpers ------------------------------------------------------------

    def lines(self, name: str) -> list[str]:
        return (self.run_dir / name).read_text(
            encoding="utf-8").splitlines(keepends=True)

    def rewrite(self, name: str, lines: list[str]) -> None:
        (self.run_dir / name).write_text("".join(lines), encoding="utf-8")

    def failed_checks(self, report) -> set[str]:
        return {finding.check for finding in report.errors}

    def assertFailsWith(self, check: str):
        report = audit_run(self.run_dir)
        self.assertFalse(report.ok, "the corrupted bundle passed the audit")
        self.assertIn(check, self.failed_checks(report),
                      f"expected {check}; got {sorted(self.failed_checks(report))}")
        return report


# ==========================================================================
# The control
# ==========================================================================

class TestValidBundlePasses(AuditCase):

    def test_a_consistent_bundle_passes(self):
        report = audit_run(self.run_dir)
        self.assertTrue(
            report.ok,
            "a self-consistent bundle must pass: "
            + json.dumps([f.to_dict() for f in report.errors], indent=2))
        self.assertEqual(report.to_dict()["result"], "PASS")

    def test_the_report_carries_the_run_identity_and_counts(self):
        report = audit_run(self.run_dir)
        self.assertEqual(report.run_id, self.run_dir.name)
        self.assertEqual(report.stats["cycles"], 2)
        self.assertEqual(report.stats["ledger_rows"], 2)
        self.assertEqual(report.stats["alerts"], 1)
        self.assertEqual(report.stats["snapshots"], 2)
        self.assertEqual(report.stats["active_at_end"], 1)

    def test_a_directory_that_is_not_a_run_is_refused(self):
        empty = self.root / "not-a-run"
        empty.mkdir()
        with self.assertRaises(AuditError):
            audit_run(empty)

    def test_a_missing_directory_is_refused(self):
        with self.assertRaises(AuditError):
            audit_run(self.root / "absent")


# ==========================================================================
# The six required failure modes
# ==========================================================================

class TestCorruptedManifestFails(AuditCase):

    def test_editing_core_after_the_run_breaks_the_run_id(self):
        """The strongest identity check: `run_id` must recompute from `core`."""
        path = self.run_dir / MANIFEST_FILENAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["core"]["burn_in_h1"] = 336
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        self.assertFailsWith("M9")

    def test_a_missing_manifest_key_is_reported(self):
        path = self.run_dir / MANIFEST_FILENAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["declared_biases"]
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        report = self.assertFailsWith("M2")
        self.assertIn("B1", self.failed_checks(report))

    def test_a_key_smuggled_into_core_is_reported(self):
        path = self.run_dir / MANIFEST_FILENAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["core"]["caching_analyzer"] = True
        manifest["run_id"] = _run_id(manifest["core"])
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        report = audit_run(self.run_dir)
        self.assertFalse(report.ok)
        self.assertIn("M3", self.failed_checks(report))
        self.assertIn("O2", self.failed_checks(report))

    def test_an_unparseable_manifest_aborts_the_audit(self):
        (self.run_dir / MANIFEST_FILENAME).write_text("{oops", encoding="utf-8")
        with self.assertRaises(AuditError):
            audit_run(self.run_dir)

    def test_window_arithmetic_is_recomputed(self):
        path = self.run_dir / MANIFEST_FILENAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["cycles"] = 3
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        self.assertFailsWith("M5")


class TestMissingJsonlLineFails(AuditCase):

    def test_a_deleted_ledger_line_fails_the_count_and_the_cycle(self):
        lines = self.lines(LEDGER_FILENAME)
        self.rewrite(LEDGER_FILENAME, lines[:1])
        report = self.assertFailsWith("L1")
        self.assertIn("L4", self.failed_checks(report))

    def test_a_deleted_event_line_fails_the_per_cycle_count(self):
        lines = self.lines(LIFECYCLE_EVENTS_FILENAME)
        self.rewrite(LIFECYCLE_EVENTS_FILENAME, lines[1:])
        report = audit_run(self.run_dir)
        self.assertFalse(report.ok)
        self.assertIn("E1", self.failed_checks(report))

    def test_a_truncated_final_line_is_reported(self):
        """The signature of a run killed mid-write — the only way a missing
        line is detectable from the file alone."""
        path = self.run_dir / LEDGER_FILENAME
        text = path.read_text(encoding="utf-8")
        path.write_text(text[:-20], encoding="utf-8")
        self.assertFailsWith("J")

    def test_an_unparseable_line_is_reported_with_its_number(self):
        lines = self.lines(LEDGER_FILENAME)
        lines[1] = "{not json}\n"
        self.rewrite(LEDGER_FILENAME, lines)
        report = self.assertFailsWith("J")
        finding = next(f for f in report.errors if f.check == "J")
        self.assertEqual(finding.detail["examples"][0]["line"], 2)


class TestLifecycleMismatchFails(AuditCase):

    def test_an_insert_the_snapshot_does_not_reflect_fails_reconstruction(self):
        """Replaying the mutations must reproduce the recorded memory."""
        lines = self.lines(LIFECYCLE_EVENTS_FILENAME)
        row = json.loads(lines[0])
        row["mutation_payload"]["symbol"] = "BBBUSDT"
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(LIFECYCLE_EVENTS_FILENAME, lines)
        report = self.assertFailsWith("E7")
        detail = next(f for f in report.errors if f.check == "E7").detail
        self.assertIn(SYMBOL,
                      detail["examples"][0]["in_snapshot_not_reconstructed"])

    def test_an_evict_that_never_happened_fails_reconstruction(self):
        lines = self.lines(LIFECYCLE_EVENTS_FILENAME)
        row = json.loads(lines[2])
        row.update(kind=KIND_MUTATION, mutation_type="EvictMutation",
                   mutation_payload={"symbol": SYMBOL, "reason": "kill"},
                   evict_reason="kill")
        lines[2] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(LIFECYCLE_EVENTS_FILENAME, lines)
        self.assertFailsWith("E7")

    def test_a_broken_seq_sequence_is_reported(self):
        lines = self.lines(LIFECYCLE_EVENTS_FILENAME)
        row = json.loads(lines[1])
        row["seq"] = 7
        lines[1] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(LIFECYCLE_EVENTS_FILENAME, lines)
        self.assertFailsWith("E2")

    def test_a_value_outside_the_closed_vocabulary_is_reported(self):
        lines = self.lines(LIFECYCLE_EVENTS_FILENAME)
        row = json.loads(lines[0])
        row["mutation_type"] = "TeleportMutation"
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(LIFECYCLE_EVENTS_FILENAME, lines)
        self.assertFailsWith("E3")


class TestAlertOrphanFails(AuditCase):

    def test_an_alert_for_a_symbol_with_no_ledger_row_is_an_orphan(self):
        lines = self.lines(ALERTS_FILENAME)
        row = json.loads(lines[0])
        row["alert"]["symbol"] = "ZZZUSDT"
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(ALERTS_FILENAME, lines)
        report = self.assertFailsWith("A4")
        detail = next(f for f in report.errors if f.check == "A4").detail
        self.assertEqual(detail["examples"][0]["symbol"], "ZZZUSDT")

    def test_an_extra_alert_fails_the_manifest_and_per_cycle_counts(self):
        lines = self.lines(ALERTS_FILENAME)
        self.rewrite(ALERTS_FILENAME, lines + lines)
        report = self.assertFailsWith("A1")
        self.assertIn("A3", self.failed_checks(report))

    def test_an_alert_from_another_run_is_reported(self):
        lines = self.lines(ALERTS_FILENAME)
        row = json.loads(lines[0])
        row["run_id"] = "replay-ffffffffffffffff"
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(ALERTS_FILENAME, lines)
        self.assertFailsWith("A2")


class TestMemoryMismatchFails(AuditCase):

    def test_active_pois_disagreeing_with_the_final_snapshot_fails(self):
        (self.run_dir / ACTIVE_POIS_FILENAME).write_text(
            json.dumps({"BBBUSDT": _active_record()}, indent=4),
            encoding="utf-8")
        report = self.assertFailsWith("S5")
        detail = next(f for f in report.errors if f.check == "S5").detail
        self.assertEqual(detail["only_in_state"], ["BBBUSDT"])

    def test_a_same_symbol_different_record_disagreement_fails(self):
        record = _active_record()
        record["score"] = 4
        (self.run_dir / ACTIVE_POIS_FILENAME).write_text(
            json.dumps({SYMBOL: record}, indent=4), encoding="utf-8")
        report = self.assertFailsWith("S5")
        detail = next(f for f in report.errors if f.check == "S5").detail
        self.assertEqual(detail["symbols"], [SYMBOL])

    def test_a_killed_thesis_with_no_event_to_explain_it_fails(self):
        (self.run_dir / KILLED_THESIS_FILENAME).write_text(
            json.dumps({f"{SYMBOL}|long|CONTINUATION": float(T_0)}),
            encoding="utf-8")
        report = self.assertFailsWith("K2")
        self.assertIn("K3", self.failed_checks(report))

    def test_a_snapshot_over_max_open_positions_fails(self):
        path = self.run_dir / MEMORY_SNAPSHOTS_FILENAME
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        row = json.loads(lines[1])
        row["active"] = {f"S{i}USDT": _active_record() for i in range(11)}
        lines[1] = json.dumps(row, sort_keys=True) + "\n"
        path.write_text("".join(lines), encoding="utf-8")
        report = self.assertFailsWith("S4")
        self.assertIn("E7", self.failed_checks(report))


# ==========================================================================
# run_health, ledger lineage and provenance
# ==========================================================================

class TestHealthInvariants(AuditCase):

    def test_a_broken_c1508_identity_is_reported(self):
        lines = self.lines(HEALTH_FILENAME)
        row = json.loads(lines[0])
        row["guard_skips"] = 1
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(HEALTH_FILENAME, lines)
        self.assertFailsWith("H4")

    def test_a_swallowed_exception_fails_the_run(self):
        lines = self.lines(HEALTH_FILENAME)
        row = json.loads(lines[0])
        row.update(ledger_rows=0, swallowed_before_ledger=1,
                   swallowed_after_ledger=-1)
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(HEALTH_FILENAME, lines)
        self.assertFailsWith("H6")

    def test_a_gap_in_the_boundary_grid_is_reported(self):
        lines = self.lines(HEALTH_FILENAME)
        row = json.loads(lines[1])
        row["scan_T"] = T_0 + 2 * H1
        lines[1] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(HEALTH_FILENAME, lines)
        self.assertFailsWith("H2")

    def test_a_missing_health_line_fails_the_cycle_count(self):
        self.rewrite(HEALTH_FILENAME, self.lines(HEALTH_FILENAME)[:1])
        self.assertFailsWith("H1")

    def test_a_ttl_cross_check_mismatch_is_reported(self):
        lines = self.lines(HEALTH_FILENAME)
        row = json.loads(lines[0])
        row["ttl_log_lines"] = 1
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(HEALTH_FILENAME, lines)
        self.assertFailsWith("H7")


class TestLedgerAndProvenance(AuditCase):

    def test_a_duplicated_canonical_key_is_reported(self):
        lines = self.lines(LEDGER_FILENAME)
        self.rewrite(LEDGER_FILENAME, [lines[0], lines[0], lines[1]])
        report = self.assertFailsWith("L2")
        self.assertIn("L1", self.failed_checks(report))

    def test_a_row_from_another_strategy_version_is_reported(self):
        lines = self.lines(LEDGER_FILENAME)
        row = json.loads(lines[0])
        row["strategy_version"] = "0.0.1"
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(LEDGER_FILENAME, lines)
        self.assertFailsWith("I1")

    def test_a_rank_outside_the_universe_limit_is_reported(self):
        lines = self.lines(LEDGER_FILENAME)
        row = json.loads(lines[0])
        row["universe_rank"] = 99
        lines[0] = json.dumps(row, sort_keys=True) + "\n"
        self.rewrite(LEDGER_FILENAME, lines)
        self.assertFailsWith("L5")

    def test_a_missing_optimization_block_is_reported(self):
        path = self.run_dir / MANIFEST_FILENAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["optimizations"]
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        self.assertFailsWith("O1")

    def test_an_archived_strategy_version_is_a_warning_not_an_error(self):
        """A bundle produced by older code is not defective — but a reader must
        be told, or the result gets read as if it came from today's code."""
        path = self.run_dir / MANIFEST_FILENAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["core"]["strategy_version"] = "2.1.0"
        manifest["run_id"] = _run_id(manifest["core"])
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        # The directory name must follow the identity, as it would in a real run.
        moved = self.run_dir.parent / manifest["run_id"]
        self.run_dir.rename(moved)
        self.run_dir = moved
        for name in (LEDGER_FILENAME,):
            lines = self.lines(name)
            rows = []
            for line in lines:
                row = json.loads(line)
                row.update(run_id=manifest["run_id"], strategy_version="2.1.0")
                rows.append(json.dumps(row, sort_keys=True) + "\n")
            self.rewrite(name, rows)
        for name in (ALERTS_FILENAME, LIFECYCLE_EVENTS_FILENAME,
                     MEMORY_SNAPSHOTS_FILENAME):
            rows = []
            for line in self.lines(name):
                row = json.loads(line)
                row["run_id"] = manifest["run_id"]
                rows.append(json.dumps(row, sort_keys=True) + "\n")
            self.rewrite(name, rows)

        report = audit_run(self.run_dir)
        self.assertTrue(report.ok, [f.to_dict() for f in report.errors])
        self.assertIn("I4", {f.check for f in report.warnings})


# ==========================================================================
# Architecture
# ==========================================================================

class TestAuditIsSeparateFromReplay(unittest.TestCase):

    def test_the_audit_never_imports_the_replay_package(self):
        """Auditing a run must not require the machinery that produced it."""
        from tests.backtest.test_historical_adapter import imported_modules

        for module in imported_modules(Path("backtest") / "audit.py"):
            self.assertFalse(
                module.startswith("backtest.replay"),
                f"backtest/audit.py imports {module}")

    def test_the_audit_defines_no_strategy_logic(self):
        source = (Path("backtest") / "audit.py").read_text(encoding="utf-8")
        for forbidden in ("def score", "def reconcile", "def analyze"):
            self.assertNotIn(forbidden, source)

    def test_state_filenames_match_persistence(self):
        """The audit names the two state files itself rather than importing the
        production adapter. That duplication is pinned here so it cannot drift."""
        from v2.infra import persistence

        self.assertEqual(ACTIVE_POIS_FILENAME, persistence._ACTIVE_POIS_FILE)
        self.assertEqual(KILLED_THESIS_FILENAME, persistence._KILLED_THESIS_FILE)

    def test_alerts_filename_is_shared_with_the_writer(self):
        from backtest.replay.capture import ALERTS_FILENAME as writer_side

        self.assertEqual(ALERTS_FILENAME, writer_side)


class TestCli(AuditCase):
    """The command's contract: the report on stdout, the verdict in the exit
    code, and nothing written into the bundle."""

    def run_cli(self, *argv: str) -> int:
        """Invoke the CLI with both streams captured — a test suite should not
        print a run report for every case it exercises."""
        from backtest.cli import main

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(["audit", *argv])

    def test_the_command_exits_zero_on_a_clean_bundle(self):
        self.assertEqual(self.run_cli("--run", str(self.run_dir)), 0)

    def test_the_command_exits_one_on_a_failing_bundle(self):
        (self.run_dir / ACTIVE_POIS_FILENAME).write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_cli("--run", str(self.run_dir)), 1)

    def test_the_report_can_be_written_beside_the_bundle(self):
        out = self.root / "audit_report.json"
        self.assertEqual(
            self.run_cli("--run", str(self.run_dir), "--out", str(out)), 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["result"], "PASS")
        self.assertEqual(payload["run_id"], self.run_dir.name)

    def test_auditing_does_not_write_into_the_run_directory(self):
        before = sorted(p.name for p in self.run_dir.iterdir())
        digests = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        self.run_cli("--run", str(self.run_dir))
        self.assertEqual(before, sorted(p.name for p in self.run_dir.iterdir()))
        for name, blob in digests.items():
            self.assertEqual((self.run_dir / name).read_bytes(), blob, name)


if __name__ == "__main__":
    unittest.main()
