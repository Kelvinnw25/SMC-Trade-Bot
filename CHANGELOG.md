# CHANGELOG.md — Engineering History

> **This is not a release changelog.** It is the engineering history of how the
> repository evolved — the *why* and *how* behind each milestone.
>
> Read it together with **`PROJECT_STATE.md`**:
> - `PROJECT_STATE.md` → *"What does the project look like today?"*
> - `CHANGELOG.md` → *"How did it get here, and why was it built this way?"*
>
> Entries are semantic milestones, not commits. Never let this file degrade into
> a commit dump — every entry must carry engineering intent.
>
> The history has **two chapters**. Chapter A (**Migration**, §§1–10) is *closed*
> — the V1→V2 re-architecture, complete as of 2026-07-25. Chapter B (**Strategy
> Evolution**, §11+) is *open* — improving the trading system itself, with V2 as
> the only implementation and V1 no longer a reference. New entries go in Chapter
> B and must not be framed as migration work.

---

# CHAPTER A — THE MIGRATION (CLOSED)

*The V1→V2 re-architecture. Complete 2026-07-25; behavior preserved and proven
against V1. Retained as history — no longer an active constraint.*

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

### M5 — Phase 3: Pure Analysis Layer (`v2/analysis/`)
- **Date:** 2026-07-24
- **Purpose:** Port V1's analysis — the hardest, highest-leverage code — into
  decomposed, **stateless, pure** modules: `MarketSnapshot → Signal`, with no
  memory, persistence, notification, or clock dependency.
- **What happened:** 14 modules built in pipeline order (`htf`, `regime`,
  `structure`, `order_block`, `fair_value_gap`, `liquidity`, `entry`, `risk`,
  `scoring`, plus the `frames` / `stage` / `context` / `pipeline` scaffolding and
  the `engine` `V2Analyzer` that runs the chain). Each was diffed field-for-field
  against a **fresh `SMCStrategy` V1 oracle** on synthetic corpora and the
  committed fixture.
- **Architecture impact:** The pure business core now exists above infra. The
  regression harness's `Analyzer` seam was filled by `V2Analyzer` exactly as
  designed at M3.
- **Important decisions:** `V2Analyzer.analyze()` **owns the M5 `score≥8`
  eligibility gate** and the choppy penalty — these are analysis rules, not
  orchestration (they change a signal's tier, so per Principle 12 they must never
  leak upward). OB **first-match** selection, RR from the structural entry, and
  floating-point/arithmetic-order determinism preserved bit-for-bit. `analyze()`
  returns only the final `Signal` (not the analysis context) — a constraint that
  later shaped the orchestrator (see M7).
- **Verification:** 478 tests, incl. V2-vs-V1 diffs on scoring/HTF/entry/risk.
- **Status:** ✅ Complete · frozen.

### M6 — Phase 4: Lifecycle Core (`v2/lifecycle/`)
- **Date:** 2026-07-24
- **Purpose:** Extract every lifecycle responsibility (memory, TTL, decay, kill
  switch, miss counter, killed-thesis cooldown, slot cap, duplicate suppression,
  alert-upgrade path) into a **stateful but deterministic** layer driven by the
  **injected clock** — exactly one implementation of each V1 rule.
- **What happened:** `config` (V1 defaults), `rules` (ONE copy of every V1
  predicate — incl. the KILL predicate `is_poi_invalidated` with its full V1
  quirk set, `math.isfinite` for `np.isfinite`, the `None`-sl→`False` behavior),
  `memory` (read-only `MemoryView`), `store` (single writer; `apply(decision)`;
  **pure** `ttl_sweep` returning an eviction decision), and `reconciler` (a
  verbatim transcription of V1's memory engine:
  KILL→STALE→cooldown→same-thesis→insert/replace/cap→alert→GUARD/decay→SKIP).
- **Architecture impact:** Lifecycle sits above analysis; `LifecycleDecision`
  became the declarative bridge to delivery (ordered mutations + alerts +
  log_events + killed_theses) — nothing is mutated in place by a caller.
- **Important decisions:** the KILL predicate is a **lifecycle** rule (per
  Architecture §3), not analysis; the reconciler needs the *fresh* cycle's HTF
  biases (V1's `context["htf_map"]`), which later forced the orchestrator's
  one-pipeline-run design (M7). Silent decay and the mixed-source DECAY log
  fields (Decision #009) preserved.
- **Verification:** 58 tests, incl. a **12-seed × 60-cycle × 6-symbol replay**
  against a byte-transcribed V1 reference (`tests/lifecycle/v1_reference.py`)
  asserting identical active memory, killed store, alerts, and event sequence;
  plus a purity test (no IO / wall-clock; predicates only in `rules`).
- **Status:** ✅ Complete · frozen.

### M7 — Phase 5.1: Thin Orchestrator (`v2/app/orchestrator.py`)
- **Date:** 2026-07-25
- **Purpose:** Replace V1's `run()` loop with a **coordination-only** layer:
  startup, wiring, scan loop, per-symbol error isolation, `session_active` (clock
  only), and the fetch → analyze → reconcile → apply → dispatch → persist
  sequence — **with no branch that changes a signal's outcome** (Principle 12).
- **What happened:** `Orchestrator` with full constructor DI (`create()` wires the
  real adapters); `startup()` (load → store, swallow-to-empty on
  `PersistenceError`); `run_cycle()`; `run_forever()`; `_process_symbol` (fetch
  with M5, data-sufficiency guard, analyze, reconcile, apply); `_end_of_cycle`
  (TTL sweep → prune → save).
- **Key design discovery — one pipeline run + one analyze.** Because
  `V2Analyzer.analyze()` returns only the final `Signal`, but both the kill/stale
  predicate and the delivery layer need the fresh cycle's `HTFContext` (and the
  pre-M5 signal), the orchestrator obtains the context via one pure `pipeline.run`
  and the final Signal via `analyze`. Both are pure functions of the snapshot, so
  the double evaluation is behavior-neutral — and it keeps the M5 gate inside
  analysis. Verified against V1: `kill_htf = context.htf if signal.direction is
  not None else None` reproduces exactly which biases V1's `is_poi_invalidated`
  saw (present past the direction/veto gate; absent → "CHOPPY").
- **Sanctioned divergence documented:** V2 always fetches M5 and lets `analyze`
  decide whether to use it (V1 fetched only past the score gate) — a
  behavior-neutral I/O cadence difference, not an outcome change.
- **Verification:** orchestrator tests with injected fakes (no network / FS /
  Telegram / wall clock); "owns-no-logic" tests prove the reconciler receives the
  analyzer's Signal and derived inputs straight through.
- **Status:** ✅ Complete.

### M8 — Phase 5.2: Delivery & Observability (`v2/app/`)
- **Date:** 2026-07-25
- **Purpose:** Route the analysis/lifecycle **outputs** to presentation — the
  dashboard, terminal SCAN/HTF/CONF/RAW + lifecycle logs, the notifier, and the
  retracement OB-audit JSONL — as **pure presentation + thin routing**, with no
  business logic entering delivery.
- **What happened:** `rendering.py` (pure `render_dashboard` / `render_scan` /
  `render_log_event`, byte-exact to V1's `print` strings incl. emojis and the
  Indonesian log text); `audit.py` (`assemble_ob_audit` from domain objects only,
  reusing `lifecycle.rules.levels_valid` — no re-analysis); the orchestrator wired
  with an injected `writer` and `sleeper`, the OB-audit dedup set, render/dispatch/
  audit routing, and V1's cadence (0.20s inter-fetch, 30s empty-universe, 60s
  cycle).
