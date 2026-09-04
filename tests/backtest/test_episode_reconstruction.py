"""Tests for the episode reconstruction reader.

Fast tests script `CapturedEvent` sequences directly (the same seam
`TradeIntentBuilder.build_from` uses) so no replay bundle is required. The
slow tests at the bottom run against the official frozen bundle, guarded and
skipped when it is absent.
"""
from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from backtest.analytics.studies import episode_reconstruction as er
from backtest.capture_schema import (
    KIND_ALERT,
    KIND_LOG,
    KIND_MUTATION,
    PHASE_END_OF_CYCLE,
    PHASE_SYMBOL,
    CapturedEvent,
)

OFFICIAL_BUNDLE = Path("data/runs/official/runs")
BASELINE_DIR = Path("data/baselines/v2.2.0-official")

RUN_ID = "run-test"
SOURCE = "replay"


def official_run() -> Optional[Path]:
    if not OFFICIAL_BUNDLE.exists():
        return None
    for candidate in sorted(OFFICIAL_BUNDLE.iterdir()):
        if candidate.is_dir() and (candidate / "metrics.json").exists():
            return candidate
    return None


def _imported_modules(module: Any) -> set:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    found: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
    return found


def _record(*, signal_id, direction="LONG", entry_mode="RETRACEMENT",
            decision="VALID", score=10) -> dict:
    return {
        "direction": direction, "score": score, "decision": decision,
        "entry_price": 1.0, "sl": 0.9, "tp": 1.2, "regime": "expansion_up",
        "timestamp": 0.0, "miss_count": 0, "status": "WATCH",
        "alert_sent_tier": decision, "alert_sent_ts": 0.0,
        "last_miss_h1_ts": 0.0, "last_ob_audit_h1_ts": 0.0,
        "entry_mode": entry_mode, "signal_id": signal_id,
    }


def _base(cycle_index: int, seq: int, symbol: Optional[str], scan_T: int,
          h1_last_ts: float, *, phase: str = PHASE_SYMBOL,
          kind: str = KIND_MUTATION, **overrides: Any) -> CapturedEvent:
    payload = dict(
        run_id=RUN_ID, source=SOURCE, cycle_index=cycle_index, scan_T=scan_T,
        h1_last_ts=h1_last_ts, seq=seq, symbol=symbol, universe_rank=1,
        phase=phase, kind=kind, mutation_type=None, mutation_payload=None,
        evict_reason=None, target_symbol=None, log_event_type=None,
        log_details=None, alert_kind=None, alert_tier=None,
        alert_payload=None, attribution="reconciler",
    )
    payload.update(overrides)
    return CapturedEvent(**payload)


