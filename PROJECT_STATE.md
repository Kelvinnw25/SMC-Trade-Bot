# PROJECT_STATE.md — SMC Signal Radar

> **Single source of truth for the project.** Read this before touching code.
>
> - **Status:** Migration complete. V2 is the **only** implementation and the
>   authoritative system. **Phase 8 architecture is closed** (Milestone M0);
>   implementation starts at M1.
> - **Last updated:** 2026-08-02
> - **Doc version:** 3.8
> - **Performance roadmap:** `IMPLEMENTATION_PLAN.md`. **All six acquisition
>   merges are landed** — 0 (environment pinning), 1 (pooled HTTP), 2
>   (`Retry-After` / 418 / 429), 2b (`StorageTransient`), 3a (deterministic
>   `DownloadReport`), 3b (parallel downloader) — **plus the S11 interrupt bug
>   fix**. Next is **Gate A: Stage 2 acquisition** — `1d` from 2022-01-01 and
>   `5m` from 2022-12-01, with the TA.3 warmup pre-flight. Steps 4
>   (`CachingAnalyzer`) and 5 (vectorized `identify_swings`) may be developed in
>   parallel with Gate A.
> - **Gate A is two commands, not one.** `CorpusSpec` carries a single `start`
>   for all timeframes, so the plan's per-timeframe lead-ins cannot be expressed
>   in one invocation. Pass A (`--timeframes 1d --start 2022-01-01`) then Pass B
>   (`--timeframes 1h 1d 5m --start 2022-12-01`). **Pass B is mandatory**:
>   `CanonicalBuilder` replaces `manifest.json` with only the timeframes of that
>   call, so after Pass A the corpus is 1d-only and not replay-usable. Pass B
>   rebuilds from every committed raw record and restores all three. The final
>   manifest's `spec.start` therefore *under-declares* the corpus — verify TA.4
>   against each dataset's `first_open_ms`, not against the manifest.
> - **Runtime flag:** `corpus build --workers N` (default **1** = the sequential
>   path exactly; max 6). Ordering, artifacts and corpus identity are provably
>   independent of this value. Acquisition tunables:
>   `BAN_PAUSE_LIMIT_SECONDS = 300` in `backtest/acquire/downloader.py` — a
>   stated 418 ban longer than this stops the run instead of sleeping on it.
> - **Interrupting an acquisition:** Ctrl+C cancels all queued work and aborts
>   within the longest **in-flight** item — worst case 300 s (a ban pause), 60 s
>   (a transport timeout), typically sub-second. Work already running finishes
>   and commits normally; that is deliberate, because cutting a commit short is
>   the only way to leave a partial artifact. Re-run the identical command to
>   resume. Note that **absent periods are never committed and are therefore
>   re-requested on every restart** — a floor of ~20 min of repeated work at
>   Gate A scale, regardless of how late the run stopped.
> - **Environment:** **pinned in `requirements.txt`** (Python 3.13.5, 34 `==`
>   distributions), asserted by `tests/test_environment.py`. Windows, deps in
>   `venv/`. Changing a pin is a re-baselining event (C-19, C-5.7.2), not an
>   upgrade. `bottleneck` and `numexpr` must never be installed — they change
>   pandas' reduction dispatch and would violate C-8.5.
> - **Run tests:** `venv/Scripts/python.exe -m unittest discover -s tests -t .`
>   (repo root). `-t .` is required: without it `discover` puts `tests/` on
>   `sys.path`, and `tests/backtest/` then shadows the top-level `backtest`
>   package, so those modules fail to import and their tests never run.

---

## 1. What this is

**SMC Signal Radar** is a Python **advisory** trading-signal generator for
Binance USDT-M perpetual futures. It scans the top-volume pairs, runs top-down
Smart Money Concepts (SMC) analysis (D1/H1 context + optional M5 enrichment),
emits structural trade hypotheses (direction, entry, SL, TP, score, decision
tier), tracks them in a lightweight lifecycle memory (kill switch, decay, TTL,
slot cap, killed-thesis cooldown), and pushes Telegram alerts. It **does not
place orders** — every alert is a structural hypothesis; the operator decides
execution ("Contract B": `entry_price` is a *structural reference*, not an
executable order price).

