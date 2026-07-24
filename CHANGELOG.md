# CHANGELOG.md — Engineering History of the V2 Migration

> **This is not a release changelog.** It is the engineering history of how the
> repository evolved — the *why* and *how* behind each architectural milestone.
>
> Read it together with **`PROJECT_STATE.md`**:
> - `PROJECT_STATE.md` → *"What does the project look like today?"*
> - `CHANGELOG.md` → *"How did it get here, and why was it built this way?"*
>
> Entries are semantic milestones, not commits. Never let this file degrade into
> a commit dump — every entry must carry engineering intent.

---

## 1. Project History

The repository began as **V1** — a working but monolithic SMC signal radar
(`main.py` + `src/`), where the loop was written first and logic grew inward.
It runs in production and is the behavioral reference.

The **V2 migration** re-architects that logic into clean, testable layers
**without changing observable behavior**. The guiding constraint is unusual and
worth stating up front: this is *not* a rewrite validated at the end — it is a
migration where **each step must be provably equivalent to V1 before the next
begins**. That single constraint explains almost every decision recorded below,
including the seemingly-backwards move of building a regression harness *before*
porting any analysis.

This changelog exists so any future contributor — human or AI — can reconstruct
the reasoning behind the current structure without the original conversation
history.

---

## 2. Migration Timeline

Timeline is compressed (the foundation was laid over ~2 days, 2026-07-22 →
2026-07-23). Sequence matters more than the calendar date.

### M0 — Documentation Baseline & V1 Behavioral Audit
- **Date:** 2026-07-22
- **Purpose:** Make the governing specification *truthful to V1* before writing
  any code, and lock a workflow for changing frozen docs.
- **What happened:** All six `SMC-BOT-DOCS/` documents were read, and V1 was
  audited by direct code reading (not by trusting summaries). The audit found
  five places where V1's *actual* behavior diverged from the frozen Architecture
  spec — a killed-thesis cooldown, a same-thesis partial-refresh branch that does
  not reset `miss_count`, a kill switch that also fires on HTF bias flip, a
  persisted schema wider (17 fields) than the documented contract, and an
  OB-audit trail. These were **surfaced, not silently fixed**.
- **Architecture impact:** Established `SMC-BOT-DOCS/` as an accurate spec;
  created **Decision #010** (adds `KilledThesisRecord` and `OBAuditRecord`, the
  latter classified as an *observability contract*, plus `SAME_THESIS` /
  `KILL_COOLDOWN` log events). Established the **frozen-docs patch workflow**
  (proposed patches P1–P5, applied only after explicit approval).
- **Verification:** Cross-document consistency audit; architectural review that
  placed `OBAuditRecord` in `domain` as an observability sibling of `LogEvent`,
  never read back into a decision.
- **Status:** ✅ Complete · frozen (change only via approved patch).

### M1 — Domain Model Foundation (`v2/domain/`)
- **Date:** 2026-07-22
- **Purpose:** Build the typed vocabulary every later module produces and
  consumes. This phase produces **no behavior** — only contracts.
- **What happened:** 18 immutable frozen dataclasses created. An independent
  self-review then found and fixed three issues before sign-off: dead code
  (`OrderBlock._V1_KEY_ORDER`), a **dual-representation `Signal`** (entry/risk
  scalars duplicated both flat and via a nested `candidate` — a two-source-of-
  truth leak, resolved by making `candidate` the single source with a
  construction-time consistency invariant), and weak equality tests
  (strengthened to be field-sensitive).
- **Architecture impact:** Created the `v2/` tree and its dependency-free base
  layer — the only layer that can be built against nothing.
- **Important decisions:** frozen dataclasses for immutability; `str` (not
  `Enum`) for tier/regime/mode/source fields to preserve V1's exact serialized
  strings; `MarketSnapshot` DataFrames immutable *by contract* (the one
  documented exception); the persisted `ActiveSignal` reproduces V1's full
  **17-field** schema **and** V1's lossy load (drops `entry_source`/`signal_id`,
  coerces `score` to float).
