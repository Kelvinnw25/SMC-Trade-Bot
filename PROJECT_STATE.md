# PROJECT_STATE.md — SMC Signal Radar V2 Migration

> **Single source of truth for the migration.** Read this before touching code.
> It is maintained at every milestone. If it conflicts with memory or chat
> history, **this file and the repository win.**
>
> - **Checkpoint:** Regression Harness complete
> - **Last updated:** 2026-07-23
> - **Doc version:** 1.0
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
and only replaces V1 once proven behaviorally identical.

**Current architecture (V2 target).** Strict acyclic layers:

```
domain  (pure, immutable, zero deps)
  ▲
infra   (I/O adapters that WRAP V1 code — the only layer allowed to import src/)
  ▲
analysis   (pure business logic — NOT built yet)
  ▲
lifecycle  (stateful reconciler — NOT built yet)
  ▲
app        (thin orchestrator — NOT built yet)

regression harness (tests/regression/) — verification only, outside runtime
```

**Overall philosophy.**
- Behavioral equivalence with V1 is the supreme rule — provable, not hopeful.
- Build the **pure deterministic core first**, wrap it in state and I/O last
  (the inverse of how V1 grew).
- Prove equivalence **at every step** with the regression harness.
- One small milestone at a time; freeze each when done.
- The six frozen docs in `SMC-BOT-DOCS/` are the governing specification.

---

## 2. Current Version

| Field | Value |
|---|---|
| **Current milestone** | Golden Oracle / Regression Harness — **COMPLETE** |
| **Current phase** | Between Phase 2 (Infrastructure) and Phase 3 (Analysis) |
| **Architecture state** | Domain ✅ · Infrastructure ✅ · Regression harness ✅ · Analysis/Lifecycle/App ❌ not started |
| **Test status** | **167 tests, all passing** (domain 39 · infra 83 · regression 45) |
| **V1 status** | Untouched and runnable (`python main.py`) |
| **Git status** | ⚠️ **NOT a git repository.** No commits, no branches, no tags. No VCS rollback exists. |
| **Git tag** | N/A (no git). Recommended once initialized: tag this checkpoint `v2-harness-complete`. |

> **Action item (unblocked, needs owner decision):** run `git init`, commit the
> current tree, and tag each completed milestone. The migration currently has no
> version-control safety net.

---

## 3. Completed Milestones

