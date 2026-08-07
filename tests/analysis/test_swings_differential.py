"""Step 5 — `identify_swings` vectorization differential (C-8.5).

Comparison method, mandated by IMPLEMENTATION_PLAN §6: **never `assertEqual`**.
It passes on a type change (`5 == 5.0`) and fails on NaN (`nan != nan`), and both
are exactly what this differential must catch. Every comparison below goes
through `_identical`, which checks list length, `index` type and value, `price`
type, and the price's **bit pattern** via `np.float64(...).tobytes()`.

Acceptance: one differing swing — one index, one float bit, one list length, one
element type — rejects the change.

The heavy corpus sweeps (T5.1a, T5.1b) are budgeted at ~6 h and are gated behind
`SWINGS_DIFFERENTIAL=1` so the default suite stays fast. They were executed once
for the merge; see CHANGELOG S13 for the recorded result.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.analysis import structure as ST
from v2.analysis.structure import _swings_v1, _swings_vectorized, identify_swings

#: The five parameter sets IMPLEMENTATION_PLAN §5 declares.
PARAMETER_SETS = ((2, 0.30), (3, 0.18), (1, 0.15), (2, 0.25), (2, 0.0))
#: The two that run on H1 frames in replay.
H1_SETS = ((2, 0.30), (3, 0.18))

HEAVY = os.environ.get("SWINGS_DIFFERENTIAL") == "1"
STAGE1 = Path("data/stage1/canonical")
PILOT = Path("data/corpus-pilot/canonical")


def _identical(case, actual, expected, context=""):
    """Bit-exact, type-exact list comparison. The only comparator used here."""
    case.assertEqual(len(expected), len(actual),
                     f"{context}: length {len(actual)} != {len(expected)}")
    for position, (a, e) in enumerate(zip(actual, expected)):
        where = f"{context}[{position}]"
        case.assertIs(type(a["index"]), type(e["index"]),
                      f"{where}: index type {type(a['index'])}")
        case.assertEqual(e["index"], a["index"], f"{where}: index")
        case.assertIs(type(a["price"]), type(e["price"]),
                      f"{where}: price type {type(a['price'])}")
        case.assertEqual(
            np.float64(e["price"]).tobytes(), np.float64(a["price"]).tobytes(),
            f"{where}: price bits {a['price']!r} != {e['price']!r}")


def _frame(highs, lows, atr=None):
    data = {
        "timestamp": list(range(len(highs))),
        "open": list(highs), "high": list(highs), "low": list(lows),
        "close": list(lows), "v": [1.0] * len(highs),
    }
    if atr is not None:
        data["atr"] = atr
    return pd.DataFrame(data)


def _both(frame, window, prominence):
    """`(vectorized, v1)` for one frame and parameter set, prologue included."""
    original = ST.SWING_IMPL
    try:
        ST.SWING_IMPL = "vectorized"
        fast = identify_swings(frame, window, prominence)
        ST.SWING_IMPL = "v1"
        slow = identify_swings(frame, window, prominence)
    finally:
        ST.SWING_IMPL = original
    return fast, slow


# ---------------------------------------------------------------------------
# T5.3 / T5.5 — the named hazards and tie behaviour
# ---------------------------------------------------------------------------

class TestHazards(unittest.TestCase):

    def test_nan_in_a_low_window(self):
        # Hazard 1: Series.min() skips NaN; np.min() would propagate it.
        highs = [1.0, 2.0, 5.0, 2.0, 1.0, 3.0, 1.0]
        lows = [0.5, float("nan"), 0.4, float("nan"), 0.3, 0.2, 0.1]
        frame = _frame(highs, lows, atr=[1.0] * 7)
        for window, prominence in PARAMETER_SETS:
            with self.subTest(window=window, prominence=prominence):
                fast, slow = _both(frame, window, prominence)
                _identical(self, fast[0], slow[0], "highs")
                _identical(self, fast[1], slow[1], "lows")

    def test_nan_on_exactly_one_side_of_the_max(self):
        # Hazard 2: max(nan, x) is nan but max(x, nan) is x. np.maximum would
        # collapse both to nan and change the outcome.
        highs = [3.0, 1.0, 9.0, 1.0, 3.0]
        lows = [float("nan"), float("nan"), 0.5, 2.0, 2.0]
        frame = _frame(highs, lows, atr=[2.0] * 5)
        fast, slow = _both(frame, 2, 0.30)
        _identical(self, fast[0], slow[0], "highs")
        _identical(self, fast[1], slow[1], "lows")

    def test_a_skipna_reduction_would_drop_this_swing(self):
        """Hazard 2, pinned by construction rather than by inspection.

        Every left low is NaN, so Python's `max(NaN, r)` is NaN, the prominence
        is NaN, `NaN < min_prominence` is False and the swing survives. A skipna
        reduction returns `r` instead, giving prominence 0.1 < 0.6 and dropping
        the swing. Verified: substituting `pd.concat(...).max(axis=1)` turns the
        expected `[{index: 2, price: 5.0}]` into `[]`.
        """
        frame = _frame([1.0, 1.0, 5.0, 1.0, 1.0],
                       [float("nan"), float("nan"), 0.5, 4.9, 4.9],
                       atr=[2.0] * 5)
        fast, slow = _both(frame, 2, 0.3)
        _identical(self, fast[0], slow[0], "highs")
        self.assertEqual(1, len(fast[0]), "the NaN-side swing must survive")
        self.assertEqual(2, fast[0][0]["index"])

    def test_all_nan_window_matches_pandas(self):
        highs = [1.0, 2.0, 5.0, 2.0, 1.0]
        lows = [float("nan")] * 5
        frame = _frame(highs, lows, atr=[1.0] * 5)
        fast, slow = _both(frame, 2, 0.30)
        _identical(self, fast[0], slow[0], "highs")
        _identical(self, fast[1], slow[1], "lows")

    def test_n_at_or_below_twice_the_window(self):
        # Hazard 3: the shifted form would raise; V1's range() is simply empty.
        for rows in range(0, 8):
            for window in (1, 2, 3):
                if rows > 2 * window:
                    continue
                with self.subTest(rows=rows, window=window):
                    frame = _frame([1.0] * rows, [0.5] * rows,
                                   atr=[1.0] * rows)
                    fast, slow = _both(frame, window, 0.30)
                    self.assertEqual(([], []), fast)
                    self.assertEqual(slow, fast)

    def test_prominence_exactly_at_the_floor_survives(self):
        # T5.5: the test is strict `<`, so equality must NOT disqualify.
        highs = [1.0, 1.0, 3.0, 1.0, 1.0]
        lows = [1.0, 1.0, 1.0, 1.0, 1.0]
        frame = _frame(highs, lows, atr=[2.0] * 5)   # atr_mean 2.0
        fast, slow = _both(frame, 2, 1.0)            # min_prominence == 2.0
        _identical(self, fast[0], slow[0], "highs")
        self.assertEqual(1, len(fast[0]))

    def test_equal_neighbours_remain_swings(self):
        highs = [5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        lows = [1.0] * 7
        frame = _frame(highs, lows)                  # no atr -> mean 0.0
        for window, prominence in PARAMETER_SETS:
            with self.subTest(window=window, prominence=prominence):
                fast, slow = _both(frame, window, prominence)
                _identical(self, fast[0], slow[0], "highs")
                _identical(self, fast[1], slow[1], "lows")

    def test_consecutive_equal_suppression_is_preserved(self):
        highs = [1.0, 4.0, 4.0, 4.0, 4.0, 1.0]
        lows = [0.5] * 6
        frame = _frame(highs, lows)
        fast, slow = _both(frame, 1, 0.0)
        _identical(self, fast[0], slow[0], "highs")

    def test_frame_without_an_atr_column(self):
        # The H4 resample has no `atr`; atr_mean is 0.0 and filtering is off.
        frame = _frame([1.0, 3.0, 9.0, 3.0, 1.0], [0.5, 0.4, 0.3, 0.4, 0.5])
        for window, prominence in PARAMETER_SETS:
            with self.subTest(window=window, prominence=prominence):
                fast, slow = _both(frame, window, prominence)
                _identical(self, fast[0], slow[0], "highs")
                _identical(self, fast[1], slow[1], "lows")

    def test_all_nan_atr_column(self):
        frame = _frame([1.0, 3.0, 9.0, 3.0, 1.0], [0.5, 0.4, 0.3, 0.4, 0.5],
                       atr=[float("nan")] * 5)
        fast, slow = _both(frame, 2, 0.30)
        _identical(self, fast[0], slow[0], "highs")
        _identical(self, fast[1], slow[1], "lows")

    def test_returned_types_match_v1_exactly(self):
        frame = _frame([1.0, 3.0, 9.0, 3.0, 1.0], [0.5, 0.4, 0.3, 0.4, 0.5],
                       atr=[1.0] * 5)
        fast, slow = _both(frame, 2, 0.0)
        self.assertTrue(fast[0], "fixture produced no swing; test is vacuous")
        for produced, oracle in zip(fast[0], slow[0]):
            self.assertIs(int, type(produced["index"]))
            self.assertIs(type(oracle["price"]), type(produced["price"]))


# ---------------------------------------------------------------------------
# T5.4 — the C-8.5 source assertion
# ---------------------------------------------------------------------------

class TestFrozenAtrMean(unittest.TestCase):

    def test_atr_mean_still_uses_pandas_mean(self):
        source = Path("v2/analysis/structure.py").read_text(encoding="utf-8")
        self.assertIn('atr_mean = float(df["atr"].dropna().mean())', source,
                      "C-8.5 mandates pandas' own summation order here")

    def test_the_prologue_is_reached_before_dispatch(self):
        source = Path("v2/analysis/structure.py").read_text(encoding="utf-8")
        prologue = source.index('atr_mean = float(df["atr"].dropna().mean())')
        dispatch = source.index('if SWING_IMPL == "v1":')
        self.assertLess(prologue, dispatch,
                        "both implementations must receive the frozen value")

    def test_the_v1_oracle_is_still_in_the_tree(self):
        self.assertTrue(callable(_swings_v1))
        self.assertTrue(callable(_swings_vectorized))

    def test_the_rollback_seam_exists_and_works(self):
        self.assertIn(ST.SWING_IMPL, ("v1", "vectorized"))
        frame = _frame([1.0, 3.0, 9.0, 3.0, 1.0], [0.5, 0.4, 0.3, 0.4, 0.5],
                       atr=[1.0] * 5)
        original = ST.SWING_IMPL
        try:
            ST.SWING_IMPL = "v1"
            self.assertTrue(identify_swings(frame, 2, 0.0)[0])
        finally:
            ST.SWING_IMPL = original


# ---------------------------------------------------------------------------
# Randomised differential — cheap, always on
# ---------------------------------------------------------------------------

class TestRandomisedDifferential(unittest.TestCase):
    """Deterministic pseudo-random frames, including NaN-injected ones."""

    def test_two_thousand_random_frames(self):
        rng = np.random.default_rng(20260803)
        for trial in range(2000):
            rows = int(rng.integers(0, 60))
            highs = rng.normal(100.0, 5.0, rows)
            lows = highs - np.abs(rng.normal(1.0, 0.5, rows))
            if rows and trial % 3 == 0:                    # inject NaN
                holes = rng.integers(0, rows, max(1, rows // 8))
                highs[holes] = np.nan
                lows[holes] = np.nan
            if rows and trial % 5 == 0:                    # force ties
                highs[: rows // 2] = highs[0] if rows else 0.0
            atr = None if trial % 7 == 0 else list(
                np.abs(rng.normal(1.0, 0.3, rows)))
            frame = _frame(list(highs), list(lows), atr=atr)
            window, prominence = PARAMETER_SETS[trial % len(PARAMETER_SETS)]
            with self.subTest(trial=trial, rows=rows, window=window):
                fast, slow = _both(frame, window, prominence)
                _identical(self, fast[0], slow[0], f"t{trial}.highs")
                _identical(self, fast[1], slow[1], f"t{trial}.lows")


# ---------------------------------------------------------------------------
# T5.1a / T5.1b — the corpus sweeps
# ---------------------------------------------------------------------------

def _datasets(root: Path, timeframe: str = "1h"):
    import json
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for key in sorted(manifest["datasets"]):
        symbol, tf = key.split("|")
        if tf == timeframe:
            yield symbol, root / "klines" / symbol / f"{tf}.npy"


def _frame_from(path: Path) -> pd.DataFrame:
    rows = np.load(path, mmap_mode="r")
    return pd.DataFrame({
        "timestamp": rows["timestamp"], "open": rows["open"],
        "high": rows["high"], "low": rows["low"], "close": rows["close"],
        "v": rows["v"],
    })


@unittest.skipUnless(HEAVY and STAGE1.is_dir(),
                     "set SWINGS_DIFFERENTIAL=1 (~6 h sweep)")
class TestCorpusDifferential(unittest.TestCase):

    def test_t51a_whole_frame_all_datasets_all_parameter_sets(self):
        checked = 0
        for symbol, path in _datasets(STAGE1):
            frame = _frame_from(path)
            for window, prominence in PARAMETER_SETS:
                fast, slow = _both(frame, window, prominence)
                _identical(self, fast[0], slow[0],
                           f"{symbol} w{window} p{prominence} highs")
                _identical(self, fast[1], slow[1],
                           f"{symbol} w{window} p{prominence} lows")
                checked += 1
        self.assertGreater(checked, 0)

    def test_t51b_i_strided_windows(self):
        for symbol, path in _datasets(STAGE1):
            frame = _frame_from(path)
            n = len(frame)
            if n < 499:
                continue
            stride = max(1, (n - 499) // 100)
            offsets = list(range(0, n - 499 + 1, stride)) + [n - 499]
            for offset in sorted(set(offsets)):
                sliced = frame.iloc[offset:offset + 499].reset_index(drop=True)
                for window, prominence in H1_SETS:
                    fast, slow = _both(sliced, window, prominence)
                    _identical(self, fast[0], slow[0],
                               f"{symbol}@{offset} w{window} highs")
                    _identical(self, fast[1], slow[1],
                               f"{symbol}@{offset} w{window} lows")

    def test_t51b_ii_pathology_dense_segments(self):
        """Stride-1 windows over the 15 shakedown symbols' worst region.

        Two deviations from the plan's literal rule, both forced by the corpus
        and both asserted here rather than left implicit:

        * CAPUSDT/GRAMUSDT/DATAIPUSDT hold 66-205 rows, so **no** 499-row window
          fits and "every available window" is empty. Substituted with every
          frame the walker can actually present, `rows[max(0, k-499):k]`.
        * Six symbols have `gap_count == 0`, so there is no longest gap to
          centre on; the block is taken from the series start instead.
        """
        import json

        candidates = json.loads(
            (STAGE1.parent / "coverage.json").read_text(encoding="utf-8")
        )["shakedown_candidates"]
        self.assertEqual(15, len(candidates))

        for symbol in candidates:
            frame = _frame_from(STAGE1 / "klines" / symbol / "1h.npy")
            n = len(frame)
            if n < 499:
                slices = [(max(0, k - 499), k) for k in range(1, n + 1)]
            elif n < 2499:
                slices = [(o, o + 499) for o in range(0, n - 499 + 1)]
            else:
                deltas = np.diff(np.asarray(frame["timestamp"], dtype="int64"))
                gap_row = 1 if int(deltas.max()) <= 3_600_000 else int(deltas.argmax()) + 1
                start = max(0, min(gap_row - 249 - 1000, n - 499 - 2000 + 1))
                slices = [(o, o + 499) for o in range(start, start + 2000)]
            self.assertGreater(len(slices), 0, symbol)

            for start, stop in slices:
                sliced = frame.iloc[start:stop].reset_index(drop=True)
                for window, prominence in H1_SETS:
                    fast, slow = _both(sliced, window, prominence)
                    _identical(self, fast[0], slow[0],
                               f"{symbol}@{start} w{window} highs")
                    _identical(self, fast[1], slow[1],
                               f"{symbol}@{start} w{window} lows")


@unittest.skipUnless(HEAVY and PILOT.is_dir(),
                     "set SWINGS_DIFFERENTIAL=1 (~20 min sweep)")
class TestDerivedFrameDifferential(unittest.TestCase):
    """T5.1b(iii) — the frame classes no raw corpus contains.

    The frames are never reconstructed here. The real snapshot adapter is
    positioned at each sampled boundary and the real analyzer runs over the
    snapshot, with `identify_swings` wrapped at its three module-level call
    sites; what gets compared is the exact object production passes in. That
    covers the H4 resample (no `atr` column — the `atr_mean = 0.0` branch), the
    EMA-augmented D1 frame, and the raw H1 frame.

    Two frames are additionally forced, because the pipeline reaches them only
    conditionally and the plan declares both: the EMA-augmented H1 frame, built
    by calling `_derive_simple_bias` itself (the function the htf.py:297
    fallback calls), and `snapshot.m5`, which `_m5_setup` sees only once V1's
    `score >= 8` gate opens.
    """

    #: The three parameter sets IMPLEMENTATION_PLAN §5 declares for (iii).
    SETS = ((3, 0.18), (1, 0.15), (2, 0.25))
    SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT")

    def test_t51b_iii_derived_frames(self):
        from datetime import datetime, timezone

        from backtest.data.repository import KlineRepository
        from backtest.replay.adapter import HistoricalMarketDataAdapter
        from v2.analysis import htf as HTF
        from v2.analysis import scoring as SC
        from v2.analysis.engine import V2Analyzer

        captured: list = []
        real = ST.identify_swings

        def capture(df, window, prominence_mult):
            captured.append(df)
            return real(df, window, prominence_mult)

        adapter = HistoricalMarketDataAdapter(KlineRepository(PILOT))
        analyzer = V2Analyzer()
        start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2025, 3, 31, tzinfo=timezone.utc).timestamp())
        compared = 0

        for boundary in range(start, end + 3600, 10 * 3600):
            adapter.set_time(boundary)
            now = datetime.fromtimestamp(boundary + 1, tz=timezone.utc)
            session_active = now.weekday() < 5 and 7 <= now.hour < 22
            for symbol in self.SYMBOLS:
                snapshot = adapter.get_market_snapshot(symbol, include_m5=True)
                if snapshot is None:
                    continue
                if len(snapshot.d1) < 10 or len(snapshot.h1) < 50:
                    continue      # orchestrator.py:261, the same guard

                frames = []
                for produce in (
                    lambda: analyzer.analyze(snapshot, session_active=session_active),
                    lambda: HTF._derive_simple_bias(snapshot.h1),
                ):
                    captured.clear()
                    ST.identify_swings = capture
                    HTF.identify_swings = capture
                    SC.identify_swings = capture
                    try:
                        produce()
                    finally:
                        ST.identify_swings = real
                        HTF.identify_swings = real
                        SC.identify_swings = real
                    frames.extend(captured)
                if snapshot.m5 is not None:
                    frames.append(snapshot.m5)

                for frame in frames:
                    for window, prominence in self.SETS:
                        fast, slow = _both(frame, window, prominence)
                        where = f"{symbol}@{boundary} n{len(frame)} w{window}"
                        _identical(self, fast[0], slow[0], f"{where} highs")
                        _identical(self, fast[1], slow[1], f"{where} lows")
                        compared += 1
        self.assertGreater(compared, 0)


if __name__ == "__main__":
    unittest.main()
