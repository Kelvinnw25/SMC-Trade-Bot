"""Phase 3.7: Entry stage — behavioral equivalence with V1.

V1 publishes `entry_ready`, `entry_source`, `entry_reason` and `entry_price` in
`h1_setup` on every path where it completes its setup, so all four are compared
field-by-field against the real `build_signal_context` across a deterministic
corpus covering every entry outcome V1 can produce.

Controlled frames and hand-built order blocks then exercise each branch: the
continuation displacement scan and micro-pullback test, the retracement OB gate,
the rotational-LONG guard, and the two non-actionable branches.

Also asserts the prior stages are untouched, and that Entry owns no risk.

No network, no Telegram, no filesystem writes.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from tests.regression.v1_recorded import SMCStrategy
from tests.analysis.test_order_block import (
    build_frame,
    corpus,
    htf_context,
    set_bar,
    snapshot_of,
    snapshot_with,
)
from tests.regression.snapshot import InputSnapshot
from v2.analysis.context import AnalysisContext
from v2.analysis.entry import (
    CONTINUATION_LOOKBACK,
    MICRO_RETRACE_ATR_MULT,
    EntryStage,
    plan_entry,
    retracement_ob_valid,
)
from v2.analysis.fair_value_gap import FairValueGapStage
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import LiquidityStage, detect_sweep
from v2.analysis.order_block import OrderBlockStage, fresh_ob
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.regime import RegimeStage
from v2.analysis.structure import (
    STRUCTURE_PROMINENCE_MULT,
    STRUCTURE_SWING_WINDOW,
    StructureStage,
    identify_swings,
    to_swings,
)
from v2.domain import EntryPlan, FairValueGap, HTFContext, LiquiditySweep, OrderBlock

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "fixtures"
    / "synthetic_short.json"
)

ENTRY_FIELDS = ("entry_ready", "entry_source", "entry_reason", "entry_price")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def displacement_bar(df: pd.DataFrame, i: int, *, direction: str, close: float):
    """A bar passing V1's displacement test (body >= 0.6*atr, ratio >= 0.5)."""
    if direction == "LONG":
        return set_bar(df, i, open_=close - 1.0, close=close,
                       high=close + 0.1, low=close - 1.05)
    return set_bar(df, i, open_=close + 1.0, close=close,
                   high=close + 1.05, low=close - 0.1)


def block(*, top: float, bottom: float, fresh: bool = True, selected: bool = True,
          passed: bool = True, index: int = 25) -> OrderBlock:
    return OrderBlock(
        index=index, rank_from_last=1, top=top, bottom=bottom,
        size=abs(top - bottom), fresh_status=fresh,
        passed_quality_filter=passed, is_selected=selected,
        size_atr=abs(top - bottom), depth_from_price=1.0,
    )


def rotational_conflict_htf(location: str = "premium") -> HTFContext:
    """weekly BULLISH + D1/H4 BEARISH — V1's `htf_conflict_long` shape."""
    return HTFContext(
        pdh=110.0, pdl=90.0, daily_eq=100.0,
        weekly_bias="BULLISH", d1_bias="BEARISH", h4_bias="BEARISH",
        location=location, current_price=100.0, momentum_weakening=False,
        bull=8.5, bear=0.0, direction_source="weighted_bull",
        anchor_override=False, veto_active=False, direction="LONG",
    )


def entry_of(plan: EntryPlan) -> tuple:
    return tuple(getattr(plan, f) for f in ENTRY_FIELDS)


def v1_entry_of(h1_setup: dict) -> tuple:
    return tuple(h1_setup[f] for f in ENTRY_FIELDS)


def v2_entry(d1: pd.DataFrame, h1: pd.DataFrame) -> EntryPlan:
    """Run the real pipeline prefix and return the entry plan."""
    snapshot = snapshot_of(d1, h1)
    context = AnalysisContext.initial(snapshot)
    context = context.with_(htf=HTFStage.analyze(snapshot))
    context = context.with_(regime=RegimeStage.analyze(snapshot, context.htf))
    context = StructureStage().run(context)
    context = OrderBlockStage().run(context)
    return EntryStage().run(context).entry