**Project posture (new as of 2026-07-26).** The V1→V2 architectural migration is
**complete**. V2 is now the authoritative implementation. **Behavioral
equivalence with V1 is no longer a goal.** The objective is now to build the best
possible SMC system: improve signal quality, entries, exits, risk, and
validation. Changes to strategy behavior are now **allowed and expected** —
governed by evidence (backtests / forward tests), not by matching V1.

---

## 2. Current Architecture

Five strict, acyclic layers (`domain ← infra ← analysis ← lifecycle ← app`):

```
domain      v2/domain/     Immutable, dependency-free vocabulary (18 frozen dataclasses).
infra       v2/infra/      I/O adapters: clock, market_data, persistence, notifier.
                           Native — imports nothing from src/ (standalone since Phase 7.1).
analysis    v2/analysis/   Pure business logic: MarketSnapshot → Signal (14 modules).
lifecycle   v2/lifecycle/  Stateful reconciler + rules + store, on an injected clock.
app         v2/app/        Thin orchestrator + delivery/observability (no signal logic).

tests/                     784 tests (stdlib unittest, hermetic).
```

**Layer responsibilities**

| Layer | Owns | Must not own |
|---|---|---|
| `domain` | Typed immutable objects, equality, V1-schema serialization | Any logic, I/O, time |
| `infra` | Native exchange fetch, JSON/JSONL persistence (incl. the signal-ledger sink), Telegram send, clock | Strategy/scoring/lifecycle decisions |
| `analysis` | HTF/regime/structure/OB/FVG/liquidity/entry/risk/scoring; M5 gate; choppy penalty | Memory, persistence, notify, clock |
| `lifecycle` | Kill, stale, cooldown, same-thesis, insert/replace/cap, alert-upgrade, miss/decay, TTL, slot cap, killed store | Analysis, I/O, wall-clock |
| `app` | Scan loop, wiring, session_active, error isolation, delivery routing/rendering | Any branch that changes a signal's outcome (Principle 12) |

**Enforced invariants** (verified by tests + inspection): domain imports nothing;
**the V2 runtime imports nothing from `src/`** (standalone since Phase 7.1 — `src/`
is imported only by characterization tests); acyclic imports, no cycles; no
wall-clock reads in domain/analysis/lifecycle (injected `Clock`); the orchestrator
holds no signal-deciding branch. Observability records (`OBAuditRecord`,
`SignalLedgerRecord`) are write-only — never read back into a runtime decision.

---

## 3. Repository Layout

```
bot_trading_smc/
├─ v2/                     THE SYSTEM (all five layers). Authoritative. STANDALONE.
│   ├─ domain/  infra/  analysis/  lifecycle/  app/
│   ├─ run.py                entrypoint: `python -m v2.run` → Orchestrator.create().run_forever()
│   ├─ infra/config.py       native runtime config (Phase 7.1; replaced src/config)
│   ├─ domain/signal_ledger.py · app/ledger.py   Signal Ledger (Phase 7.1 evaluation dataset)
├─ src/                    V1 — HISTORICAL. No longer imported by the V2 runtime:
│   ├─ api_handler.py · telegram_notifier.py · config.py   internalized into v2/infra (Phase 7.1); now orphaned
│   ├─ strategy.py           legacy analysis — reimplemented in v2/analysis; still referenced by characterization tests
│   └─ executor.py           legacy order-exec helper — dead (no importer)
├─ main.py                 legacy V1 loop — reimplemented by v2/app; reference only
├─ tests/                  domain·infra·analysis·lifecycle·app·regression (784)
├─ signal_ledger.jsonl     LIVE evaluation dataset (append-only; created on first run)
├─ PROJECT_STATE.md · CHANGELOG.md · STRATEGY_NOTES.md · REPLAY_CONTRACT.md
│                          living docs (see §8 for responsibilities)
├─ SMC-BOT-DOCS/           migration-era governing spec (historical; see §7)
├─ BLUEPRINT.md · MODULE_INTENT.md   V1-era design notes (historical)
├─ active_pois.json · killed_thesis.json · retracement_ob_audit.jsonl   LIVE runtime state
└─ debug_api.py · final_test.py · test_api.py · test_strategy.py   legacy scratch scripts
```