class StreamBuilder:
    """Scripts a valid `(cycle_index, seq)`-ordered event stream, mirroring
    what `CaptureRecorder.end_cycle` actually assigns."""

    def __init__(self) -> None:
        self.events: list = []
        self._seq_by_cycle: dict = {}

    def _seq(self, cycle_index: int) -> int:
        seq = self._seq_by_cycle.get(cycle_index, 0)
        self._seq_by_cycle[cycle_index] = seq + 1
        return seq

    def _add(self, cycle_index, symbol, scan_T, h1_last_ts, **fields):
        self.events.append(_base(cycle_index, self._seq(cycle_index), symbol,
                                  scan_T, h1_last_ts, **fields))

    def insert(self, symbol, cycle_index, scan_T, *, h1_last_ts=None,
               signal_id=None, direction="LONG", entry_mode="RETRACEMENT",
               decision="VALID", alert="new") -> str:
        h1 = h1_last_ts if h1_last_ts is not None else float(scan_T - 3600)
        sid = signal_id or f"{symbol}|{entry_mode}|{h1}"
        self._add(cycle_index, symbol, scan_T, h1,
                   mutation_type="InsertMutation",
                   mutation_payload={"symbol": symbol,
                                      "record": _record(signal_id=sid,
                                                        direction=direction,
                                                        entry_mode=entry_mode,
                                                        decision=decision)})
        if alert:
            self._add(cycle_index, symbol, scan_T, h1, kind=KIND_ALERT,
                       alert_kind=alert, alert_tier=decision, alert_payload={})
        return sid

    def replace(self, target_symbol, symbol, cycle_index, scan_T, *,
                h1_last_ts=None, signal_id=None, direction="LONG",
                entry_mode="RETRACEMENT", decision="VALID",
                alert="new") -> str:
        h1 = h1_last_ts if h1_last_ts is not None else float(scan_T - 3600)
        sid = signal_id or f"{symbol}|{entry_mode}|{h1}"
        self._add(cycle_index, symbol, scan_T, h1,
                   mutation_type="ReplaceMutation", target_symbol=target_symbol,
                   mutation_payload={"target_symbol": target_symbol,
                                      "symbol": symbol,
                                      "record": _record(signal_id=sid,
                                                        direction=direction,
                                                        entry_mode=entry_mode,
                                                        decision=decision)})
        if alert:
            self._add(cycle_index, symbol, scan_T, h1, kind=KIND_ALERT,
                       alert_kind=alert, alert_tier=decision, alert_payload={})
        return sid

    def partial_refresh(self, symbol, cycle_index, scan_T, *, h1_last_ts=None,
                         decision="VALID", score=12):
        h1 = h1_last_ts if h1_last_ts is not None else float(scan_T - 3600)
        self._add(cycle_index, symbol, scan_T, h1,
                   mutation_type="PartialRefreshMutation",
                   mutation_payload={"symbol": symbol, "score": score,
                                      "decision": decision, "entry_price": 1.0,
                                      "sl": 0.9, "tp": 1.2,
                                      "regime": "expansion_up",
                                      "timestamp": float(scan_T)})

    def increment_miss(self, symbol, cycle_index, scan_T, *, h1_last_ts=None):
        h1 = h1_last_ts if h1_last_ts is not None else float(scan_T - 3600)
        self._add(cycle_index, symbol, scan_T, h1,
                   mutation_type="IncrementMissMutation",
                   mutation_payload={"symbol": symbol, "h1_last_ts": h1})

    def evict(self, symbol, reason, cycle_index, scan_T, *, h1_last_ts=None,
              phase=PHASE_SYMBOL):
        h1 = h1_last_ts if h1_last_ts is not None else float(scan_T - 3600)
        self._add(cycle_index, symbol, scan_T, h1, phase=phase,
                   mutation_type="EvictMutation", evict_reason=reason,
                   mutation_payload={"symbol": symbol, "reason": reason})

    def stale(self, symbol, cycle_index, scan_T, *, h1_last_ts=None):
        h1 = h1_last_ts if h1_last_ts is not None else float(scan_T - 3600)
        self._add(cycle_index, symbol, scan_T, h1, kind=KIND_LOG,
                   log_event_type="STALE", log_details={})

    def capped(self, symbol, cycle_index, scan_T, *, h1_last_ts=None):
        h1 = h1_last_ts if h1_last_ts is not None else float(scan_T - 3600)
        self._add(cycle_index, symbol, scan_T, h1, kind=KIND_LOG,
                   log_event_type="CAPPED",
                   log_details={"score": 5, "weakest_score": 9})

    def raw(self, event: CapturedEvent) -> None:
        self.events.append(event)

    def build(self) -> list:
        return list(self.events)


def episodes_by_symbol(result: "er.ReconstructionResult") -> dict:
    out: dict = {}
    for episode in result.episodes:
        out.setdefault(episode.symbol, []).append(episode)
    return out


# ---------------------------------------------------------------------------
# 1 — determinism
# ---------------------------------------------------------------------------

class TestDeterminism(unittest.TestCase):

    def _mixed_stream(self) -> list:
        stream = StreamBuilder()
        stream.insert("AAA/USDT", 0, 1_000_000_000)
        stream.increment_miss("AAA/USDT", 1, 1_000_003_600)
        stream.partial_refresh("AAA/USDT", 2, 1_000_007_200, decision="A+")
        stream.evict("AAA/USDT", "decay", 3, 1_000_010_800)
        stream.insert("BBB/USDT", 4, 1_000_014_400)
        return stream.build()

    def test_e1_reconstruction_is_deterministic(self):
        first = er.reconstruct(self._mixed_stream())
        second = er.reconstruct(self._mixed_stream())
        self.assertEqual([e.to_dict() for e in first.episodes],
                          [e.to_dict() for e in second.episodes])
        self.assertEqual(first.total_events, second.total_events)
        self.assertEqual(first.evictions, second.evictions)