| # | Milestone | What was delivered | Key architectural decisions |
|---|---|---|---|
| 0 | **Documentation baseline** | 6 frozen governing docs in `SMC-BOT-DOCS/` synchronized; Decision **#010** added; proposed patches **P1–P5 applied** (with approval). | Frozen-docs workflow: never edit `SMC-BOT-DOCS/` without an explicit approved patch. |
| 1 | **Domain Model Foundation** (`v2/domain/`) | 18 immutable frozen dataclasses (the full typed vocabulary). 39 tests. | Immutable `@dataclass(frozen=True)`; `str` enums to preserve V1 serialization; `MarketSnapshot` DataFrames immutable **by contract**; `Signal.candidate` is single source of truth; exact 17-field `ActiveSignal` schema + V1 load-lossiness reproduced. |
| 2.1 | **Clock adapter** (`v2/infra/clock.py`) | `Clock` Protocol, `SystemClock`, `FakeClock`. 21 tests. | Injectable time (no wall-clock reads leak); enables deterministic lifecycle later. |
| 2.2 | **Market Data adapter** (`v2/infra/market_data.py`) | Wraps V1 `BinanceHandler`; produces `MarketSnapshot`. 24 tests. | Sole V2 entry to exchange data; mirrors V1 fetch limits, `iloc[:-1]` strip, live-price separation, M5 guards. |
| 2.3 | **Persistence adapter** (`v2/infra/persistence.py`) | Wraps V1 JSON persistence; reuses domain serialization. 24 tests. | Zero schema drift (V1↔V2 read each other); missing→empty; **error-surfacing divergence**: malformed/OS errors raise `PersistenceError` where V1 swallowed. |
| 2.4 | **Notifier adapter** (`v2/infra/notifier.py`) | `Notifier` Protocol + `TelegramNotifier` wrapping V1 senders. 14 tests. | Backend-agnostic seam; routes `Alert` by `.kind`; text-only (V1 has no images); `NotifierError` on failure (error-surfacing divergence). |
| — | **Golden Oracle / Regression Harness** (`tests/regression/`) | `InputSnapshot`, `ObservableBehavior`, `Analyzer` + `V1Oracle`, comparison engine, reports, runner, generator CLI, committed fixture + golden. 45 tests. | Verification-only, outside runtime; runs V1's **unmodified** functions and reads intermediates from the `context["debug"]` V1 already returns (no V1 instrumentation). |

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
│   ├─ 00-project-context.md   mission, doc-priority order, current-phase field
│   ├─ 01-legacy-documentation.md  V1 technical reference
│   ├─ 02-software-architecture.md  the 4 layers, 18 domain objects, contracts
│   ├─ 03-implementation-roadmap.md phases, build order, DoD, golden-dataset strategy
│   ├─ 04-engineering-rules.md   the never/always rules
│   └─ 05-decision-log.md        Decisions #001–#010 (binding)
├─ BLUEPRINT.md · MODULE_INTENT.md   V1-era design notes (legacy reference).
│
├─ v2/                     THE MIGRATION TARGET.
│   ├─ domain/             Phase 1 — 18 immutable objects. Zero dependencies. FROZEN.
│   └─ infra/              Phase 2 — clock, market_data, persistence, notifier. FROZEN.
│                          (Only layer permitted to import src/.)
│
├─ tests/                  Authoritative test suite (stdlib unittest).
│   ├─ domain/             Phase 1 tests + purity checks (39)
│   ├─ infra/              Phase 2 adapter tests (83)
│   └─ regression/         Golden Oracle / Regression Harness (45) + README + fixtures. FROZEN.
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
| A2 | **Only `v2/infra` may import `src/`.** It is the sole anti-corruption boundary that wraps V1. Every other V2 layer (domain now; analysis/lifecycle/app later) must never import `src/`. |
| A3 | **V1 is never modified.** `main.py`, `src/`, `BLUEPRINT.md`, `MODULE_INTENT.md` stay byte-for-byte as-is for the entire migration. |
| A4 | **Dependency direction is acyclic:** `domain ← infra ← analysis ← lifecycle ← app`. No layer imports a layer above it; no cycles. |
| A5 | **All domain objects are immutable** frozen dataclasses. The single exception is `MarketSnapshot`'s pandas DataFrames (immutable *by contract* — never mutate a received snapshot). |
| A6 | **Adapters use constructor dependency injection.** No singletons, no global state, no DI framework. The real dependency is the default; tests inject fakes. |
| A7 | **Responsibility separation:** infra = I/O only; analysis = pure business logic only; lifecycle = state only; app = coordination only (no signal-deciding logic). |
| A8 | **The regression harness is verification-only.** It lives outside runtime (`tests/regression/`); no runtime module (`v2/`, `src/`, `main.py`) may import it. |

---

## 6. Migration Principles (every future change must obey)

1. **Behavioral equivalence is supreme.** V2 must produce identical observable
   output to V1. When the Architecture spec and V1's *actual behavior* disagree,
   **V1 wins** (doc-priority order: Engineering Rules > V1 actual behavior >
   Architecture > Roadmap > Decision Log).
2. **No redesign, no optimization.** Never "improve" strategy, scoring,
   thresholds, or lifecycle. A cleaner V2 that behaves differently has **failed**.
3. **Regression-first.** The golden dataset + harness exist *before* analysis is
   ported. Prove each layer against V1 before building the layer above it.
4. **Incremental, small milestones.** One phase (or sub-phase) at a time. **STOP**
   at each milestone boundary and wait for explicit approval before the next.
5. **Freeze completed modules.** Once a milestone passes, its code is frozen;
   reopening it requires a new, explicitly-approved milestone.
6. **Surface, don't silently fix, questionable legacy behavior.** If V1 looks
   buggy, preserve it and document the concern separately — never quietly change it.
7. **No new dependencies** without explicit approval. Tests use **stdlib
   `unittest`** only. **pytest is not installed and must not be introduced.**
8. **Frozen docs.** Never edit `SMC-BOT-DOCS/` directly; deliver a proposed patch
   and wait for approval.
9. **Deliberate divergences are documented.** The only sanctioned behavior change
   so far is *error surfacing* (adapters raise typed exceptions where V1 swallowed);
   message/format/values are unchanged. Any such divergence must be documented.

---

## 7. Regression Workflow (how every future milestone is verified)

```
   InputSnapshot (JSON, deterministic, local file)
        │  .to_market_snapshot()
        ▼
   MarketSnapshot  ── identical input fed to BOTH sides ──┐
        │                                                 │
        ▼                                                 ▼
   V1Oracle.analyze()                          V2Analyzer.analyze()   ← Phase 3+
        │  (runs V1's unmodified functions)               │
        ▼                                                 ▼
   ObservableBehavior  ────────► compare ◄──────── ObservableBehavior
                                    │
                                    ▼
                     ComparisonReport  →  PASS (report.ok == True)
                     per-field PASS/FAIL, exact-equality by default
```