> **Standalone since Phase 7.1.** The V2 runtime imports nothing from `src/`;
> `api_handler.py`, `telegram_notifier.py` and `config.py` were internalized into
> `v2/infra`. `src/strategy.py`, `main.py` and `src/executor.py` are reference only —
> but `strategy.py` is still imported by the characterization test suite, so it
> stays on disk until those tests are re-based on captured golden output.
>
> `backtest/` (the replay engine, Phase 8) does not exist yet. Its layout and
> dependency direction are fixed by `REPLAY_CONTRACT.md` §10: it imports `v2`
> freely, and **`v2` never imports `backtest`**.

---

## 4. Current Strategy (as implemented in `v2/analysis`)

A top-down, single-pass pipeline per symbol per cycle:

1. **HTF context** (`htf.py`) — prior-day/week levels; `weekly_bias` = price vs
   prior-week midpoint; `d1_bias`/`h4_bias` = EMA50/EMA200 + 2-swing structure
   (`_derive_simple_bias`); `location` = discount/premium/equilibrium;
   `momentum_weakening`. Direction from a weighted vote (weekly 4 · d1 2 · h4 1 ·
   location ±0.5–1.5, H1 fallback), with a rotational weekly-anchor override and a
   hard expansion-vs-direction veto.
2. **Regime** (`regime.py`) — 3-vote compression/expansion classifier over ATR
   ratios, body ratio, drift → {expansion_up/down, accumulation, distribution,
   rotational, compression, neutral, unknown} → execution mode {CONTINUATION,
   RETRACEMENT, WATCH_ONLY, NONE}.
3. **Structure / OB / FVG / liquidity** — swings; order blocks (raw origin candle
   in a 10-bar window, size/not-lost/distance quality filter, first-match
   selection); FVG (boolean three-candle imbalance); liquidity sweep
   (pierce + reclaim + depth + body-reclaim).
4. **Entry** (`entry.py`) — CONTINUATION: displacement (≤3 bars back) + micro-
   pullback (0.10 ATR) + reclaim → pullback close. RETRACEMENT: fresh quality OB →
   entry at block edge. WATCH_ONLY / NONE otherwise.
5. **Risk** (`risk.py`) — SL: continuation pullback bar ±0.30 ATR; retracement OB
   far edge ±0.70 ATR. TP: RR-aware liquidity target (EQH/EQL cluster → swing →
   PDH/PDL), else fixed **2R** fallback. Hard **1.2 RR floor**.
6. **Scoring** (`scoring.py`) — additive score: HTF alignment (weekly 2 · d1 2 ·
   h4 1 · location 2), mode-aware confirmations (sweep/FVG/OB/displacement),
   structure quality (target_clear/session/htf_clean), regime, RR bonus (1.5/2/3),
   momentum, M5 CHoCH; choppy penalty −2. Tiers per mode → SKIP/WATCH/VALID/A+.
7. **M5 enrichment** — re-scores with M5 CHoCH confirmation when the pre-M5 score
   ≥ 8.

**Lifecycle** (`v2/lifecycle`) — kill (HTF D1+H4 flip **or** SL break ±0.30 ATR),
pre-memory stale, kill-cooldown (1h), same-thesis partial refresh, insert /
replace-weakest-by-score / slot cap (10), alert new/upgrade dedup by tier,
H1-gated miss increment → decay eviction at 3 misses, TTL (24h untapped / 6h
tapped / 10h manual).

