"""app.rendering — the pure presentation functions, against V1's exact format.

The expected strings are transcribed independently from V1's ``print``
statements (main.py), so a drift in either the renderer or this transcription
fails the test.
"""
from __future__ import annotations

import dataclasses
import unittest

from v2.app.rendering import render_dashboard, render_log_event, render_scan
from v2.domain import ActiveSignal, LogEvent

from tests.lifecycle.support import CycleSpec, make_signal


def _active(**kw) -> ActiveSignal:
    base = dict(
        direction="LONG", score=12, decision="VALID", entry_price=100.0,
        sl=95.0, tp=115.0, regime="expansion_up", timestamp=1.0,
        entry_mode="RETRACEMENT", miss_count=0,
    )
    base.update(kw)
    return ActiveSignal(**base)


def _valid_long(symbol="BTC/USDT", **kw):
    base = dict(
        symbol=symbol, decision="VALID", direction="LONG", entry_mode="RETRACEMENT",
        regime="expansion_up", score=12, entry_ready=True, entry_price=100.0,
        sl=95.0, tp=115.0, entry_source="retracement_ob", tp_source="SWING_HIGH",
        d1_bias="BULLISH", h4_bias="BULLISH", weekly_bias="BULLISH",
        location="discount", atr_last=1.0, live_price=100.0, h1_last_ts=5000.0,
    )
    base.update(kw)
    return make_signal(CycleSpec(**base))


class TestDashboard(unittest.TestCase):
    def test_empty(self):
        lines = render_dashboard({})
        self.assertIn("📋 RADAR SUMMARY", lines)
        self.assertIn("   [∅] No active setups.", lines)

    def test_rows_sorted_by_score_desc_with_labels(self):
        active = {
            "AAA": _active(score=8, decision="WATCH", entry_mode="CONTINUATION",
                           direction="LONG", entry_price=1.2345, sl=1.2, tp=1.3, miss_count=1),
            "BBB": _active(score=15, decision="A+", direction="SHORT",
                           entry_price=100.0, sl=105.0, tp=85.0),
        }
        lines = render_dashboard(active)
        rows = [ln for ln in lines if ln.startswith("   ➤")]
        # sorted by score desc: BBB (15) before AAA (8)
        self.assertIn("BBB", rows[0])
        self.assertIn("AAA", rows[1])
        # exact row format (independently transcribed from main.py:236-245)
        self.assertEqual(
            rows[0],
            f"   ➤ {'BBB':<14}| {'SHORT':<5} | {'A+':<10} | "
            f"Score: {15:<4} | Entry: {100.0:.4f} | SL: {105.0:.4f} | "
            f"TP: {85.0:.4f} | Miss: {0}",
        )
        # WATCH + CONTINUATION -> WATCH-CONT label
        self.assertIn("WATCH-CONT", rows[1])


class TestScanBlock(unittest.TestCase):
    def test_full_block_for_scored_signal(self):
        signal, htf = _valid_long()
        lines = render_scan(signal, htf, signal)
        self.assertEqual(len(lines), 4)  # SCAN + HTF + CONF + RAW
        self.assertEqual(
            lines[0],
            f"   [SCAN] {'BTC/USDT':<14}| Score: {12:<3} | {'VALID':<10} | Reason: -",
        )
        self.assertEqual(
            lines[1],
            "      HTF  => W:BULLISH | D1:BULLISH | H4:BULLISH | LOC:discount "
            "| REGIME:expansion_up | H1_FB:None | SRC:none | DIR:LONG "
            "| BULL:0.0 | BEAR:0.0",
        )
        self.assertEqual(
            lines[2],
            "      CONF => sweep=False | fvg=False | disp=False | vol=False "
            "| ob=False | target=False | mode=RETRACEMENT | ready=True",
        )
        self.assertEqual(
            lines[3],
            "      RAW  => type=dict | score=12 | decision=VALID | mode=RETRACEMENT",
        )

    def test_raw_score_adds_back_choppy_penalty(self):
        signal, htf = _valid_long(score=10)
        raw = dataclasses.replace(signal, notes=("choppy_penalty",))
        lines = render_scan(signal, htf, raw)
        raw_line = next(ln for ln in lines if "RAW" in ln)
        self.assertIn("score=12", raw_line)  # 10 + 2 (choppy) = 12

    def test_no_htf_prints_only_scan(self):
        # invalid-input: htf None, candidate None
        signal, _ = make_signal(CycleSpec(symbol="X/USDT", has_htf=False))
        lines = render_scan(signal, None, signal)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("   [SCAN]"))

    def test_no_direction_prints_scan_and_htf_only(self):
        signal, htf = make_signal(
            CycleSpec(symbol="X/USDT", direction=None, regime="neutral",
                      d1_bias="BEARISH", h4_bias="BEARISH")
        )
        lines = render_scan(signal, htf, signal)
        self.assertEqual(len(lines), 2)  # SCAN + HTF (no CONF/RAW: candidate None)
        self.assertTrue(lines[1].startswith("      HTF"))

    def test_not_ready_prints_conf_but_no_raw(self):
        # candidate present, not entry_ready, not WATCH_ONLY -> CONF yes, RAW no
        signal, htf = make_signal(
            CycleSpec(symbol="X/USDT", direction="LONG", entry_mode="RETRACEMENT",
                      entry_ready=False, regime="rotational", d1_bias="BULLISH",
                      h4_bias="BULLISH", reason="no_micro_pullback")
        )
        lines = render_scan(signal, htf, signal)
        self.assertTrue(any(ln.startswith("      CONF") for ln in lines))
        self.assertFalse(any("RAW" in ln for ln in lines))