# ---------------------------------------------------------------------------
# 2-4 — basic continuation semantics
# ---------------------------------------------------------------------------

class TestBasicLifecycle(unittest.TestCase):

    def test_e2_insert_creates_episode(self):
        stream = StreamBuilder()
        sid = stream.insert("AAA/USDT", 0, 1_000_000_000, direction="LONG",
                             entry_mode="RETRACEMENT", decision="VALID")
        result = er.reconstruct(stream.build())
        self.assertEqual(result.insertions, 1)
        self.assertEqual(len(result.episodes), 1)
        episode = result.episodes[0]
        self.assertEqual(episode.signal_id, sid)
        self.assertEqual(episode.symbol, "AAA/USDT")
        self.assertEqual(episode.direction, "LONG")
        self.assertEqual(episode.entry_mode, "RETRACEMENT")
        self.assertEqual(episode.thesis_key, "AAA/USDT|LONG|RETRACEMENT")
        self.assertEqual(episode.start_boundary, 1_000_000_000)
        self.assertEqual(episode.tier_at_start, "VALID")
        self.assertEqual(episode.tier_at_end, "VALID")
        self.assertTrue(episode.ever_alert_bearing)
        self.assertEqual(episode.end_reason, "STILL_RESIDENT_AT_END")

    def test_e3_partial_refresh_preserves_identity(self):
        stream = StreamBuilder()
        sid = stream.insert("AAA/USDT", 0, 1_000_000_000, decision="VALID")
        stream.increment_miss("AAA/USDT", 1, 1_000_003_600)
        stream.partial_refresh("AAA/USDT", 2, 1_000_007_200, decision="A+")
        result = er.reconstruct(stream.build())
        self.assertEqual(len(result.episodes), 1)
        episode = result.episodes[0]
        self.assertEqual(episode.signal_id, sid)              # unchanged
        self.assertEqual(episode.increment_miss_count, 1)     # unaffected
        self.assertEqual(episode.partial_refresh_count, 1)
        self.assertEqual(episode.tier_at_start, "VALID")
        self.assertEqual(episode.tier_at_end, "A+")

    def test_e4_increment_miss_counts_exactly(self):
        stream = StreamBuilder()
        stream.insert("AAA/USDT", 0, 1_000_000_000)
        stream.increment_miss("AAA/USDT", 1, 1_000_003_600)
        stream.increment_miss("AAA/USDT", 2, 1_000_007_200)
        stream.increment_miss("AAA/USDT", 3, 1_000_010_800)
        result = er.reconstruct(stream.build())
        episode = result.episodes[0]
        self.assertEqual(episode.increment_miss_count, 3)
        self.assertEqual(episode.first_miss_boundary, 1_000_003_600)
        self.assertEqual(episode.last_miss_boundary, 1_000_010_800)


# ---------------------------------------------------------------------------
# 5-6 — supersede
# ---------------------------------------------------------------------------

class TestSupersede(unittest.TestCase):

    def test_e5_same_thesis_higher_tier_supersedes(self):
        stream = StreamBuilder()
        first_sid = stream.insert("AAA/USDT", 0, 1_000_000_000,
                                   direction="LONG", entry_mode="RETRACEMENT",
                                   decision="VALID")
        second_sid = stream.insert("AAA/USDT", 5, 1_000_018_000,
                                    direction="LONG", entry_mode="RETRACEMENT",
                                    decision="A+")
        self.assertNotEqual(first_sid, second_sid)
        result = er.reconstruct(stream.build())
        self.assertEqual(result.supersedes, 1)
        self.assertEqual(len(result.episodes), 2)
        by_sid = {e.signal_id: e for e in result.episodes}
        self.assertEqual(by_sid[first_sid].end_reason, "SUPERSEDED")
        self.assertEqual(by_sid[first_sid].end_boundary, 1_000_018_000)
        self.assertEqual(by_sid[second_sid].end_reason, "STILL_RESIDENT_AT_END")

    def test_e6_different_thesis_same_symbol_supersedes(self):
        stream = StreamBuilder()
        first_sid = stream.insert("AAA/USDT", 0, 1_000_000_000,
                                   direction="LONG", entry_mode="RETRACEMENT")
        second_sid = stream.insert("AAA/USDT", 5, 1_000_018_000,
                                    direction="SHORT", entry_mode="CONTINUATION")
        result = er.reconstruct(stream.build())
        self.assertEqual(result.supersedes, 1)
        by_sid = {e.signal_id: e for e in result.episodes}
        self.assertEqual(by_sid[first_sid].end_reason, "SUPERSEDED")
        self.assertEqual(by_sid[second_sid].thesis_key,
                          "AAA/USDT|SHORT|CONTINUATION")