def retracement_plan(direction="LONG", *, blocks=None, frame=None, atr_last=1.0,
                     htf=None, regime_label="rotational") -> EntryPlan:
    return plan_entry(
        "RETRACEMENT", direction, frame if frame is not None else build_frame(),
        atr_last,
        order_blocks=blocks,
        htf=htf if htf is not None else htf_context(direction),
        regime_label=regime_label,
    )


def continuation_plan(direction, frame, atr_last=1.0) -> EntryPlan:
    return plan_entry(
        "CONTINUATION", direction, frame, atr_last,
        order_blocks=None, htf=htf_context(direction), regime_label="expansion_up",
    )


# --------------------------------------------------------------------------
# V1 equivalence
# --------------------------------------------------------------------------

class TestEntryMatchesV1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        strategy = SMCStrategy()
        cls.cases = []
        for name, d1, h1 in corpus():
            cls.cases.append((name, d1, h1, strategy.build_signal_context(d1, h1)))

    def test_matches_v1_h1_setup_field_for_field(self):
        compared = 0
        for name, d1, h1, v1 in self.cases:
            h1_setup = v1.get("h1_setup")
            if not h1_setup:
                continue
            compared += 1
            with self.subTest(frame=name):
                self.assertEqual(entry_of(v2_entry(d1, h1)), v1_entry_of(h1_setup))
        self.assertGreater(compared, 20)

    def test_matches_v1_debug_conf_ready_and_guard(self):
        for name, d1, h1, v1 in self.cases:
            conf = (v1.get("debug") or {}).get("conf")
            if not conf:
                continue
            with self.subTest(frame=name):
                plan = v2_entry(d1, h1)
                self.assertEqual(plan.entry_ready, conf["ready"])
                self.assertEqual(plan.entry_reason, conf["guard"])

    def test_corpus_covers_every_reachable_entry_outcome(self):
        outcomes = set()
        for _, _d1, _h1, v1 in self.cases:
            h1_setup = v1.get("h1_setup") or {}
            if h1_setup:
                outcomes.add((h1_setup["entry_mode"], h1_setup["entry_reason"]))
        for expected in (
            ("CONTINUATION", "continuation_micro_pullback"),
            ("CONTINUATION", "no_continuation_displacement"),
            ("CONTINUATION", "no_micro_pullback"),
            ("RETRACEMENT", "no_entry"),          # successful retracement entry
            ("NONE", "non_actionable_regime"),
        ):
            with self.subTest(outcome=expected):
                self.assertIn(expected, outcomes)

    def test_matches_v1_on_the_fixture(self):
        # The fixture is the remaining outcome: a failed retracement gate.
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        self.assertEqual(v1["h1_setup"]["entry_reason"], "ob_quality_failed")
        self.assertEqual(
            entry_of(v2_entry(snapshot.d1, snapshot.h1)), v1_entry_of(v1["h1_setup"])
        )

    def test_is_deterministic(self):
        _, d1, h1 = corpus()[0]
        self.assertEqual(v2_entry(d1, h1), v2_entry(d1, h1))

    def test_v1_parameters_preserved(self):
        self.assertEqual((CONTINUATION_LOOKBACK, MICRO_RETRACE_ATR_MULT), (3, 0.10))


# --------------------------------------------------------------------------
# Continuation entry
# --------------------------------------------------------------------------