class TestLogEventRendering(unittest.TestCase):
    def test_kill(self):
        e = LogEvent("KILL", "BTC/USDT")
        self.assertEqual(
            render_log_event(e),
            f"   [☠️ KILL] {'BTC/USDT':<14} | Emergency invalidation triggered.",
        )

    def test_alerted_kill_new_upgrade(self):
        self.assertEqual(
            render_log_event(LogEvent("ALERTED", "BTC/USDT", (("kind", "KILL"),))),
            f"   [📱 ALERTED] {'BTC/USDT':<14} | KILL",
        )
        self.assertEqual(
            render_log_event(LogEvent("ALERTED", "BTC/USDT", (("tier", "VALID"),))),
            f"   [📱 ALERTED] {'BTC/USDT':<14} | VALID",
        )
        self.assertEqual(
            render_log_event(
                LogEvent("ALERTED", "BTC/USDT", (("from", "VALID"), ("to", "A+")))
            ),
            f"   [📱 ALERTED] {'BTC/USDT':<14} | VALID -> A+",
        )

    def test_stale(self):
        self.assertEqual(
            render_log_event(LogEvent("STALE", "BTC/USDT")),
            f"   [⚠️ STALE] {'BTC/USDT':<14} | Setup invalidated by live market price.",
        )

    def test_kill_cooldown(self):
        e = LogEvent("KILL_COOLDOWN", "BTC/USDT",
                     (("entry_mode", "RETRACEMENT"), ("direction", "LONG"),
                      ("remaining_min", 42.6)))
        self.assertEqual(
            render_log_event(e),
            f"   [🚫 KILL COOLDOWN] {'BTC/USDT':<14} | RETRACEMENT | 43m remaining",
        )

    def test_same_thesis(self):
        e = LogEvent("SAME_THESIS", "BTC/USDT",
                     (("entry_mode", "RETRACEMENT"), ("direction", "LONG"),
                      ("existing", "VALID"), ("current", "VALID")))
        self.assertEqual(
            render_log_event(e),
            f"   [🟰 SAME THESIS] {'BTC/USDT':<14} | RETRACEMENT | LONG | "
            f"existing=VALID current=VALID",
        )

    def test_replace(self):
        e = LogEvent("REPLACE", "NEW/USDT",
                     (("weakest_symbol", "OLD/USDT"), ("weakest_score", 5), ("score", 12)))
        self.assertEqual(
            render_log_event(e),
            "   [🔄 REPLACE] Slot penuh. OLD/USDT (score=5) diganti "
            "NEW/USDT (score=12)",
        )

    def test_capped(self):
        e = LogEvent("CAPPED", "X/USDT", (("score", 4), ("weakest_score", 7)))
        self.assertEqual(
            render_log_event(e),
            f"   [🚫 CAPPED] {'X/USDT':<14} | Score=4 tidak cukup kuat "
            f"untuk masuk (weakest=7)",
        )

    def test_guard(self):
        e = LogEvent("GUARD", "X/USDT",
                     (("decision", "VALID"), ("entry_price", 100.0),
                      ("sl", 0.0), ("tp", 115.0)))
        self.assertEqual(
            render_log_event(e),
            f"   [⚠️ GUARD] {'X/USDT':<14} | VALID blocked — invalid levels "
            f"(entry=100.0, sl=0.0, tp=115.0)",
        )

    def test_decay(self):
        e = LogEvent("DECAY", "X/USDT",
                     (("from_decision", "WATCH"), ("mode", "RETRACEMENT"),
                      ("reason", "bad_rr"), ("miss_count", 3)))
        self.assertEqual(
            render_log_event(e),
            f"   [⏳ DECAY] {'X/USDT':<14} | from=WATCH | mode=RETRACEMENT | "
            f"reason=bad_rr | miss_count=3",
        )

    def test_ttl(self):
        e = LogEvent("TTL", "X/USDT",
                     (("age_seconds", 90000.0), ("ttl_seconds", 86400.0)))
        self.assertEqual(
            render_log_event(e),
            f"   [⏰ TTL EXPIRED] {'X/USDT':<14} | Age: 25.0h | TTL: 24.0h",
        )


if __name__ == "__main__":
    unittest.main()