# ---------------------------------------------------------------------------
# 7-9 — termination paths
# ---------------------------------------------------------------------------

class TestTermination(unittest.TestCase):

    def test_e7_replace_terminates_target(self):
        stream = StreamBuilder()
        target_sid = stream.insert("AAA/USDT", 0, 1_000_000_000)
        new_sid = stream.replace("AAA/USDT", "BBB/USDT", 5, 1_000_018_000)
        result = er.reconstruct(stream.build())
        self.assertEqual(result.replacements, 1)
        by_sid = {e.signal_id: e for e in result.episodes}
        self.assertEqual(by_sid[target_sid].end_reason, "REPLACED")
        self.assertEqual(by_sid[target_sid].end_boundary, 1_000_018_000)
        self.assertEqual(by_sid[new_sid].symbol, "BBB/USDT")
        self.assertEqual(by_sid[new_sid].end_reason, "STILL_RESIDENT_AT_END")

    def test_e8_evict_reasons_map_exactly(self):
        for reason, expected in (("kill", "KILL"), ("decay", "DECAY"),
                                  ("ttl", "TTL")):
            with self.subTest(reason=reason):
                stream = StreamBuilder()
                stream.insert("AAA/USDT", 0, 1_000_000_000)
                stream.evict("AAA/USDT", reason, 1, 1_000_003_600)
                result = er.reconstruct(stream.build())
                self.assertEqual(result.episodes[0].end_reason, expected)
                self.assertEqual(result.evictions[reason], 1)

    def test_e9_end_of_stream_is_still_resident(self):
        stream = StreamBuilder()
        stream.insert("AAA/USDT", 0, 1_000_000_000)
        result = er.reconstruct(stream.build())
        self.assertEqual(result.active_at_end, 1)
        self.assertEqual(result.episodes[0].end_reason,
                          "STILL_RESIDENT_AT_END")
        self.assertEqual(result.episodes[0].end_boundary, 1_000_000_000)


# ---------------------------------------------------------------------------
# 10-11 — non-episodes
# ---------------------------------------------------------------------------

class TestNonEpisodes(unittest.TestCase):

    def test_e10_capped_creates_no_episode(self):
        stream = StreamBuilder()
        stream.capped("AAA/USDT", 0, 1_000_000_000)
        result = er.reconstruct(stream.build())
        self.assertEqual(result.episodes, ())
        self.assertEqual(result.insertions, 0)
        self.assertEqual(result.capped, 1)

    def test_e11_stale_creates_no_transition(self):
        stream = StreamBuilder()
        stream.stale("AAA/USDT", 0, 1_000_000_000)
        result = er.reconstruct(stream.build())
        self.assertEqual(result.episodes, ())
        self.assertEqual(result.total_events, 1)
        self.assertEqual(result.active_at_end, 0)


# ---------------------------------------------------------------------------
# 12-14 — invariant violations
# ---------------------------------------------------------------------------