class TestContinuationEntry(unittest.TestCase):
    def long_frame(self):
        df = build_frame()
        displacement_bar(df, 28, direction="LONG", close=101.0)
        # Pullback: dips to within 0.10*atr of the displacement close, then
        # closes back above it.
        return set_bar(df, 29, open_=101.0, close=101.2, high=101.3, low=100.8)

    def short_frame(self):
        df = build_frame()
        displacement_bar(df, 28, direction="SHORT", close=99.0)
        return set_bar(df, 29, open_=99.0, close=98.8, high=99.2, low=98.5)

    def test_long_micro_pullback(self):
        plan = continuation_plan("LONG", self.long_frame())
        self.assertTrue(plan.entry_ready)
        self.assertEqual(plan.entry_source, "continuation_micro_pullback")
        self.assertEqual(plan.entry_reason, "continuation_micro_pullback")
        self.assertEqual(plan.entry_price, 101.2)
        self.assertEqual(plan.displacement_index, 28)
        self.assertEqual(plan.pullback_index, 29)

    def test_short_micro_pullback(self):
        plan = continuation_plan("SHORT", self.short_frame())
        self.assertTrue(plan.entry_ready)
        self.assertEqual(plan.entry_price, 98.8)
        self.assertEqual((plan.displacement_index, plan.pullback_index), (28, 29))

    def test_no_displacement(self):
        plan = continuation_plan("LONG", build_frame())
        self.assertFalse(plan.entry_ready)
        self.assertEqual(plan.entry_reason, "no_continuation_displacement")
        self.assertEqual(plan.entry_source, "no_continuation_displacement")
        self.assertEqual(plan.entry_price, 0.0)
        self.assertIsNone(plan.displacement_index)
        self.assertIsNone(plan.pullback_index)

    def test_reclaim_failure_is_no_micro_pullback(self):
        df = build_frame()
        displacement_bar(df, 28, direction="LONG", close=101.0)
        set_bar(df, 29, open_=101.0, close=100.5, high=101.1, low=100.4)
        plan = continuation_plan("LONG", df)
        self.assertFalse(plan.entry_ready)
        self.assertEqual(plan.entry_reason, "no_micro_pullback")
        self.assertEqual(plan.displacement_index, 28)
        self.assertIsNone(plan.pullback_index)

    def test_micro_retrace_failure_is_no_micro_pullback(self):
        # Closes above the displacement but never dips within 0.10*atr of it.
        df = build_frame()
        displacement_bar(df, 28, direction="LONG", close=101.0)
        set_bar(df, 29, open_=101.6, close=101.9, high=102.0, low=101.5)
        plan = continuation_plan("LONG", df)
        self.assertFalse(plan.entry_ready)
        self.assertEqual(plan.entry_reason, "no_micro_pullback")

    def test_micro_retrace_boundary(self):
        # pb_low <= disp_close + atr*0.10 -> 101.10 passes, 101.11 does not.
        for low, expected in ((101.10, True), (101.11, False)):
            with self.subTest(low=low):
                df = build_frame()
                displacement_bar(df, 28, direction="LONG", close=101.0)
                set_bar(df, 29, open_=101.2, close=101.3, high=101.4, low=low)
                self.assertEqual(continuation_plan("LONG", df).entry_ready, expected)

    def test_reclaim_boundary_is_inclusive(self):
        # pb_close >= disp_close, so an exactly-equal close still enters.
        df = build_frame()
        displacement_bar(df, 28, direction="LONG", close=101.0)
        set_bar(df, 29, open_=100.9, close=101.0, high=101.1, low=100.8)
        self.assertTrue(continuation_plan("LONG", df).entry_ready)

    def test_newest_displacement_wins(self):
        df = build_frame()
        displacement_bar(df, 27, direction="LONG", close=100.5)
        displacement_bar(df, 28, direction="LONG", close=101.0)
        set_bar(df, 29, open_=101.0, close=101.2, high=101.3, low=100.8)
        self.assertEqual(continuation_plan("LONG", df).displacement_index, 28)

    def test_last_bar_is_not_scanned_for_displacement(self):
        df = build_frame()
        displacement_bar(df, 29, direction="LONG", close=101.0)
        self.assertEqual(
            continuation_plan("LONG", df).entry_reason, "no_continuation_displacement"
        )

    def test_displacement_older_than_lookback_ignored(self):
        df = build_frame()
        displacement_bar(df, 25, direction="LONG", close=101.0)  # window starts at 26
        self.assertEqual(
            continuation_plan("LONG", df).entry_reason, "no_continuation_displacement"
        )

    def test_oldest_bar_in_lookback_is_scanned(self):
        df = build_frame()
        displacement_bar(df, 26, direction="LONG", close=101.0)
        set_bar(df, 27, open_=101.0, close=101.2, high=101.3, low=100.8)
        plan = continuation_plan("LONG", df)
        self.assertEqual((plan.displacement_index, plan.pullback_index), (26, 27))

    def test_pullback_scan_runs_forward_and_takes_the_first_match(self):
        df = build_frame()
        displacement_bar(df, 26, direction="LONG", close=101.0)
        set_bar(df, 27, open_=101.0, close=101.2, high=101.3, low=100.8)
        set_bar(df, 28, open_=101.0, close=101.5, high=101.6, low=100.9)
        self.assertEqual(continuation_plan("LONG", df).pullback_index, 27)

    def test_pullback_window_is_clamped_to_the_frame(self):
        # disp at 28 -> pullback_end = min(29, 31) = 29; no IndexError.
        df = build_frame()
        displacement_bar(df, 28, direction="LONG", close=101.0)
        self.assertEqual(continuation_plan("LONG", df).entry_reason, "no_micro_pullback")

    def test_entry_source_mirrors_the_failure_reason(self):
        # V1 assigns entry_source = entry_reason unconditionally (1562).
        for frame, reason in (
            (build_frame(), "no_continuation_displacement"),
            (
                displacement_bar(build_frame(), 28, direction="LONG", close=101.0),
                "no_micro_pullback",
            ),
        ):
            with self.subTest(reason=reason):
                plan = continuation_plan("LONG", frame)
                self.assertEqual(plan.entry_source, reason)
                self.assertEqual(plan.entry_reason, reason)

    def test_non_long_direction_takes_the_short_pullback_test(self):
        for direction in (None, "", "NONE", "long"):
            with self.subTest(direction=direction):
                plan = plan_entry(
                    "CONTINUATION", direction, self.short_frame(), 1.0,
                    order_blocks=None, htf=htf_context(direction),
                    regime_label="expansion_down",
                )
                self.assertTrue(plan.entry_ready)
                self.assertEqual(plan.entry_price, 98.8)


