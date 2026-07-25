# PROJECT_STATE.md — SMC Signal Radar V2 Migration

> **Single source of truth for the migration.** Read this before touching code.
> It is maintained at every milestone. If it conflicts with memory or chat
> history, **this file and the repository win.**
>
> - **Checkpoint:** Phase 5.2 (Delivery & Observability) complete — **migration
>   functionally complete**; now in **Phase 6 (Shadow Validation & Cutover Prep)**.
> - **Last updated:** 2026-07-25
> - **Doc version:** 2.0
> - **Environment:** Python 3.13, pandas 3.0.3, all deps in `venv/`. Windows.
> - **Run tests:** `venv/Scripts/python.exe -m unittest discover tests` (from repo root)

---

## 1. Project Overview

**Project purpose.** "SMC Signal Radar" is a Python **advisory** trading-signal
generator for Binance USDT-M perpetual futures. It scans the top-volume pairs,
runs top-down Smart Money Concepts (SMC) analysis (D1/H1 context + optional M5
enrichment), emits structural trade hypotheses (direction, entry, SL, TP, score,
decision tier), tracks them in a lightweight memory with a lifecycle (kill
switch, decay, TTL, slot cap), and pushes Telegram alerts. It **does not place
orders** — every alert is a structural hypothesis; the operator decides
execution (see Decision #001, "Contract B").

**Migration purpose.** Re-architect the working-but-monolithic **V1**
(`main.py` + `src/`) into a **layered, testable V2** (`v2/`) **without changing
any observable behavior.** V1 keeps running untouched; V2 is built underneath it
and only replaces V1 once proven behaviorally identical. **All five layers now
exist** — the migration is *code complete*; what remains is shadow validation
against V1 and the operational cutover (Phase 6).

**Current architecture (V2 — all layers built).** Strict acyclic layers:

```
domain  (pure, immutable, zero deps)                         ✅ built · frozen
  ▲
infra   (I/O adapters that WRAP V1 code — the only layer allowed to import src/)  ✅ built · frozen
  ▲
analysis   (pure business logic — MarketSnapshot → Signal)   ✅ built · frozen
  ▲
lifecycle  (stateful reconciler + rules + store; injected clock)  ✅ built · frozen
  ▲
app        (thin orchestrator + delivery/observability)      ✅ built
            (v2/app/orchestrator.py · rendering.py · audit.py)

regression harness (tests/regression/) — verification only, outside runtime
```

**Overall philosophy.**
- Behavioral equivalence with V1 is the supreme rule — provable, not hopeful.
- Build the **pure deterministic core first**, wrap it in state and I/O last
  (the inverse of how V1 grew).
- Prove equivalence **at every step** (regression harness + per-layer V1
  reference oracles).
- One small milestone at a time; freeze each when done.
- The six frozen docs in `SMC-BOT-DOCS/` are the governing specification.

---

## 2. Current Version

| Field | Value |
|---|---|
| **Current milestone** | Phase 5.2 Delivery & Observability — **COMPLETE**. Migration is **code complete**. |
| **Current phase** | **Phase 6 — Shadow Validation, Cutover Preparation & Final Review** (no new development; validation + docs only) |
| **Architecture state** | Domain ✅ · Infrastructure ✅ · Analysis ✅ · Lifecycle ✅ · App (orchestrator + delivery) ✅ · Regression harness ✅ |
| **Test status** | **763 tests** (domain 39 · infra 83 · analysis 478 · lifecycle 58 · app 60 · regression 45). **762 pass; 1 known calendar-dependent flake** — see note below. |
| **V1 status** | Untouched and runnable (`python main.py`) |
| **Git status** | ✅ **Git repository.** Branch `main`, 12 commits, tag `v0.1.0`. One commit per phase. |
| **Git tag** | `v0.1.0` present. Recommended before cutover: tag this checkpoint `v2-code-complete` and the post-shadow cutover `v2-cutover`. |

> **Known non-blocking flake:** `tests/analysis/test_scoring.py::test_corpus_covers_every_decision_tier`
> asserts the inline corpus reaches an `A+` tier, but its `SESSION_ACTIVE`
> reads the **wall clock** (`SMCStrategy().get_session_mode()`). On weekends /
> outside 07:00–22:00 UTC the +1 session bonus is absent, so the corpus tops out
> at `VALID` and the assertion fails. This is a **test-construction** issue in a
> frozen Phase-3 analysis test (it depends on when the suite runs, not on any V2
> code). It is **documented, not fixed**, because touching it would reopen a
> frozen module outside an approved milestone. Fixing it (injecting a fixed
> session flag into the corpus builder) is a candidate Phase-6 housekeeping item.

---

## 3. Completed Milestones

| # | Milestone | What was delivered | Key architectural decisions |
|---|---|---|---|
| 0 | **Documentation baseline** | 6 frozen governing docs in `SMC-BOT-DOCS/` synchronized; Decision **#010** added; proposed patches **P1–P5 applied** (with approval). | Frozen-docs workflow: never edit `SMC-BOT-DOCS/` without an explicit approved patch. |
| 1 | **Domain Model Foundation** (`v2/domain/`) | 18 immutable frozen dataclasses (the full typed vocabulary). 39 tests. | Immutable `@dataclass(frozen=True)`; `str` enums to preserve V1 serialization; `MarketSnapshot` DataFrames immutable **by contract**; `Signal.candidate` is single source of truth; exact 17-field `ActiveSignal` schema + V1 load-lossiness reproduced. |
| 2.1 | **Clock adapter** (`v2/infra/clock.py`) | `Clock` Protocol, `SystemClock`, `FakeClock`. 21 tests. | Injectable time (no wall-clock reads leak); enables deterministic lifecycle. |
| 2.2 | **Market Data adapter** (`v2/infra/market_data.py`) | Wraps V1 `BinanceHandler`; produces `MarketSnapshot`. 24 tests. | Sole V2 entry to exchange data; mirrors V1 fetch limits, `iloc[:-1]` strip, live-price separation, M5 guards. |
| 2.3 | **Persistence adapter** (`v2/infra/persistence.py`) | Wraps V1 JSON persistence; reuses domain serialization; `append_ob_audit` JSONL sink. 24 tests. | Zero schema drift (V1↔V2 read each other); missing→empty; **error-surfacing divergence**: malformed/OS errors raise `PersistenceError` where V1 swallowed. |
| 2.4 | **Notifier adapter** (`v2/infra/notifier.py`) | `Notifier` Protocol + `TelegramNotifier` wrapping V1 senders. 14 tests. | Backend-agnostic seam; routes `Alert` by `.kind`; text-only; `NotifierError` on failure (error-surfacing divergence). |
| — | **Golden Oracle / Regression Harness** (`tests/regression/`) | `InputSnapshot`, `ObservableBehavior`, `Analyzer` + `V1Oracle`, comparison engine, reports, runner, generator CLI, committed fixture + golden. 45 tests. | Verification-only, outside runtime; runs V1's **unmodified** functions and reads intermediates from `context["debug"]` (no V1 instrumentation). |
| 3 | **Pure Analysis Layer** (`v2/analysis/`) | 14 pure modules (`htf`, `regime`, `structure`, `order_block`, `fair_value_gap`, `liquidity`, `entry`, `risk`, `scoring`, `frames`, `stage`, `context`, `pipeline`, `engine`) producing an immutable `Signal` from a `MarketSnapshot`. 478 tests. | Pure & stateless (no memory/persistence/notify/clock); staged pipeline; `V2Analyzer.analyze()` owns the M5 `score≥8` gate and the choppy penalty; diffed field-for-field against the real `SMCStrategy` V1 oracle. |
| 4 | **Lifecycle Core** (`v2/lifecycle/`) | `config`, `rules` (ONE copy of each V1 predicate incl. the KILL `is_poi_invalidated`), `memory` (read-only view), `store` (single writer + pure TTL sweep), `reconciler` (verbatim transcription of V1's memory engine). 58 tests. | Lifecycle owns all state/transitions; injected clock only; `LifecycleDecision` is declarative (mutations + alerts + log_events + killed_theses); proven via a 12-seed × 60-cycle × 6-symbol replay against a V1 reference. |
| 5.1 | **Thin Orchestrator** (`v2/app/orchestrator.py`) | Startup/wiring, scan loop, per-symbol isolation, `session_active` (clock only), fetch → analyze → reconcile → apply → dispatch → persist. 23 tests (initial). | Coordination only, no signal-deciding branch (Principle 12); runs `pipeline.run` once (context/HTF/pre-M5) + `analyze` once (final Signal); M5 gate stays in analysis. |
| 5.2 | **Delivery & Observability** (`v2/app/rendering.py`, `audit.py`, orchestrator) | Pure dashboard/scan/LogEvent renderers; `assemble_ob_audit` from domain objects; wired routing (terminal / notifier / OB-audit JSONL) + V1 cadence (0.20s fetch, 30s empty, 60s cycle). 60 app tests total. | Presentation is pure; audit reads domain objects only (reuses `lifecycle.rules.levels_valid`, no re-analysis); RAW-score & OB-audit V1 quirks reproduced; dedup set orchestrator-owned (Decision #010). |

Full rationale for each behavioral decision is in `SMC-BOT-DOCS/05-decision-log.md`
(**Decisions #001–#010**, all binding).

---

## 4. Current Folder Structure

```
bot_trading_smc/
├─ main.py                 V1 orchestrator loop (the running system). DO NOT MODIFY.
├─ src/                    V1 modules. DO NOT MODIFY.
│   ├─ api_handler.py        Binance fetch + ATR (BinanceHandler)
│   ├─ strategy.py           SMCStrategy: build_signal_context / score_signal /
│   │                        enrich_with_m5 / is_poi_invalidated  ← all analysis logic
│   ├─ telegram_notifier.py  send_signal_alert / send_upgrade_alert / send_kill_alert
│   ├─ config.py             env + thresholds
│   └─ executor.py           V1 order-execution helper (not used by the radar path)
│
├─ SMC-BOT-DOCS/           Frozen governing specification (6 docs). Change only via approved patch.
│   ├─ 00-project-context.md · 01-legacy-documentation.md · 02-software-architecture.md
│   ├─ 03-implementation-roadmap.md · 04-engineering-rules.md · 05-decision-log.md (#001–#010)
├─ BLUEPRINT.md · MODULE_INTENT.md   V1-era design notes (legacy reference).
│
├─ v2/                     THE MIGRATION TARGET — all layers built.
│   ├─ domain/             Phase 1 — 18 immutable objects. Zero dependencies. FROZEN.
│   ├─ infra/              Phase 2 — clock, market_data, persistence, notifier. FROZEN.
│   │                      (Only layer permitted to import src/.)
│   ├─ analysis/           Phase 3 — 14 pure modules; MarketSnapshot → Signal. FROZEN.
│   ├─ lifecycle/          Phase 4 — config, rules, memory, store, reconciler. FROZEN.
│   └─ app/                Phase 5 — orchestrator.py, rendering.py, audit.py.
│                          Coordination + delivery routing only (no business logic).
│
├─ tests/                  Authoritative test suite (stdlib unittest) — 763 tests.
│   ├─ domain/ (39) · infra/ (83) · analysis/ (478) · lifecycle/ (58) · app/ (60)
│   └─ regression/ (45)   Golden Oracle / Regression Harness + README + fixtures. FROZEN.
│
├─ active_pois.json · killed_thesis.json · retracement_ob_audit.jsonl
│                          V1 LIVE runtime state/artifacts. Treat as production data.
└─ debug_api.py · final_test.py · test_api.py · test_strategy.py
                           Legacy V1 ad-hoc scripts. NOT part of the migration suite.
```

> The **authoritative test suite is `tests/`** (run via `unittest discover
> tests`). The root-level `test_*.py` / `debug_api.py` / `final_test.py` are V1-era
> scratch scripts — do not treat them as the migration's tests.

---

## 5. Architecture Rules (immutable)

| # | Rule |
|---|---|
| A1 | **`v2/domain` depends on NOTHING** — no `src/`, no `v2/infra`, no I/O, no clock, no network. (Enforced by `tests/domain/test_domain_purity.py`.) |
| A2 | **Only `v2/infra` may import `src/`.** It is the sole anti-corruption boundary that wraps V1. No other V2 layer imports `src/`. (Verified: zero `src/` imports outside infra.) |
| A3 | **V1 is never modified.** `main.py`, `src/`, `BLUEPRINT.md`, `MODULE_INTENT.md` stay byte-for-byte as-is until cutover. |
| A4 | **Dependency direction is acyclic:** `domain ← infra ← analysis ← lifecycle ← app`. No layer imports a layer above it; no cycles. (Verified: import graph clean, no circular imports.) |
| A5 | **All domain objects are immutable** frozen dataclasses. The single exception is `MarketSnapshot`'s pandas DataFrames (immutable *by contract*). |
| A6 | **Adapters & orchestrator use constructor dependency injection.** No singletons, no global state, no DI framework. The real dependency is the default; tests inject fakes (incl. `writer`/`sleeper`). |
| A7 | **Responsibility separation:** infra = I/O only; analysis = pure business logic only; lifecycle = state only; app = coordination + presentation only (no signal-deciding logic). |
| A8 | **The regression harness is verification-only.** It lives outside runtime (`tests/regression/`); no runtime module (`v2/`, `src/`, `main.py`) may import it. |
| A9 | **No wall-clock reads in analysis/lifecycle/domain.** All time comes from the injected `Clock`. The orchestrator reads the clock only for `session_active`, and `time.sleep` only for cadence (both injectable). (Verified: zero `datetime.now`/`utcnow`/`time.time` in those layers.) |
| A10 | **Principle 12 — the orchestrator holds no branch that changes a signal's outcome.** The M5 `score≥8` gate and the choppy penalty live in `analysis`, not `app`. (Verified by inspection + the "owns-no-logic" orchestrator tests.) |

---

## 6. Migration Principles (every future change must obey)

1. **Behavioral equivalence is supreme.** V2 must produce identical observable
   output to V1. When the Architecture spec and V1's *actual behavior* disagree,
   **V1 wins** (doc-priority: Engineering Rules > V1 actual behavior >
   Architecture > Roadmap > Decision Log).
2. **No redesign, no optimization.** Never "improve" strategy, scoring,
   thresholds, or lifecycle. A cleaner V2 that behaves differently has **failed**.
3. **Regression-first / equivalence-proven.** Every behavior-bearing layer is
   diffed against V1's unmodified functions before the layer above consumes it.
4. **Incremental, small milestones.** One phase at a time. **STOP** at each
   boundary and wait for explicit approval before the next.
5. **Freeze completed modules.** Once a milestone passes, its code is frozen;
   reopening it requires a new, explicitly-approved milestone.
6. **Surface, don't silently fix, questionable legacy behavior.** Preserve V1
   quirks; document concerns separately (Decision #010).
7. **No new dependencies** without explicit approval. Tests use **stdlib
   `unittest`** only. **pytest is not installed and must not be introduced.**
8. **Frozen docs.** Never edit `SMC-BOT-DOCS/` directly; deliver a proposed patch
   and wait for approval.
9. **Deliberate divergences are documented.** The sanctioned behavior changes:
   *error surfacing* (adapters raise typed exceptions where V1 swallowed — the
   orchestrator re-applies V1's swallow-and-continue at the edge) and the
   *behavior-neutral M5 fetch cadence* (V2 always fetches M5; `analyze` decides
   whether to use it). Message/format/values are unchanged.

---

## 7. Verification Model (how equivalence is proven)

**Two complementary mechanisms, both hermetic (no network / Telegram / real FS /
wall clock):**

1. **Regression harness** (`tests/regression/`) — feeds one `MarketSnapshot`
   identically to `V1Oracle` (V1's unmodified `build_signal_context` /
   `enrich_with_m5` / `is_poi_invalidated`, reading its own `context["debug"]`)
   and to `V2Analyzer`, then diffs two `ObservableBehavior` records field-by-field
   with **exact equality by default**. `compare_analyzers(snap, V1Oracle(),
   V2Analyzer()).ok == True` means zero divergence.
2. **Per-layer V1 reference oracles** — analysis tests diff `V2Analyzer` against a
   fresh `SMCStrategy` on synthetic corpora (`tests/analysis/test_scoring.py`,
   `test_htf.py`); lifecycle tests replay a byte-transcribed V1 reference
   (`tests/lifecycle/v1_reference.py`) over 12 seeds × 60 cycles × 6 symbols,
   asserting identical active memory, killed store, alerts, and event sequence;
   app tests assert byte-exact rendered strings and routing via injected fakes.

**Standing gap (Phase 6):** the harness has only run against the **committed
synthetic fixture** (a plumbing check) and inline corpora — **no real,
multi-symbol, multi-cycle golden dataset has been captured**. Meaningful
end-to-end shadow equivalence still requires that capture (see §11).

**Commands:**
```bash
venv/Scripts/python.exe -m unittest discover tests        # 763 tests (1 known flake)

# Rebuild the deterministic fixture (no network):
venv/Scripts/python.exe -m tests.regression.generate synthetic \
    --out tests/regression/fixtures/synthetic_short.json

# Capture a real snapshot from live V1 (network; manual):
venv/Scripts/python.exe -m tests.regression.generate live \
    --symbol BTC/USDT --out tests/regression/fixtures/btcusdt.json --m5 --golden
```

---

## 8. Roadmap

- [x] **Milestone 0** — Documentation baseline (frozen docs, Decision #010, P1–P5)
- [x] **Phase 1** — Domain Model Foundation (`v2/domain/`, 18 objects)
- [x] **Phase 2** — Infrastructure Adapters (2.1 Clock · 2.2 Market Data · 2.3 Persistence · 2.4 Notifier)
- [x] **Golden Oracle / Regression Harness** (`tests/regression/`)
- [x] **Phase 3** — Pure Analysis Layer (`v2/analysis/`, 14 modules)
- [x] **Phase 4** — Lifecycle Core (`v2/lifecycle/`; reconciler + rules + store; injected clock)
- [x] **Phase 5.1** — Thin Orchestrator (`v2/app/orchestrator.py`)
- [x] **Phase 5.2** — Delivery & Observability (`v2/app/rendering.py`, `audit.py`)
- [ ] **Golden dataset capture** — real multi-symbol, multi-cycle V1 recordings across diverse market conditions *(still outstanding; prerequisite for meaningful shadow validation)*
- [ ] **Phase 6** — Shadow Validation & Cutover  ⬅️ **CURRENT** (see §11)
  - [ ] Shadow-run V2 alongside V1 to **zero divergence** across a diverse window
  - [ ] Complete the cutover checklist → switch production to `Orchestrator.create()` → retire V1

---

## 9. Definition of Done (per milestone)

A milestone is complete only when **all** hold:

- [ ] **Scope respected** — only the approved milestone was built; no scope creep.
- [ ] **Architecture rules hold** (§5) — verified (domain purity, no `src/` outside
      infra, acyclic imports, no clock leak, orchestrator holds no signal branch).
- [ ] **Equivalence proven** — for behavior-bearing phases, diffed against V1's
      unmodified functions with **zero divergence** (harness and/or per-layer oracle).
- [ ] **All tests pass** — `venv/Scripts/python.exe -m unittest discover tests`
      (currently 763; the one calendar-dependent flake is documented, §2).
- [ ] **V1 untouched** — `main.py`, `src/`, `BLUEPRINT.md`, `MODULE_INTENT.md`
      unchanged.
- [ ] **No new dependencies** — stdlib only unless explicitly approved.
- [ ] **Frozen docs untouched** — or delivered as an approved patch.
- [ ] **`PROJECT_STATE.md` + `CHANGELOG.md` updated** — reflect the new state.
- [ ] **Project memory updated** — `…/memory/v2-migration-status.md`.
- [ ] **Committed & tagged** — one commit per milestone (+ a tag at checkpoints).
- [ ] **STOP** — do not begin the next milestone without explicit approval.

---

## 10. Current Constraints — Frozen Subsystems

**Must NOT be modified (frozen):**

| Subsystem | Reason |
|---|---|
| `main.py`, `src/` (V1) | V1 is the behavioral reference and stays runnable until cutover. |
| `BLUEPRINT.md`, `MODULE_INTENT.md` | V1 design reference. |
| `SMC-BOT-DOCS/` (6 docs) | Frozen governing spec — change only via approved patch. |
| `v2/domain/` | Phase 1 complete → frozen. |
| `v2/infra/` | Phase 2 complete → frozen. |
| `v2/analysis/` | Phase 3 complete → frozen. |
| `v2/lifecycle/` | Phase 4 complete → frozen. |
| `tests/regression/` (harness, fixtures, goldens) | The equivalence reference — must stay stable so future results are comparable. |
| `active_pois.json`, `killed_thesis.json`, `retracement_ob_audit.jsonl` | V1 **live** runtime state — production data, not test scratch. |

`v2/app/` is the last-built layer; it is not yet frozen (Phase 6 may touch it only
for a genuine behavioral bug found during shadow validation, never for cleanup).

**Reopening a frozen subsystem requires a new, explicitly-approved milestone.**

---

## 11. Current Phase — Phase 6: Shadow Validation & Cutover

**Objective.** Prove V2 is behaviorally identical to V1 in production conditions,
then cut over. **No redesign, no optimization, no new features** — validation and
documentation only.

**Shadow validation (what must be demonstrated).**
- Capture a **real golden dataset** first: `generate live` over a diverse window
  (trending both directions, ranging, ≥1 regime transition, plus messy cases —
  near-RR-floor, OB-questionable, KILL-latency). The committed synthetic fixture
  proves plumbing only.
- Run V1 and V2 **side by side on identical inputs** and diff, at minimum: signal,
  score, notes, decision, reason, HTF, regime, entry, risk, lifecycle state,
  memory, killed thesis, alerts, dashboard, OB-audit JSONL, terminal logs,
  persistence, and notification payloads.
- **Pass criterion:** zero divergence across the full field set over the window.
- **Failure criterion:** any single-field divergence that is not an already-
  sanctioned, documented divergence (error surfacing; M5 fetch cadence).

**Cutover (only after zero-divergence).** Backup live JSON state → tag
`v2-code-complete` → switch the production entrypoint to `Orchestrator.create()`
→ observe → retire V1 (`main.py`, `src/`) → tag `v2-cutover`.

**Rollback.** V1 remains byte-intact until `v2-cutover`; reverting is switching
the entrypoint back and restoring the JSON backup. Git tag `v0.1.0` and the
per-phase commits are the safety net.

> Do not begin any new development. Phase 6 is validation + docs. STOP and wait
> for approval before cutover.

---

## 12. Important Notes (read before writing any code)

1. **V1 is the source of truth for behavior.** If a doc and V1 disagree, V1 wins.
   Read the actual `src/strategy.py` / `main.py`, don't trust summaries.
2. **This IS a git repo now** (branch `main`, 12 commits, tag `v0.1.0`). One
   commit per phase. Tag checkpoints; back up live JSON before cutover.
3. **Use the venv Python:** `venv/Scripts/python.exe`. `pytest` is **not**
   installed and must not be added — tests are stdlib `unittest`.
4. **The harness deliberately does NOT cover everything.** It captures
   analysis-level observables + a single-cycle persistence/notification
   projection. Path-dependent lifecycle sequencing (miss_count, dedup/upgrade,
   TTL, replace, OB-audit dedup) is covered by the **lifecycle replay tests**;
   delivery formatting/routing by the **app tests**. End-to-end shadow diffing
   over a real dataset is the Phase-6 step still outstanding.
5. **Injected clock everywhere below app.** Never read the wall clock in
   analysis/lifecycle. The orchestrator uses it only for `session_active`.
6. **Serialization fidelity is exact.** The persisted `ActiveSignal` is a
   **17-field** schema; V1's `load_memory` drops `entry_source` + `signal_id` and
   coerces `score` to float on load (lossy **by design** — reproduce, don't fix).
7. **Verified V1 quirks that must stay preserved:**
   - Killed-thesis cooldown exists (`killed_thesis.json`, 1h / one closed H1).
   - Same-thesis partial refresh does **NOT** reset `miss_count`/`last_miss_h1_ts`.
   - Kill switch fires on **D1+H4 HTF bias flip** *and* on SL breach (+0.30·ATR).
   - OB-audit dedup is an in-memory set owned by the orchestrator (Decision #010 —
     `OBAuditRecord` is an observability contract, never read back into a decision).
   - RAW-log score adds back the choppy penalty (pre-M5, pre-choppy value).
   - OB-audit `location==""` and `window_bounds`/`selected_ob_*`/`current_price`
     are always `None` (V1 key-name mismatch — reproduced faithfully).
8. **Binding decisions:** `SMC-BOT-DOCS/05-decision-log.md` #001–#010. Notably
   **#001** (Contract B) and **#010** (domain extensions + observability contract).
9. **Stop discipline.** Each milestone ends with a STOP. Deliver the report,
   update this file + `CHANGELOG.md`, and wait for approval. Do not chain phases.

---

*End of PROJECT_STATE.md — update this file at every milestone boundary (§9).*