- **Verification:** 39 tests — immutability, value-equality, serialization
  round-trip, and an AST-based purity check that the domain imports nothing
  outside stdlib/pandas/intra-domain.
- **Status:** ✅ Complete · frozen.

### M2 — Infrastructure Adapters (`v2/infra/`)
- **Date:** 2026-07-22
- **Purpose:** Wrap V1's proven I/O behind clean contracts *without changing what
  it does* — an anti-corruption boundary, so the pure core can later be tested
  against real or recorded data while V1's code remains underneath.
- **Sub-milestones (built in this order, for this reason):**

  | Step | Adapter | Why it exists / notable behavior |
  |---|---|---|
  | 2.1 | `clock` | Built **first** because analysis and lifecycle both need *injectable time*; unlocks deterministic testing downstream. `Clock` Protocol + `SystemClock` + `FakeClock`. |
  | 2.2 | `market_data` | Wraps `BinanceHandler` → `MarketSnapshot`. **First V2 code to import `src/`** — which forced narrowing the purity test from "no `src/` anywhere in v2" to "no `src/` **outside infra**" (the architecturally precise rule). Mirrors V1's fetch limits, `iloc[:-1]` strip, live-price separation, and M5 guards. |
  | 2.3 | `persistence` | Reuses the domain serialization; reproduces V1's file mechanics exactly (atomic temp+`os.replace`, `indent=4`). Deliberately does **not** call V1's `main.py` persistence functions (hardcoded paths, untestable) — it reproduces the format instead. |
  | 2.4 | `notifier` | Backend-agnostic `Notifier` Protocol + `TelegramNotifier` routing `Alert` by `.kind`. Text-only (V1 has no image support). Completes the infra layer. |

- **Architecture impact:** Created the I/O boundary (`v2/infra/`). After 2.4,
  the full infra layer exists: clock, market_data, persistence, notifier.
- **Important decision (applies to all four):** **constructor dependency
  injection** with the real dependency as default — production wiring stays
  trivial; tests inject fakes and never touch network/filesystem/Telegram. Plus
  one **sanctioned behavioral divergence — error surfacing**: adapters raise
  typed exceptions (`MarketDataError`, `PersistenceError`, `NotifierError`) where
  V1 silently swallowed failures. Message/format/values are unchanged; *whether*
  to swallow is deferred to the future orchestrator.
- **Verification:** 83 tests total (clock, market_data, persistence, notifier),
  all with injected fakes — no external service is ever contacted.
- **Status:** ✅ Complete · frozen.

### M3 — Golden Oracle / Regression Harness (`tests/regression/`)
- **Date:** 2026-07-23
- **Purpose:** Turn *"V2 matches V1"* from a judgment call into an automated,
  field-level check — **before** the hardest layer (analysis) is ported.
- **What happened:** Built a verification-only package that captures deterministic
  input snapshots, runs V1's **unmodified** analysis functions as the reference
  oracle, extracts a backend-neutral `ObservableBehavior`, and diffs two of them
  field-by-field (PASS/FAIL). A key discovery: V1's functions already return all
  their intermediates in `context["debug"]`, so the "throwaway V1 instrumentation"
  the roadmap budgeted for was **unnecessary** — reading V1 beat modifying it.
- **Architecture impact:** Introduced the `Analyzer` protocol as the comparison
  seam (`V1Oracle` today; a `V2Analyzer` plugs in unchanged at Phase 3). Added a
  layer of the project that is explicitly *outside* runtime.
- **Important decisions:** exact-equality comparison by default (a rounding
  difference that flips a tier is a real divergence); a documented **scope
  boundary** — the harness captures analysis-level observables plus a single-cycle
  persistence/notification *projection*, and deliberately excludes wall-clock
  fields and path-dependent lifecycle sequencing (Phase 4's job, which reuses this
  comparison machinery).
- **Verification:** 45 tests; the V1-vs-V1 self-comparison is green across all 45
  observable fields — the deterministic baseline every future V2 result is
  measured against.
- **Status:** ✅ Complete · frozen (the equivalence reference must stay stable).