# --------------------------------------------------------------------------
# Retracement entry (order block)
# --------------------------------------------------------------------------

class TestRetracementEntry(unittest.TestCase):
    def setUp(self):
        # Frame closes at 100.0; a zone at 96-99 is valid for a LONG.
        self.frame = build_frame()
        self.long_block = block(top=99.0, bottom=96.0)

    def test_long_enters_at_the_block_top(self):
        plan = retracement_plan("LONG", blocks=(self.long_block,))
        self.assertTrue(plan.entry_ready)
        self.assertEqual(plan.entry_source, "retracement_ob")
        self.assertEqual(plan.entry_price, 99.0)

    def test_short_enters_at_the_block_bottom(self):
        short_block = block(top=104.0, bottom=101.0)
        plan = retracement_plan("SHORT", blocks=(short_block,),
                                htf=htf_context("SHORT"))
        self.assertTrue(plan.entry_ready)
        self.assertEqual(plan.entry_price, 101.0)

    def test_successful_entry_keeps_the_initial_reason(self):
        # V1 quirk: the ob_valid branch never assigns entry_reason (1622-1658).
        plan = retracement_plan("LONG", blocks=(self.long_block,))
        self.assertEqual(plan.entry_reason, "no_entry")

    def test_stale_block_fails_the_gate(self):
        stale = block(top=99.0, bottom=96.0, fresh=False)
        plan = retracement_plan("LONG", blocks=(stale,))
        self.assertFalse(plan.entry_ready)
        self.assertEqual(plan.entry_reason, "ob_quality_failed")
        self.assertEqual(plan.entry_source, "none")
        self.assertEqual(plan.entry_price, 0.0)

    def test_unselected_block_fails_the_gate(self):
        unselected = block(top=99.0, bottom=96.0, selected=False)
        self.assertEqual(
            retracement_plan("LONG", blocks=(unselected,)).entry_reason,
            "ob_quality_failed",
        )

    def test_no_blocks_fails_the_gate(self):
        for blocks in (None, ()):
            with self.subTest(blocks=blocks):
                self.assertEqual(
                    retracement_plan("LONG", blocks=blocks).entry_reason,
                    "ob_quality_failed",
                )

    def test_zone_too_small_fails_the_gate(self):
        tiny = block(top=99.0, bottom=98.9)  # size 0.1 < 0.25*atr
        self.assertFalse(retracement_plan("LONG", blocks=(tiny,)).entry_ready)

    def test_zone_price_ran_away_from_fails_the_gate(self):
        # Frame closes at 100.0; a SHORT zone entirely below price is lost.
        lost = block(top=99.0, bottom=96.0)
        plan = retracement_plan("SHORT", blocks=(lost,), htf=htf_context("SHORT"))
        self.assertFalse(plan.entry_ready)

    def test_zone_too_close_to_price_fails_the_gate(self):
        close_zone = block(top=101.5, bottom=98.5)  # mid == 100.0 == price
        self.assertFalse(retracement_plan("LONG", blocks=(close_zone,)).entry_ready)

    def test_zero_atr_fails_the_gate(self):
        self.assertFalse(
            retracement_plan("LONG", blocks=(self.long_block,), atr_last=0.0).entry_ready
        )

    def test_gate_helper_agrees_with_the_plan(self):
        for blocks in ((self.long_block,), (block(top=99.0, bottom=96.0, fresh=False),), ()):
            with self.subTest(blocks=blocks):
                self.assertEqual(
                    retracement_ob_valid("LONG", self.frame, 1.0, blocks),
                    retracement_plan("LONG", blocks=blocks).entry_ready,
                )


