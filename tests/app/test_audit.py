"""app.audit — OB-audit assembly from domain objects (Decision #010)."""
from __future__ import annotations

import dataclasses
import unittest

from v2.app.audit import assemble_ob_audit
from v2.domain import OrderBlock

from tests.app.support import make_snapshot
from tests.lifecycle.support import CycleSpec, make_signal


def _ob(top, bottom, *, index, selected):
    return OrderBlock(
        index=index, rank_from_last=index, top=top, bottom=bottom,
        size=abs(top - bottom), fresh_status=True, passed_quality_filter=True,
        is_selected=selected, size_atr=abs(top - bottom), depth_from_price=0.5,
    )


def _retracement_signal(symbol="BTC/USDT", *, direction="LONG", entry_price=100.0,
                        sl=95.0, tp=115.0, entry_source="retracement_ob",
                        obs=None, **kw):
    base = dict(
        symbol=symbol, decision="VALID", direction=direction,
        entry_mode="RETRACEMENT", regime="expansion_up", score=12,
        entry_ready=True, entry_price=entry_price, sl=sl, tp=tp,
        entry_source=entry_source, tp_source="SWING_HIGH",
        d1_bias="BULLISH", h4_bias="BULLISH", weekly_bias="BULLISH",
        location="discount", atr_last=1.0, volume_spike=True,
    )
    base.update(kw)
    signal, _ = make_signal(CycleSpec(**base))
    cand = dataclasses.replace(
        signal.candidate,
        order_blocks=tuple(obs) if obs else (),
        fresh_ob=True,
        fvg_valid=True,
    )
    return dataclasses.replace(signal, candidate=cand)


class TestEligibility(unittest.TestCase):
    def test_none_when_no_candidate(self):
        signal, _ = make_signal(CycleSpec(symbol="X", has_htf=False))
        self.assertIsNone(assemble_ob_audit(make_snapshot("X"), signal))

    def test_none_for_continuation(self):
        signal = _retracement_signal(entry_mode="CONTINUATION",
                                     entry_source="continuation_micro_pullback")
        self.assertIsNone(assemble_ob_audit(make_snapshot("BTC/USDT"), signal))

    def test_none_for_non_ob_entry_source(self):
        signal = _retracement_signal(entry_source="no_micro_pullback")
        self.assertIsNone(assemble_ob_audit(make_snapshot("BTC/USDT"), signal))

    def test_none_for_invalid_levels(self):
        signal = _retracement_signal(sl=0.0)
        self.assertIsNone(assemble_ob_audit(make_snapshot("BTC/USDT"), signal))


class TestPayload(unittest.TestCase):
    def setUp(self):
        self.ob_sel = _ob(100.0, 98.0, index=5, selected=True)
        self.ob_other = _ob(97.0, 95.0, index=4, selected=False)
        self.signal = _retracement_signal(obs=[self.ob_other, self.ob_sel])
        self.snapshot = make_snapshot("BTC/USDT", live_price=101.0, h1_last_ts=5000.0)
        self.rec = assemble_ob_audit(self.snapshot, self.signal)

    def test_core_fields(self):
        r = self.rec
        self.assertEqual(r.symbol, "BTC/USDT")
        self.assertEqual(r.h1_last_ts, 5000.0)
        self.assertEqual(r.direction, "LONG")
        self.assertEqual(r.entry_mode, "RETRACEMENT")
        self.assertEqual(r.decision, "VALID")
        self.assertEqual(r.score, 12)
        self.assertEqual(r.regime, "expansion_up")
        self.assertEqual(r.market_price, 101.0)
        self.assertEqual((r.entry_price, r.sl, r.tp), (100.0, 95.0, 115.0))
        self.assertEqual(r.entry_source, "retracement_ob")
        self.assertEqual(r.tp_source, "SWING_HIGH")
        self.assertTrue(r.volume_spike)

    def test_rr_is_structural_ratio(self):
        self.assertAlmostEqual(self.rec.rr, 15.0 / 5.0)  # |tp-entry|/|entry-sl|

    def test_selected_ob_meta_and_candidates(self):
        self.assertEqual(self.rec.ob_meta, (100.0, 98.0))  # selected top/bottom
        self.assertEqual(self.rec.ob_candidates, (self.ob_other, self.ob_sel))
        self.assertTrue(self.rec.fresh_ob)
        self.assertTrue(self.rec.fvg_valid)

    def test_entry_equals_ob_bottom_long_uses_top(self):
        # LONG entry 100.0 == selected.top 100.0 -> True
        self.assertTrue(self.rec.entry_equals_ob_bottom)

    def test_bug2_metadata_populated(self):
        # BUG-2 fix (re-based deliberately; REPLAY_CONTRACT.md §12): V1 left
        # these ALWAYS empty via a key-name mismatch. The values existed on the
        # same call and are now recorded. Write-only observability — no
        # decision reads the record (Decision #010).
        r = self.rec
        self.assertEqual(r.location, "discount")          # candidate.htf.location
        self.assertEqual(r.window_bounds, (49, 58))       # candidate_window(59)
        self.assertEqual(r.selected_ob_index, 5)
        self.assertEqual(r.selected_ob_rank, 5)
        self.assertEqual(r.selected_ob_top, 100.0)
        self.assertEqual(r.selected_ob_bottom, 98.0)
        self.assertEqual(r.selected_ob_size, 2.0)
        self.assertEqual(r.current_price, 100.0)          # last CLOSED H1 close
        # ...and stays distinct from the live forming-candle price.
        self.assertEqual(r.market_price, 101.0)

    def test_serializes_to_v1_dict(self):
        d = self.rec.to_v1_dict()
        self.assertEqual(d["ob_meta"], {"top": 100.0, "bottom": 98.0})
        self.assertEqual(d["window_bounds"], {"start_index": 49, "end_index": 58})
        self.assertEqual(len(d["ob_candidates"]), 2)
        self.assertEqual(d["location"], "discount")
        self.assertEqual(d["current_price"], 100.0)

    def test_short_uses_bottom_for_entry_equality(self):
        ob = _ob(105.0, 100.0, index=5, selected=True)
        signal = _retracement_signal(direction="SHORT", entry_price=100.0,
                                     sl=105.0, tp=85.0, obs=[ob])
        rec = assemble_ob_audit(make_snapshot("BTC/USDT"), signal)
        # SHORT entry 100.0 == selected.bottom 100.0 -> True
        self.assertTrue(rec.entry_equals_ob_bottom)


if __name__ == "__main__":
    unittest.main()