### M4 — Project Checkpoint Documents
- **Date:** 2026-07-23
- **Purpose:** Make the migration resumable with **zero conversation history**.
- **What happened:** `PROJECT_STATE.md` (current-state source of truth) and this
  `CHANGELOG.md` (engineering history) were created at the repo root.
- **Status:** ✅ Complete (living documents; update at every milestone).

---

## 3. Architectural Evolution

How the shape of the system changed, step by step:

```
V1 Monolith                         main.py + src/  (loop-first; logic grown inward)
     │
     ▼  add typed contracts
+ Domain Layer            (M1)       v2/domain/  — immutable, zero-dependency vocabulary
     │
     ▼  wrap V1 I/O behind contracts
+ Infrastructure Layer    (M2)       v2/infra/   — anti-corruption adapters (only layer that imports src/)
     │
     ▼  make equivalence measurable BEFORE porting logic
+ Regression Harness      (M3)       tests/regression/  — Golden Oracle, outside runtime
     │
     ▼  (next)
+ Analysis Layer          (Phase 3)  v2/analysis/  — pure business logic  ← NOT built
     │
     ▼
+ Lifecycle Layer         (Phase 4)  v2/lifecycle/ — stateful reconciler  ← NOT built
     │
     ▼
+ Orchestrator + Delivery (Phase 5)  v2/app/       — thin coordination    ← NOT built
     │
     ▼
+ Parallel Validation & Cutover (Phase 6)          — shadow-diff, retire V1
```

The build order is the **inverse of how V1 grew**: V1 started with the loop and
grew logic inward; V2 builds the pure, provable core first and grows the loop
outward. Each layer is real and tested before the layer above it consumes it —
no stubs hiding behavioral gaps.

---

## 4. Major Architectural Decisions (with reasoning)

Reasoning is captured here so it is not lost. (For *what* the rules are today,
see `PROJECT_STATE.md §5–§6`; this section explains *why*.)

- **Immutable domain.** Frozen dataclasses eliminate an entire class of aliasing
  and hidden-state bugs, make equality and serialization the *only* semantics an
  object has, and let objects be shared freely across layers. Immutability is
  cheap to enforce and expensive to retrofit — so it was mandated from object #1.

- **Infrastructure as an anti-corruption layer.** V1's I/O (network quirks, exact
  JSON format, Telegram wire behavior) is *proven* and *fragile*. Rewriting it
  would inject the riskiest kind of change into the migration. Wrapping it behind
  clean contracts yields a testable boundary with zero I/O risk — the pure core
  gets a mockable source/sink without anyone re-implementing exchange access.

- **Behavioral equivalence over cleanliness.** V1's behavior contains subtle,
  hard-won emergent details no specification fully captures. A "cleaner" V2 that
  behaves differently is a regression, not an improvement. Hence the hard rule:
  when the Architecture spec and V1's *actual* behavior disagree, **V1 wins**.

- **Regression-first development / Golden Oracle before Analysis.** You cannot
  safely port the hardest, highest-leverage layer (analysis) without an objective
  way to prove you didn't change it. Building the harness first makes every later
  claim of "still equivalent" a *test result*, and localizes any divergence to a
  named field instead of surfacing three steps downstream.

- **V1 remains untouched.** V1 is simultaneously the running production system,
  the rollback path, and the diff baseline. Modifying it would jeopardize all
  three. This was upheld even for capturing intermediates: V1 already exposes them
  via its returned `debug` dict, so the oracle *reads* V1 rather than instrumenting
  it.

- **Dependency injection (no globals, singletons, or DI framework).** Every
  adapter must be testable without real network/filesystem/Telegram. Constructor
  injection with the real dependency as default keeps production wiring trivial
  and tests fully hermetic — no framework required.

- **Error-surfacing as the one sanctioned divergence.** V1 swallowed I/O failures
  and returned falsy. V2 adapters raise typed exceptions so failures are
  deterministic and testable. This is the *only* intentional behavior change, it
  touches error handling only (never message/format/values), and the final
  swallow-or-propagate policy is deferred to the orchestrator.