> The full itemized strategy audit (weak assumptions, duplicated confirmations,
> score inflation, entry/exit/kill/lifecycle weaknesses A1–A25), the two confirmed
> implementation bugs (BUG-1, BUG-2), the validated design decisions, and the
> experiment log live in **`STRATEGY_NOTES.md`** — the permanent strategy
> notebook. This file (`PROJECT_STATE.md`) does not duplicate them; it points to
> them.

---

## 5. Testing Status

**784 tests**, stdlib `unittest`, hermetic (no network / Telegram / real FS /
wall clock): domain 45 · infra 87 · analysis 478 · lifecycle 58 · app 71 ·
regression 45. (The +21 over the prior 763 are the Phase-7.1 Signal Ledger:
domain schema, infra sink, app assembler + orchestrator wiring.)

- **All 784 pass when run inside session hours.** One test is
  **calendar-dependent**: `tests/analysis/test_scoring.py::test_corpus_covers_every_decision_tier`
  reads the wall clock (`SMCStrategy().get_session_mode()`), so run off-session it
  cannot reach the `A+` tier and fails. Verified green 2026-07-27. Fix candidate:
  inject a fixed session flag into the corpus builder (Phase-7 test cleanup).
- Analysis and lifecycle tests currently assert against V1's outputs
  (characterization / golden-master). As strategy behavior intentionally changes
  in Phase 9+, these must be **re-based on captured expected output**, not on the
  V1 oracle. Track this in the Phase-7 evaluation.

---

## 6. Roadmap (post-migration)

**Current priorities (Phase 7→8 transition):**
- ✅ **V2 runtime standalone** — `infra` decoupled from `src/` (Phase 7.1); runner
  `v2/run.py` confirmed present.
- ✅ **Signal Ledger** — every completed analysis (all tiers) recorded to
  `signal_ledger.jsonl` (Phase 7.1). Write-only, append-only.
- ✅ **BUG-1 + BUG-2 fixed** (2026-07-26, re-validated under the contract):
  continuation `tp_source`/`tp_rr` published; OB-audit selected-OB/window/
  location/current_price populated. Metadata-only (proven: no decision-bearing
  test changed); `STRATEGY_VERSION` → **2.0.1**.
- ✅ **Replay & Outcome Semantics Contract v1.1** — `REPLAY_CONTRACT.md`
  (2026-07-27, Milestone **M0**). Replaces v1.0 entirely. Closes the four
  architectural blockers that stood between the repository and Phase 8:
  | Blocker | Finding | Closed by |
  |---|---|---|
  | B1 replay time semantics | A47 | §5 (C-5.0–C-5.8): `T` = close boundary, `h1_last_ts = T − 3600`, clock = `T + 1s`, ordering guarantees, burn-in |
  | B2 window construction | A49 | §6 (C-6.2–C-6.4, C-6.7–C-6.11): row-slice windows, prohibition list, partial history, integrity |
  | B3 lifecycle capture | A50 | §15: `RecordingReconciler`, event schema, TTL attribution with a completeness proof |
  | B4 ledger lineage | A53 | §16: `schema_version` 2, nine lineage fields, zero-cost migration |
  Also corrected a factual error in v1.0 (C-5.2 M5 fidelity, finding A48) and
  closed the residual fill-model gaps (walk origin, pre-crossed limits, missing-M5
  fallback, R arithmetic, shadow classes).
- ⏳ **Phase 8 implementation** — starts at **M1** (ledger schema 2, the only
  production change the contract authorizes; scope fixed by C-16.3.1), then
  M2 ports · M3 repository · M4 downloader · M5 universe table · M6 adapter ·
  M7 captures · M8 driver · M9 performance · M10 outcome engine · M11 metrics.
  **No strategy-behavior change anywhere in Phase 8.**

