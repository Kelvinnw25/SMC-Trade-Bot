# SMC Signal Radar V2 — Implementation Roadmap

**Governing principle:** This roadmap prioritizes one thing above all — **behavioral equivalence with V1, provable at every step.** V2 is not a rewrite you validate at the end; it is a migration where each phase produces a system whose output can be diffed against V1 before proceeding. The golden dataset (Phase-independent, built first) is the spine that makes every subsequent claim of "still equivalent" objective rather than hopeful.

A note on sequencing philosophy: the safest order builds **the pure, deterministic core first** (because it's the easiest to prove correct), then wraps it in state and I/O last (because those are where divergence hides). This is the inverse of how V1 grew — V1 started with the loop and grew logic inward; V2 starts with logic and grows the loop outward.

---

## 0. Prerequisite — Golden Dataset (Before Any Phase)

This is not a phase; it is the foundation the behavioral-equivalence phases lean on. It must exist before **Phase 3's** analysis validation is meaningful; Phase 1's domain-model validation instead relies on the existing `active_pois.json` and `retracement_ob_audit.jsonl` (see Project Context for the approved sequencing — golden-dataset capture is scheduled as its own milestone before Phase 3). Full design in §6, but flagged here because **build order depends on it existing before the analysis layer.**

Capture, from the running V1 system, a frozen record of inputs and outputs across a diverse market window. Without this, "V2 matches V1" is untestable and the whole roadmap loses its safety net.

---

## 1. Implementation Phases

### Phase 1 — Domain Model Foundation
**Purpose:** Establish the immutable objects that every other module produces and consumes. These are the typed contracts; nothing can be built or tested against a boundary that doesn't exist yet. This phase produces no behavior — it produces the vocabulary.

### Phase 2 — Infrastructure Adapters (behind contracts)
**Purpose:** Wrap the existing V1 I/O — exchange fetching, persistence, notification, clock — behind clean contracts, *without changing what they do*. This lets the pure core be developed and tested against real or recorded data while the actual I/O remains V1's proven code underneath. Critically, the **clock becomes injectable** here, which unlocks deterministic testing for everything downstream.

### Phase 3 — Pure Analysis Layer
**Purpose:** Port V1's `build_signal_context` / `score_signal` / entry / risk / structure logic into the decomposed, stateless analysis modules — producing an immutable Signal from a MarketSnapshot. This is the largest and most valuable phase: once complete, the hardest-to-test part of V1 becomes the easiest-to-test part of V2. Behavior must be bit-for-bit identical to V1's analysis output.

### Phase 4 — Lifecycle Core (stateful, but isolated)
**Purpose:** Port V1's memory-engine branches, kill switch, decay, TTL, and slot management into the reconciler + rules + store, operating on immutable decisions. This is where V1's most tangled code (the three memory branches in `run()`) becomes explicit and testable.

### Phase 5 — Orchestration & Delivery Wiring
**Purpose:** Assemble the thin orchestrator that sequences the phases, and wire delivery (alerts, dashboard, logs) as consumers of lifecycle decisions. After this phase, V2 is a complete, runnable system.

### Phase 6 — Parallel Validation & Cutover
**Purpose:** Run V2 alongside V1, diff outputs against the golden dataset and live, close any divergences, then retire V1. No new capability — only proof and switchover.

---

## 2. Module Build Order

The order is dictated by **dependency direction (build what others depend on first) and testability (build what's easiest to prove first).**

1. **Domain objects first** — because every module's inputs and outputs are these types. You cannot write or test `analysis.htf` until `HTFContext` exists as a contract. Domain has zero dependencies, so it is the only thing that can be built against nothing.

2. **Infrastructure contracts second, adapters wrapping V1 code** — because the pure core needs a *source* of MarketSnapshots and a *sink* for decisions to be testable against reality. By wrapping V1's proven fetch/persist/notify code behind contracts (rather than rewriting it), you get a working I/O boundary without introducing I/O risk. The clock adapter comes first within this phase because analysis and lifecycle both depend on injectable time.

3. **Analysis layer third, and within it in pipeline order** — because each analysis sub-module feeds the next, and each can be validated independently against V1's intermediate values:
   - `analysis.htf` before `analysis.regime` (regime mapping consumes direction)
   - `analysis.structure` before `analysis.entry` (entry consumes swings/OB/FVG)
   - `analysis.entry` before `analysis.risk` (risk consumes the entry price)
   - `analysis.risk` before `analysis.scoring` (RR gate consumes risk)
   - `analysis.engine` last (it orchestrates all the above)
   
   Building in pipeline order means each module can be tested by feeding it a golden-dataset MarketSnapshot and asserting its output matches the V1 intermediate — you never test a module whose inputs aren't yet proven.

4. **Lifecycle fourth** — because it consumes Signals (which only exist once analysis is done) and because it is stateful, making it harder to test; you want the pure layer proven before layering state on top. Within lifecycle: `lifecycle.rules` (pure predicates) before `lifecycle.reconciler` (uses rules) before `lifecycle.store` (applies reconciler's decisions).

5. **Orchestrator and delivery last** — because they depend on everything and contain only coordination. The orchestrator is built last precisely because it should contain no logic to test — if it does, logic has leaked upward and must be pushed back down.

**Why not build the orchestrator first (as V1 did)?** Because a top-down build forces you to stub everything below, and stubs hide behavioral gaps. Bottom-up means every layer is real and proven before the layer above consumes it.

---

## 3. Deliverables Per Phase

### Phase 1 — Domain Model Foundation
- **Modules:** all immutable domain objects (MarketSnapshot, HTFContext, Regime, Swing, OrderBlock, FairValueGap, LiquiditySweep, EntryPlan, RiskPlan, SignalCandidate, Signal, ActiveSignal, Alert, LifecycleDecision, LogEvent, LifecycleState, KilledThesisRecord, OBAuditRecord — the last two per Decision #010; 18 objects total).
- **Expected outputs:** typed, immutable objects with construction and equality semantics; serialization contract for the persistable ones.
- **Dependencies:** none (domain depends on nothing).
- **Completion criteria:** every object from the architecture spec exists; each persistable object can round-trip through serialization losslessly; immutability is enforced (attempts to mutate fail).

### Phase 2 — Infrastructure Adapters
- **Modules:** `infra.clock`, `infra.market_data`, `infra.persistence`, `infra.notifier`.
- **Expected outputs:** each adapter satisfies its contract and, underneath, delegates to V1's existing proven code. `market_data` produces valid MarketSnapshots; `persistence` round-trips the V1 JSON format exactly; `notifier` sends via V1's Telegram code; `clock` provides injectable time.
- **Dependencies:** Phase 1 (produces/consumes domain objects).
- **Completion criteria:** `market_data` produces a MarketSnapshot from live or recorded data that contains exactly the fields V1's pipeline received; `persistence` loads and saves the existing `active_pois.json` with zero schema drift (V1 and V2 can read each other's files); `clock` is fully injectable (no direct wall-clock reads leak).

### Phase 3 — Pure Analysis Layer
- **Modules:** `analysis.htf`, `analysis.regime`, `analysis.structure`, `analysis.entry`, `analysis.risk`, `analysis.scoring`, `analysis.engine`.
- **Expected outputs:** a pure function chain that turns a MarketSnapshot into a Signal, producing every intermediate (HTFContext, Regime, OB candidate set, EntryPlan, RiskPlan) as inspectable immutable objects.
- **Dependencies:** Phase 1 (domain), Phase 2 (clock injection, MarketSnapshot source).
- **Completion criteria:** for every symbol/cycle in the golden dataset, `analysis.engine` produces a Signal whose decision, score, direction, entry_mode, entry_price, sl, tp, and reason **exactly match** V1's `result` for that same input. Every intermediate also matches its V1 counterpart (HTF bias, regime label, OB selected, RR). Zero divergence across the entire golden dataset.

### Phase 4 — Lifecycle Core
- **Modules:** `lifecycle.rules`, `lifecycle.reconciler`, `lifecycle.store`.
- **Expected outputs:** given a Signal + memory view + live price + H1 timestamp + clock, produce a LifecycleDecision (mutations + alerts + logs); apply mutations atomically to the store.
- **Dependencies:** Phase 1, Phase 3 (consumes Signals), Phase 2 (clock, persistence contract).
- **Completion criteria:** replaying the golden dataset's *sequence* of Signals through the lifecycle produces the identical sequence of memory states, evictions, alerts, and log events that V1 produced — including exact miss_count progression (H1-gated), TTL evictions at the same ages, kill/stale firing at the same points, and identical alert dedup behavior.

### Phase 5 — Orchestration & Delivery Wiring
- **Modules:** `app.orchestrator`, delivery routing (alerts → notifier, logs → notifier, dashboard).
- **Expected outputs:** a complete runnable V2 that executes the full cycle: fetch → analyze → reconcile → apply → dispatch → persist → sleep.
- **Dependencies:** all prior phases.
- **Completion criteria:** V2 runs a full cycle end-to-end against live data without error; its terminal output, alerts, and persisted memory match what V1 produces for the same cycle (validated in Phase 6). The orchestrator contains coordination only — no business logic (verified by inspection: any conditional that decides a *signal outcome* is a leak and must be pushed down).

### Phase 6 — Parallel Validation & Cutover
- **Modules:** none new — a comparison harness and the cutover.
- **Expected outputs:** a documented divergence report reaching zero, then V1 retired.
- **Dependencies:** all phases.
- **Completion criteria:** defined in §5 and §8.

---

## 4. Testing Strategy Per Phase

Testing priority throughout: **behavioral compatibility with V1 first, everything else second.** A V2 that is cleaner but behaves differently has failed.

### Phase 1
- **What to test:** construction, immutability, serialization round-trip.
- **How to validate:** property tests that mutation attempts fail; round-trip tests that serialize→deserialize yields an equal object; the persistable objects match V1's JSON field-for-field.
- **Regressions to watch:** silent field drops or type coercions during serialization (V1's `load_memory` had specific coercion + optional-field-default behavior that must be replicated exactly, or restart-loaded state will differ).

### Phase 2
- **What to test:** each adapter's contract compliance and fidelity to V1's underlying behavior.
- **How to validate:** feed `market_data` recorded exchange responses, assert the MarketSnapshot matches what V1's pipeline consumed (same closed-candle slicing via `iloc[:-1]`, same live-price field, same H1 timestamp). Assert `persistence` reads a V1-written JSON and writes one V1 can read.
- **Regressions to watch:** the forming-candle strip and the live-price separation — if `market_data` mishandles which candle is "closed," every downstream analysis diverges. Timezone handling in the clock/timestamp conversion (V1 had tz-naive epoch subtleties in `last_miss_h1_ts` — replicate the exact conversion method).

### Phase 3
- **What to test:** exact output equivalence of each analysis module against V1 intermediates, across the full golden dataset.
- **How to validate:** for each golden input, run the V2 module and diff its output against the captured V1 intermediate. Test module-by-module in pipeline order so a divergence is localized to the first module that produced it, not discovered three steps later.
- **Regressions to watch:** the subtle behavioral details we identified during analysis — the CONTINUATION `entry_source` gate keying, the choppy penalty applied in two places, the M5-only-at-score≥8 discontinuity, the OB first-match selection, the RR floor computed from structural entry. Each is a place where a "cleaner" reimplementation could silently change behavior. Also: floating-point determinism — the score/RR must match exactly, so the arithmetic order must be preserved where it affects rounding.

### Phase 4
- **What to test:** lifecycle transition equivalence — the *sequence* of states, not just single decisions.
- **How to validate:** replay the golden dataset's ordered Signals through the lifecycle with an injected clock, and diff the resulting memory-state sequence, eviction events, and alerts against V1's captured sequence. The injected clock is essential — it makes TTL and decay deterministic.
- **Regressions to watch:** the H1-gated decay (increment only on new H1 close — the exact `>` vs `>=` boundary, the reset-to-current-H1-on-refresh semantics we locked); the miss_count dual-role feeding TTL tiering; alert dedup (new vs upgrade routing, the rank comparison); the two eviction branches (GUARD vs SKIP) both emitting DECAY. These are the areas where V1's behavior was subtle and hard-won — any deviation reintroduces bugs we already fixed.

### Phase 5
- **What to test:** end-to-end cycle equivalence and orchestrator-contains-no-logic.
- **How to validate:** run one full cycle, compare all outputs to V1. Inspect the orchestrator for leaked logic.
- **Regressions to watch:** ordering effects — V1's per-coin loop had a specific sequence (kill check before analysis-persist, pre-memory STALE before memory engine) that affects outcomes; the orchestrator must preserve that exact sequence.

---

## 5. Migration Strategy

**V1 and V2 coexist in parallel, read-only-comparison mode, before V2 ever acts alone.**

**Stage A — Shadow mode (V2 analysis only).** Once Phase 3 is done, V2's analysis runs *alongside* V1 on the same live data, but V2 takes no action — it only computes Signals and logs them. Every cycle, V2's Signal is diffed against V1's `result` for the same symbol. This runs continuously; divergences are logged and investigated. V1 remains the sole system of record. This stage validates the hardest layer against *live* data, not just the golden dataset, catching inputs the golden set didn't cover.

**Stage B — Shadow mode (full V2).** Once Phase 5 is done, V2 runs its complete pipeline in parallel but its side effects are *suppressed* — it computes lifecycle decisions, would-be alerts, would-be persisted state, but does not send Telegram or write the real memory file (it writes a shadow file). Every cycle, V2's shadow memory, would-be alerts, and logs are diffed against V1's real ones.

**How outputs are compared:** a comparison harness runs both systems on the same input and produces a per-cycle divergence report: any difference in decision, score, entry/sl/tp, memory state, eviction, or alert is flagged with the symbol and cycle. Zero divergence over a sustained window (see §8) is the bar.

**When it is safe to replace V1:** only after Stage B shows zero divergence across a window covering *diverse market conditions* — critically including at least one regime transition (a bounce/reversal), because the trending-only period is where V1 and V2 are most likely to *accidentally* agree while both being wrong about edge behavior. Cutover is a single switch: V2 begins acting (real Telegram, real persistence), V1 is stopped. Keep V1 runnable-in-shadow for a rollback window after cutover.

**Why not big-bang replace?** Because V1's behavior includes subtle emergent details (the ones we spent this whole engagement uncovering) that no spec fully captures. Shadow-mode diffing is the only way to catch a behavioral difference that the spec didn't know to mention.

---

## 6. Golden Dataset Strategy

**Purpose:** a frozen, replayable record that makes "V2 matches V1" an objective, automated check rather than a judgment call.

**What to capture (from running V1):**
- **Inputs:** for every scan cycle over the capture window — the exact raw D1/H1/M5 data V1 fetched per symbol, the top-volume universe returned, and the wall-clock time of the cycle. This is the complete input surface; with it, V2's pipeline is fully reproducible offline.
- **Intermediates:** V1's HTFContext values, regime label, OB candidates + selection, EntryPlan, RiskPlan, and raw score per symbol per cycle. These enable *localized* Phase 3 testing (diff at each pipeline stage, not just the end).
- **Outputs:** V1's final Signal (decision, score, levels, reason) per symbol per cycle; the memory state after each cycle; every eviction, alert, and log event with its trigger.
- **Sequence:** the ordered cycle-by-cycle progression, because lifecycle behavior (decay, TTL) is path-dependent — a single cycle's memory state depends on all prior cycles.

**Capture window requirements:** must span **diverse conditions** — trending (both directions), ranging, at least one regime transition, and enough duration for TTL evictions and multi-cycle decay to occur naturally. A dataset captured only during the uniform bearish period would validate V2 against V1's easy cases and miss exactly the edge behaviors most likely to diverge. Include the messy cases we know exist (near-RR-floor signals, the OB-questionable cases, KILL-latency events).

**Expected outputs:** the captured V1 outputs *are* the expected outputs. V2 is correct for a given input iff it reproduces the captured V1 output for that input.

**Comparison methodology:** an automated diff at three granularities — (1) per-analysis-stage intermediate (for Phase 3), (2) per-cycle final Signal (for Phase 3 + 5), (3) per-cycle post-lifecycle memory-and-events (for Phase 4 + 5). Any non-match is a divergence with a precise location (symbol, cycle, field). Floating-point comparisons use exact equality where V1's values feed thresholds (score, RR) — a rounding difference that flips a decision tier is a real divergence, not noise.

**A subtlety to plan for:** capturing V1's intermediates requires instrumenting V1 to emit them, which V1 wasn't built to do cleanly. Budget for adding non-invasive capture hooks to V1 (log-only, no behavior change) as part of building the golden dataset. This instrumentation is throwaway — it exists only to build the dataset.

---

## 7. Risk Assessment

Highest-risk stages, why, and what to validate before continuing:

**Risk 1 (Highest) — Phase 3 analysis fidelity, specifically the subtle behaviors.** The analysis layer contains the behaviors we spent this engagement discovering were subtle: the CONTINUATION entry_source gate, the choppy double-application guard, OB first-match, RR-from-structural-entry, the M5-≥8 discontinuity. A reimplementation that is "obviously equivalent" can silently differ. **Why highest:** these feed everything downstream, and a divergence here is a behavior change disguised as a refactor — exactly what the roadmap forbids. **Validate before continuing:** zero divergence on the *full* golden dataset including the known-messy cases, per-stage, before any lifecycle work begins. Do not proceed to Phase 4 with any unexplained Phase 3 divergence.

**Risk 2 — Phase 4 lifecycle path-dependence.** Lifecycle state is a function of the entire cycle history, so a small per-cycle difference compounds. The H1-gated decay, the miss_count dual-role, and the reset semantics are subtle and were hard-won. **Why high:** a divergence may not appear for many cycles, then manifest as a wrong eviction — hard to trace. **Validate before continuing:** replay the *full ordered sequence* and diff the memory-state trajectory, not just endpoints. An injected clock is mandatory here; without deterministic time, TTL/decay tests are flaky and prove nothing.

**Risk 3 — Phase 2 data-boundary fidelity.** If `market_data` slices candles or handles timestamps even slightly differently from V1, every downstream comparison fails for reasons that look like analysis bugs but are actually input bugs. **Why significant:** it's the easiest divergence to misdiagnose — you'll hunt in analysis when the fault is in fetching. **Validate before continuing:** prove MarketSnapshot equivalence against captured V1 inputs before trusting any Phase 3 result. The timezone/epoch conversion for `last_miss_h1_ts` is a specific known trap.

**Risk 4 — Golden dataset coverage gaps.** If the dataset lacks a regime transition or the messy edge cases, V2 can pass every test and still diverge in production. **Why insidious:** a green test suite creates false confidence. **Validate before continuing:** audit the dataset for condition diversity *before* relying on it; treat "all tests pass on an inadequate dataset" as untested, not tested.

**Lower risk:** Phases 1 and 5 — Phase 1 is mechanical (types), Phase 5 is coordination (thin). Their risk is low precisely because the roadmap pushes all logic and state into the well-tested Phases 3 and 4.

---

## 8. Definition of Done (Objective, Per Phase)

### Phase 1
- Every domain object in the architecture spec exists and is immutable (mutation attempts provably fail).
- Every persistable object round-trips through serialization to an equal object.
- Persistable objects' serialized form is byte-compatible with V1's JSON schema (V1 can load a V2-written record and vice versa).

### Phase 2
- `market_data` produces a MarketSnapshot that matches V1's consumed input for 100% of golden-dataset cycles (same closed candles, same live price, same H1 timestamp).
- `persistence` loads every V1-written memory file without loss and writes files V1 loads without loss.
- No module outside `infra` reads the wall clock or performs I/O directly (verified by inspection).

### Phase 3
- `analysis.engine` reproduces V1's final Signal (decision, score, direction, mode, entry, sl, tp, reason) for 100% of golden-dataset inputs with zero divergence.
- Every analysis intermediate (HTF, regime, OB selection, RR) matches V1's captured intermediate for 100% of inputs.
- No analysis module references memory, persistence, notification, or previous-signal state (verified by dependency inspection — the forbidden dependencies of §4 are absent).

### Phase 4
- Replaying the golden dataset's ordered Signals produces a memory-state trajectory identical to V1's for 100% of cycles.
- Every eviction (kill, decay, TTL, replace) fires at the identical cycle with identical reason as V1.
- Every alert (new, upgrade, kill) and its dedup outcome matches V1 exactly.
- miss_count progression matches V1 cycle-by-cycle, including H1-gating and reset semantics.

### Phase 5
- V2 executes a full live cycle end-to-end without error.
- The orchestrator contains no signal-outcome-deciding logic (verified by inspection).
- V2's per-cycle output (Signals, memory, alerts, logs) matches V1's for a live cycle.

### Phase 6 (Cutover)
- Shadow-mode Stage B shows **zero divergence** across a sustained window that provably includes trending, ranging, and at least one regime-transition period.
- All known-messy cases (near-RR-floor, OB-questionable, KILL-latency) are present in the divergence-free window.
- A rollback path to V1 is documented and tested before the switch.
- Only when all above hold: V2 acts, V1 stops.

---

**Closing note:** The roadmap's entire safety rests on two things existing before real work begins — the **golden dataset** (so equivalence is measurable) and the **injected clock** (so stateful behavior is deterministically testable). Build those first, prove the pure layer against them exhaustively, layer state on only once the pure layer is bit-exact, and never let a phase proceed with an unexplained divergence. Done this way, V2 replaces V1's architecture without ever putting its behavior at risk — which is the only definition of success this migration allows.