- **Small milestones + freeze-on-completion.** Small, independently-verifiable
  steps stay reviewable; freezing a completed module stops a later phase from
  quietly reopening and drifting settled behavior. Each milestone ends with an
  explicit STOP.

- **Surface, don't fix.** When the V1 audit found questionable behavior, the
  discipline was to preserve it and document the concern (Decision #010) — never
  to silently "correct" V1 mid-migration.

---

## 5. Repository Evolution

Only major structural changes are listed.

| After | New top-level structure |
|---|---|
| V1 (start) | `main.py`, `src/`, `SMC-BOT-DOCS/`, `BLUEPRINT.md`, `MODULE_INTENT.md`, live JSON artifacts, legacy root scripts |
| M0 | `SMC-BOT-DOCS/` synchronized (Decision #010, patches applied); no new dirs |
| M1 | **+ `v2/`**, **+ `v2/domain/`**, **+ `tests/domain/`** |
| M2 | **+ `v2/infra/`**, **+ `tests/infra/`** |
| M3 | **+ `tests/regression/`** (with `fixtures/`, `README.md`, committed golden) |
| M4 | **+ `PROJECT_STATE.md`**, **+ `CHANGELOG.md`** (repo root) |

`v2/analysis/`, `v2/lifecycle/`, `v2/app/` do **not** exist yet — they arrive with
Phases 3–5.

---

## 6. Testing Evolution

```
V1 era            ad-hoc root scripts (test_api.py, test_strategy.py,
                  final_test.py, debug_api.py) — manual, not a suite
     │
     ▼  M1
Domain tests      immutability · equality · serialization round-trip ·
(39)              AST-based purity (domain imports nothing forbidden)
     │
     ▼  M2
Infra tests       adapter fidelity with injected fakes — no network,
(83)              filesystem, or Telegram ever touched
     │
     ▼  M3
Regression        observable-equivalence machinery + V1-vs-V1 baseline
harness (45)      (45/45 fields green) on a deterministic fixture
     │
     ▼  Phase 3+
Behavioral        V2 analysis/lifecycle diffed against V1 across the
validation        golden dataset (zero divergence == done)
```

Constants throughout: **stdlib `unittest` only** (pytest is not installed and is
forbidden), and **every test is hermetic** — no external service, temp files
only. Current total: **167 tests, all passing.**

---

## 7. Completed Checkpoints

| Checkpoint | Architecture complete? | Regression complete? | Frozen? | Ready for next phase? |
|---|:--:|:--:|:--:|:--:|
| M0 — Docs baseline | ✅ (spec truthful to V1) | n/a | ✅ (patch-only) | ✅ |
| M1 — Domain model | ✅ | n/a (no behavior) | ✅ | ✅ |
| M2.1 — Clock | ✅ | fidelity tests ✅ | ✅ | ✅ |
| M2.2 — Market data | ✅ | fidelity tests ✅ | ✅ | ✅ |
| M2.3 — Persistence | ✅ | fidelity tests ✅ | ✅ | ✅ |
| M2.4 — Notifier | ✅ | fidelity tests ✅ | ✅ | ✅ |
| M3 — Regression harness | ✅ | ✅ (V1-vs-V1 baseline) | ✅ | ✅ (pending golden dataset + Phase 3) |

"Regression complete" for M2 means *adapter-fidelity* tests (behavior against
injected V1-shaped data), not golden-dataset diffing — that begins at Phase 3,
which is what M3 was built to enable.

---

## 8. Lessons Learned

Distilled so future contributors don't relearn them the hard way:

- **Adapters isolate risk.** Wrapping V1's I/O let the pure core be built and
  tested against fakes while the fragile network/format/Telegram code stayed
  exactly as-is. The boundary paid for itself immediately in test hermeticity.

- **Equivalence must be *measured*, not asserted.** "It obviously behaves the
  same" is how silent regressions enter. The harness converts that claim into a
  per-field diff — which is why it was built before, not after, the analysis port.

- **Reading V1 beats modifying it.** The plan anticipated instrumenting V1 to emit
  intermediates; in practice V1 already returned them. Keeping V1 untouched
  preserved production, the rollback path, *and* the diff baseline at once.

- **Build the safety net before the high-wire act.** Porting analysis without the
  harness would surface divergences three stages downstream, misattributed to the
  wrong module. With it, a break points at a named field (`regime`,
  `ob_selected_top`, …).

- **Small, frozen milestones prevent drift.** Freezing completed modules stopped
  later work from quietly reopening settled behavior "for convenience." The STOP
  at each boundary kept every step reviewable in isolation.

- **Surface questionable behavior; never silently fix it.** The V1 audit found
  real discrepancies. Preserving them (Decision #010) — rather than "improving"
  them — is what keeps the migration honest and reversible.

- **A green suite on an inadequate dataset is *untested*, not tested.** The
  committed synthetic fixture proves the harness *plumbing* is correct; it does
  **not** prove behavioral coverage. A real, diverse golden dataset is still
  required before Phase 3 equivalence means anything. Treat false confidence as a
  risk, not a milestone.

---

## 9. Future Milestones

Straight from `SMC-BOT-DOCS/03-implementation-roadmap.md` — **no speculation, no
redesign proposals**:

- [ ] **Golden dataset capture** — real multi-symbol, multi-cycle V1 recordings
      across diverse market conditions (prerequisite for meaningful Phase 3).
- [ ] **Phase 3 — Pure Analysis Layer** (`v2/analysis/`): port
      `build_signal_context` / `score_signal` / entry / risk / structure into
      pure modules; `MarketSnapshot → Signal`.
- [ ] **Phase 4 — Lifecycle Core** (`v2/lifecycle/`): reconciler + rules + store,
      driven by the injected clock.
- [ ] **Phase 5 — Orchestration & Delivery Wiring** (`v2/app/`): a thin
      orchestrator with no signal-deciding logic.
- [ ] **Phase 6 — Parallel Validation & Cutover**: shadow-mode diff to zero
      divergence, then retire V1.

---

## 10. AI Continuation Notes

Written specifically for the next AI assistant picking this up cold.

**Read, in this order:**
1. `PROJECT_STATE.md` — current state, rules, next milestone.
2. `CHANGELOG.md` (this file) — why the structure is what it is.
3. `SMC-BOT-DOCS/` — `00-project-context.md`, then `02-software-architecture.md`,
   `03-implementation-roadmap.md`, `04-engineering-rules.md`, `05-decision-log.md`.
4. `tests/regression/README.md` — how equivalence is verified.
5. The V1 reference code itself: `src/strategy.py`, then `main.py`.

**How to resume work:** `PROJECT_STATE.md §2` gives the current phase; `§11`
describes the *only* next milestone. Do **not** start it without explicit
approval, and do not design beyond it.

**How to verify progress before and after any change:**
```bash
venv/Scripts/python.exe -m unittest discover tests    # expect: 167 passing
```
For a behavior-bearing phase, additionally require
`compare_analyzers(snap, V1Oracle(), V2Analyzer()).ok == True` across the golden
dataset (see the regression workflow in `PROJECT_STATE.md §7`).

**Common mistakes to avoid:**
- Modifying V1 (`main.py`, `src/`, `BLUEPRINT.md`, `MODULE_INTENT.md`) — never.
- Importing `src/` from any V2 layer other than `v2/infra`.
- Adding a dependency (especially pytest). Stdlib `unittest` only.
- Editing `SMC-BOT-DOCS/` directly instead of proposing an approved patch.
- "Improving" strategy, scoring, thresholds, or lifecycle behavior.
- Trusting the synthetic fixture as coverage — capture a real golden dataset.
- Comparing with a float tolerance instead of exact equality (tolerance is for
  diagnosis only).
- Reopening a frozen module, or chaining past a milestone STOP.

**Repository entry order for *understanding the design*:** contracts
(`v2/domain/`) → boundary (`v2/infra/`) → proof (`tests/regression/`) → reference
behavior (`src/`).

---

*Maintain this file at every milestone: add one semantic entry to §2, update §3
/ §5–§7 if the structure or test surface changed, and record any new reasoning in
§4 / §8. Never turn it into a commit log.*