# --------------------------------------------------------------------------
# The rotational-LONG guard
# --------------------------------------------------------------------------

class TestRotationalLongGuard(unittest.TestCase):
    def setUp(self):
        self.valid_block = (block(top=99.0, bottom=96.0),)

    def test_conflict_outside_discount_blocks_the_entry(self):
        plan = retracement_plan("LONG", blocks=self.valid_block,
                                htf=rotational_conflict_htf("premium"))
        self.assertFalse(plan.entry_ready)
        self.assertEqual(plan.entry_reason, "rotational_long_needs_discount")
        self.assertEqual(plan.entry_source, "none")
        self.assertEqual(plan.entry_price, 0.0)

    def test_equilibrium_also_blocks(self):
        self.assertEqual(
            retracement_plan("LONG", blocks=self.valid_block,
                             htf=rotational_conflict_htf("equilibrium")).entry_reason,
            "rotational_long_needs_discount",
        )

    def test_discount_falls_through_to_the_ob_gate(self):
        # Unreachable in V1 (weekly BULLISH means price > weekly_eq, which
        # excludes "discount"), but the branch is preserved verbatim.
        plan = retracement_plan("LONG", blocks=self.valid_block,
                                htf=rotational_conflict_htf("discount"))
        self.assertTrue(plan.entry_ready)
        self.assertEqual(plan.entry_source, "retracement_ob")

    def test_guard_requires_every_condition(self):
        conflict = rotational_conflict_htf("premium")
        variations = {
            "d1_bias": "BULLISH",
            "h4_bias": "BULLISH",
            "weekly_bias": "BEARISH",
        }
        for field, value in variations.items():
            with self.subTest(field=field):
                htf = HTFContext(**{**conflict.__dict__, field: value})
                plan = retracement_plan("LONG", blocks=self.valid_block, htf=htf)
                self.assertNotEqual(
                    plan.entry_reason, "rotational_long_needs_discount"
                )

    def test_guard_requires_the_rotational_regime(self):
        plan = retracement_plan("LONG", blocks=self.valid_block,
                                htf=rotational_conflict_htf("premium"),
                                regime_label="accumulation")
        self.assertNotEqual(plan.entry_reason, "rotational_long_needs_discount")

    def test_guard_is_long_only(self):
        htf = HTFContext(**{**rotational_conflict_htf("premium").__dict__,
                            "direction": "SHORT"})
        plan = retracement_plan("SHORT", blocks=(block(top=104.0, bottom=101.0),),
                                htf=htf)
        self.assertNotEqual(plan.entry_reason, "rotational_long_needs_discount")


