"""Tests for the session decomposition.

Mirrors the census test families, reduced to what this module adds: the
session axis, the parent-cell-scoped shares, and quarterly persistence at the
frozen floor.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backtest.analytics.studies import performance_census as pc
from backtest.analytics.studies import session_decomposition as sd

OFFICIAL_BUNDLE = Path("data/runs/official/runs")
BASELINE_DIR = Path("data/baselines/v2.2.0-official")

#: A UTC epoch inside each session band, for fixtures.
HOUR_EPOCH = {
    "ASIA": 1_704_070_800,               # 2024-01-01 01:00 UTC
    "LONDON": 1_704_092_400,             # 2024-01-01 07:00 UTC
    "LONDON_NY_OVERLAP": 1_704_110_400,  # 2024-01-01 12:00 UTC
    "NEWYORK": 1_704_124_800,            # 2024-01-01 16:00 UTC
    "OFF": 1_704_142_800,                # 2024-01-01 21:00 UTC
}


@dataclass
class FakeRecord:
    symbol: str = "AAA/USDT"
    entry_mode: str = "CONTINUATION"
    direction: str = "SHORT"
    realized_r: Optional[float] = 1.0
    boundary: int = HOUR_EPOCH["ASIA"]
    entry_price: float = 100.0
    sl: float = 102.0
    tp: float = 96.0
    fill_price: Optional[float] = 100.0
    structural_rr: Optional[float] = 2.0
    mfe_r: Optional[float] = 1.5
    mae_r: Optional[float] = -0.5
    state: str = "WIN"
    run_id: str = "run"
    h1_last_ts: float = 0.0


def row(**kwargs: Any) -> pc.Row:
    labels = kwargs.pop("labels", {})
    labels.setdefault("atr_last", 4.0)
    return pc.Row(FakeRecord(**kwargs), labels)


def book_of(rows: list[pc.Row], shadows: int = 100) -> pc.Book:
    resolved = tuple(rows)
    return pc.Book(
        resolved=resolved,
        scored=tuple(entry for entry in resolved if entry.filled),
        unfilled=tuple(entry for entry in resolved if not entry.filled),
        counts={"shadows": shadows, "resolved": len(resolved)},
        join={"records": len(resolved), "joined": len(resolved),
              "unjoined": 0, "completeness": 1.0, "floor": 0.999},
    )


def spread(n: int, mode: str, direction: str, session: str, value: float,
           *, symbols: int = 4, quarters: int = 6) -> list[pc.Row]:
    """`n` rows in one (cell, session), spread over symbols and quarters."""
    base = HOUR_EPOCH[session]
    out = []
    for i in range(n):
        # step whole quarters so the hour-of-day, and thus the session, holds
        out.append(row(symbol=f"S{i % symbols}/USDT", entry_mode=mode,
                       direction=direction, realized_r=value,
                       boundary=base + (i % quarters) * 92 * 86_400,
                       state="WIN" if value > 0 else "LOSS"))
    return out


def full_book(per_session: int = 150) -> pc.Book:
    """All 20 combinations populated above MIN_SIDE_N."""
    rows: list[pc.Row] = []
    for mode, direction, value in (("CONTINUATION", "SHORT", 0.5),
                                   ("CONTINUATION", "LONG", -0.4),
                                   ("RETRACEMENT", "SHORT", 0.3),
                                   ("RETRACEMENT", "LONG", -0.2)):
        for session in sd.SESSION_NAMES:
            rows += spread(per_session, mode, direction, session, value)
    return book_of(rows)


def official_run() -> Optional[Path]:
    if not OFFICIAL_BUNDLE.exists():
        return None
    for candidate in sorted(OFFICIAL_BUNDLE.iterdir()):
        if candidate.is_dir() and (candidate / "metrics.json").exists():
            return candidate
    return None


def _imported_modules(module: Any) -> set[str]:
    import ast

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
    return found


def _all_keys(payload: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(str(key).lower())
            keys |= _all_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            keys |= _all_keys(item)
    return keys


# ---------------------------------------------------------------------------
# S-1..S-4 — the session axis itself
# ---------------------------------------------------------------------------

class TestSessionAxis(unittest.TestCase):

    def test_s1_definitions_come_from_the_registry(self):
        from backtest.metrics.segments import SESSIONS as REGISTRY

        self.assertIs(sd.SESSIONS, REGISTRY)
        self.assertEqual(sd.SESSIONS, (
            ("ASIA", 0, 7), ("LONDON", 7, 12), ("LONDON_NY_OVERLAP", 12, 16),
            ("NEWYORK", 16, 21), ("OFF", 21, 24)))
        self.assertEqual(sd.SESSION_NAMES,
                         ("ASIA", "LONDON", "LONDON_NY_OVERLAP", "NEWYORK",
                          "OFF"))

    def test_s2_bands_are_exhaustive_and_disjoint(self):
        covered: set[int] = set()
        for _name, start, end in sd.SESSIONS:
            span = set(range(start, end))
            self.assertEqual(covered & span, set(), "bands overlap")
            covered |= span
        self.assertEqual(covered, set(range(24)))

    def test_s2b_every_hour_maps_to_exactly_one_band(self):
        for hour in range(24):
            epoch = int(datetime(2024, 1, 1, hour, tzinfo=timezone.utc)
                        .timestamp())
            matched = [name for name, start, end in sd.SESSIONS
                       if start <= hour < end]
            self.assertEqual(len(matched), 1, f"hour {hour}")
            self.assertEqual(sd._session_of(row(boundary=epoch)), matched[0])

    def test_s3_boundary_hours_land_on_the_half_open_edge(self):
        for hour, expected in ((0, "ASIA"), (6, "ASIA"), (7, "LONDON"),
                               (11, "LONDON"), (12, "LONDON_NY_OVERLAP"),
                               (15, "LONDON_NY_OVERLAP"), (16, "NEWYORK"),
                               (20, "NEWYORK"), (21, "OFF"), (23, "OFF")):
            epoch = int(datetime(2024, 6, 15, hour, tzinfo=timezone.utc)
                        .timestamp())
            self.assertEqual(sd._session_of(row(boundary=epoch)), expected,
                             f"hour {hour}")

    def test_s4_accounting_asserts_the_partition(self):
        book = full_book()
        block = sd._session_accounting(book)
        self.assertTrue(block["identity"]["bands_cover_the_clock"])
        self.assertTrue(block["identity"]["bands_are_disjoint"])
        self.assertTrue(block["identity"]["counts_sum_to_resolved"])
        self.assertEqual(sum(block["per_session_resolved"].values()),
                         len(book.resolved))
        self.assertEqual(block["unassigned"], 0)

    def test_s4b_unassignable_record_raises(self):
        broken = book_of([row(boundary=None)])
        with self.assertRaises(sd.SessionDecompositionError):
            sd._session_accounting(broken)


# ---------------------------------------------------------------------------
# S-5..S-8 — the grid, scoped shares, and their closure
# ---------------------------------------------------------------------------

class TestGridAndShares(unittest.TestCase):

    def test_s5_grid_is_exactly_four_by_five(self):
        payload = sd.analyse.__wrapped__ if False else None
        self.assertIsNone(payload)
        grid = sd._grid(full_book())
        self.assertEqual(len(grid), 4)
        for cell in grid.values():
            self.assertEqual(len(cell), 5)
            self.assertEqual(list(cell), list(sd.SESSION_NAMES))

    def test_s5b_empty_combination_still_exists(self):
        rows = spread(150, "CONTINUATION", "SHORT", "ASIA", 0.5)
        grid = sd._grid(book_of(rows))
        self.assertEqual(len(grid["CONTINUATION×SHORT"]), 5)
        self.assertEqual(grid["CONTINUATION×SHORT"]["OFF"].resolved, ())

    def test_s6_session_shares_sum_to_one_within_each_cell(self):
        payload = _analyse_fake(full_book())
        for cell, block in payload["cells"].items():
            positive = sum(
                entry["gross"]["share_of_parent_gross_positive"] or 0.0
                for entry in block["sessions"])
            negative = sum(
                entry["gross"]["share_of_parent_gross_negative"] or 0.0
                for entry in block["sessions"])
            if block["cell_gross_positive"]:
                self.assertAlmostEqual(positive, 1.0, places=9, msg=cell)
            if block["cell_gross_negative"]:
                self.assertAlmostEqual(negative, 1.0, places=9, msg=cell)

    def test_s6b_closure_flags_agree_with_the_arithmetic(self):
        payload = _analyse_fake(full_book())
        for block in payload["cells"].values():
            closure = block["closure"]
            for flag in ("session_shares_sum_to_one_positive",
                         "session_shares_sum_to_one_negative"):
                if closure[flag] is not None:
                    self.assertTrue(closure[flag])
            self.assertTrue(closure["session_n_sums_to_cell"])

    def test_s7_shares_are_parent_scoped_not_book_scoped(self):
        """The renamed keys must be the only ones present."""
        payload = _analyse_fake(full_book())
        keys = _all_keys(payload)
        self.assertIn("share_of_parent_gross_positive", keys)
        self.assertIn("share_of_parent_gross_negative", keys)
        self.assertNotIn("share_of_gross_positive", keys)
        self.assertNotIn("share_of_gross_negative", keys)
        self.assertNotIn("share_of_net", keys)

    def test_s7b_a_cell_share_is_not_the_book_share(self):
        """A session holding all of a small cell reads 1.0, not its book weight."""
        rows = spread(150, "CONTINUATION", "SHORT", "ASIA", 0.5)
        rows += spread(600, "CONTINUATION", "LONG", "LONDON", 0.5)
        payload = _analyse_fake(book_of(rows))
        asia = next(entry for entry in payload["cells"]["CONTINUATION×SHORT"]
                    ["sessions"] if entry["session"] == "ASIA")
        self.assertAlmostEqual(
            asia["gross"]["share_of_parent_gross_positive"], 1.0, places=9)

    def test_s8_session_n_sums_to_evaluated_n(self):
        book = full_book()
        payload = _analyse_fake(book)
        total = sum(entry["evaluated_n"] for entry in payload["rows"])
        self.assertEqual(total, len(book.resolved))
        self.assertEqual(len(payload["rows"]), 20)


# ---------------------------------------------------------------------------
# S-9..S-11 — quarterly persistence at the frozen floor
# ---------------------------------------------------------------------------

class TestPersistence(unittest.TestCase):

    def test_s9_bucket_is_quarterly_at_the_frozen_floor(self):
        self.assertEqual(sd.PERSISTENCE_BUCKET, "quarter")
        self.assertEqual(sd.MIN_SIDE_N, pc.MIN_SIDE_N)
        self.assertEqual(sd.MIN_SIDE_N, 120)
        payload = _analyse_fake(full_book())
        for entry in payload["rows"]:
            self.assertEqual(entry["persistence"]["bucket"], "quarter")
            self.assertEqual(entry["persistence"]["floor"], 120)

    def test_s9b_no_second_floor_constant_is_introduced(self):
        source = Path(sd.__file__).read_text(encoding="utf-8")
        self.assertNotIn("MIN_QUARTER_N", source)
        self.assertNotIn("MIN_MONTH_N", source)
        self.assertNotIn("PERSISTENCE_FLOOR", source)
        # MIN_SIDE_N is imported, never redefined
        self.assertNotIn("MIN_SIDE_N =", source)

    def test_s10_unusable_quarters_are_named_not_dropped(self):
        segment = sd.Segment(
            cell="CONTINUATION×SHORT", session="ASIA",
            resolved=tuple(spread(130, "CONTINUATION", "SHORT", "ASIA", 0.5,
                                  quarters=2)
                           + spread(10, "CONTINUATION", "SHORT", "ASIA", -1.0,
                                    quarters=1)))
        block = sd._persistence(segment, {})
        self.assertGreater(block["quarters_present"], 0)
        named = {entry["quarter"] for entry in block["quarters_unusable"]}
        self.assertEqual(block["quarters_present"],
                         block["quarters_usable"] + len(named))
        for entry in block["quarters_unusable"]:
            self.assertEqual(entry["marker"], "BELOW_MIN_SIDE_N")
            self.assertIn("n", entry)
            self.assertIn("net", entry)

    def test_s10b_thin_quarter_is_excluded_from_the_tally_but_visible(self):
        rows = spread(240, "CONTINUATION", "SHORT", "ASIA", 0.5, quarters=2)
        rows += [row(boundary=HOUR_EPOCH["ASIA"] + 5 * 92 * 86_400,
                     realized_r=-1.0, state="LOSS") for _ in range(9)]
        segment = sd.Segment("CONTINUATION×SHORT", "ASIA", tuple(rows))
        block = sd._persistence(segment, {})
        self.assertEqual(block["quarters_usable"], 2)
        self.assertEqual(len(block["quarters_unusable"]), 1)
        self.assertEqual(block["quarters_unusable"][0]["n"], 9)
        # the thin quarter is negative but must not enter the positive tally
        self.assertEqual(block["quarters_positive"], 2)

    def test_s11_monthly_is_raw_accounting_not_persistence(self):
        segment = sd.Segment(
            "CONTINUATION×SHORT", "ASIA",
            tuple(spread(150, "CONTINUATION", "SHORT", "ASIA", 0.5)))
        block = sd._persistence(segment, {})
        monthly = block["monthly_raw_accounting"]
        self.assertIn("raw counts and net R only", monthly["note"])
        self.assertIn("months", monthly)
        for key in ("quarters_positive", "positive_quarter_share",
                    "longest_negative_run_quarters"):
            self.assertNotIn(key.replace("quarter", "month"), monthly)
        for entry in monthly["months"]:
            self.assertIn("below_floor", entry)


# ---------------------------------------------------------------------------
# S-12..S-15 — governance
# ---------------------------------------------------------------------------

class TestGovernance(unittest.TestCase):

    def test_s12_no_row_spans_two_cells_or_two_sessions(self):
        cells = {name for name, _m, _d in pc.CELLS}
        for bad in ({"cell": "CONTINUATION", "session": "ASIA"},
                    {"cell": "CONTINUATION×SHORT", "session": "ASIA+LONDON"},
                    {"cell": "ALL", "session": "ASIA"}):
            with self.assertRaises(sd.SessionDecompositionError):
                sd._guard_row(bad)
        payload = _analyse_fake(full_book())
        for entry in payload["rows"]:
            self.assertIn(entry["cell"], cells)
            self.assertIn(entry["session"], sd.SESSION_NAMES)

    def test_s12b_no_table_line_pairs_two_modes(self):
        text = sd.render(_analyse_fake(full_book()))
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            lowered = line.lower()
            if "continuation" in lowered and "retracement" in lowered:
                self.fail(f"a table line pairs both modes: {line}")

    def test_s13_no_ranking_and_sessions_stay_in_clock_order(self):
        payload = _analyse_fake(full_book())
        for block in payload["cells"].values():
            order = [entry["session"] for entry in block["sessions"]]
            self.assertEqual(order, list(sd.SESSION_NAMES))
        self.assertIn("never", payload["overview"]["ordering"])
        self.assertIn("clock order",
                      payload["governance"]["session_ordering"])

    def test_s13b_no_inferential_keys_in_payload(self):
        keys = _all_keys(_analyse_fake(full_book()))
        for forbidden in ("p_value", "p_adjusted", "significant", "verdict",
                          "passed", "candidate", "rank", "best", "worst",
                          "coherence", "promoted", "recommendation"):
            self.assertNotIn(forbidden, keys)

    def test_s14_prohibited_words_absent_from_rendered_output(self):
        text = sd.render(_analyse_fake(full_book())).lower()
        for word in pc.PROHIBITED_WORDS:
            self.assertNotIn(word, text, f"output contains {word!r}")

    def test_s14b_evidence_class_is_descriptive_everywhere(self):
        payload = _analyse_fake(full_book())
        self.assertEqual(payload["evidence_class"], "DESCRIPTIVE")
        for entry in payload["rows"]:
            self.assertEqual(entry["evidence_class"], "DESCRIPTIVE")
        for entry in payload["overview"]["rows"]:
            self.assertEqual(entry["evidence_class"], "DESCRIPTIVE")

    def test_s15_win_loss_is_accounting_only(self):
        for key in ("state", "exit_reason", "fill_kind", "cancel_reason"):
            with self.assertRaises(pc.CensusError):
                sd.guard_grouping_key(key)
        payload = _analyse_fake(full_book())
        for entry in payload["rows"]:
            block = entry["win_loss"]
            self.assertIn("closed as a conditioning key", block["note"])
            self.assertEqual(
                block["win_count"] + block["lose_count"] <= block["scored_n"],
                True)

    def test_s15b_win_loss_never_appears_as_a_grouping_key(self):
        payload = _analyse_fake(full_book())
        for entry in payload["rows"]:
            self.assertEqual(set(entry) & {"win", "loss", "outcome"}, set())
            self.assertIn("cell", entry)
            self.assertIn("session", entry)

    def test_s16_specification_hash_stable_and_sensitive(self):
        first = sd.specification_hash()
        self.assertEqual(first, sd.specification_hash())
        original = sd.PERSISTENCE_BUCKET
        try:
            sd.PERSISTENCE_BUCKET = "month"
            self.assertNotEqual(sd.specification_hash(), first)
        finally:
            sd.PERSISTENCE_BUCKET = original
        self.assertEqual(sd.specification_hash(), first)

    def test_s16b_hash_tracks_the_parent_specification(self):
        first = sd.specification_hash()
        original = pc.MIN_SIDE_N
        try:
            pc.MIN_SIDE_N = original + 1
            self.assertNotEqual(sd.specification_hash(), first,
                                "parent drift must move this hash")
        finally:
            pc.MIN_SIDE_N = original
        self.assertEqual(sd.specification_hash(), first)

    def test_s17_both_denominators_in_every_block(self):
        payload = _analyse_fake(full_book())
        for name in ("session_accounting", "overview"):
            denominators = payload[name]["denominators"]
            self.assertIn("shadow_records_excluded", denominators)
            self.assertIn("unfilled_decisions_excluded_from_fill_basis",
                          denominators)
        for entry in payload["rows"]:
            for block in ("win_loss", "concentration", "persistence",
                          "friction"):
                self.assertIn("shadow_records_excluded",
                              entry[block]["denominators"], block)

    def test_s17b_rendered_sections_each_repeat_denominators(self):
        text = sd.render(_analyse_fake(full_book()))
        self.assertGreaterEqual(text.count("Denominators:"), 7)

    def test_s18_every_row_carries_fill_rate_and_shrinkage(self):
        payload = _analyse_fake(full_book())
        for entry in payload["rows"]:
            self.assertIn("fill_rate", entry)
            self.assertIn("decision_basis_shrinkage_factor",
                          entry["fill_selection"])
            self.assertIn("bracket_width_r", entry["fill_selection"])


# ---------------------------------------------------------------------------
# S-19..S-22 — isolation and determinism
# ---------------------------------------------------------------------------

class TestIsolation(unittest.TestCase):

    def test_s19_no_rng_and_deterministic(self):
        for module in _imported_modules(sd):
            self.assertNotIn("random", module)
        book = full_book()
        first = json.dumps(_analyse_fake(book), sort_keys=True, default=str)
        second = json.dumps(_analyse_fake(book), sort_keys=True, default=str)
        self.assertEqual(first, second)

    def test_s19b_input_order_does_not_change_output(self):
        rows: list[pc.Row] = []
        for mode, direction, value in (("CONTINUATION", "SHORT", 0.5),
                                       ("RETRACEMENT", "LONG", -0.2)):
            for session in sd.SESSION_NAMES:
                rows += spread(150, mode, direction, session, value)
        forward = json.dumps(_analyse_fake(book_of(rows)), sort_keys=True,
                             default=str)
        backward = json.dumps(_analyse_fake(book_of(list(reversed(rows)))),
                              sort_keys=True, default=str)
        self.assertEqual(forward, backward)

    def test_s20_no_forbidden_imports(self):
        modules = _imported_modules(sd)
        self.assertNotIn("backtest.replay", modules)
        for module in modules:
            self.assertNotIn(module.split(".")[0], {"v2", "src"})
        self.assertEqual(Path(sd.__file__).read_text(
            encoding="utf-8").count("write_text"), 3)

    def test_s21_not_registered_anywhere(self):
        from backtest.analytics.studies import STUDIES

        names = {name for name, _runner in STUDIES}
        self.assertNotIn("session_decomposition", names)
        self.assertEqual(len(STUDIES), 5)
        registry = (Path(sd.__file__).parent / "__init__.py").read_text(
            encoding="utf-8")
        self.assertNotIn("session_decomposition", registry)
        cli = Path("backtest/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("session_decomposition", cli)

    def test_s21b_performance_census_is_unmodified_by_import(self):
        self.assertEqual(pc.MIN_SIDE_N, 120)
        self.assertEqual(len(pc.CONTRASTS), 9)
        self.assertEqual(pc.EVIDENCE_CLASS, "DESCRIPTIVE")

    def test_s22_artifacts_land_only_in_the_given_directory(self):
        import tempfile

        payload = _analyse_fake(full_book())
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "decomposition"
            written = sd.write_decomposition(payload, out,
                                             identity={"run_id": "x"})
            self.assertEqual(set(written), {
                "session_decomposition.json", "session_decomposition.md",
                "session_decomposition.csv"})
            for path in written.values():
                self.assertTrue(path.exists())
                self.assertEqual(path.parent, out)

    def test_s22b_csv_is_one_row_per_cell_session(self):
        rows = sd.csv_rows(_analyse_fake(full_book()))
        self.assertEqual(len(rows), 21)          # header + 20
        header = rows[0]
        self.assertIn("share_of_parent_gross_positive", header)
        self.assertIn("shadow_records_excluded", header)
        self.assertIn("quarters_usable", header)
        for record in rows[1:]:
            self.assertEqual(len(record), len(header))

    def test_s23_baseline_files_untouched(self):
        if not BASELINE_DIR.exists():
            self.skipTest("baseline absent")
        import hashlib

        digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                   for path in sorted(BASELINE_DIR.iterdir()) if path.is_file()}
        self.assertEqual(digests["baseline.json"], "bfbfea07a6871b08")
        self.assertEqual(digests["metrics.json"], "4b8a91e77df644ef")
        self.assertEqual(digests["report.md"], "5924072f406a69bf")
        self.assertEqual(digests["segments.csv"], "2a448e5690021fd7")


# ---------------------------------------------------------------------------
# Real bundle
# ---------------------------------------------------------------------------

class TestAgainstOfficialRun(unittest.TestCase):

    def setUp(self):
        self.run = official_run()
        if self.run is None:
            self.skipTest("official bundle absent")

    def test_session_counts_sum_to_evaluated_n(self):
        book = pc.load(self.run)
        block = sd._session_accounting(book)
        self.assertEqual(block["resolved_decisions"], 34_874)
        self.assertEqual(sum(block["per_session_resolved"].values()), 34_874)
        self.assertEqual(block["unassigned"], 0)
        self.assertEqual(block["per_session_resolved"], {
            "ASIA": 9_158, "LONDON": 8_241, "LONDON_NY_OVERLAP": 6_369,
            "NEWYORK": 8_027, "OFF": 3_079})

    def test_parent_cell_shares_close_on_the_real_book(self):
        payload = sd.analyse(self.run)
        for cell, block in payload["cells"].items():
            positive = sum(
                entry["gross"]["share_of_parent_gross_positive"] or 0.0
                for entry in block["sessions"])
            self.assertAlmostEqual(positive, 1.0, places=9, msg=cell)
            negative = sum(
                entry["gross"]["share_of_parent_gross_negative"] or 0.0
                for entry in block["sessions"])
            self.assertAlmostEqual(negative, 1.0, places=9, msg=cell)
            self.assertTrue(block["closure"]["session_n_sums_to_cell"])

    def test_quarterly_floor_behaviour_on_the_real_book(self):
        payload = sd.analyse(self.run)
        empty = sum(1 for entry in payload["rows"]
                    if entry["persistence"]["quarters_usable"] == 0)
        self.assertEqual(empty, 4, "expected 4 of 20 combinations to fail")
        for entry in payload["rows"]:
            block = entry["persistence"]
            self.assertEqual(
                block["quarters_present"],
                block["quarters_usable"] + len(block["quarters_unusable"]))

    def test_full_decomposition_is_well_formed(self):
        payload = sd.analyse(self.run)
        self.assertEqual(len(payload["rows"]), 20)
        self.assertEqual(len(payload["overview"]["rows"]), 20)
        self.assertEqual(len(payload["specification_hash"]), 16)
        text = sd.render(payload)
        for word in pc.PROHIBITED_WORDS:
            self.assertNotIn(word, text.lower())
        self.assertEqual(len(sd.csv_rows(payload)), 21)


# ---------------------------------------------------------------------------

def _analyse_fake(book: pc.Book) -> dict[str, Any]:
    """Run the assembly stages over a fixture book, bypassing disk."""
    denominators = book.denominators
    accounting = sd._session_accounting(book)
    grid = sd._grid(book)

    cells_out: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for cell_name, _mode, _direction in pc.CELLS:
        cell_rows = [entry for entry in book.scored if entry.cell == cell_name]
        cell_values = [float(entry.record.realized_r) for entry in cell_rows]
        cell_win, cell_loss = pc._gross(cell_values)
        session_rows = []
        for session in sd.SESSION_NAMES:
            record = sd._segment_record(grid[cell_name][session], cell_win,
                                        cell_loss, denominators)
            session_rows.append(record)
            rows.append(record)
        share_positive = sum(
            entry["gross"]["share_of_parent_gross_positive"] or 0.0
            for entry in session_rows)
        share_negative = sum(
            entry["gross"]["share_of_parent_gross_negative"] or 0.0
            for entry in session_rows)
        cells_out[cell_name] = {
            "denominators": denominators,
            "cell_scored_n": len(cell_rows),
            "cell_gross_positive": cell_win,
            "cell_gross_negative": cell_loss,
            "cell_net": cell_win + cell_loss,
            "sessions": session_rows,
            "closure": {
                "session_shares_sum_to_one_positive":
                    abs(share_positive - 1.0) < 1e-9 if cell_win else None,
                "session_shares_sum_to_one_negative":
                    abs(share_negative - 1.0) < 1e-9 if cell_loss else None,
                "session_n_sums_to_cell":
                    sum(entry["filled_n"] for entry in session_rows)
                    == len(cell_rows),
            },
        }

    return {
        "study": "session_decomposition",
        "engine_version": sd.DECOMPOSITION_ENGINE_VERSION,
        "evidence_class": pc.EVIDENCE_CLASS,
        "specification_hash": sd.specification_hash(),
        "parent_specification_hash": pc.specification_hash(),
        "governance": {
            "evidence_class": pc.EVIDENCE_CLASS,
            # The real strings, not paraphrases: the word-list screen must be
            # exercised by the fixture tests, not only by the slow real-run one.
            "answers": sd.ANSWERS_TEXT,
            "does_not_answer": sd.DOES_NOT_ANSWER_TEXT,
            "scope": sd.SCOPE_TEXT,
            "legal_comparison": sd.LEGAL_COMPARISON_TEXT,
            "prohibited_statements": list(sd.PROHIBITED_STATEMENTS),
            "prohibited_words_in_output": list(pc.PROHIBITED_WORDS),
            "prohibited_grouping_keys": sorted(pc.PROHIBITED_GROUPING_KEYS),
            "min_side_n": pc.MIN_SIDE_N,
            "persistence_bucket": sd.PERSISTENCE_BUCKET,
            "time_basis": pc.TIME_BASIS,
            "fee_band_bp": list(pc.FEE_BAND_BP),
            "leg_conventions": [label for label, _l in pc.LEG_CONVENTIONS],
            "session_ordering": sd.SESSION_ORDERING_TEXT,
            "registered_in_studies": False,
        },
        "session_accounting": accounting,
        "overview": sd._overview(cells_out, denominators),
        "cells": cells_out,
        "rows": rows,
        "limitations": list(sd.LIMITATIONS),
    }


if __name__ == "__main__":               # pragma: no cover
    unittest.main()