| Phase | Name | Goal |
|---|---|---|
| **7** | **Evaluation** ⬅ current | Fix metadata bugs; add a runner + instrumentation; capture a real signal dataset; publish the audit (`STRATEGY_NOTES.md`) + baseline metrics; decouple `infra` from `src/`. **No strategy-behavior change.** |
| 8 | Backtesting Framework | A deterministic, event-driven backtester over historical klines that replays `analysis`+`lifecycle` and scores hypothetical outcomes. **Semantics fixed by `REPLAY_CONTRACT.md` v1.1** (Orchestrator reuse, H1-cadence replay at `T`/`T+1s`, row-slice windows, lifecycle capture, M5 outcome resolution, alert-instantiated trades). Architecture closed at M0; implementation M1–M11. |
| 9 | Strategy Refinement | Fix the confirmed defects and weak heuristics (evidence-driven): OB quality/selection, HTF de-duplication, kill confirmation, decay semantics, TP/SL structure. |
| 10 | Parameter Optimization | Calibrate thresholds (regime cutoffs, RR floor, ATR multiples, score weights, TTLs) against the backtester with walk-forward validation; guard against overfitting. |
| 11 | Forward Testing | Paper/shadow-run the refined system live; compare predicted vs realized; measure hit-rate, expectancy, drawdown of the *advice*. |
| 12 | Production | Promote the validated configuration; optional execution integration; monitoring, alerting, and a governed change process. |

Details, acceptance criteria, and the improvement backlog are in the CHANGELOG's
Strategy Evolution chapter.

---

## 7. Historical: the V1→V2 Migration (closed)

The repository began as **V1** (`main.py` + `src/`), a working but monolithic SMC
radar. Between 2026-07-22 and 2026-07-25 it was re-architected into the layered V2
above **without changing observable behavior**, proven at every step against V1
(regression harness + per-layer V1 reference oracles). Milestones M0–M8: docs
baseline → domain → infra → regression harness → analysis → lifecycle →
orchestrator → delivery. The migration is complete; V2 is authoritative. Full
engineering history is in `CHANGELOG.md`; the migration-era governing spec is in
`SMC-BOT-DOCS/` (now historical reference, not an active constraint). Decisions
#001–#010 (`SMC-BOT-DOCS/05-decision-log.md`) still explain *why* the code is
shaped as it is — useful context, but no longer binding on new work.

---

## 8. Working Rules (current)

1. **Evidence over intuition.** A strategy change ships only with a backtest /
   forward-test result showing it helps (or is neutral with a sound rationale).
   No "obvious improvements" merged blind.
2. **One change at a time, measured.** Isolate each strategy change so its effect
   is attributable. Keep the pipeline's purity and layer boundaries intact.
3. **Preserve the architecture.** The layer rules (§2) still hold. New logic goes
   in the layer that owns it; the orchestrator never gains a signal-deciding
   branch; analysis/lifecycle stay clock-free and pure.
4. **No new dependencies without approval.** Tests remain stdlib `unittest`.
   pytest is not installed.
5. **Characterization tests are a safety net, not a spec.** When you intend a
   behavior change, update the affected tests deliberately (re-base on new
   expected output) — never silently loosen them.
6. **V1 is no longer the reference.** Do not add new "match V1" assertions. Retire
   the V1 coupling as tests are re-based.
7. **Live state is production data.** `active_pois.json`, `killed_thesis.json`,
   `retracement_ob_audit.jsonl` — back up before schema changes.
8. **One responsibility per living document.** `PROJECT_STATE.md` = current state
   (this file). `CHANGELOG.md` = history. `STRATEGY_NOTES.md` = strategy knowledge
   base (bugs, weaknesses, findings, decisions, experiment log).
   `REPLAY_CONTRACT.md` = **binding** replay/outcome/backtest semantics (clause
   numbers C-x.y; amendments bump its version). Do not duplicate their content
   here — link to them.
9. **One canonical replay specification.** `REPLAY_CONTRACT.md` v1.1 is the only
   place replay semantics are defined. Any other document that touches replay
   points at a clause number instead of restating it; if a needed fact is absent
   from the contract, that is a contract defect and the fix is an amendment (a
   version bump), never a local decision.

---

*End of PROJECT_STATE.md — update at every milestone boundary.*