# --------------------------------------------------------------------------
# Non-actionable modes + degenerate input
# --------------------------------------------------------------------------

class TestNonActionableModes(unittest.TestCase):
    def plan(self, entry_mode: str) -> EntryPlan:
        return plan_entry(
            entry_mode, "LONG", build_frame(), 1.0,
            order_blocks=(block(top=99.0, bottom=96.0),),
            htf=htf_context(), regime_label="compression",
        )

    def test_watch_only(self):
        # V1 has this branch, though `derive_entry_mode` can never produce
        # WATCH_ONLY: it needs weekly BULLISH + discount (or BEARISH + premium),
        # which `build_htf_map` makes mutually exclusive.
        plan = self.plan("WATCH_ONLY")
        self.assertFalse(plan.entry_ready)
        self.assertEqual(plan.entry_reason, "watch_only_regime")
        self.assertEqual(plan.entry_source, "none")

    def test_none_mode(self):
        plan = self.plan("NONE")
        self.assertFalse(plan.entry_ready)
        self.assertEqual(plan.entry_reason, "non_actionable_regime")

    def test_unknown_mode_is_non_actionable(self):
        for mode in ("", "continuation", "anything"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.plan(mode).entry_reason, "non_actionable_regime"
                )

    def test_short_frame_does_not_raise(self):
        plan = plan_entry(
            "CONTINUATION", "LONG", build_frame(n=1), 1.0,
            order_blocks=None, htf=htf_context(), regime_label="expansion_up",
        )
        self.assertEqual(plan.entry_reason, "no_continuation_displacement")

    def test_contract_marker_is_always_b(self):
        self.assertEqual(self.plan("NONE").contract, "B")


# --------------------------------------------------------------------------
# Sweep and FVG are not entry gates
# --------------------------------------------------------------------------

class TestSweepAndFvgAreNotGates(unittest.TestCase):
    """V1's entry engine reads neither `sweep_valid` nor `fvg_valid`."""

    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        context = AnalysisContext.initial(self.snapshot)
        context = context.with_(htf=HTFStage.analyze(self.snapshot))
        context = context.with_(
            regime=RegimeStage.analyze(self.snapshot, context.htf)
        )
        self.context = OrderBlockStage().run(StructureStage().run(context))

    def test_entry_ignores_the_sweep(self):
        detected = self.context.with_(
            sweep=LiquiditySweep(detected=True, index=1, ref_index=0, ref_price=1.0)
        )
        absent = self.context.with_(sweep=LiquiditySweep(detected=False))
        self.assertEqual(
            EntryStage().run(detected).entry, EntryStage().run(absent).entry
        )

    def test_entry_ignores_the_fvg(self):
        valid = self.context.with_(fvg=FairValueGap(valid=True))
        invalid = self.context.with_(fvg=FairValueGap(valid=False))
        self.assertEqual(
            EntryStage().run(valid).entry, EntryStage().run(invalid).entry
        )


# --------------------------------------------------------------------------
# Stage behavior
# --------------------------------------------------------------------------

class TestEntryStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        context = AnalysisContext.initial(self.snapshot)
        context = context.with_(htf=HTFStage.analyze(self.snapshot))
        context = context.with_(
            regime=RegimeStage.analyze(self.snapshot, context.htf)
        )
        self.context = OrderBlockStage().run(StructureStage().run(context))

    def test_populates_entry(self):
        self.assertIsInstance(EntryStage().run(self.context).entry, EntryPlan)

    def test_matches_v1(self):
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        self.assertEqual(
            entry_of(EntryStage().run(self.context).entry),
            v1_entry_of(v1["h1_setup"]),
        )

    def test_uses_the_regimes_entry_mode(self):
        self.assertEqual(
            EntryStage().run(self.context).entry.entry_mode,
            self.context.regime.entry_mode,
        )

    def test_populates_only_entry(self):
        result = EntryStage().run(self.context)
        for field in ("risk", "candidate", "signal"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_no_entry_without_htf(self):
        bare = AnalysisContext.initial(self.snapshot)
        self.assertIsNone(EntryStage().run(bare).entry)

    def test_no_entry_without_regime(self):
        no_regime = AnalysisContext.initial(self.snapshot).with_(
            htf=HTFStage.analyze(self.snapshot), atr_last=1.0
        )
        self.assertIsNone(EntryStage().run(no_regime).entry)

    def test_no_entry_without_structure(self):
        no_structure = AnalysisContext.initial(self.snapshot).with_(
            htf=self.context.htf, regime=self.context.regime
        )
        self.assertIsNone(EntryStage().run(no_structure).entry)

    def test_no_entry_when_h1_unusable(self):
        thin = AnalysisContext.initial(snapshot_with(build_frame(n=5))).with_(
            htf=htf_context(), regime=self.context.regime, atr_last=1.0
        )
        self.assertIsNone(EntryStage().run(thin).entry)

    def test_does_not_mutate_input_context(self):
        EntryStage().run(self.context)
        self.assertIsNone(self.context.entry)

    def test_is_deterministic(self):
        self.assertEqual(
            EntryStage().run(self.context), EntryStage().run(self.context)
        )

    def test_stage_name(self):
        self.assertEqual(EntryStage().name, "entry")


# --------------------------------------------------------------------------
# Extraction neutrality + module ownership
# --------------------------------------------------------------------------

class TestPriorStagesUnchanged(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.v1 = SMCStrategy().build_signal_context(
            self.snapshot.d1, self.snapshot.h1
        )
        context = AnalysisContext.initial(self.snapshot)
        context = context.with_(htf=HTFStage.analyze(self.snapshot))
        self.context = context.with_(
            regime=RegimeStage.analyze(self.snapshot, context.htf)
        )

    def test_htf_unchanged(self):
        htf = self.context.htf
        for field in ("weekly_bias", "d1_bias", "h4_bias", "location",
                      "pdh", "pdl", "daily_eq", "current_price"):
            with self.subTest(field=field):
                self.assertEqual(getattr(htf, field), self.v1["htf_map"][field])
        self.assertEqual(htf.direction, self.v1["debug"]["htf"]["dir"])

    def test_regime_unchanged(self):
        self.assertEqual(self.context.regime.label, self.v1["debug"]["htf"]["regime"])
        self.assertEqual(
            self.context.regime.entry_mode, self.v1["h1_setup"]["entry_mode"]
        )

    def test_structure_unchanged(self):
        result = StructureStage().run(self.context)
        self.assertEqual(result.atr_last, self.v1["h1_setup"]["atr_last"])
        self.assertEqual(
            result.displacement_valid, self.v1["h1_setup"]["displacement_valid"]
        )
        self.assertEqual(result.volume_spike, self.v1["h1_setup"]["volume_spike"])

    def test_liquidity_unchanged(self):
        h1 = safe_frame(self.snapshot.h1, H1_MIN_ROWS)
        raw_highs, raw_lows = identify_swings(
            h1, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT
        )
        sweep = detect_sweep(
            self.context.htf.direction, h1,
            to_swings(raw_highs, "high"), to_swings(raw_lows, "low"),
        )
        self.assertEqual(sweep.detected, self.v1["h1_setup"]["sweep_valid"])

    def test_order_block_unchanged(self):
        result = OrderBlockStage().run(StructureStage().run(self.context))
        self.assertEqual(
            fresh_ob(result.order_blocks), self.v1["h1_setup"]["fresh_ob"]
        )

    def test_fair_value_gap_unchanged(self):
        result = FairValueGapStage().run(StructureStage().run(self.context))
        self.assertEqual(result.fvg.valid, self.v1["h1_setup"]["fvg_valid"])


class TestModuleOwnership(unittest.TestCase):
    def test_entry_reuses_the_extracted_helpers(self):
        import v2.analysis.entry as entry_module
        import v2.analysis.order_block as ob_module
        import v2.analysis.structure as structure_module

        self.assertIs(
            entry_module.passes_quality_filter, ob_module.passes_quality_filter
        )
        self.assertIs(entry_module.selected_block, ob_module.selected_block)
        self.assertIs(entry_module.fresh_ob, ob_module.fresh_ob)
        self.assertIs(entry_module.last_close, ob_module.last_close)
        self.assertIs(
            entry_module.is_displacement_candle,
            structure_module.is_displacement_candle,
        )

    def test_entry_owns_no_risk_or_scoring(self):
        import v2.analysis.entry as entry_module

        for name in (
            "find_liquidity_target", "cluster_liquidity_levels",
            "previous_day_high_low", "stop_loss", "take_profit",
            "score_signal", "build_signal", "detect_sweep", "detect_fvg",
        ):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(entry_module, name))

    def test_entry_plan_carries_no_risk_fields(self):
        plan = plan_entry(
            "NONE", "LONG", build_frame(), 1.0,
            order_blocks=None, htf=htf_context(), regime_label="neutral",
        )
        for field in ("sl", "tp", "tp_source", "tp_rr", "rr"):
            with self.subTest(field=field):
                self.assertFalse(hasattr(plan, field))

    def test_entry_planning_has_a_single_owner(self):
        import v2.analysis.fair_value_gap as fvg_module
        import v2.analysis.htf as htf_module
        import v2.analysis.liquidity as liquidity_module
        import v2.analysis.order_block as ob_module
        import v2.analysis.regime as regime_module
        import v2.analysis.structure as structure_module

        for module in (htf_module, regime_module, structure_module,
                       liquidity_module, ob_module, fvg_module):
            for name in ("plan_entry", "retracement_ob_valid"):
                with self.subTest(module=module.__name__, symbol=name):
                    self.assertFalse(hasattr(module, name))


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

class TestPipelineIntegration(unittest.TestCase):
    def stages(self):
        return [HTFStage(), RegimeStage(), StructureStage(), LiquidityStage(),
                OrderBlockStage(), FairValueGapStage(), EntryStage()]

    def test_first_seven_stages_run_in_order(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot)
        )
        self.assertIsNotNone(result.htf)
        self.assertIsNotNone(result.regime)
        self.assertIsNotNone(result.swing_highs)
        self.assertIsNotNone(result.sweep)
        self.assertIsNotNone(result.order_blocks)
        self.assertIsNotNone(result.fvg)
        self.assertIsInstance(result.entry, EntryPlan)
        self.assertIsNone(result.risk)
        self.assertIsNone(result.signal)

    def test_entry_matches_v1_through_the_pipeline(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot)
        )
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        self.assertEqual(entry_of(result.entry), v1_entry_of(v1["h1_setup"]))

    def test_entry_skipped_when_htf_bails(self):
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
        )
        self.assertIsNone(result.htf)
        self.assertIsNone(result.entry)


if __name__ == "__main__":
    unittest.main()
