# Execution Checklist — first full historical research run

The operational playbook. Another engineer should be able to execute the whole
roadmap from this file alone.

Authority: `REPLAY_CONTRACT.md` for replay semantics, `PHASE0_FREEZE.md` for what
is frozen and what changing it costs. This file never overrides either.

**Conventions.** Every phase has a **GO/NO-GO gate**; a NO-GO is a stop, not a
warning. *Immutable* decisions cannot be revisited without invalidating work
already done. Every `--heartbeat`/`--quiet` flag is available on long jobs; logs
land in `<root>/logs/<job>.jsonl`.

**Total budget:** ~5 GB and ~4 h (Stage 1) → minutes (Coverage) → ~10 h
(Shakedowns) → ~60 GB and ~3 h (Stage 2) → ~100 h + ~27 h (Replay) → minutes
(Baseline).

---

## Phase 0 — Repository freeze

**Prerequisites** — clean working tree; full suite green.

| # | Action | Done when |
|---|---|---|
| 0.1 | Run the suite | `venv/Scripts/python.exe -m unittest discover -s tests -t .` passes |
| 0.2 | Universe Definition frozen | `v2/domain/universe.py` present; `UNIVERSE_DEFINITION_VERSION = 1` |
| 0.3 | `STRATEGY_VERSION` verified | `backtest.lineage.STRATEGY_VERSION == v2.app.ledger.STRATEGY_VERSION == "2.1.0"` |
| 0.4 | Falsification protocol dated | `PHASE0_FREEZE.md` §4, six dimensions listed |
| 0.5 | **Delisted classification table** | 180 archive-only symbols adjudicated, committed, hashed |

**Artifacts** — `PHASE0_FREEZE.md` · `EXECUTION_CHECKLIST.md` · `v2/domain/universe.py`

**Immutable** — Universe Definition · falsification protocol · the six dimensions

**Success** — 0.1–0.4 complete. **Failure** — versions disagree, or the protocol
is undated. Either is a stop: an undated protocol is not a pre-registration.

> **GATE.** Universe frozen and falsification protocol written **before any data
> is acquired**. Item 0.5 may run in parallel with Stage 1 but must complete
> before Stage 2.

---

## Phase 1 — Stage 1 corpus (inventory probe)

**Prerequisites** — Phase 0 gate; ~5 GB free; network.

```
python -m backtest corpus build \
    --root data/stage1 --timeframes 1h \
    --start YYYY-MM-DD --end YYYY-MM-DD --heartbeat 60
```

**Cost** — ~3 GB, ~4 h, unattended.

**Artifacts** — `corpus.json` · raw 1h · discovery evidence (`exchangeInfo` +
archive listing) · `logs/corpus-build.jsonl`

**Operator decisions** — window bounds; whether isolated non-union failures are
acceptable.

**Success** — `include_archive=true`; ~828 symbols discovered; `strict` passes or
every failure is attributed in the failure manifest.