- **Important discoveries reproduced faithfully:** the RAW-log score is the
  pre-M5, **pre-choppy** value (`raw.score + 2 if "choppy_penalty" in notes`); the
  OB-audit carries V1's key-name-mismatch quirks (`location==""`;
  `window_bounds`/`selected_ob_*`/`current_price` always `None`); a killed symbol
  skips its SCAN block (V1 `continue`d); the audit dedup key is recorded even when
  the append surfaces an error (V1 swallowed append failures).
- **Architecture impact:** completed the `app` layer — coordination + delivery,
  still holding no signal-deciding branch. `OBAuditRecord` remains an
  observability contract (Decision #010): orchestrator-assembled, domain schema,
  infra JSONL sink, never read back into a decision.
- **Verification:** 60 app tests total (rendering byte-exact, audit payload/quirk,
  delivery routing/dedup/cadence). Full suite: **763 tests** (one pre-existing
  calendar-dependent analysis flake, documented in `PROJECT_STATE.md §2`).
- **Status:** ✅ Complete — **migration is code complete.**

### M9 — Phase 6: Final Audit & Migration Close-out
- **Date:** 2026-07-25 → 2026-07-26
- **Purpose:** Final whole-repository audit, then formally **close the migration**
  and declare V2 the authoritative implementation.
- **What happened:** full architecture + layer-ownership audit (every V1
  responsibility has exactly one owner; no duplicated business logic; no layer
  violation, dependency inversion, circular import, hidden mutable state, or
  business logic in adapters/orchestrator — all verified); a shadow-validation
  plan and a cutover checklist were produced; a repository review classified the
  V1-era scratch scripts and design notes as legacy. `PROJECT_STATE.md` and this
  `CHANGELOG.md` were brought current, then **rewritten** to a post-migration
  framing (doc v3.0) when the migration was declared complete.
- **Decision (2026-07-26):** the migration is **complete**. V2 is authoritative;
  V1 is no longer the reference; behavioral equivalence is no longer required. The
  project pivots to **Strategy Evolution** (Chapter B). A formal V1 *code cutover*
  (deleting `main.py`/`src/`) is deferred: `v2/infra` still wraps
  `src/api_handler.py` + `src/telegram_notifier.py` at runtime, so those retire
  only after the adapters are internalized (a Phase-7 decoupling task).
- **Status:** ✅ Migration closed.

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
     ▼  port the hardest layer first, proven against V1
+ Analysis Layer          (M5)       v2/analysis/  — 14 pure modules; MarketSnapshot → Signal  ✅
     │
     ▼  add deterministic state on the injected clock
+ Lifecycle Layer         (M6)       v2/lifecycle/ — stateful reconciler + rules + store  ✅
     │
     ▼  grow the loop outward — coordination only
+ Orchestrator            (M7)       v2/app/orchestrator.py  — thin fetch→analyze→reconcile→persist  ✅
     │
     ▼  route outputs to presentation/sinks
+ Delivery & Observability(M8)       v2/app/rendering.py · audit.py  — pure presentation + routing  ✅
     │
     ▼  final audit + close-out
+ Migration Close-out     (M9)       V2 declared authoritative; V1 no longer a reference  ✅
```

The build order is the **inverse of how V1 grew**: V1 started with the loop and
grew logic inward; V2 builds the pure, provable core first and grows the loop
outward. Each layer is real and tested before the layer above it consumes it —
no stubs hiding behavioral gaps. **All five layers exist and the migration is
closed** (M9). Current work is in Chapter B (Strategy Evolution), not here.

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
| M5 | **+ `v2/analysis/`** (14 modules), **+ `tests/analysis/`** |
| M6 | **+ `v2/lifecycle/`**, **+ `tests/lifecycle/`** (incl. `v1_reference.py`) |
| M7–M8 | **+ `v2/app/`** (`orchestrator.py`, `rendering.py`, `audit.py`), **+ `tests/app/`** |

All five V2 layers (`domain`, `infra`, `analysis`, `lifecycle`, `app`) now exist.
The repository also became a **git repository** (branch `main`, per-phase commits,
tag `v0.1.0`) — the version-control safety net the earlier checkpoints lacked.

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
     ▼  M5
Analysis tests    V2 analysis diffed field-for-field against a fresh
(478)             SMCStrategy V1 oracle on synthetic corpora + fixture
     │
     ▼  M6
Lifecycle tests   12-seed × 60-cycle × 6-symbol replay vs a byte-transcribed
(58)              V1 reference — identical memory / killed / alerts / events
     │
     ▼  M7–M8
App tests         orchestrator wiring + byte-exact delivery rendering &
(60)              routing, all via injected fakes (no I/O, no wall clock)
```

Current total: **763 tests** (domain 39 · infra 83 · analysis 478 · lifecycle 58
· app 60 · regression 45); 762 pass, plus one documented calendar-dependent
analysis flake (`PROJECT_STATE.md §2`).

Constants throughout: **stdlib `unittest` only** (pytest is not installed and is
forbidden), and **every test is hermetic** — no external service, temp files
only.

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
| M3 — Regression harness | ✅ | ✅ (V1-vs-V1 baseline) | ✅ | ✅ |
| M5 — Analysis layer | ✅ | ✅ (V2-vs-V1 oracle, synthetic corpora) | ✅ | ✅ |
| M6 — Lifecycle core | ✅ | ✅ (multi-seed replay vs V1 reference) | ✅ | ✅ |
| M7 — Thin orchestrator | ✅ | ✅ (wiring/isolation via fakes) | — | ✅ |
| M8 — Delivery & observability | ✅ | ✅ (byte-exact rendering + routing) | — | ✅ (code complete) |

"Regression complete" for M2 means *adapter-fidelity* tests (behavior against
injected V1-shaped data), not golden-dataset diffing — that begins at M5. For M5+
it means V2 diffed against V1's unmodified functions on **synthetic/committed**
inputs; **end-to-end shadow diffing over a real captured golden dataset remains
the outstanding Phase-6 step** (see §9).

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

## 9. Migration Milestones (all complete)

The migration phases, as they landed:

- [x] **Phase 3 — Pure Analysis Layer** (`v2/analysis/`) — M5.
- [x] **Phase 4 — Lifecycle Core** (`v2/lifecycle/`) — M6.
- [x] **Phase 5 — Orchestration & Delivery Wiring** (`v2/app/`) — M7 + M8.
- [x] **Phase 6 — Final audit & close-out** — M9. V2 declared authoritative.

> A formal V1-vs-V2 **shadow diff over a captured golden dataset** was planned but
> **not executed**: the migration was declared complete on the strength of the
> per-layer V1-reference proofs (analysis diffed against `SMCStrategy`, lifecycle
> replayed against a V1 transcription, delivery byte-checked) plus the clean final
> audit. Behavioral equivalence is no longer a goal, so the golden-dataset capture
> was repurposed toward **Phase 7 evaluation** (a *forward-looking* dataset for
> measuring the strategy, not a backward V1 diff). See Chapter B.

---

## 10. Continuation Notes (current era)

Written for the next contributor (human or AI) picking this up cold, **post-
migration**.

**Read, in this order:**
1. `PROJECT_STATE.md` — current state, architecture, roadmap, working rules.
2. `STRATEGY_NOTES.md` — the strategy knowledge base (bugs, weaknesses, decisions,
   experiment log). This is where strategy work is planned and recorded.
3. `CHANGELOG.md` (this file) — history: Chapter A (migration, closed) then
   Chapter B (strategy evolution, open).
4. The V2 code: `v2/analysis/` (the strategy), then `v2/lifecycle/`, then `v2/app/`.
5. `SMC-BOT-DOCS/` and `src/strategy.py` — **historical** reference only (why the
   code is shaped as it is); no longer binding.

**How to resume work:** `PROJECT_STATE.md §6` gives the current phase (Phase 7 —
Evaluation) and priorities. Strategy changes are proposed in `STRATEGY_NOTES.md`,
approved, measured against a backtest, then logged in its Experiment Log. **Do not
start any strategy change without approval and a measurement plan.**

**Current-era rules (supersede Chapter A's migration rules):**
- V1 is **not** a reference. Do not add "match V1" assertions; retire the V1
  coupling as characterization tests are re-based on captured output.
- Improving strategy/scoring/thresholds/lifecycle is now the **goal** — but only
  on evidence (a backtest/forward-test result), one change at a time.
- Keep the architecture: layer boundaries, pure analysis, injected clock,
  orchestrator holds no signal branch. No new dependencies (stdlib `unittest`).
- The two Fix-Now metadata bugs (BUG-1, BUG-2 in `STRATEGY_NOTES.md`) may be fixed
  before live collection — they change data, not decisions.

**How to verify progress before and after any change:**
```bash
venv/Scripts/python.exe -m unittest discover tests    # expect: 763 (1 known flake)
```
When you intend a behavior change, update the affected characterization tests
deliberately (re-base on new expected output) — never silently loosen them.

> **Historical (Chapter A) — no longer active.** During the migration these rules
> held and are recorded for context: V1 was never modified; only `v2/infra`
> imported `src/`; strategy/scoring/threshold/lifecycle behavior was frozen;
> equivalence was diffed with exact equality (`compare_analyzers(snap, V1Oracle(),
> V2Analyzer()).ok`); frozen modules were not reopened. **These are superseded by
> the current-era rules above** — improving the strategy is now the goal, and V1
> is no longer a reference. The only rules that carry over unchanged are the
> architectural ones (layer boundaries, purity, no new dependencies).

---

*Chapter A is closed. Do not add migration milestones below; new work belongs in
Chapter B.*

---

# CHAPTER B — STRATEGY EVOLUTION (OPEN)

*V2 is the only implementation. V1 is not a reference. Behavioral equivalence is
no longer a goal — the goal is the best possible SMC system, changed on evidence
(backtests / forward tests), not intuition. Entries here describe strategy and
system improvements, never migration.*

## 11. Strategy Evolution Timeline

### S0 — Pivot: Migration → Strategy Evolution
- **Date:** 2026-07-26
- **Purpose:** Change the project's objective from "match V1" to "improve the
  trading system," and take inventory before any change.
- **What happened:** the migration was declared complete; V2 became authoritative.
  A full **strategy audit** (weak assumptions, fragile heuristics, duplicated
  confirmations, score inflation/starvation, entry/exit/HTF/liquidity/OB/FVG,
  continuation/retracement, lifecycle, TP/SL/RR, kill/cooldown, risk management),
  a **migration-artifact classification** (KEEP / REMOVE / ARCHIVE), and a
  **prioritized improvement backlog** were produced. No code behavior changed.
- **Key findings (headline):** HTF alignment is effectively counted ~4× across
  direction-weighting, scoring, `htf_context_clean`, and location — inflating
  scores from a single correlated signal; a trend-aligned continuation can reach
  A+ with **zero** liquidity confirmation; OB selection is first-match and ignores
  freshness, so a stale block can win the slot and suppress a fresh one; the
  continuation TP `tp_source`/`tp_rr` are dropped (a preserved V1 bug now fixable);
  kill fires on an unconfirmed forming-candle wick and on crude EMA bias flips;
  the `miss_count` decay clock evicts continuously-refreshed valid theses after 3
  H1 candles; no position sizing / correlation cap exists.
- **Status:** ✅ Audit + backlog produced; awaiting approval to start Phase 7
  (Evaluation). No strategy change begins without approval.

### S1 — Enter Evaluation Mode; create the strategy notebook
- **Date:** 2026-07-26
- **Purpose:** Stand up the permanent strategy knowledge base and triage the audit
  so evaluation can begin on a clean footing.
- **What happened:** created **`STRATEGY_NOTES.md`** — the permanent strategy
  notebook (current-strategy overview, confirmed implementation bugs, weaknesses
  A1–A25 grouped, validated design decisions, future research, and an empty
  experiment log). Triaged all 25 audit findings into **Fix-Now** vs **Future
  Refinement**: only the two metadata bugs (**BUG-1** continuation TP metadata,
  **BUG-2** OB-audit selected-OB fields) are Fix-Now — they change recorded data,
  not decisions, and must land before the evaluation dataset is collected. All
  A1–A25 that change trading decisions are Future Refinement, gated behind a
  baseline + backtester. Defined the evaluation workflow (fix metadata bugs →
  build a V2 runner + signal-ledger instrumentation → collect a dataset → build
  the backtester on history → freeze the baseline → one measured refinement at a
  time). Living docs synchronized (`PROJECT_STATE.md` v3.0, this file).
- **Blockers surfaced for running V2 live:** no V2 entrypoint runner exists yet
  (V1's `main.py` is not it); no full signal-ledger instrumentation (the OB audit
  covers only retracement OB entries). Neither is a strategy issue; both are
  Phase-7 setup work.
- **Status:** 🔄 Evaluation mode. No code changed this session — planning,
  documentation, and triage only.

### S2 — Signal Ledger (evaluation dataset)
- **Date:** 2026-07-26
- **Purpose:** Build the permanent, append-only dataset every future strategy
  evaluation reads from — the first concrete evaluation infrastructure. **No
  strategy logic touched** (scoring/analysis/lifecycle/entry/risk unchanged).
- **What happened:** added the Signal Ledger as an observability contract, the
  same discipline as the OB audit (Decision #010) — schema in `domain`
  (`SignalLedgerRecord`, ~40 evaluation-grade fields, versioned by
  `schema_version` + `strategy_version`), an append-only JSONL sink in `infra`
  (`PersistenceAdapter.append_signal_ledger` → `signal_ledger.jsonl`), and an
  `app` assembler (`app.ledger.assemble_ledger_record`) reading domain objects
  only. The orchestrator writes **one record per analyzed symbol per cycle, for
  every tier (A+/VALID/WATCH/SKIP)**, right after a completed `analyze()` —
  best-effort, write-only, never read back into a decision, and never written on a
  pre-analysis skip or runtime failure. Distinct from the existing artifacts
  (Telegram / `active_pois.json` / `killed_thesis.json` /
  `retracement_ob_audit.jsonl`), which all remain.
- **Design decisions:** the ledger stores *what the strategy decided*, never an
  outcome — WIN/LOSS/BE/EXPIRED/NOT_FILLED are derived LATER by replaying forward
  price and joined by `(symbol, h1_last_ts)`, keeping the append-only source
  pure/immutable. Schema is additive-only (old lines stay readable forever). The
  backtester (Phase 8) will emit the **same** `SignalLedgerRecord` schema so live
  and simulated records share one analysis pipeline.
- **Verification:** +21 tests (domain schema/round-trip/forward-compat; infra sink
  append/order/round-trip/OS-error; app assembler across scored/no_direction/
  invalid tiers + orchestrator wiring: one record per tier, none on pre-analysis
  skip, write-failure swallowed). Full suite **784**, only the pre-existing
  calendar flake failing. Behavior proof: a real scored-retracement line carries
  full HTF/confirmations/levels/selected-OB; a SKIP line nulls candidate-only
  fields but keeps HTF from context; JSONL round-trips identically.
- **Status:** ✅ Signal Ledger complete. Evaluation workflow documented in
  `STRATEGY_NOTES.md`. Next: collect data + build the backtester (Phase 8).

### S3 — Replay & Outcome Semantics Contract; BUG-1/BUG-2 fixed
- **Date:** 2026-07-26
- **Purpose:** Close the Phase-8 design blocker — eliminate every execution/
  outcome ambiguity before any replay code exists — and land the two approved
  metadata fixes so the evaluation dataset is complete from day one.
- **What happened:**
  - **`REPLAY_CONTRACT.md` v1.0 created** (a new living document, binding once
    approved): signals are opinions, trades are **alert-instantiated** frozen
    orders (C-2); RETRACEMENT = touch-filled limit, CONTINUATION = market at
    next H1 open (C-4.1/C-4.2); the **lifecycle is the order-lifetime model**
    (C-4.8); outcome vocabulary PENDING/ACTIVE/WIN/LOSS/NOT_FILLED(reason)/
    INVALIDATED_EXIT/EXPIRED/OPEN with a full state machine (C-3); M5
    sub-resolution with worst-case SL-first tie-break (C-4.3); **1 scan per
    closed H1** replay cadence with declared fidelity losses (C-5); exact
    window-shape data contract exploiting ATR causality (C-6);
    `HistoricalMarketDataAdapter` spec (§7); **replay must reuse the production
    `Orchestrator`** via dependency swaps (C-8); Outcome Engine + backtester
    architecture (§9–10); full metrics inventory (§12); declared biases (§13).
  - **BUG-1 fixed** ([v2/analysis/risk.py](v2/analysis/risk.py)): continuation
    `tp_source`/`tp_rr` — dropped in V1 by a missing-`nonlocal` scoping accident
    — are now published. Proven metadata-only: consumer enumeration shows
    nothing branches on them; `sl`/`tp` remain byte-identical to V1 across the
    corpus (asserted); the one V1 divergence is carved out explicitly in the
    re-based tests.
  - **BUG-2 fixed** ([v2/app/audit.py](v2/app/audit.py)): OB-audit now records
    `location`, `window_bounds`, `selected_ob_index/rank/top/bottom/size`, and
    `current_price` (defined by the contract as the OB-filter reference price —
    the last **closed** H1 close, distinct from `market_price`). Write-only
    observability; zero decision impact.
  - **`STRATEGY_VERSION` → 2.0.1** (dataset lineage for the metadata change);
    A26–A46 audit findings absorbed into `STRATEGY_NOTES.md`; a stray
    accidental keystroke in a `context.py` docstring was reverted to the
    committed state.
- **Verification:** full suite **784 tests — the only failure is the
  pre-existing calendar flake**. Three metadata tests re-based deliberately
  (`test_risk.py` field-for-field carve-out + inverted quirk test,
  `test_audit.py` populated-metadata assertions, `test_scoring.py` bridge
  carve-out); every decision-bearing assertion (scoring/HTF V1 diffs, lifecycle
  replay equivalence, orchestrator behavior) passes unchanged.
- **Status:** ✅ Contract delivered; bugs closed. Superseded by **S4** — v1.0's
  time semantics (C-6.4) and its M5 fidelity claim (C-5.2) were later found to be
  **wrong**; see below.

### S4 — Milestone M0: REPLAY_CONTRACT v1.1; the four Phase-8 blockers closed
- **Date:** 2026-07-27
- **Purpose:** A final architecture review before any replay code, then permanently
  eliminate every ambiguity that could make two engineers build two different
  replay engines. Design and documentation only — **no production code.**
- **What the review found (12 new findings, A47–A58 in `STRATEGY_NOTES.md`):**
  four blockers, one factual error in v1.0, and a measured performance ceiling.
  - **B1 / A47 — replay time semantics were ambiguous *and* v1.0 was wrong.**
    `h1_last_ts` is the **open** time of the last closed H1 (`T − 3600`), not the
    close boundary: kline field 0 is the open time and live reads it after the
    forming-candle strip. Confirmed against production data — the tail of
    `retracement_ob_audit.jsonl` carries `h1_last_ts = 02:00Z` for rows appended at
    ≈`03:00Z`. This was not a metadata ambiguity: anchoring the clock to
    `h1_last_ts` instead of `T` shifts `session_active`, a **scoring input**, and
    therefore changes decision tiers at the 07:00/22:00 UTC edges.
  - **B2 / A49 — window construction was undefined** as row-based vs. time-based.
    Live receives the *N most recent existing* klines; because `safe_frame` drops
    NaN rows, a well-intentioned reindex-to-grid shortens the analyzed frame and
    shifts every positional index (OB window, swing loop, EMA seeds).
  - **B3 / A50 — `cancel_reason` was unobservable.** `LifecycleDecision` never
    leaves `_process_symbol`, `SignalStore` is not injectable, and TTL decisions
    reach the outside world only as rendered strings.
  - **B4 / A53 — the ledger had no lineage.** Live and replay rows were
    structurally identical, and lineage cannot be backfilled.
  - **A48 — v1.0 documented a fidelity loss that does not exist.** The M5
    attachment guard only passes for wall times in `[hh:00, hh:05)`, so live can
    enrich with M5 only in the first five minutes of an hour. The replay scan at
    `T + 1s` reproduces exactly that scan — and the real divergence is the reverse
    of what v1.0 claimed: replay is *more* M5-complete than live.
  - **A52 — measured performance ceiling:** 213.7 ms per symbol-scan, ~90 % inside
    `identify_swings`; 5 years × 30 symbols ≈ 78 h single-threaded, which would
    make the Phase 9–10 experiment loop impractical.
- **What happened:**
  - **`REPLAY_CONTRACT.md` rewritten as v1.1**, replacing v1.0 entirely (not an
    addendum). v1.0's clause numbers §1–§14 are **preserved** — `risk.py`,
    `audit.py` and three living documents already cite C-1.3, C-4.7, C-5.2, C-6.2,
    C-8.1, C-9.4 and §12 — with amended clauses marked and new material appended as
    §15–§19. New/rewritten: §0 glossary and citation convention; §5 replay time
    (C-5.0–C-5.8: `T`, scan instant, reference candle, ordering guarantees,
    burn-in, worked example incl. the midnight edge case); §6 window construction
    (C-6.3's eight prohibitions, partial history/outage/listing table, universe
    data scope + name tie-break + 24-row eligibility, integrity and
    `data_snapshot_id`); §7 invariants I-1–I-10; §15 lifecycle capture
    (`RecordingReconciler`, event schema, TTL attribution **with a completeness
    proof** from the two removal branches of `_apply_mutation`, plus a writer-based
    cross-check); §16 ledger `schema_version` 2 (nine lineage fields, each with its
    justification, and the exact bounded production change M1 may make); §17
    testing architecture; §18 proof of determinism (every nondeterminism source
    enumerated and checked against the source); §19 decisions log with 20 recorded
    decisions and their rejected alternatives.
  - **Residual ambiguities found by mentally implementing the rewrite** and then
    closed: the fill-walk origin (`W0` = the candle opening at `T`); pre-crossed
    limits (immediate fill, not a patient limit); missing-M5 fallback; expiry
    counting; `R_unit` measured from the **fill** price, not the structural entry;
    a canonical `OutcomeConfig` field list and hash; deterministic equity ordering
    for max-drawdown; and v1.0's self-contradictory shadow population (it defined
    shadows as *insertions without an alert* yet included CAPPED, which produces no
    insertion) — now three explicit `shadow_kind` classes.
  - **Living documents synchronized.** `PROJECT_STATE.md` → doc v3.1: the stale
    "not yet standalone" blockquote removed (it contradicted §2 and §6), test
    counts corrected to 784, the M0/M1–M11 roadmap recorded, and a new working
    rule 9 — *one canonical replay specification*. `STRATEGY_NOTES.md` gained the
    A47–A58 table and its evaluation section now defers to v1.1 explicitly, with
    the outdated join-key and "expiry via TTL" statements corrected.
- **Verification:** full suite re-run — **784 tests, 0 failures** (the
  calendar-dependent test passes when run in session hours). No production file was
  modified in this milestone; the only code-adjacent finding, a stale join-key
  docstring in `domain/signal_ledger.py`, is scheduled as part of M1 (C-16.3.1
  item 6) rather than fixed here.
- **Status:** ✅ **Phase 8 architecture closed.** Two independent engineers can now
  build the same replay engine from the repository alone. M1 (ledger schema 2) may
  begin immediately; it is the only production change the contract authorizes
  before the data layer.

### S5 — Performance roadmap, Step 0: the environment becomes a checked property
- **Date:** 2026-08-02
- **Purpose:** Close the reproducibility gap that a performance architecture review
  surfaced, before any optimization is written. Merge #1 of the ten-step roadmap in
  `IMPLEMENTATION_PLAN.md`.
- **What happened:** the review that sized the acquisition and replay optimizations
  found that its largest scientific risk was not any proposed optimization — it was
  that the project had **no dependency pinning at all**. `REPLAY_CONTRACT.md` C-19
  conditions float determinism on version identity, and C-16.2 records the
  environment in every run manifest; but recording makes drift *detectable after the
  fact*, never impossible. A `pip install` between two baselines would have
  invalidated the comparison while both manifests faithfully reported their own
  (different) environments and nothing refused the diff.
  - **`requirements.txt`** now pins all 34 installed distributions with `==`, plus
    the interpreter as a structured `# python==3.13.5` comment, so one file remains
    the single source. Captured verbatim from `pip freeze` and diffed against it.
  - **`tests/test_environment.py`** asserts the running environment matches, and
    fails at test time rather than at interpretation time.
- **The `bottleneck` finding.** C-8.5 mandates that `identify_swings` keep computing
  `atr_mean` with `df["atr"].dropna().mean()` — *"(pandas' summation order) rather
  than a numpy equivalent."* That mandate turned out to be satisfied only by an
  accident of the install: `pd.options.compute.use_bottleneck` is `True`, and pandas
  dispatches the reduction to `bottleneck` whenever it is importable. `bottleneck`
  sums sequentially; numpy sums pairwise. **Measured: the two orders disagree in ~90%
  of realistic 499-element ATR arrays.** Installing `bottleneck` — a transitive
  dependency of several scientific packages — would therefore have changed
  `min_prominence`, and potentially the swing set, with no code change anywhere. It
  is now explicitly forbidden and asserted absent by metadata *and* by import, since
  a vendored module has no distribution record but pandas would still dispatch to it.
  - Setting the pandas option in `v2/` would be the other enforcement route and was
    deliberately rejected: C-16.3.1 authorizes only the M1 lineage set and the C-8.5
    work as production edits. The guard lives in `tests/`, where it changes no
    runtime behavior.
- **Verification:** 6 new tests, all passing. Each was **negative-controlled** — a
  drifted version pin, a drifted interpreter pin, a missing `requirements.txt` and an
  importable `bottleneck` were each simulated and each produced a failure, then the
  control run returned to green. A pinning test that cannot fail is worthless, so the
  discrimination is proven rather than assumed. Full suite: **1,829 tests, 1
  failure** — `test_corpus_covers_every_decision_tier`, the known wall-clock flake:
  `SESSION_ACTIVE` is read at import from `get_session_mode()`, which returned
  `STRICT` at 20:59 UTC, and the A+ tier is unreachable without the SNIPER session
  bonus. Pre-existing and unrelated; confirmed by inspection at run time.
- **What did NOT change:** no production file. No `v2/`, no `backtest/`, no `src/`.
  Runtime behavior, replay output, the downloader and corpus identity are all
  untouched by construction — the merge adds two files and edits two documents.
- **Status:** ✅ Step 0 complete. Step 1 (`requests.Session` per worker) may begin.

### S6 — Performance roadmap, Step 1: pooled HTTP connections in acquisition
- **Date:** 2026-08-02
- **Purpose:** Remove the per-request TLS handshake from the acquisition transport.
  Merge #2 of the roadmap in `IMPLEMENTATION_PLAN.md`.
- **What happened:** `RequestsTransport.get` called the module-level
  `requests.get()`, which constructs a throwaway `Session` — and therefore a fresh
  TCP connection and TLS handshake — for **every** request. Stage 1 made ~45,000 of
  them; Stage 2 will make ~79,000. The transport now holds one `requests.Session`
  per thread and reuses it.
  - Headers are still passed **per request** rather than moved onto the session, so
    `Session.prepare_request`'s merge order is byte-identical to what
    `requests.get(url, headers=...)` produced. Only the connection underneath the
    request changes.
  - The session is **thread-local**, not shared: `requests.Session` is not documented
    thread-safe. A single-threaded caller sees exactly one session; the structure is
    what lets Step 3b reuse this class unchanged.
  - A `close()` method was added because the class now owns a resource it did not own
    before — `requests.get()` closed its session every call. Deliberately **not** on
    the `HttpTransport` Protocol, which would force every test double to implement a
    method that means nothing to it.
- **Equivalence, proven end to end.** A hermetic A/B harness ran the pre-change and
  post-change transports through a real `Downloader` + `RawStore` over an identical
  66-item plan, served byte-identical payloads. Identical: download plan and paths,
  report output (excluding the two wall-clock fields), committed artifacts, every
  SHA-256, the raw manifest, and the `(path, sha256)` tuple set that `_corpus_id`
  hashes. TCP handshakes fell from **132 to 1**.
- **Measured speedup: 1.58×, below the 1.84× the plan projected.** 20 requests per
  arm against the Binance archive host: 392.4 ms → 247.7 ms mean, 2.55 → 4.04
  requests/sec. The unpooled arm reproduced its earlier measurement almost exactly
  (394.0 → 392.4 ms); the pooled arm was slower than before (214.3 → 247.7 ms),
  which is latency variance on a path where the pooled case is dominated by RTT
  rather than by handshake. The honest range is **1.58–1.84×**, and Step 1's
  contribution to Stage 2 should be read as ~8.4–9.7 h rather than 8.4 h. This does
  not move the roadmap: Step 3b's ~6× dominates the acquisition budget.
- **A regression the full suite caught, and what it teaches.** Moving the seam broke
  `tests/backtest/test_acquisition_status.py`, which stubbed the module-level
  `requests.get`. With the transport routing through `session.get`, the stub was
  bypassed and the "hermetic" test **opened a real socket to `fapi.binance.com`** —
  one of its two cases errored on a live `-1102` response, and the other *passed for
  the wrong reason* because the exchange happened to return a non-`-1121` 400, which
  is what it asserts. Both now stub `requests.Session`. The whole module was then
  re-run with `socket.connect` hard-blocked: **29 tests, 0 failures**, runtime
  0.744 s → 0.007 s. The new transport tests were verified the same way (99 tests,
  sockets blocked). A stubbed seam is a coupling between test and implementation,
  and moving an implementation seam silently converts a hermetic test into a network
  test that can pass by luck.
- **Verification:** 13 new tests (99 in the acquisition module, all passing), each
  **negative-controlled** — reverting `get()` to its pre-Step-1 body failed 3 of the
  4 pooling tests while the semantics tests stayed green, so the new tests are shown
  to discriminate rather than assumed to. Full suite: **1,842 tests, 1 failure** —
  the known session-hours flake, unrelated.
- **What did NOT change:** the downloader, the planner, storage, the retry policy,
  integrity verification, corpus identity, replay, coverage, strategy, universe and
  canonical datasets. One file changed in production: `backtest/acquire/transport.py`.
- **Status:** ✅ Step 1 complete. Step 2 (`Retry-After` + 418/429) may begin.

### S7 — Performance roadmap, Step 2: the server's `Retry-After` is obeyed
- **Date:** 2026-08-02
- **Purpose:** Stop retrying rate-limit responses on our own backoff. Merge #3 of
  the roadmap in `IMPLEMENTATION_PLAN.md`, and the safety prerequisite for Step 3b.
- **The defect.** `RequestsTransport` mapped every non-200/404/`-1121` answer to a
  generic `TransportError`, and `Downloader._retry` retried it at 1 s, 2 s, 4 s.
  That is the exact behaviour Binance documents as escalating a 429 into a 418
  auto-ban, and a repeat 418 lengthens the ban **from 2 minutes to 3 days**. Stage 1
  already hit six 418s at one worker; raising concurrency without fixing this first
  would have converted a throttle into a multi-day outage.
- **What changed.** Two `TransportError` subclasses — `TransportRateLimited` (429)
  and `TransportBanned` (418) — each carrying the stated `retry_after`, plus
  `parse_retry_after`. Subclassing is what keeps every existing
  `except TransportError` site working untouched. `_retry` handles them ahead of the
  generic branch:
  - **429** keeps its place in the attempt budget but waits the server's figure
    instead of `backoff_base * 2**attempt`. No parseable header ⇒ the exponential
    schedule is used unchanged.
  - **418** is a ban, not a fault. A *stated* ban of ≤ `BAN_PAUSE_LIMIT_SECONDS`
    (300) is paused through **once**; an unstated ban, a longer one, or a second ban
    propagates and stops the run with the duration in its message. A ban therefore
    never becomes an item `failed` — it is a condition of the whole run, and burying
    it among 79,000 rows is how it would be missed.
  - Only the delta-seconds form of `Retry-After` is parsed. RFC 9110 also allows an
    HTTP-date, but Binance sends delta-seconds and reading a date needs a clock the
    transport deliberately does not have. Unparseable, negative and NaN values all
    return `None`, so the caller falls back to its own backoff rather than to a
    fabricated number.
- **Equivalence, proven.** An A/B harness ran the pre- and post-change `_retry`
  through a real `Downloader` + `RawStore` over an identical 66-item plan. On the
  clean path all eight identity checks pass: download plan, report output (ex
  wall-clock fields), artifacts, checksums, manifest, `(path, sha256)` identity
  input, retry ordering **and counts**, and request count. The identity digest
  `d4e464deab78bb57` is the same value Step 1's harness produced.
- **The intended difference, measured.** Against a persistent failure, 4 attempts:

  | scenario | pre → post requests | pre → post retries | pre → post delay |
  |---|---|---|---|
  | 418, no `Retry-After` | 4 → **1** | 3 → **0** | 7 s → 0 s, run aborts |
  | 418, stated 3600 s | 4 → **1** | 3 → **0** | 7 s → 0 s, run aborts |
  | 429, stated 30 s | 4 → 4 | 3 → 3 | 7 s → **90 s** (server-directed) |
  | 500 (unchanged) | 4 → 4 | 3 → 3 | 7 s → 7 s |

  **Three unnecessary retries are avoided per banned item**, and each one was
  previously deepening the ban. Clean-path throughput is unaffected: 4.45 req/s
  post-Step-2 against Step 1's 4.04 req/s reference, the difference being latency
  noise on a path Step 2 does not touch.
- **Negative controls.** Reverting `_retry` to its pre-Step-2 body failed **8 of 13**
  policy tests (Retry-After demonstrably ignored before), while the 19 pre-existing
  `TestDownloader` tests passed under **both** implementations — non-429 retry
  behaviour is invariant across the change, not merely asserted to be.
- **Verification:** 21 new tests, 120 in the acquisition module. Both acquisition
  modules re-run with `socket.connect` hard-blocked (120 + 29 tests, zero failures)
  — the carried check from S6, since Step 2 touches the same seam. Full suite:
  **1,863 tests, 1 failure**, the known session-hours flake.
- **What did NOT change:** the planner, storage, the commit protocol, integrity
  verification, corpus identity, replay, coverage, strategy, universe and canonical
  datasets. `TransportNotFound` and `DuplicateArtifact` are still never retried.
- **Status:** ✅ Step 2 complete. Step 2b (`StorageTransient`) may begin.

### S8 — Performance roadmap, Step 2b: a locked file no longer kills the run
- **Date:** 2026-08-02
- **Purpose:** Stop a transient filesystem error from terminating a multi-hour
  acquisition. Merge #4, and the second safety prerequisite for Step 3b.
- **The defect.** `RawStore.commit` writes through `_atomic_write`, whose
  `os.replace` raises `PermissionError` on Windows while a scanner, indexer or
  backup agent holds the destination open. `PermissionError` is an `OSError`, not
  an `AcquisitionError`, so it fell through `_retry`'s catch tuple, through
  `acquire()`, through `run()`, and killed the entire acquisition — losing hours of
  work to a condition that clears in milliseconds. The probability scales with
  file-creation rate, which is exactly what Step 3b raises.
- **What changed.** A `StorageTransient(AcquisitionError)` type, raised from
  `_atomic_write` for any `OSError` (open, write, fsync or replace) and added to
  `_retry`'s generic branch, so it takes the same 1/2/4 backoff as a corrupted
  transfer.
  - **Deliberately not an `IntegrityError`.** A locked file is not corrupt bytes.
    Conflating them would let a genuine checksum failure read as a filesystem
    hiccup — the two are retried identically but mean opposite things, and only one
    says the evidence cannot be trusted. Asserted directly by a subclass test.
  - The temp-file cleanup on the failure path is now itself guarded: a `.part` we
    cannot remove must not mask the failure that produced it. It is inert —
    nothing reads a `.part`, and `records()` globs only `*.meta.json`.
  - **Scope judgment, flagged for review:** the plan named `_atomic_write`, and the
    `mkdir` immediately preceding it in `commit` is wrapped too. It is the same
    condition one line earlier, and leaving it bare would still lose the run to the
    identical Windows fault. Two lines, individually revertible.
- **Equivalence, proven.** An A/B harness ran pre- and post-change through a real
  `Downloader` + `RawStore` over an identical 66-item plan. On the clean path all
  nine checks pass — download plan, report output (ex wall-clock), artifacts,
  checksums, manifest, corpus identity input, retry ordering **and counts**, request
  count, and no `.part` residue. Identity digest `d4e464deab78bb57`: the same value
  Steps 1 and 2 produced.
- **Behaviour under fault, measured.**

  | scenario | pre-Step-2b | post-Step-2b |
  |---|---|---|
  | every 10th `os.replace` → `PermissionError` | **RUN DIED**, `PermissionError` escaped | completed; 16 faults injected, 16 retries, **66/66 artifacts recovered**, `failed=0` |
  | every `os.replace` → `ENOSPC` | — | completed; **66/66 items correctly failed**, 0 artifacts committed, no `.part` residue |

  The recovered corpus is **bit-identical to the clean one** — identity
  `d4e464deab78bb57` in both. Recovery does not merely finish; it produces the same
  corpus. Clean-path overhead is unmeasurable (397 ms vs 407 ms over 66 items, i.e.
  within noise and negative in this sample).
- **Negative controls.** Reverting `_atomic_write` and `_retry` to their pre-Step-2b
  bodies failed **8 of 11** `StorageTransient` tests, while the 15 pre-existing
  `TestStorage` and 19 `TestDownloader` tests passed under **both** implementations
  — the commit protocol and downloader behaviour are invariant across the change,
  not merely asserted to be. Step 2's 13 `Retry-After` policy tests also still pass.
- **Verification:** 11 new tests, 131 in the acquisition module. Both acquisition
  modules re-run with `socket.connect` hard-blocked (131 + 29, zero failures). Full
  suite: **1,874 tests, 1 failure**, the known session-hours flake.
- **What did NOT change:** the commit protocol (temp → fsync → `os.replace`, sidecar
  as the commit marker), integrity verification, the planner, transport policy,
  download ordering, corpus identity, replay, coverage, strategy, universe and
  canonical datasets. `DuplicateArtifact` and `TransportNotFound` are still never
  retried.
- **Status:** ✅ Step 2b complete. Step 3a (deterministic `DownloadReport`) may begin.

### S9 — Performance roadmap, Step 3a: the report is ordered by plan, not by schedule
- **Date:** 2026-08-02
- **Purpose:** Make `DownloadReport.outcomes` ordering a property of the structure
  rather than a consequence of the schedule — **before** concurrency exists to
  confound it. Merge #5, and the last prerequisite for Step 3b.
- **Why it is its own merge.** At one worker, appending and index-assignment are
  identical. Landing the reassembly separately means that if a report ever diverges
  after Step 3b, the cause is attributable to the pool and not to this refactor.
  That bisect insurance is the entire value of the merge; it buys no speed.
- **What changed.** `Downloader.run` assigns into a pre-sized
  `list[Optional[ItemOutcome]]` by plan index instead of appending, then asserts no
  slot is empty before building the tuple. The assertion is not decoration: a short
  report reads as a smaller plan rather than as a bug, and `verify_coverage` cannot
  catch it because the gap is in the report, not in the store.
- **The golden was captured before the edit**, as the plan requires — the append
  implementation's output for a fixed 66-item plan, sha256 `31a2eeec…`, verified
  reproducible across three independent builds against unmodified code, and
  deliberately constructed to exercise **all four** outcome statuses (63
  `downloaded`, 1 `empty`, 1 `absent`, 1 `failed`, 3 retries). Regenerating it from
  post-change code would have made the test tautological, which is why the capture
  script refuses to overwrite an existing golden.
- **Equivalence.** All nine A/B checks pass between the pre- and post-change
  implementations: download plan, report contents (ex wall-clock fields), outcome
  ordering, failure classification, retry counts, artifacts, checksums, manifest,
  and corpus identity input.
- **The negative control that gives the merge its meaning.** Results were produced
  in a seeded permutation — what Step 3b will actually generate — and reassembled
  both ways. Appending recorded the permutation (`2023-11-04, 2023-11-10,
  2023-11-30, …`); indexing recorded the plan (`2023-11-01, 2023-11-02, …`). The
  old assembly is demonstrably unstable under out-of-order completion; the new one
  is not. **No concurrency was added to production code to prove this.**
- **Report ordering provably cannot reach the corpus.** The permuted run and the
  in-order run produced the same identity digest `82c92976e16cd16c`, because
  `records()` sorts by `artifact_path` and `_corpus_id` hashes *sorted*
  `(path, sha256)`. Asserted in the suite so a future report change cannot quietly
  acquire that power.
- **Overhead.** Best of 5 over 66 items: 385.1 ms → 385.3 ms, **+3.09 µs/item** —
  indistinguishable from noise. Memory: 528 bytes for the pre-sized list (8 B/item),
  released once the tuple is built. Report serialization 0.07 ms.
- **Verification:** 7 new tests, 138 in the acquisition module; both acquisition
  modules re-run with `socket.connect` hard-blocked (138 + 29, zero failures). Full
  suite: **1,881 tests, 1 failure**, the known session-hours flake.
- **What did NOT change:** the transport, storage, the commit protocol, retry
  policy, the planner, download ordering, corpus identity, replay, coverage,
  strategy, universe and canonical datasets. One production file changed.
- **Status:** ✅ Step 3a complete. Step 3b (parallel downloader) may begin.

### S10 — Performance roadmap, Step 3b: a bounded parallel downloader
- **Date:** 2026-08-02
- **Purpose:** Overlap network latency. 98.5% of per-item cost is HTTP and 1.5% is
  CPU, so the sequential loop was idle almost all of the time. Merge #6, the last
  before Gate A.
- **What changed.**
  - `Downloader(workers=N)` runs items across a bounded `ThreadPoolExecutor`. Each
    worker runs the **entire existing `acquire(item)` unchanged** — no stage is
    split, no queue is introduced between download and commit. The per-item
    pipeline was already correct and crash-safe; splitting it would create a new
    failure mode for a 1.5% CPU saving.
  - **`workers == 1` takes the sequential path, not a pool of one**, so
    `--workers 1` is a literal rollback to pre-Step-3b behaviour with no executor
    in the picture. A test asserts no `ThreadPoolExecutor` is ever constructed at
    one worker.
  - New `backtest/acquire/ratelimit.py`: a strict-spacing `TokenBucket` and a
    host-scoped `HostRateLimiter`. `fapi.binance.com` allows 2,400 weight/min — 4
    req/s at `limit=1500` — and is bounded to **3/s, leaving 25% headroom**. The
    bulk archive is unpaced. Worker count is the wrong control here: at ~214 ms
    even *two* workers issue ~9.3 req/s, so the bound has to be on the rate, not
    on concurrency.
  - `_atomic_write` temp names now carry pid **and thread id**. Today the planner
    gives every item a distinct path so no two writers collide anyway — the thread
    id is there because that disjointness is a property of the planner, and the
    write path should not depend on it.
  - `--workers` on `corpus build`, default 1, validated against
    `RECOMMENDED_MAX_WORKERS = 6` and recorded in the job config.
- **Two design corrections made during implementation, both worth recording.**
  - The rate limiter was first defaulted **inside `Downloader`**. That made every
    test which plans real fapi URLs sleep against a wall clock — the suite went
    from 3.6 s to minutes — and it quietly changed `--workers 1` from "the
    sequential path exactly" into "the sequential path, throttled". Pacing is an
    operational policy about a specific server, so it now lives at the production
    entry point (`build_corpus`) and the library defaults to unpaced.
  - The bucket was first a classic refill-from-elapsed-time token bucket. With a
    no-op sleeper — which every test injects — it **busy-loops** until real time
    catches up. It is now a strict-spacing reservation: the wait is computed from
    a reserved slot rather than re-read from the clock, so it cannot spin and the
    schedule is observable instead of waited on.
- **Determinism, proven at every worker count.** Against the pre-Step-3b
  implementation at `workers=1`, and at `workers = 1, 2, 4, 6`: identical download
  plan, report contents, outcome ordering, statuses, retry counts, artifacts,
  checksums, manifest and corpus identity (`82c92976e16cd16c` at all four). **The
  Step 3a golden is reproduced byte-for-byte at every worker count.**
- **Every future is observed exactly once.** `as_completed` yields into the main
  thread, each future's `result()` is consumed there, and an `observed` set is
  reconciled against the submitted set after the pool shuts down — an unobserved
  future raises rather than silently shortening the report. On an abort the
  remainder are cancelled and any that had *already failed* have their exceptions
  attached to the raised error as notes, so a pool cannot hide failures a
  sequential run would have reported. `progress` is called **only** from the main
  thread, so callers need no locking.
- **Negative controls.** Under a seeded shuffle of completion order: `append()`
  assembly fails, indexed assembly passes. A dropped future is detected
  (`AcquisitionError: 1 future(s) were never observed`). A duplicated completion
  cannot corrupt the report — the indexed write is idempotent. Corpus identity is
  a single value across all four worker counts.
- **Fault injection**, each with an assertion that the fault actually fired —
  three of these tests initially passed while injecting nothing, because they
  targeted daily ZIPs that this plan window 404s by design: `Retry-After`,
  `StorageTransient`, `TransportError` and `IntegrityError` on one worker each
  leave every other item's artifacts, checksums and classification identical.
- **Scaling**, 24 real requests per arm against `data.binance.vision`:

  | workers | wall | req/s | speedup | efficiency | CPU | sessions |
  |---:|---:|---:|---:|---:|---:|---:|
  | 1 | 5.42 s | 4.43 | 1.00x | 100% | 0.6% | 1 |
  | 2 | 2.78 s | 8.63 | 1.95x | 97% | 15.2% | 2 |
  | 4 | 1.60 s | 15.01 | 3.39x | 85% | 59.6% | 4 |
  | 6 | 1.26 s | 18.99 | **4.29x** | 72% | 121.2% | 6 |

  One session per worker, confirming the Step 1 thread-local pooling. **The 72%
  at six workers is pessimistic for this sample**: each worker pays one TLS
  handshake, amortized over only 4 requests here versus ~13,000 in Stage 2. A
  132-request simulation gives 5.09x at 85% efficiency, and CPU at 121% of one
  core shows TLS and decompression are real work under the GIL. Realistic Stage 2
  projection: **~1.9–2.4 h**, slightly above the plan's 1.6–2.0 h.
- **Verification:** 36 new tests, 167 in the acquisition module; both acquisition
  modules re-run with `socket.connect` hard-blocked (167 + 29, zero failures).
  Full suite: **1,910 tests, 1 failure**, the known session-hours flake.
- **What did NOT change:** the commit protocol, integrity verification, retry
  policy, `Retry-After` and `StorageTransient` semantics, report semantics, the
  planner, transport classification, replay, coverage, strategy, universe and
  canonical datasets.
- **Status:** ✅ Step 3b complete. All six merges landed; Gate A (Stage 2
  acquisition) is unblocked.

### S11 — Bug fix: Ctrl+C cancels queued work instead of draining the plan

Found by the pre-Gate-A operational readiness review, not by a test. **A bug fix,
not an enhancement**: [downloader.py](backtest/acquire/downloader.py) already
documented the guarantee — *"On an abort, the remainder are cancelled"* — and
already contained the cancel loop, but delivered it for only one of the two abort
classes.

- **The defect.** A worker exception surfaces from `future.result()` and is caught.
  An operator interrupt does not: `KeyboardInterrupt` is raised while the `for`
  statement is evaluating `as_completed`, which is *outside* that guard. It
  escaped to `ThreadPoolExecutor.__exit__`, whose `shutdown(wait=True)` queues its
  stop sentinel **behind** every pending work item — so the interrupt executed the
  entire remaining plan before re-raising. Measured: **900 of 900** items ran after
  the interrupt. At Gate A's 75,426 items that is roughly an hour of downloading
  after the operator asked it to stop.
- **Why it mattered beyond ergonomics.** Step 3b's own rollback criterion is
  "sustained 418s → drop to `--workers 1`", which requires halting the run. The
  bulk archive host is deliberately unpaced and carries ~99% of requests; Binance
  418 bans escalate 2 min → 3 days at IP level. The one situation where stopping
  matters most was the one where stopping did not work.
- **The change.** One outer `try/except BaseException` around the completion loop,
  routing both abort classes through a new `_cancel_pending` helper that holds the
  cancel loop the inner guard already used. ~15 lines in one file. No new concept,
  no new state, no dependency, no output change.
- **Bounded, not instant.** `cancel()` never interrupts a *running* item, by
  design — cutting a commit short is the one thing that could leave a partial
  artifact. Worst-case abort is the longest in-flight operation:
  `BAN_PAUSE_LIMIT_SECONDS` = 300 s, or the 60 s transport timeout. Previously the
  bound was the whole remaining plan.
- **Measured** (66-item plan, 4 workers): before 135 fetches / 0.38 s; after
  **21 fetches / 0.04 s**; 114 queued fetches never ran; 8.5x faster abort.
- **Negative controls.** (i) Removing the outer guard from the source restores the
  full drain — 135 of 135 — and the two interrupt tests fail. (ii) Neutering
  `_cancel_pending` reproduces the same drain behaviourally. (iii) A successful run
  never cancels anything, at every worker count. (iv) The worker-exception path
  still cancels exactly once.
- **Success path proven unchanged:** report, ordering, statuses, retries,
  artifacts, checksums, plan, manifest and corpus identity `82c92976e16cd16c`
  identical at workers 1/2/4/6, and the **Step 3a golden byte-identical** at all
  four (sha256 `31a2eeec…` untouched).
- **After an interrupt:** every committed artifact verifies, no orphan `.part`
  files, no corrupt sidecar, and a resumed run reaches a corpus identical to an
  uninterrupted one.
- **Verification:** 10 new tests, **177 in the acquisition module**; both
  acquisition modules re-run with `socket.connect` hard-blocked (206, zero
  failures). Full suite **1,920 tests, 1 failure** — the known session-hours flake.
- **What did NOT change:** the commit protocol, retry policy, `Retry-After` and
  `StorageTransient` semantics, report semantics, the planner, transport,
  storage, rate limiter, `corpus.py`, `cli.py`, replay, coverage, strategy,
  universe and canonical datasets. Two files touched.
- **Status:** ✅ Complete. Gate A has no remaining technical blocker.

## 12. Strategy Evolution Roadmap

Evidence-driven. Each phase gates the next; no calibration before a backtester
exists, no refinement before a baseline is measured.

- [ ] **Phase 7 — Evaluation** *(current)*: instrument the pipeline; capture a
      real multi-symbol/multi-cycle signal dataset; publish the strategy audit +
      baseline metrics + a `STRATEGY_NOTES.md`. Decouple `infra` from `src/` so V2
      can run standalone. **No behavior change.**
- [ ] **Phase 8 — Backtesting Framework**: deterministic, event-driven replay of
      `analysis` + `lifecycle` over historical klines, with an entry-fill model
      and SL/TP resolution, producing realized RR / hit-rate / expectancy /
      drawdown per signal. Hermetic, reproducible.
- [ ] **Phase 9 — Strategy Refinement**: fix the confirmed defects and weak
      heuristics from the backlog (OB quality + freshness-aware selection, HTF
      de-duplication, kill confirmation, decay semantics, TP/SL structure, FVG
      usefulness) — each measured against the backtester.
- [ ] **Phase 10 — Parameter Optimization**: calibrate thresholds (regime cutoffs,
      RR floor, ATR multiples, score weights, TTLs, cooldown) with walk-forward
      validation and explicit overfitting guards.
- [ ] **Phase 11 — Forward Testing**: paper/shadow the refined system live;
      compare predicted vs realized; measure the *advice's* expectancy and
      drawdown over a diverse window.
- [ ] **Phase 12 — Production**: promote the validated configuration; monitoring +
      alerting; a governed change process; optional execution integration.

---

*Maintain this file at every milestone. Chapter A is frozen history. For new work,
add one semantic entry to §11 and update §12. Never turn it into a commit log, and
never frame new work as migration.*