**Mechanics:**
- A snapshot captures exactly what one analysis run consumes (closed D1/H1/M5
  frames + forming-candle `live_price` + `h1_last_ts`). Stored as JSON; loads
  deterministically; no network.
- `V1Oracle` is the reference: it runs V1's **existing** `build_signal_context`
  / `enrich_with_m5` / `is_poi_invalidated` and reads observables out of the
  `context["debug"]` dict V1 already returns — **no V1 instrumentation**.
- `ObservableBehavior` is the backend-neutral record of every observable field
  (trend, BOS/CHOCH, liquidity, OB, FVG, score, entry/TP/SL, kill/alert-tier,
  persistence & notification payloads). It is the unit of comparison.
- Comparison is **exact-equality by default** (a rounding difference that flips a
  tier is a real divergence). `float_tolerance` exists for diagnosis only.
  A field on only one side is always a FAIL.

**Commands:**
```bash
# Rebuild the deterministic test fixture (no network):
venv/Scripts/python.exe -m tests.regression.generate synthetic \
    --out tests/regression/fixtures/synthetic_short.json

# Capture a real snapshot from live V1 (network; manual only):
venv/Scripts/python.exe -m tests.regression.generate live \
    --symbol BTC/USDT --out tests/regression/fixtures/btcusdt.json --m5 --golden
```
```python
# Verify a future V2 analyzer against V1 on a snapshot:
from tests.regression.snapshot import InputSnapshot
from tests.regression.oracle import V1Oracle
from tests.regression.runner import compare_analyzers
snap = InputSnapshot.load("tests/regression/fixtures/synthetic_short.json")
report = compare_analyzers(snap, V1Oracle(), V2Analyzer())   # expected, actual
assert report.ok      # zero divergence == behaviorally equivalent
```

Full detail: **`tests/regression/README.md`**.

---

## 8. Roadmap

- [x] **Milestone 0** — Documentation baseline (frozen docs, Decision #010, P1–P5)
- [x] **Phase 1** — Domain Model Foundation (`v2/domain/`, 18 objects)
- [x] **Phase 2** — Infrastructure Adapters
  - [x] 2.1 Clock · [x] 2.2 Market Data · [x] 2.3 Persistence · [x] 2.4 Notifier
- [x] **Golden Oracle / Regression Harness** (`tests/regression/`)
- [ ] **Golden dataset capture** — real multi-symbol, multi-cycle V1 recordings across diverse market conditions *(prerequisite for meaningful Phase 3 validation; the committed synthetic fixture is a plumbing check, not coverage)*
- [ ] **Phase 3** — Pure Analysis Layer  ⬅️ **NEXT** (see §11)
- [ ] **Phase 4** — Lifecycle Core (reconciler + rules + store; injected clock)
- [ ] **Phase 5** — Orchestration & Delivery Wiring (thin `app.orchestrator`)
- [ ] **Phase 6** — Parallel Validation & Cutover (shadow-mode diff → zero divergence → retire V1)

---

## 9. Definition of Done (per milestone)

A milestone is complete only when **all** hold:

- [ ] **Scope respected** — only the approved milestone was built; no scope creep.
- [ ] **Architecture rules hold** (§5) — verified, incl. domain purity / no `src/`
      import outside infra.
- [ ] **Regression passes** — for behavior-bearing phases (3+),
      `compare_analyzers(..., V1Oracle(), V2...).ok` across the golden dataset
      with **zero divergence**. Plus the phase-specific DoD in
      `SMC-BOT-DOCS/03-implementation-roadmap.md §8`.
- [ ] **All tests pass** — `venv/Scripts/python.exe -m unittest discover tests`
      (currently 167).
- [ ] **V1 untouched** — `main.py`, `src/`, `BLUEPRINT.md`, `MODULE_INTENT.md`
      unchanged (confirm via mtime / no diff).
- [ ] **No new dependencies** — stdlib only unless explicitly approved.
- [ ] **Frozen docs untouched** — or delivered as an approved patch.
- [ ] **`PROJECT_STATE.md` updated** — this file reflects the new state.
- [ ] **Project memory updated** — `…/memory/v2-migration-status.md`.
- [ ] **Committed & tagged** — once git exists: one commit per milestone + a tag.
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
| `tests/regression/` (harness, fixtures, goldens) | The equivalence reference — must stay stable so future results are comparable. |
| `active_pois.json`, `killed_thesis.json`, `retracement_ob_audit.jsonl` | V1 **live** runtime state — production data, not test scratch. |

**Reopening a frozen subsystem requires a new, explicitly-approved milestone**
(e.g. a genuine defect found during a later phase). It is never done casually to
make a later phase convenient.

---

## 11. Next Milestone — Phase 3: Pure Analysis Layer

**Goal.** Port V1's analysis into decomposed, stateless, pure modules under
`v2/analysis/`, producing an immutable `Signal` from a `MarketSnapshot`.

**Modules, built in pipeline order** (roadmap §2/§3):
`analysis.htf` → `analysis.regime` → `analysis.structure` → `analysis.entry` →
`analysis.risk` → `analysis.scoring` → `analysis.engine` (orchestrates the chain).

**Deliverable.** A `V2Analyzer` implementing the harness `Analyzer` protocol
(`analyze(MarketSnapshot, *, prior=None) -> ObservableBehavior`) so it plugs
directly into the existing comparison workflow.

**Definition of Done (roadmap §8, Phase 3).**
- For 100% of golden-dataset inputs, V2's final Signal (decision, score,
  direction, entry_mode, entry_price, sl, tp, reason) **exactly matches** V1.