**Failure** — the archive listing or `exchangeInfo` fetch fails (**stop** —
discovery evidence is the corpus's provenance); failure rate above the declared
threshold.

**Notes** — fully resumable. Re-running skips every committed file; interrupt
freely. `already_covered` in the log distinguishes "nothing is happening" from
"40,000 files correctly skipped".

```
python -m backtest corpus verify --root data/stage1 --shallow
```

> **GATE.** Corpus verifies and `corpus_id` reproduces on a second build.

---

## Phase 2 — Coverage review *(the decision gate)*

**Prerequisites** — Phase 1 gate.

```
python -m backtest coverage \
    --corpus data/stage1 --out data/stage1 \
    --candidates 2021-01-01T00:00:00Z 2022-01-01T00:00:00Z … \
    --top 30 --wide 50 --stride 6 --heartbeat 30
```

**Cost** — minutes.

**Artifacts** — `coverage.json` · `coverage.md`

**Operator decisions — both immutable once taken**

1. **Corpus window start.** Read the candidate table. The report scores; it does
   not choose. Record the reason alongside the choice.
2. **Holdout length.** 12 months only if the usable window is ≥ 5 years;
   otherwise 6 months, declared explicitly as reduced power.

**Success criteria** (thresholds from `PHASE0_FREEZE.md` §8)

- ≥ 30 rankable symbols in **every** hour of the chosen window
- union symbols ≥ 99.5 % H1 complete over their listed life
- no unattributed gap > 24 h in a union symbol while it was ranked
- gap ratio ≤ 0.01

**Failure** — no candidate reaches PASS. Narrow the window; do not relax the
thresholds. Relaxing a threshold after seeing the data converts a protocol into a
rationalisation.

**Stage 2 sizing** — read `promotion_rate`: of the symbols ever in ranks 31–50,
the share later reaching the top-30. High → the band feeds the traded universe and
Top-50 is justified; low → Top-30 is defensible. Incremental cost is
`marginal_count × months × 6.9 MB` (measured).

> **GATE — formal.** Report reviewed; window and holdout length chosen; an
> explicit acknowledgement recorded in the audit trail naming both and the reason.

⚠ **If the data floor is later than hoped, do not stretch the window on thin early
coverage.** A clean three-year corpus beats a five-year one whose first two years
are sparse — the gaps concentrate in exactly the small and delisted symbols whose
absence created the survivorship problem this work exists to solve.

---

## Phase 3 — Shakedown A (pathology)

**Prerequisites** — Phase 2 gate; 5m acquired for the shakedown candidates.

```
python -m backtest replay \
    --corpus data/stage1 --out data/shakedown-a \
    --start <T0> --end <T0 + 90 days> --burn-in 336 \
    --universe-mode static --static <shakedown candidates> --heartbeat 60
```

**Cost** — ~4.4 h.

**Why static** — the candidates must be scanned *every cycle*; under volume mode a
low-liquidity pathological symbol would rarely rank into the top-30 and would go
untested.

**Success** — completes with `max_swallowed_errors = 0`, zero swallowed,
`health.clean == true`.

**Failure** — any crash. **Fix and re-run; do not proceed.** This is the highest
value-per-hour step in the project: ~1.3 M symbol-scans against data the analyzer
has never seen — flat ATR, zero-volume hours, tokenized-equity trading calendars.

---

## Phase 4 — Shakedown B (scale)

```
python -m backtest replay \
    --corpus data/stage1 --out data/shakedown-b \
    --start <T0> --end <T0 + 30 days> --burn-in 336 \
    --universe-mode volume --top 30 --heartbeat 60
```

**Cost** — ~5.3 h.

**Why it is mandatory** — `universe_mode="volume"` has **never** been run on a
real corpus. Every run on disk used `static` with five symbols. The hourly ranking
path across a large inventory is untested code, and it is the one cost that scales
with inventory size.

**Success** — completes clean; RSS flat across heartbeats; trailing throughput not
decaying against cumulative.

**Failure** — throughput decay or unbounded RSS means the ranking path is
O(inventory) per cycle. **Stop and investigate**; at five-year scale it turns 100 h
into 1,000 h.

**Artifact** — the **measured** replay-hours/second that replaces the extrapolated
~100 h estimate. Recompute the Phase 6 budget from it.

> **GATE.** Both shakedowns clean. Only now is the no-resume policy safe.

---

## Phase 5 — Stage 2 corpus

**Prerequisites** — Phase 4 gate; Phase 0 item 0.5 complete; ~60 GB free.

```
python -m backtest corpus build \
    --root data/stage2 --timeframes 1h 1d 5m \
    --symbols <top-50 union ∩ eligible> \
    --start <chosen> --end <chosen> --heartbeat 60
```

Acquire the 5m footprint for the **top-50** union, not top-30: the first planned
A-series experiment is `top_coins_limit=50`, and pruning to 30 would silently
sabotage it.

Then:

```
python -m backtest holdout declare --root data/research \
    --corpus data/stage2 --start <H0> --end <H1> --label baseline-holdout \
    --reason "most recent 12 months; declared before any experiment"

python -m backtest splits --start <T0> --end <H0> \
    --train … --test … --embargo 168 --burn-in 336 --research data/research
```

**Immutable** — holdout window · split specification hash · `corpus_id` ·
`data_snapshot_id`

> **GATE.** `corpus verify` (deep) passes; holdout declared and `assert_untouched`;
> split spec frozen and hashed.

---

## Phase 6 — Official replay

**Prerequisites** — Phase 5 gate. Disable OS auto-updates; verify disk headroom
against projected output; run detached.

**6a — canonical baseline (Approach A).** One unsharded run per C-5.7.4, scoped to
the **non-holdout window** so holdout records cannot exist in `outcomes.jsonl`.

```
python -m backtest replay \
    --corpus data/stage2 --out data/runs \
    --start <T0> --end <H0> --burn-in 336 \
    --top <30|50> --universe-mode volume --heartbeat 60
```

**6b — fold independence evidence (B′).** Five independently burned-in fold runs,
spanning the corpus (early / bull / bear / chop / late). Parallelisable — ~27 h.

**Pre-registered carryover threshold:** if, for every sampled fold, the A-slice
estimate differs from the B′ estimate by less than the fold-level bootstrap CI on
every `HEADLINE` metric, declare lifecycle carryover **immaterial** and licence
A-slicing for A1–A25. Otherwise the divergence is itself a finding.

**Success** — `cycles_processed == cycles`; zero swallowed; `health.clean`.

**Failure** — **restart from zero.** No resume in V1. The failed bundle is
archived automatically on `--overwrite`; read `logs/replay.jsonl` for the
traceback, `error_type` and how far it got before deciding what to fix.

---

## Phase 7 — Baseline freeze

```
python -m backtest outcome --corpus data/stage2 --run data/runs/runs/<run_id>
python -m backtest metrics --run data/runs/runs/<run_id>
```

**Apply `PHASE0_FREEZE.md` §4 before interpreting anything.**

1. **Tier 1 apparatus gates.** Any hard failure → results uninterpretable; stop.
2. **Split-half segment-ordering stability** across the six pre-registered
   dimensions.
3. **Fold sign-consistency** of `expectancy_per_signal`.

> **GATE.** If ordering is unstable in half or more of the six dimensions,
> **A1–A25 does not begin.** That is the protocol working.

```
python -m backtest freeze --run data/runs/runs/<run_id> \
    --out data/baselines/v1.0 --label baseline-v1.0
```

**Immutable** — `metrics_hash` · `baseline_id`

---

## Critical mistakes

1. Editing or truncating any append-only evidence file.
2. Changing analysis, scoring, lifecycle **or the Universe Definition** without
   bumping `STRATEGY_VERSION`.
3. Evaluating the holdout because you were curious.
4. Relaxing a Coverage threshold after seeing which candidate fails.
5. Rebuilding a corpus in place while runs reference its `data_snapshot_id`.
6. Deleting a failed run bundle instead of archiving it.
7. Letting the disk fill — the evidence sinks truncate quietly.
8. Stretching the corpus window onto thin early coverage.