class TestInvariants(unittest.TestCase):

    def test_e12_duplicate_signal_id_across_symbols_fails(self):
        stream = StreamBuilder()
        shared_sid = "AAA/USDT|RETRACEMENT|1000000000.0"
        stream.insert("AAA/USDT", 0, 1_000_000_000, signal_id=shared_sid)
        stream.insert("BBB/USDT", 1, 1_000_003_600, signal_id=shared_sid)
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct(stream.build())

    def test_e13_missing_signal_id_on_insertion_fails(self):
        event = _base(0, 0, "AAA/USDT", 1_000_000_000, 999_996_400.0,
                       mutation_type="InsertMutation",
                       mutation_payload={
                           "symbol": "AAA/USDT",
                           "record": _record(signal_id=None)})
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct([event])

    def test_e14_reopening_the_same_signal_id_on_one_symbol_fails(self):
        stream = StreamBuilder()
        sid = "AAA/USDT|RETRACEMENT|1000000000.0"
        stream.insert("AAA/USDT", 0, 1_000_000_000, signal_id=sid)
        stream.insert("AAA/USDT", 1, 1_000_003_600, signal_id=sid)
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct(stream.build())

    def test_e14b_partial_refresh_without_open_episode_fails(self):
        stream = StreamBuilder()
        stream.partial_refresh("AAA/USDT", 0, 1_000_000_000)
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct(stream.build())

    def test_e14c_increment_miss_without_open_episode_fails(self):
        stream = StreamBuilder()
        stream.increment_miss("AAA/USDT", 0, 1_000_000_000)
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct(stream.build())

    def test_e14d_evict_without_open_episode_fails(self):
        stream = StreamBuilder()
        stream.evict("AAA/USDT", "kill", 0, 1_000_000_000)
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct(stream.build())

    def test_e14e_out_of_order_events_fail(self):
        stream = StreamBuilder()
        stream.insert("AAA/USDT", 0, 1_000_000_000)
        events = stream.build()
        events.append(_base(0, 0, "BBB/USDT", 1_000_000_000, 999_996_400.0,
                             mutation_type=None, kind=KIND_LOG,
                             log_event_type="STALE"))
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct(events)

    def test_e14f_mixed_run_id_fails(self):
        stream = StreamBuilder()
        stream.insert("AAA/USDT", 0, 1_000_000_000)
        events = stream.build()
        events.append(_base(1, 0, "BBB/USDT", 1_000_003_600, 1_000_000_000.0,
                             run_id="other-run", kind=KIND_LOG,
                             log_event_type="STALE"))
        with self.assertRaises(er.ReconstructionError):
            er.reconstruct(events)


# ---------------------------------------------------------------------------
# 16 (fast half) — outcome join
# ---------------------------------------------------------------------------

class TestOutcomeJoin(unittest.TestCase):

    def _episode(self, signal_id: str) -> er.Episode:
        return er.Episode(
            signal_id=signal_id, symbol="AAA/USDT", direction="LONG",
            entry_mode="RETRACEMENT", thesis_key="AAA/USDT|LONG|RETRACEMENT",
            start_boundary=0, start_h1_last_ts=0.0, end_boundary=1,
            end_reason="STILL_RESIDENT_AT_END", increment_miss_count=0,
            partial_refresh_count=0, first_miss_boundary=None,
            last_miss_boundary=None, first_refresh_boundary=None,
            last_refresh_boundary=None, tier_at_start="VALID",
            tier_at_end="VALID", ever_alert_bearing=True,
        )

    def test_e16_join_is_exact_and_nothing_is_dropped(self):
        episodes = [self._episode("A"), self._episode("B"), self._episode("C")]
        joined = er.join_outcomes(episodes, {"A", "C"})
        self.assertEqual(len(joined), 3)
        flags = {e.signal_id: e.outcome_joined for e in joined}
        self.assertEqual(flags, {"A": True, "B": False, "C": True})


# ---------------------------------------------------------------------------
# 18 — isolation
# ---------------------------------------------------------------------------

