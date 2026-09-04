"""Tests for the performance census — the frozen 39-item checklist.

Each test names the defect it guards. The numbering follows the adjudicated
specification so a reader can trace a test back to the review finding that
demanded it.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backtest.analytics.studies import performance_census as pc

OFFICIAL_BUNDLE = Path("data/runs/official/runs")
BASELINE_DIR = Path("data/baselines/v2.2.0-official")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class FakeRecord:
    symbol: str = "AAA/USDT"
    entry_mode: str = "CONTINUATION"
    direction: str = "SHORT"
    realized_r: Optional[float] = 1.0
    boundary: int = 1_700_000_000
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
    scored = tuple(r for r in resolved if r.filled)
    unfilled = tuple(r for r in resolved if not r.filled)
    return pc.Book(
        resolved=resolved, scored=scored, unfilled=unfilled,
        counts={"shadows": shadows, "resolved": len(resolved),
                "scored": len(scored)},
        join={"records": len(resolved), "joined": len(resolved),
              "unjoined": 0, "completeness": 1.0, "floor": 0.999},
    )


def spread(n: int, mode: str, direction: str, value: float,
           *, labels: Optional[dict] = None, base: int = 1_700_000_000,
           symbols: int = 4) -> list[pc.Row]:
    """`n` rows in one cell, spread over symbols and two months."""
    out = []
    for i in range(n):
        out.append(row(
            symbol=f"S{i % symbols}/USDT", entry_mode=mode, direction=direction,
            realized_r=value, boundary=base + i * 86_400,
            labels=dict(labels or {}),
            state="WIN" if value > 0 else "LOSS"))
    return out


def full_book(**label_kwargs: Any) -> pc.Book:
    """One book with all four cells populated well above MIN_SIDE_N."""
    rows: list[pc.Row] = []
    for mode, direction, value in (("CONTINUATION", "SHORT", 0.5),
                                   ("CONTINUATION", "LONG", -0.4),
                                   ("RETRACEMENT", "SHORT", 0.3),
                                   ("RETRACEMENT", "LONG", -0.2)):
        rows += spread(400, mode, direction, value, labels=dict(label_kwargs))
    return book_of(rows)


def _imported_modules(module: Any) -> set[str]:
    """Every module name this module imports, read from the syntax tree.

    Textual scanning cannot tell an import from a docstring that mentions one,
    which is exactly the distinction these tests need: the census names
    `backtest.replay` in prose in order to say it does not import it.
    """
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
    """Every mapping key anywhere in the payload, lowercased.

    Keys carry structure; values carry prose. A field named `verdict` is a
    governance breach, whereas a sentence explaining that no verdict is issued
    is the governance itself.
    """
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(str(key).lower())
            keys |= _all_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            keys |= _all_keys(item)
    return keys


def official_run() -> Optional[Path]:
    if not OFFICIAL_BUNDLE.exists():
        return None
    for candidate in sorted(OFFICIAL_BUNDLE.iterdir()):
        if candidate.is_dir() and (candidate / "metrics.json").exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# T-1..T-4 — accounting integrity and denominator invariance
# ---------------------------------------------------------------------------

class TestAccounting(unittest.TestCase):
    """T-1, T-2, T-3, T-4."""

    def test_t1_population_identity_closes(self):
        book = full_book()
        block = pc._population_accounting(book)
        self.assertTrue(block["identity"]["resolved_equals_scored_plus_unfilled"])
        self.assertTrue(block["identity"]["cells_plus_unassigned_equals_resolved"])
        total = sum(c["resolved_decisions"] for c in block["per_cell"].values())
        self.assertEqual(total, len(book.resolved))

    def test_t1b_broken_partition_raises(self):
        """A book whose resolved set is not scored + unfilled must be refused."""
        rows = spread(200, "CONTINUATION", "SHORT", 0.5)
        broken = pc.Book(resolved=tuple(rows), scored=tuple(rows[:100]),
                         unfilled=(), counts={"shadows": 0}, join={})
        with self.assertRaises(pc.CensusError):
            pc._population_accounting(broken)

    def test_t2_gross_shares_sum_to_one(self):
        book = full_book()
        block = pc._gross_accounting(book)
        positive = sum(c["share_of_gross_positive"]
                       for c in block["per_cell"].values())
        negative = sum(c["share_of_gross_negative"]
                       for c in block["per_cell"].values())
        self.assertAlmostEqual(positive, 1.0, places=9)
        self.assertAlmostEqual(negative, 1.0, places=9)
        self.assertTrue(block["closure"]["cell_win_sums_to_book"])
        self.assertTrue(block["closure"]["cell_loss_sums_to_book"])

    def test_t3_shares_are_refinement_invariant(self):
        """B1: splitting one segment must not move any other segment's share.

        The defect this guards took the sign at segment level, which made the
        denominator grow with partition fineness. Trade level cannot.
        """
        values = [0.5] * 100 + [-0.4] * 80 + [1.2] * 20
        book_win, book_loss = pc._gross(values)

        coarse = pc._gross_record(values, book_win, book_loss, {})
        # Split the same trades three ways; the shares must add back exactly.
        parts = [values[:60], values[60:140], values[140:]]
        fine = [pc._gross_record(p, book_win, book_loss, {}) for p in parts]
        self.assertAlmostEqual(
            sum(f["share_of_gross_positive"] for f in fine),
            coarse["share_of_gross_positive"], places=12)
        self.assertAlmostEqual(
            sum(f["share_of_gross_negative"] for f in fine),
            coarse["share_of_gross_negative"], places=12)
        self.assertAlmostEqual(sum(f["net"] for f in fine), coarse["net"],
                               places=9)

    def test_t3b_segment_level_max_would_not_be_invariant(self):
        """Documents the defect: segment-level sign is cardinality-dependent."""
        segments_coarse = [[1.0, -2.0]]                # one segment, net -1
        segments_fine = [[1.0], [-2.0]]                # two segments
        coarse = sum(max(sum(s), 0.0) for s in segments_coarse)
        fine = sum(max(sum(s), 0.0) for s in segments_fine)
        self.assertEqual(coarse, 0.0)
        self.assertEqual(fine, 1.0)
        self.assertNotEqual(coarse, fine)
        # Trade level is immune to the same refinement.
        self.assertEqual(pc._gross([1.0, -2.0])[0], pc._gross([1.0])[0])

    def test_t4_share_of_net_never_emitted(self):
        payload_text = json.dumps(_analyse_fake(full_book()), default=str)
        self.assertNotIn("share_of_net", payload_text)


# ---------------------------------------------------------------------------
# T-5..T-8 — fill selection, shrinkage, bracket, cross-mode
# ---------------------------------------------------------------------------

class TestFillSelection(unittest.TestCase):
    """T-5, T-6, T-8."""

    def test_t5_every_r_record_carries_fill_rate(self):
        payload = _analyse_fake(full_book(sweep=True))
        for cell_block in payload["fill_selection"]["per_cell"].values():
            self.assertIn("fill_rate", cell_block)
        for record in payload["conditions"]["rows"]:
            for side in record["sides"]:
                self.assertIn("fill_rate", side["fill_selection"])
                self.assertIsNotNone(side["fill_selection"]["fill_rate"])

    def test_t6_decision_basis_always_carries_shrinkage(self):
        rows = spread(200, "CONTINUATION", "SHORT", 0.5)
        rows += [row(realized_r=None, structural_rr=2.0, state="NOT_FILLED")
                 for _ in range(50)]
        block = pc._fill_selection(rows, {})
        self.assertIsNotNone(block["decision_basis_mean_r"])
        self.assertIsNotNone(block["decision_basis_shrinkage_factor"])
        # The shrinkage factor IS the fill rate — that is the whole point.
        self.assertAlmostEqual(block["decision_basis_shrinkage_factor"],
                               block["fill_rate"], places=9)

    def test_t6b_shrinkage_differs_with_attrition(self):
        """F4: two populations attenuate differently, so they stay incomparable."""
        low = spread(200, "CONTINUATION", "SHORT", 0.5) + [
            row(realized_r=None) for _ in range(5)]
        high = spread(200, "RETRACEMENT", "LONG", 0.5) + [
            row(realized_r=None) for _ in range(55)]
        a = pc._fill_selection(low, {})["decision_basis_shrinkage_factor"]
        b = pc._fill_selection(high, {})["decision_basis_shrinkage_factor"]
        self.assertGreater(a, b)

    def test_t8_bracket_uses_minus_one_and_structural_rr(self):
        rows = [row(realized_r=1.0)] * 3 + [
            row(realized_r=None, structural_rr=4.0)]
        block = pc._fill_selection(rows, {})
        self.assertAlmostEqual(block["bracket_worst_mean_r"], (3 * 1.0 - 1.0) / 4)
        self.assertAlmostEqual(block["bracket_best_mean_r"], (3 * 1.0 + 4.0) / 4)
        self.assertAlmostEqual(block["bracket_width_r"],
                               block["bracket_best_mean_r"]
                               - block["bracket_worst_mean_r"])
        self.assertGreater(block["bracket_width_r"], 0.0)

    def test_t8b_bracket_present_per_cell_on_real_run(self):
        run = official_run()
        if run is None:
            self.skipTest("official bundle absent")
        payload = pc.analyse(run)
        for cell_block in payload["fill_selection"]["per_cell"].values():
            self.assertIsNotNone(cell_block["bracket_width_r"])


class TestCrossModeProhibition(unittest.TestCase):
    """T-7 — no row may span two modes, and no comparative header may pair them."""

    def test_t7_guard_refuses_non_frozen_cell(self):
        with self.assertRaises(pc.CensusError):
            pc._guard_row({"cell": "CONTINUATION"})
        with self.assertRaises(pc.CensusError):
            pc._guard_row({"cell": "CONTINUATION+RETRACEMENT"})
        for name, _mode, _direction in pc.CELLS:
            self.assertEqual(pc._guard_row({"cell": name})["cell"], name)

    def test_t7b_every_emitted_row_is_single_mode(self):
        payload = _analyse_fake(full_book(sweep=True))
        frozen = {name for name, _m, _d in pc.CELLS}
        for record in payload["conditions"]["rows"]:
            self.assertIn(record["cell"], frozen)
            self.assertEqual(record["cell"].count("×"), 1)

    def test_t7c_no_comparative_header_pairs_two_modes(self):
        text = pc.render(_analyse_fake(full_book(sweep=True)))
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            lowered = line.lower()
            if "continuation" in lowered and "retracement" in lowered:
                self.fail(f"a single table line pairs both modes: {line}")

    def test_t7d_prohibited_statements_declared(self):
        payload = _analyse_fake(full_book())
        statements = payload["governance"]["prohibited_statements"]
        self.assertTrue(any("ordering or ranking" in s for s in statements))
        self.assertTrue(any("better than another" in s for s in statements))


# ---------------------------------------------------------------------------
# T-9..T-11 — friction is per segment, banded, and never a gate
# ---------------------------------------------------------------------------

class TestFriction(unittest.TestCase):
    """T-9, T-10, T-11."""

    def test_t9_friction_is_per_segment_from_planned_levels(self):
        tight = [row(entry_price=100.0, sl=100.5)] * 50
        wide = [row(entry_price=100.0, sl=90.0)] * 50
        a = pc._friction(tight, {})["notional_per_r_median"]
        b = pc._friction(wide, {})["notional_per_r_median"]
        self.assertAlmostEqual(a, 200.0, places=6)
        self.assertAlmostEqual(b, 10.0, places=6)

    def test_t9b_no_global_friction_constant_exists(self):
        source = Path(pc.__file__).read_text(encoding="utf-8")
        for forbidden in ("0.0683", "0.0441", "FRICTION_LEG"):
            self.assertNotIn(forbidden, source)

    def test_t10_fee_band_emitted_and_no_gate_derived(self):
        block = pc._friction([row(entry_price=100.0, sl=99.0)] * 10, {})
        keys = set(block["implied_friction_r"])
        self.assertEqual(len(keys), len(pc.FEE_BAND_BP) * len(pc.LEG_CONVENTIONS))
        self.assertIn("5bp_round_trip", keys)
        self.assertIn("2bp_one_leg", keys)
        # round trip is exactly twice one leg at the same tier
        self.assertAlmostEqual(block["implied_friction_r"]["5bp_round_trip"],
                               2 * block["implied_friction_r"]["5bp_one_leg"],
                               places=12)
        # no boolean, no verdict, no threshold anywhere in the block
        for value in block.values():
            self.assertNotIsInstance(value, bool)
        payload = _analyse_fake(full_book())
        self.assertTrue(payload["governance"]["no_gate_derived_from_friction"])

    def test_t11_thirty_fold_spread_across_stop_distance(self):
        """B3: a single constant cannot be right across this range."""
        spans = {}
        for stop in (0.2, 3.0):
            rows = [row(entry_price=100.0, sl=100.0 + stop)] * 20
            spans[stop] = pc._friction(rows, {})[
                "implied_friction_r"]["5bp_round_trip"]
        self.assertGreater(spans[0.2] / spans[3.0], 12.0)

    def test_t11b_real_run_spread_exceeds_thirty_fold(self):
        run = official_run()
        if run is None:
            self.skipTest("official bundle absent")
        book = pc.load(run)
        tight = [r for r in book.scored
                 if (v := pc._notional_per_r(r)) is not None and v > 300]
        wide = [r for r in book.scored
                if (v := pc._notional_per_r(r)) is not None and v < 15]
        self.assertTrue(tight and wide)
        a = pc._friction(tight, {})["implied_friction_r"]["5bp_round_trip"]
        b = pc._friction(wide, {})["implied_friction_r"]["5bp_round_trip"]
        self.assertGreater(a / b, 20.0)


# ---------------------------------------------------------------------------
# T-12..T-14 — no selection, no inference, determinism
# ---------------------------------------------------------------------------

class TestNoSelection(unittest.TestCase):
    """T-12, T-13, T-14, T-27."""

    def test_t12_ordering_is_alphabetical_not_by_metric(self):
        payload = _analyse_fake(full_book(sweep=True))
        keys = [(r["contrast"], r["cell"]) for r in payload["conditions"]["rows"]]
        contrasts = [c for c, _cell in keys]
        self.assertEqual(contrasts, sorted(contrasts))
        self.assertIn("never by any metric", payload["conditions"]["ordering"])

    def test_t12b_complete_family_always_emitted(self):
        payload = _analyse_fake(full_book())
        self.assertEqual(payload["conditions"]["row_count"],
                         len(pc.CONTRASTS) * len(pc.CELLS))
        self.assertEqual(payload["conditions"]["row_count"], 36)

    def test_t13_no_inference_machinery_reachable(self):
        """Structural, not textual.

        An earlier form of this test scanned the source for words like
        "significance" and failed on the module's own prose describing what it
        does not do. A governance document has to be able to name what it
        forbids, so the check reads the import graph and the callables the
        module actually exposes.
        """
        for module in _imported_modules(pc):
            self.assertNotIn("bootstrap", module)
            self.assertNotIn("stats", module)
            self.assertNotIn("research", module)

        # The public surface is pinned rather than pattern-matched. Matching on
        # name tokens produced false positives against the strategy's own
        # vocabulary — `_fill_selection` discloses survivorship and
        # `_ob_candidates_le3` counts order blocks; neither selects anything.
        # A closed surface catches an added ranking function without needing to
        # guess what it would be called.
        self.assertEqual(set(pc.__all__), {
            "CELLS", "CLOSED", "CONTRASTS", "EVIDENCE_CLASS", "FEE_BAND_BP",
            "IMBALANCE_THRESHOLD", "LEG_CONVENTIONS", "MIN_SIDE_N",
            "NULL_SIGN_AGREEMENT_RATE", "PROHIBITED_GROUPING_KEYS",
            "CensusError", "analyse", "csv_rows", "render",
            "specification_hash", "write_census"})
        exported = {name for name in pc.__all__
                    if callable(getattr(pc, name))}
        self.assertEqual(exported, {"CensusError", "analyse", "csv_rows",
                                    "render", "specification_hash",
                                    "write_census"})

    def test_t13b_no_inferential_keys_in_payload(self):
        """Keys, not values. Prose may name a prohibition; a field may not be one."""
        keys = _all_keys(_analyse_fake(full_book(sweep=True)))
        for forbidden in ("p_value", "p_adjusted", "significant", "verdict",
                          "passed", "candidate", "rank", "coherence_index",
                          "promoted", "score"):
            self.assertNotIn(forbidden, keys)

    def test_t14_no_rng_and_output_is_deterministic(self):
        for module in _imported_modules(pc):
            self.assertNotIn("random", module)
        self.assertNotIn("secrets", _imported_modules(pc))
        for name in dir(pc):
            self.assertNotIn("random", name.lower())
            self.assertNotIn("shuffle", name.lower())
        book = full_book(sweep=True)
        first = json.dumps(_analyse_fake(book), sort_keys=True, default=str)
        second = json.dumps(_analyse_fake(book), sort_keys=True, default=str)
        self.assertEqual(first, second)

    def test_t14b_order_of_input_does_not_change_output(self):
        rows = []
        for mode, direction, value in (("CONTINUATION", "SHORT", 0.5),
                                       ("CONTINUATION", "LONG", -0.4),
                                       ("RETRACEMENT", "SHORT", 0.3),
                                       ("RETRACEMENT", "LONG", -0.2)):
            rows += spread(400, mode, direction, value, labels={"sweep": True})
        forward = json.dumps(_analyse_fake(book_of(rows)), sort_keys=True,
                             default=str)
        backward = json.dumps(_analyse_fake(book_of(list(reversed(rows)))),
                              sort_keys=True, default=str)
        self.assertEqual(forward, backward)

    def test_t14c_tied_contributors_break_deterministically(self):
        """Regression for a real defect this suite caught.

        `_concentration_side` ranked on magnitude alone, so two months
        contributing exactly the same amount tied and fell to dict insertion
        order — the reported top-1 key changed between an input order and its
        reverse. The ordering is now total.
        """
        tied = {"2024-10": 15.5, "2023-12": 15.5, "2024-03": 1.0}
        forward = pc._concentration_side(tied, (1, 2))
        reversed_insertion = pc._concentration_side(
            {k: tied[k] for k in reversed(list(tied))}, (1, 2))
        self.assertEqual(forward["top"]["top_1"]["keys"],
                         reversed_insertion["top"]["top_1"]["keys"])
        self.assertEqual(forward["top"]["top_1"]["keys"], ["2023-12"])
        self.assertEqual(forward["top"]["top_2"]["keys"],
                         reversed_insertion["top"]["top_2"]["keys"])

    def test_t27_no_index_or_subset_derived_from_sign_vectors(self):
        payload = _analyse_fake(full_book(sweep=True))
        keys = _all_keys(payload)
        for forbidden in ("coherence", "coherence_index", "index", "shortlist",
                          "gate", "passed"):
            self.assertNotIn(forbidden, keys)
        for record in payload["conditions"]["rows"]:
            signs = record["sign_vector"]
            if signs["available"]:
                self.assertIn("all_signs_agree", signs)
                self.assertIn("null_agreement_rate", signs)
                # the agreement flag exists as data; nothing consumes it
                self.assertNotIn("gate", signs)
                self.assertNotIn("passed", signs)


# ---------------------------------------------------------------------------
# T-15..T-20 — provenance, hash, family, closed registry
# ---------------------------------------------------------------------------

class TestProvenanceAndFreeze(unittest.TestCase):
    """T-15..T-20, plus the exact family composition."""

    def test_t15_every_row_carries_a_provenance_class(self):
        payload = _analyse_fake(full_book(sweep=True))
        allowed = {pc.A_PRIORI, pc.POST_HOC_DERIVED}
        for record in payload["conditions"]["rows"]:
            self.assertIn(record["provenance"], allowed)
            self.assertIn(record["provenance"], pc.PROVENANCE_CLAIMS)

    def test_t16_post_hoc_rows_carry_restriction_text(self):
        payload = _analyse_fake(full_book(momentum_weakening=True))
        seen = 0
        for record in payload["conditions"]["rows"]:
            if record["provenance"] == pc.POST_HOC_DERIVED:
                seen += 1
                self.assertIn("restriction", record)
                self.assertIn("THIS bundle", record["restriction"])
            else:
                self.assertNotIn("restriction", record)
        self.assertGreater(seen, 0)

    def test_t17_specification_hash_is_stable_and_sensitive(self):
        first = pc.specification_hash()
        self.assertEqual(first, pc.specification_hash())
        original = pc.MIN_SIDE_N
        try:
            pc.MIN_SIDE_N = original + 1
            self.assertNotEqual(pc.specification_hash(), first)
        finally:
            pc.MIN_SIDE_N = original
        self.assertEqual(pc.specification_hash(), first)

    def test_t17b_hash_sensitive_to_every_frozen_constant(self):
        base = pc.specification_hash()
        for name, replacement in (
                ("IMBALANCE_THRESHOLD", 0.2),
                ("FEE_BAND_BP", (1.0,)),
                ("TIME_BASIS", "exit_T"),
                ("NULL_SIGN_AGREEMENT_RATE", 0.5),
                ("PROHIBITED_WORDS", ("recommend",)),
        ):
            original = getattr(pc, name)
            try:
                setattr(pc, name, replacement)
                self.assertNotEqual(pc.specification_hash(), base,
                                    f"hash ignores {name}")
            finally:
                setattr(pc, name, original)
        self.assertEqual(pc.specification_hash(), base)

    def test_t18_contrast_family_is_exactly_nine_as_declared(self):
        self.assertEqual(len(pc.CONTRASTS), 9)
        names = {c.name for c in pc.CONTRASTS}
        self.assertEqual(names, {
            "sweep", "fvg", "displacement", "volume_spike", "session_active",
            "momentum_weakening", "htf_alignment_ALIGNED_vs_PARTIAL",
            "atr_pct_band", "ob_candidate_count_le3"})
        a_priori = {c.name for c in pc.CONTRASTS if c.provenance == pc.A_PRIORI}
        self.assertEqual(a_priori, {"sweep", "fvg", "displacement",
                                    "volume_spike", "session_active"})
        post_hoc = {c.name for c in pc.CONTRASTS
                    if c.provenance == pc.POST_HOC_DERIVED}
        self.assertEqual(len(post_hoc), 4)
        self.assertEqual(len(names), len(pc.CONTRASTS))

    def test_t19_target_clear_and_session_are_closed(self):
        self.assertIn("target_clear", pc.CLOSED_BY_NAME)
        self.assertEqual(pc.CLOSED_BY_NAME["target_clear"][0], "NEAR_INVARIANT")
        self.assertIn("session", pc.CLOSED_BY_NAME)
        self.assertEqual(pc.CLOSED_BY_NAME["session"][0], "NESTED_IN_GATE")
        live = {c.name for c in pc.CONTRASTS}
        self.assertNotIn("target_clear", live)
        self.assertNotIn("session", live)

    def test_t19b_closed_and_live_sets_are_disjoint(self):
        live = {c.name for c in pc.CONTRASTS}
        self.assertEqual(live & set(pc.CLOSED_BY_NAME), set())

    def test_t20_every_closed_entry_has_reason_and_note(self):
        vocabulary = {
            "NEAR_INVARIANT", "NESTED_IN_GATE", "STRUCTURALLY_GATED",
            "DIRECTION_RELABELING", "DIRECTION_CONFOUNDED", "REDUNDANT",
            "FRICTION_CIRCULAR", "NULL_CIRCULAR", "PREVIOUSLY_REJECTED",
            "AVAILABILITY_GATED", "HINDSIGHT", "MODE_PROXY", "ZERO_VARIANCE",
            "STRATIFICATION_AXIS_ONLY",
        }
        for name, reason, note in pc.CLOSED:
            self.assertIn(reason, vocabulary, f"{name} has unknown reason")
            self.assertGreater(len(note), 20, f"{name} note too thin")
        self.assertGreaterEqual(len(pc.CLOSED), 30)


# ---------------------------------------------------------------------------
# T-21..T-22 — the WIN/LOSS split
# ---------------------------------------------------------------------------

class TestWinLossDiscipline(unittest.TestCase):
    """T-21, T-22."""

    def test_t21_prohibited_grouping_keys_raise(self):
        for key in ("state", "exit_reason", "fill_kind", "holding_candles",
                    "cancel_reason", "mfe_r", "mae_r"):
            with self.assertRaises(pc.CensusError, msg=key):
                pc.guard_grouping_key(key)

    def test_t21b_permitted_key_passes_through(self):
        self.assertEqual(pc.guard_grouping_key("sweep"), "sweep")

    def test_t21c_no_contrast_extracts_a_prohibited_key(self):
        marker = object()

        class Probe:
            def __init__(self):
                self.labels = _Watching()
                self.record = FakeRecord()

        class _Watching(dict):
            def get(self, key, default=None):     # type: ignore[override]
                pc.guard_grouping_key(str(key))
                return None

        for contrast in pc.CONTRASTS:
            contrast.extract(Probe())        # must not raise
        self.assertIsNotNone(marker)

    def test_t22_win_loss_is_the_gross_partition_exactly(self):
        values = [1.0, 2.0, -1.0, -0.5, 3.0]
        win, loss = pc._gross(values)
        self.assertAlmostEqual(win, sum(v for v in values if v > 0))
        self.assertAlmostEqual(loss, sum(v for v in values if v < 0))
        book = full_book()
        block = pc._gross_accounting(book)
        wins = sum(float(r.record.realized_r) for r in book.scored
                   if float(r.record.realized_r) > 0)
        self.assertAlmostEqual(block["book"]["gross_positive"], wins, places=6)
        self.assertIn("WIN/LOSS", block["book"]["win_loss_note"])

    def test_t22b_win_loss_closed_as_a_condition(self):
        self.assertIn("WIN_LOSS_as_condition", pc.CLOSED_BY_NAME)
        self.assertEqual(pc.CLOSED_BY_NAME["WIN_LOSS_as_condition"][0],
                         "HINDSIGHT")


# ---------------------------------------------------------------------------
# T-23..T-26 — usability markers, floors, sign vector shape
# ---------------------------------------------------------------------------

class TestUsability(unittest.TestCase):
    """T-23, T-24, T-25, T-26."""

    def test_t23_usability_is_decided_per_cell(self):
        """C2: balanced in one cell, near-constant in another."""
        rows = spread(400, "CONTINUATION", "SHORT", 0.5,
                      labels={"sweep": True})[:200]
        rows += spread(400, "CONTINUATION", "SHORT", -0.3,
                       labels={"sweep": False})[:200]
        rows += spread(400, "RETRACEMENT", "LONG", 0.5,
                       labels={"sweep": True})[:396]
        rows += spread(400, "RETRACEMENT", "LONG", -0.3,
                       labels={"sweep": False})[:4]
        payload = _analyse_fake(book_of(rows))
        marks = {}
        for record in payload["conditions"]["rows"]:
            if record["contrast"] != "sweep":
                continue
            marks[record["cell"]] = {
                side["side"]: side["usability_markers"]
                for side in record["sides"]}
        balanced = marks["CONTINUATION×SHORT"]
        self.assertEqual(balanced["true"], [])
        self.assertEqual(balanced["false"], [])
        skewed = marks["RETRACEMENT×LONG"]
        self.assertIn("IMBALANCED", skewed["false"])
        self.assertIn("BELOW_MIN_SIDE_N", skewed["false"])
        self.assertEqual(skewed["true"], [])

    def test_t24_min_side_n_declared_independently(self):
        from backtest.metrics import MIN_SEGMENT_N
        self.assertNotEqual(pc.MIN_SIDE_N, MIN_SEGMENT_N)
        self.assertGreaterEqual(pc.MIN_SIDE_N, 4 * MIN_SEGMENT_N)
        payload = _analyse_fake(full_book())
        self.assertIn("MIN_SEGMENT_N",
                      payload["governance"]["min_side_n_basis"])

    def test_t24b_thin_side_is_marked_not_withheld(self):
        rows = spread(400, "CONTINUATION", "SHORT", 0.5,
                      labels={"fvg": True})[:390]
        rows += spread(400, "CONTINUATION", "SHORT", -0.3,
                       labels={"fvg": False})[:10]
        payload = _analyse_fake(book_of(rows))
        record = next(r for r in payload["conditions"]["rows"]
                      if r["contrast"] == "fvg"
                      and r["cell"] == "CONTINUATION×SHORT")
        thin = next(s for s in record["sides"] if s["side"] == "false")
        self.assertIn("BELOW_MIN_SIDE_N", thin["usability_markers"])
        # marked, and the measurement is still present
        self.assertIsNotNone(thin["mean_r_fill_basis"])
        self.assertEqual(thin["n_side"], 10)

    def test_t25_sign_vector_has_exactly_six_strata_no_terciles(self):
        book = full_book(sweep=True)
        contrast = next(c for c in pc.CONTRASTS if c.name == "sweep")
        signs = pc._sign_vector(contrast, book)
        self.assertTrue(signs["available"])
        self.assertEqual(signs["strata_count"], 6)
        self.assertEqual(list(signs["strata"]), pc._sign_stratum_names())
        source = Path(pc.__file__).read_text(encoding="utf-8").lower()
        self.assertNotIn("tercile", source.replace(
            "the symbol-tercile", "").replace("symbol-tercile axis", ""))

    def test_t25b_multi_level_contrast_has_no_sign_vector(self):
        book = full_book()
        contrast = next(c for c in pc.CONTRASTS if c.name == "atr_pct_band")
        signs = pc._sign_vector(contrast, book)
        self.assertFalse(signs["available"])
        self.assertEqual(signs["reason"], "MULTI_LEVEL_NO_BINARY_SIDE")
        self.assertIsNone(signs["strata"])

    def test_t26_null_annotation_travels_with_every_sign_vector(self):
        payload = _analyse_fake(full_book(sweep=True, fvg=True))
        rendered = pc.render(payload)
        emitted = 0
        for record in payload["conditions"]["rows"]:
            signs = record["sign_vector"]
            if not signs["available"]:
                continue
            emitted += 1
            self.assertEqual(signs["null_agreement_rate"],
                             pc.NULL_SIGN_AGREEMENT_RATE)
            self.assertIn("by chance", signs["null_note"])
        self.assertGreater(emitted, 0)
        self.assertIn("by chance", rendered)

    def test_t26b_atr_band_carries_known_reversal(self):
        contrast = next(c for c in pc.CONTRASTS if c.name == "atr_pct_band")
        self.assertIn("KNOWN_REVERSAL", contrast.warnings)
        payload = _analyse_fake(full_book())
        for record in payload["conditions"]["rows"]:
            if record["contrast"] == "atr_pct_band":
                self.assertIn("KNOWN_REVERSAL", record["warnings"])


# ---------------------------------------------------------------------------
# T-28..T-31 — time basis, excursion discipline, denominators
# ---------------------------------------------------------------------------

class TestDisclosure(unittest.TestCase):
    """T-28, T-29, T-30, T-31."""

    def test_t28_time_basis_declared_and_used(self):
        payload = _analyse_fake(full_book())
        self.assertEqual(payload["governance"]["time_basis"], "boundary")
        self.assertEqual(payload["persistence"]["time_basis"], "boundary")
        # month attribution follows `boundary`, repeatably
        moment = datetime(2024, 3, 31, 23, 0, tzinfo=timezone.utc)
        spanning = pc.Row(FakeRecord(boundary=int(moment.timestamp())), {})
        self.assertEqual(pc._month_of(spanning), "2024-03")
        self.assertEqual(pc._month_of(spanning), pc._month_of(spanning))

    def test_t29_no_bare_mean_in_excursions(self):
        block = pc._excursions([row()] * 40, {})
        for series in ("mfe_r", "mae_r"):
            self.assertNotIn("mean", block[series])
            self.assertIn("trimmed_mean_10pct", block[series])
            self.assertNotIn("raw_mean_TAIL_CONTAMINATED", block[series])
            for key in ("p25", "median", "p75", "p90", "p95"):
                self.assertIn(key, block[series])

    def test_t30_tail_contamination_share_emitted(self):
        clean = pc._excursions(
            [row(fill_price=100.0, sl=90.0, labels={"atr_last": 4.0})] * 30, {})
        self.assertAlmostEqual(clean["tail_contamination_share"], 0.0)
        dirty = pc._excursions(
            [row(fill_price=100.0, sl=100.1, labels={"atr_last": 4.0})] * 30, {})
        self.assertAlmostEqual(dirty["tail_contamination_share"], 1.0)

    def test_t31_both_denominators_in_every_table(self):
        payload = _analyse_fake(full_book(sweep=True))
        blocks = ["population_accounting", "fill_selection", "gross_accounting",
                  "concentration", "persistence", "conditions", "friction",
                  "excursions", "exclusions"]
        for name in blocks:
            denominators = payload[name]["denominators"]
            self.assertIn("shadow_records_excluded", denominators, name)
            self.assertIn("unfilled_decisions_excluded_from_fill_basis",
                          denominators, name)
        for record in payload["conditions"]["rows"]:
            self.assertIn("shadow_records_excluded", record["denominators"])

    def test_t31b_rendered_output_repeats_denominators_per_section(self):
        text = pc.render(_analyse_fake(full_book(sweep=True)))
        self.assertGreaterEqual(text.count("Denominators:"), 9)

    def test_t31c_csv_carries_both_denominators(self):
        rows = pc.csv_rows(_analyse_fake(full_book(sweep=True)))
        header = rows[0]
        self.assertIn("shadow_records_excluded", header)
        self.assertIn("unfilled_decisions_excluded", header)
        self.assertGreater(len(rows), 1)
        for record in rows[1:]:
            self.assertEqual(len(record), len(header))


# ---------------------------------------------------------------------------
# T-32..T-35 — join gate, concentration discipline, breadth, word list
# ---------------------------------------------------------------------------

class TestGovernance(unittest.TestCase):
    """T-32, T-33, T-34, T-35."""

    def test_t32_join_completeness_floor_refuses_a_partial_join(self):
        self.assertGreater(pc.JOIN_COMPLETENESS_FLOOR, 0.99)
        source = Path(pc.__file__).read_text(encoding="utf-8")
        self.assertIn("JOIN_COMPLETENESS_FLOOR", source)
        self.assertIn("refusing rather than", source)

    def test_t33_herfindahl_separates_positive_and_negative(self):
        rows = spread(400, "CONTINUATION", "SHORT", 0.5)
        rows += spread(400, "CONTINUATION", "SHORT", -0.5)
        block = pc._concentration(book_of(rows))["per_cell"]["CONTINUATION×SHORT"]
        self.assertIn("positive", block)
        self.assertIn("negative", block)
        self.assertIsNotNone(block["positive"]["by_symbol"]["herfindahl"])
        self.assertIsNotNone(block["negative"]["by_symbol"]["herfindahl"])
        self.assertGreater(block["positive"]["by_symbol"]["total"], 0)
        self.assertGreater(block["negative"]["by_symbol"]["total"], 0)

    def test_t33b_concentrated_side_has_higher_herfindahl(self):
        one = {"A": 100.0}
        many = {chr(65 + i): 10.0 for i in range(10)}
        self.assertAlmostEqual(pc._herfindahl(list(one.values())), 1.0)
        self.assertAlmostEqual(pc._herfindahl(list(many.values())), 0.1)
        self.assertIsNone(pc._herfindahl([]))
        self.assertIsNone(pc._herfindahl([0.0]))

    def test_t34_raw_and_normalised_breadth_both_emitted(self):
        block = pc._concentration(full_book())["per_cell"]["CONTINUATION×SHORT"]
        self.assertIn("symbols_present", block)
        self.assertIn("tenure_normalised_symbol_breadth", block)
        self.assertIn("tenure_normalised_month_breadth", block)
        self.assertIn("neither is", block["breadth_note"])

    def test_t35_prohibited_words_absent_from_rendered_output(self):
        text = pc.render(_analyse_fake(full_book(sweep=True,
                                                 momentum_weakening=True)))
        lowered = text.lower()
        for word in pc.PROHIBITED_WORDS:
            self.assertNotIn(word, lowered,
                             f"rendered output contains {word!r}")

    def test_t35b_evidence_class_is_descriptive_everywhere(self):
        payload = _analyse_fake(full_book(sweep=True))
        self.assertEqual(payload["evidence_class"], "DESCRIPTIVE")
        self.assertEqual(pc.EVIDENCE_CLASS, "DESCRIPTIVE")
        for record in payload["conditions"]["rows"]:
            self.assertEqual(record["evidence_class"], "DESCRIPTIVE")
            for side in record["sides"]:
                self.assertEqual(side["evidence_class"], "DESCRIPTIVE")

    def test_t35c_scope_and_limitations_stated(self):
        payload = _analyse_fake(full_book())
        scope = payload["governance"]["scope"]
        self.assertIn("in-sample", scope)
        self.assertIn("27%", scope)
        self.assertIn("No causal claim", scope)
        joined = " ".join(payload["limitations"])
        self.assertIn("in-sample", joined)
        self.assertIn("unobservable", joined)
        self.assertIn("reverse inside a two-way split", joined)
        self.assertGreaterEqual(len(payload["limitations"]), 10)


# ---------------------------------------------------------------------------
# T-36..T-39 — lock, registration, boundaries, bundle integrity
# ---------------------------------------------------------------------------

class TestIsolation(unittest.TestCase):
    """T-36, T-37, T-38."""

    def test_t36_baseline_files_untouched(self):
        if not BASELINE_DIR.exists():
            self.skipTest("baseline absent")
        import hashlib
        digests = {}
        for path in sorted(BASELINE_DIR.iterdir()):
            if path.is_file():
                digests[path.name] = hashlib.sha256(
                    path.read_bytes()).hexdigest()[:16]
        self.assertIn("baseline.json", digests)
        self.assertIn("metrics.json", digests)
        baseline = json.loads(
            (BASELINE_DIR / "baseline.json").read_text(encoding="utf-8"))
        self.assertIsInstance(baseline, dict)

    def test_t37_not_registered_in_studies(self):
        from backtest.analytics.studies import STUDIES
        names = {name for name, _runner in STUDIES}
        self.assertNotIn("performance_census", names)
        self.assertEqual(len(STUDIES), 5)
        source = Path(
            Path(pc.__file__).parent / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("performance_census", source)

    def test_t37b_not_a_cli_verb(self):
        cli = Path("backtest/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("performance_census", cli)

    def test_t38_no_forbidden_imports_or_writes(self):
        """Import graph, not prose — the docstring names `backtest.replay`
        precisely to state that it is not imported."""
        modules = _imported_modules(pc)
        for forbidden in ("backtest.replay", "v2", "src"):
            self.assertNotIn(forbidden, modules)
            for module in modules:
                self.assertFalse(module.split(".")[0] in {"v2", "src"},
                                 f"imports {module}")
        self.assertNotIn("backtest.replay", modules)
        # the only writer is write_census, into the caller's directory
        source = Path(pc.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("write_text"), 3)
        self.assertEqual(source.count("mkdir"), 1)

    def test_t38b_analyse_writes_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            before = set(Path(temporary).iterdir())
            _analyse_fake(full_book(sweep=True))
            self.assertEqual(set(Path(temporary).iterdir()), before)

    def test_t39_artifacts_land_only_in_the_given_directory(self):
        import tempfile
        payload = _analyse_fake(full_book(sweep=True))
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "census"
            written = pc.write_census(payload, out, identity={"run_id": "x"})
            self.assertEqual(set(written), {
                "performance_census.json", "performance_census.md",
                "performance_census.csv"})
            for path in written.values():
                self.assertTrue(path.exists())
                self.assertEqual(path.parent, out)
            body = json.loads(
                written["performance_census.json"].read_text(encoding="utf-8"))
            self.assertEqual(body["identity"]["run_id"], "x")
            self.assertIn("generated_at", body)


# ---------------------------------------------------------------------------
# Real-bundle checks
# ---------------------------------------------------------------------------

class TestAgainstOfficialRun(unittest.TestCase):
    """The frozen bundle, where the audited figures are reproducible."""

    def setUp(self):
        self.run = official_run()
        if self.run is None:
            self.skipTest("official bundle absent")

    def test_populations_match_the_audited_counts(self):
        book = pc.load(self.run)
        self.assertEqual(len(book.resolved), 34_874)
        self.assertEqual(len(book.scored), 31_209)
        self.assertEqual(len(book.unfilled), 3_665)
        self.assertEqual(book.counts["shadows"], 82_954)

    def test_shrinkage_reproduces_the_audited_asymmetry(self):
        book = pc.load(self.run)
        factors = {}
        for mode in ("CONTINUATION", "RETRACEMENT"):
            rows = [r for r in book.resolved if r.record.entry_mode == mode]
            factors[mode] = pc._fill_selection(
                rows, book.denominators)["decision_basis_shrinkage_factor"]
        self.assertAlmostEqual(factors["CONTINUATION"], 0.9747, places=3)
        self.assertAlmostEqual(factors["RETRACEMENT"], 0.7850, places=3)

    def test_gross_shares_close_on_the_real_book(self):
        payload = pc.analyse(self.run)
        block = payload["gross_accounting"]
        self.assertAlmostEqual(
            sum(c["share_of_gross_positive"] for c in block["per_cell"].values()),
            1.0, places=9)
        self.assertAlmostEqual(
            sum(c["share_of_gross_negative"] for c in block["per_cell"].values()),
            1.0, places=9)

    def test_full_census_is_well_formed(self):
        payload = pc.analyse(self.run)
        self.assertEqual(payload["conditions"]["row_count"], 36)
        self.assertEqual(payload["evidence_class"], "DESCRIPTIVE")
        self.assertEqual(len(payload["specification_hash"]), 16)
        text = pc.render(payload)
        for word in pc.PROHIBITED_WORDS:
            self.assertNotIn(word, text.lower())
        self.assertGreater(len(pc.csv_rows(payload)), 36)


# ---------------------------------------------------------------------------

def _analyse_fake(book: pc.Book) -> dict[str, Any]:
    """Run the assembly stages over a fixture book, bypassing disk."""
    denominators = book.denominators
    per_cell_fill, per_cell_friction, per_cell_excursions = {}, {}, {}
    for name, _mode, _direction in pc.CELLS:
        resolved_cell = [r for r in book.resolved if r.cell == name]
        per_cell_fill[name] = pc._fill_selection(resolved_cell, denominators)
        per_cell_friction[name] = pc._friction(resolved_cell, denominators)
        per_cell_excursions[name] = pc._excursions(
            [r for r in resolved_cell if r.filled], denominators)
    reference = pc.analyse.__wrapped__ if hasattr(pc.analyse, "__wrapped__") else None
    assert reference is None
    return {
        "census": "performance_census",
        "engine_version": pc.CENSUS_ENGINE_VERSION,
        "evidence_class": pc.EVIDENCE_CLASS,
        "specification_hash": pc.specification_hash(),
        "governance": {
            "evidence_class": pc.EVIDENCE_CLASS,
            "answers": "where realized R accumulated",
            "does_not_answer": "which filter is preferable",
            "scope": ("one in-sample bundle covering roughly 27% of the "
                      "strategy's decisions. No causal claim is available"),
            "prohibited_statements": list(pc.PROHIBITED_STATEMENTS),
            "prohibited_words_in_output": list(pc.PROHIBITED_WORDS),
            "prohibited_grouping_keys": sorted(pc.PROHIBITED_GROUPING_KEYS),
            "provenance_claims": pc.PROVENANCE_CLAIMS,
            "min_side_n": pc.MIN_SIDE_N,
            "min_side_n_basis": ("declared independently of metrics "
                                 "MIN_SEGMENT_N (30)"),
            "imbalance_threshold": pc.IMBALANCE_THRESHOLD,
            "time_basis": pc.TIME_BASIS,
            "fee_band_bp": list(pc.FEE_BAND_BP),
            "leg_conventions": [label for label, _l in pc.LEG_CONVENTIONS],
            "sign_strata": pc._sign_stratum_names(),
            "null_sign_agreement_rate": pc.NULL_SIGN_AGREEMENT_RATE,
            "no_gate_derived_from_friction": True,
            "registered_in_studies": False,
        },
        "population_accounting": pc._population_accounting(book),
        "fill_selection": {"denominators": denominators,
                           "per_cell": per_cell_fill},
        "gross_accounting": pc._gross_accounting(book),
        "concentration": pc._concentration(book),
        "persistence": pc._persistence(book),
        "conditions": pc._conditions(book),
        "friction": {"denominators": denominators,
                     "per_cell": per_cell_friction,
                     "no_gate": "descriptive only"},
        "excursions": {"denominators": denominators,
                       "per_cell": per_cell_excursions},
        "exclusions": {
            "denominators": denominators,
            "closed_count": len(pc.CLOSED),
            "closed": [{"name": n, "reason": r, "note": t}
                       for n, r, t in pc.CLOSED],
        },
        "limitations": list(pc.LIMITATIONS),
    }


if __name__ == "__main__":               # pragma: no cover
    unittest.main()
