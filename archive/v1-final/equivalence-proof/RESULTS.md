# Equivalence Proof — Results at Archive Time

Recorded 2026-08-14 against V1 at git tag `v1-final`.

## What was recorded

10,757 V1 answers across 11 methods, captured by running the entire test suite
with `SMC_V1_RECORD=1` in both wall-clock session states.

| method | records | what it answers |
|---|---:|---|
| `is_poi_invalidated` | 7,200 | the KILL / STALE predicate (the 540-combo matrix × directions) |
| `score_signal` | 3,006 | the scoring function (the 3,000-case random dict matrix) |
| `find_liquidity_target` | 292 | TP selection over the direction × level × ATR matrix |
| `build_signal_context` | 120 | the full analysis pipeline over the corpus and fixture |
| `is_market_choppy` | 34 | the chop filter |
| `enrich_with_m5` | 32 | the M5 re-score |
| `identify_swings` | 25 | swing detection under both V1 parameter sets |
| `detect_sweep` | 25 | liquidity sweep detection, both directions |
| `classify_regime` | 16 | the regime classifier |
| `cluster_liquidity_levels` | 6 | EQH/EQL clustering |
| `get_previous_day_high_low` | 1 | PDH/PDL derivation |

## What the proof shows

| | |
|---|---:|
| tests in the suite | **2,401** |
| passing with V1 present (live oracle) | **2,401** |
| passing with V1 absent (recorded oracle) | **2,401** |
| tests lost to retirement | **0** |
| test methods that query the golden store | **97** |
| golden lookups per suite run | **11,303** |

The 97 figure counts test methods that call the store during their own
execution; calls made in `setUpClass` fixtures are attributed to the preceding
test, so the true number of equivalence-backed tests is slightly higher. The
figure that matters for retirement is different and exact: **146 tests would
have been destroyed by deleting V1 outright** (every test in a class whose
fixture constructs a V1 context), and none of them were.

## Two defects the recording exposed

Both were latent in the existing tests and invisible while V1 was called live.
Both are now closed.

**1. `build_signal_context` is not a pure function of its arguments.**
V1's `_bias_cache` (`src/strategy.py:9,21-34`) memoises D1/H4 bias under
`f"{timeframe}_{last_timestamp}"`. The synthetic corpus deliberately reuses
timestamps across frames, so a *shared* `SMCStrategy` instance serves the first
frame's bias to every later frame. `tests/analysis/test_scoring.py` had
documented this as "a latent V1 collision that never fires in production";
recording proved it, aborting the first pass on a genuine conflict. The golden
key now includes the instance's `_bias_cache`, and the stand-in advances its own
replica from each recorded post-call state — so a warm instance replays V1's
memoised answers exactly as V1 gave them, and a fresh instance replays the true
bias.

**2. `test_matches_v1_on_scored_contexts` was not reproducible across runs.**
It seeded its M5 generator with `random.Random(hash(name) & 0xFFFF)`. Python
salts string hashing per process, so the M5 frame differed on every run. Live
comparison hid this because V1 and V2 both saw whatever frame that run produced.
Replaced with `zlib.crc32(name.encode())`; the assertion is unchanged.

**3. One pre-existing conditional failure, unrelated to retirement.**
`tests.analysis.test_scoring.TestSignalMatchesV1.test_corpus_covers_every_decision_tier`
asserts the corpus reaches the A+ tier. It cannot outside V1's SNIPER session
window (weekdays 07:00–22:00 UTC), because the `session_active` scoring bonus is
what lifts the corpus's best case into A+. This is a V2-only assertion about V2
signals and fails identically with the live oracle. It is a property of the test,
not of the retirement.

## Session dependence, now explicit

V1 reads the wall clock in exactly one place (`get_session_mode`,
`src/strategy.py:14-19`) and two methods branch on it: `build_signal_context`
(line 1690, the `session_active` scoring bonus) and `enrich_with_m5` (line 1837,
the M5 swing parameters). Both are keyed by session state and recorded in both
states.

This makes the suite *more* deterministic than it was. Previously a corpus
comparison run near the session boundary could compare a V1 answer computed in
one session against a V2 answer computed in the other; that race is now
structurally impossible.