- Every intermediate also matches (HTF bias, regime label, OB selection, RR).
- **Zero divergence** via `compare_analyzers(snap, V1Oracle(), V2Analyzer()).ok`.
- No analysis module references memory, persistence, notification, clock, or
  prior-signal state (verified by dependency inspection — pure).

**Prerequisite.** Capture a **real golden dataset** first (harness `generate
live` over a diverse market window — trending both directions, ranging, ≥1
regime transition, plus known-messy cases: near-RR-floor, OB-questionable,
KILL-latency). The committed synthetic fixture only proves the plumbing.

**Known subtle behaviors that MUST be preserved bit-for-bit** (roadmap §7):
the CONTINUATION `entry_source` gate keying; the choppy penalty applied in two
places; M5 enrichment only at `score >= 8`; OB **first-match** selection; RR
computed from the structural entry; and floating-point/arithmetic-order
determinism (score & RR feed decision thresholds).

> Do not begin Phase 3 without explicit approval, and do not design beyond it.

---

## 12. Important Notes (read before writing any code)

1. **V1 is the source of truth for behavior.** If a doc and V1 disagree, V1 wins.
   Read the actual `src/strategy.py` / `main.py`, don't trust summaries.
2. **This is not a git repo.** There is no rollback net. Be conservative;
   consider `git init` + per-milestone tags before large work.
3. **Use the venv Python:** `venv/Scripts/python.exe`. `pytest` is **not**
   installed and must not be added — tests are stdlib `unittest`.
4. **The harness deliberately does NOT cover everything.** It captures
   analysis-level observables + a **single-cycle, state-independent projection**
   of the persistence/notification payloads. It **excludes**: wall-clock fields
   (`timestamp`, `alert_sent_ts`) and all **path-dependent lifecycle** sequencing
   (miss_count progression, alert dedup / upgrade routing, TTL, replace, the
   OB-audit dedup set). **Phase 4 handles those** and reuses this comparison
   machinery for ordered-sequence replay with the injected clock.
5. **Injected clock is mandatory for Phase 4** determinism. `v2/infra/clock.py`
   already provides `FakeClock`; wire it — never read the wall clock in
   analysis/lifecycle.
6. **Serialization fidelity is exact.** The persisted `ActiveSignal` is a
   **17-field** schema; V1's `load_memory` drops `entry_source` + `signal_id` and
   coerces `score` to float on load (lossy **by design** — reproduce it, don't
   "fix" it). Keep all enum-like values as V1's exact strings.
7. **Verified V1 quirks that must be preserved** (do not re-derive wrongly):
   - Killed-thesis cooldown **exists** (`killed_thesis.json`, 1h / one closed H1).
   - Same-thesis partial refresh does **NOT** reset `miss_count`/`last_miss_h1_ts`.
   - Kill switch fires on **D1+H4 HTF bias flip** *and* on SL breach (+0.30·ATR).
   - `last_ob_audit_h1_ts` is vestigial; OB-audit dedup is an in-memory set owned
     by the orchestrator (Decision #010 — `OBAuditRecord` is an observability
     contract, never read back into any decision).
8. **Binding decisions:** `SMC-BOT-DOCS/05-decision-log.md` #001–#010. Notably
   **#001** (Contract B: `entry_price` is a structural reference, not executable)
   and **#010** (domain extensions + observability contract).
9. **Stop discipline.** Each milestone ends with a STOP. Deliver the milestone's
   report, update this file, and wait for approval. Do not chain phases.

---

*End of PROJECT_STATE.md — update this file at every milestone boundary (§9).*