class TestIsolation(unittest.TestCase):

    def test_e18_no_forbidden_imports(self):
        modules = _imported_modules(er)
        self.assertNotIn("backtest.replay", modules)
        for module in modules:
            self.assertNotIn(module.split(".")[0], {"v2", "src"})
        for name in ("performance_census", "session_decomposition",
                     "fill_null", "episode_inference", "exit_census"):
            self.assertNotIn(f"backtest.analytics.studies.{name}", modules)

    def test_e18b_not_registered_anywhere(self):
        from backtest.analytics.studies import STUDIES

        names = {name for name, _runner in STUDIES}
        self.assertNotIn("episode_reconstruction", names)
        registry = (Path(er.__file__).parent / "__init__.py").read_text(
            encoding="utf-8")
        self.assertNotIn("episode_reconstruction", registry)
        cli = Path("backtest/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("episode_reconstruction", cli)

    def test_e18c_no_rng(self):
        for module in _imported_modules(er):
            self.assertNotIn("random", module)

    def test_e18d_artifacts_land_only_in_the_given_directory(self):
        payload = {
            "run_id": "x", "source": "replay", "strategy_version": None,
            "outcome_config_hash": None, "data_snapshot_id": None,
            "reconstruction_specification_hash": er.specification_hash(),
            "evidence_class": er.EVIDENCE_CLASS, "scope": er.SCOPE_STATEMENT,
            "reconciliation": {}, "episodes": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "episodes"
            written = er.write_episode_reconstruction(payload, out)
            self.assertEqual(set(written), {
                "episode_reconstruction.json", "episode_reconstruction.md"})
            for path in written.values():
                self.assertTrue(path.exists())
                self.assertEqual(path.parent, out)

    def test_e19_baseline_files_untouched(self):
        if not BASELINE_DIR.exists():
            self.skipTest("baseline absent")
        digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                   for path in sorted(BASELINE_DIR.iterdir()) if path.is_file()}
        self.assertEqual(digests["baseline.json"], "bfbfea07a6871b08")
        self.assertEqual(digests["metrics.json"], "4b8a91e77df644ef")
        self.assertEqual(digests["report.md"], "5924072f406a69bf")
        self.assertEqual(digests["segments.csv"], "2a448e5690021fd7")


# ---------------------------------------------------------------------------
# Real bundle — 15, 16 (slow half), 17, 20
# ---------------------------------------------------------------------------

class TestAgainstOfficialRun(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.run_dir = official_run()
        if cls.run_dir is None:
            return
        cls.payload = er.analyse(cls.run_dir)

    def setUp(self):
        if self.run_dir is None:
            self.skipTest("official bundle absent")

    def test_15_exact_reconciliation_against_frozen_bundle(self):
        r = self.payload["reconciliation"]
        self.assertEqual(r["insertions"], 76_576)
        self.assertEqual(r["capped"], 41_625)
        self.assertEqual(r["active_at_end"], 10)
        self.assertEqual(r["replacements"], 23_292)
        self.assertEqual(r["evictions"]["kill"] + r["evictions"]["decay"]
                          + r["evictions"]["ttl"], 40_133)
        self.assertEqual(r["supersedes"], 13_141)
        self.assertEqual(r["episodes_reconstructed"], 76_576)
        self.assertEqual(
            r["insertions"] + r["capped"], 118_201,
            "insertions + capped must equal the total instantiation attempts")
        self.assertEqual(
            r["episodes_reconstructed"]
            - (r["replacements"] + r["supersedes"] + r["active_at_end"]
               + r["evictions"]["kill"] + r["evictions"]["decay"]
               + r["evictions"]["ttl"]),
            0,
            "every episode ends exactly once, by exactly one of the frozen "
            "end reasons")

    def test_16_outcome_join_is_1to1_on_the_real_bundle(self):
        r = self.payload["reconciliation"]
        self.assertEqual(r["outcomes_joined"] + r["episodes_without_outcomes"],
                          r["episodes_reconstructed"])
        self.assertGreater(r["outcomes_joined"], 0)
        # No episode is silently dropped from the ledger, joined or not.
        self.assertEqual(len(self.payload["episodes"]),
                          r["episodes_reconstructed"])

    def test_17_deterministic_byte_stable_except_generated_at(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = er.write_episode_reconstruction(
                self.payload, Path(temporary) / "a")
            second = er.write_episode_reconstruction(
                self.payload, Path(temporary) / "b")
            left = json.loads(
                first["episode_reconstruction.json"].read_text(encoding="utf-8"))
            right = json.loads(
                second["episode_reconstruction.json"].read_text(encoding="utf-8"))
            left.pop("generated_at")
            right.pop("generated_at")
            self.assertEqual(left, right)

    def test_20_cli_audit_passes_after_artifact_generation(self):
        from backtest.audit import audit_run

        out_dir = self.run_dir / "analytics" / "research" / er.DIRNAME
        er.write_episode_reconstruction(self.payload, out_dir)
        report = audit_run(self.run_dir)
        self.assertTrue(report.ok,
                         f"audit failed: {[f.to_dict() for f in report.errors]}")


if __name__ == "__main__":
    unittest.main()
