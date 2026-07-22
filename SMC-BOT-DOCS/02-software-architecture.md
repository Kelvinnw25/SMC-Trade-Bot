# SMC Signal Radar V2 — Software Architecture Specification

**Scope constraint:** This document redesigns *structure only*. Every behavior described in the V1 analysis — the weighted direction selection, the regime→mode mapping, the OB first-match policy, the scoring thresholds, the H1-gated decay, the RR floor, the Contract-B entry semantics — is treated as fixed and correct. Where V1 has a behavior that is architecturally awkward (e.g. `miss_count` serving two consumers), V2 preserves the behavior but gives it a clean home. Nothing here changes what a signal *is* or when it fires.

---

## 1. High-Level Architecture

V2 is a **layered pipeline architecture** with strict unidirectional dependencies, organized around one principle the legacy code violated repeatedly: **pure analysis is fully separated from side effects and state mutation.**

Four layers, top to bottom:

**Layer 1 — Infrastructure (I/O boundary).** Everything that touches the outside world: exchange data fetching, Telegram delivery, file persistence, clock/time access. This layer produces and consumes raw data and knows nothing about SMC concepts.

**Layer 2 — Analysis (pure domain).** The stateless strategy engine. Takes market data in, produces immutable analysis results out. No memory, no I/O, no persistence, no notion of "previous signals." This is where all of `build_signal_context`, `score_signal`, entry construction, OB/FVG/swing detection live — but decomposed. Given identical input, always identical output.

**Layer 3 — Lifecycle & Orchestration (stateful application core).** The only layer that holds and mutates signal memory. Consumes analysis results, applies lifecycle rules (kill, decay, TTL, slot management), decides what to persist and what to alert. This is the disciplined replacement for the monolithic `run()`.

**Layer 4 — Delivery (side-effect dispatch).** Translates lifecycle decisions into outbound actions (Telegram alerts, dashboard rendering, logs). Receives explicit instructions; makes no decisions.

**Data flow (one direction only):** Infrastructure fetches raw candles → Analysis transforms them into an immutable `AnalysisResult` → Lifecycle reconciles that result against existing memory and emits `LifecycleDecisions` → Delivery executes those decisions as side effects, and Infrastructure persists the new memory state. Analysis never sees memory. Lifecycle never fetches data or sends messages. Delivery never decides anything.

The critical inversion from V1: in V1, `run()` did fetching, analysis coordination, memory mutation, alerting, and logging all interleaved. In V2 these are four separable phases with typed handoffs between them, so each is independently testable.

---

## 2. Domain Model

Every object is **immutable once constructed** except where explicitly marked stateful. Immutability is the backbone of testability here.

### MarketSnapshot
- **Purpose:** the raw, closed-candle market data for one symbol at one moment.
- **Responsibilities:** carry D1/H1/(optional M5) OHLCV + ATR, the symbol, and the reference timestamp of the last closed H1 candle. Also carries the live forming-candle price as a *separate, explicitly-labeled field* (since V1 uses it only for invalidation).
- **Relationships:** input to all analysis. Never mutated.

### HTFContext
- **Purpose:** the higher-timeframe reading.
- **Responsibilities:** weekly/D1/H4 bias, location (discount/premium/equilibrium), the weighted direction result, direction source, and veto status.
- **Relationships:** derived from MarketSnapshot; consumed by Regime mapping, entry, and scoring.

### Regime
- **Purpose:** the classified market state.
- **Responsibilities:** hold the regime label (expansion_up/down, accumulation, distribution, rotational, compression, neutral) and the derived execution mode (CONTINUATION/RETRACEMENT/WATCH_ONLY/NONE).
- **Relationships:** derived from MarketSnapshot; drives which entry engine runs.

### Swing
- **Purpose:** a single detected swing point.
- **Responsibilities:** index, price, type (high/low).
- **Relationships:** produced by swing detection; consumed by OB detection, liquidity targeting, sweep detection.

### OrderBlock
- **Purpose:** a detected OB candidate zone.
- **Responsibilities:** top, bottom, origin index, freshness status, quality-filter result, and — critically for the diagnostic work in progress — its rank in the candidate ordering and whether it was the selected one.
- **Relationships:** produced by OB detection; consumed by entry construction. *In V2, all candidates are produced as first-class objects even though selection policy (first-match) is unchanged* — this preserves behavior while making the selection auditable.

### FairValueGap
- **Purpose:** a detected FVG.
- **Responsibilities:** bounds, index, validity.
- **Relationships:** produced by FVG detection; consumed by scoring (as a confirmation flag).

### LiquiditySweep
- **Purpose:** result of sweep detection.
- **Responsibilities:** whether a sweep occurred, direction.
- **Relationships:** produced from swings + price; consumed by scoring.

### EntryPlan
- **Purpose:** the constructed entry for a setup.
- **Responsibilities:** entry_price, entry_source, the displacement/pullback indices (for CONTINUATION), and the mode that produced it. Carries the **explicit contract label**: entry_price is a *structural reference* (Contract B), not an executable order price — recorded as a field so no downstream consumer misinterprets it.
- **Contract-B rule:** Any consumer computing risk/reward from `entry_price` MUST treat it as structural RR and MUST NOT substitute live price. Changing this is a behavior change and out of scope for V2.
- **Relationships:** produced by an entry engine; feeds RiskPlan and scoring.

### RiskPlan
- **Purpose:** the SL/TP/RR for a setup.
- **Responsibilities:** sl, tp, tp_source, tp_rr, and the RR value used against the floor gate.
- **Relationships:** produced from EntryPlan + liquidity targeting; consumed by scoring's RR gate.

### SignalCandidate
- **Purpose:** a fully-analyzed setup *before* the decision tier is assigned — the complete output of the analysis layer for one symbol.
- **Responsibilities:** bundle HTFContext, Regime, EntryPlan, RiskPlan, the confirmation flags, and the raw score. Immutable.
- **Relationships:** the single handoff object from Analysis to Lifecycle. Replaces V1's sprawling `context`/`result`/`h1_setup` dicts.
- **Boundary note:** SignalCandidate is primarily an internal analysis-layer concept and is not part of the standard application-level module contracts.

### Signal
- **Purpose:** a SignalCandidate that has passed scoring and received a decision tier (SKIP/WATCH/VALID/A+).
- **Responsibilities:** the candidate plus decision, notes, final score.
- **Relationships:** produced by scoring; a SKIP Signal is still a valid object (it just won't enter memory).

### ActiveSignal
- **Purpose:** a Signal that lives in memory. **The one stateful domain object.**
- **Responsibilities:** the Signal payload plus lifecycle metadata: timestamp (wall-clock, TTL), miss_count, last_miss_h1_ts, status, alert_sent_tier, alert_sent_ts. This is the thing that persists to JSON.
- **Relationships:** owned exclusively by the Lifecycle layer. Never constructed in Analysis.

### Alert
- **Purpose:** an instruction to deliver an outbound notification.
- **Responsibilities:** alert kind (new/upgrade/kill), the payload to render, target channel.
- **Relationships:** produced by Lifecycle as a *decision*; consumed by Delivery as an *instruction*. Lifecycle never sends; it only emits Alert objects.

### LifecycleDecision
- **Purpose:** the complete set of state changes and side-effect instructions from reconciling one SignalCandidate against memory.
- **Responsibilities:** memory mutations to apply (insert/replace/evict/increment), Alerts to dispatch, log events to emit.
- **Relationships:** produced by the Lifecycle reconciler; consumed by the state store and Delivery.

### LifecycleState
- **Purpose:** the enumerated state of an ActiveSignal in the lifecycle machine.
- **Responsibilities:** name the current state (see §7).
- **Relationships:** a property of ActiveSignal; transitions governed by the Lifecycle layer only.

### KilledThesisRecord
- **Purpose:** the record of a killed thesis, enforcing the re-entry cooldown. *(Added per Decision #010.)*
- **Responsibilities:** thesis key (`symbol|direction|entry_mode`) and kill timestamp (epoch). Persisted as the flat dict in `killed_thesis.json`; entries older than the cooldown window are pruned each cycle.
- **Relationships:** produced by Lifecycle on a KILL eviction; consulted by the reconciler before any insert (a matching thesis within cooldown blocks insertion with a `KILL_COOLDOWN` log); owned by the Lifecycle layer, serialized by `infra.persistence`.

### OBAuditRecord
- **Purpose:** the diagnostic audit record for retracement OB selection — an **observability contract** (a sibling of LogEvent, not a peer of Signal). *(Added per Decision #010.)*
- **Responsibilities:** carry the exact V1 audit fields (symbol, h1_last_ts, direction, entry_mode, decision, score, regime, location, market_price, entry/sl/tp, rr, entry_source, tp_source, volume_spike, window_bounds, ob_meta, the OB candidate set, selected-OB fields, fresh_ob, fvg_valid, current_price, entry_equals_ob_bottom).
- **Relationships:** assembled by `app.orchestrator` from analysis outputs for qualifying retracement signals (deduplicated via orchestrator-owned transient state — V1's in-memory `ob_audit_seen`); schema owned by `domain`; appended to `retracement_ob_audit.jsonl` by `infra`. **Observability only — never consumed by analysis or lifecycle.**

---

## 3. Module Boundaries

### `infra.market_data`
- **Responsibilities:** fetch OHLCV+ATR and top-volume universe from the exchange; hand back raw dataframes; strip forming candle to produce closed-candle data.
- **Inputs:** symbol, timeframe, limits.
- **Outputs:** raw closed-candle data + the separately-labeled live price.
- **Must NOT know:** anything about SMC, signals, scoring, or memory. It is a dumb pipe.

### `infra.persistence`
- **Responsibilities:** load and atomically save the signal memory to/from durable storage; validate schema on load with optional-field defaults.
- **Inputs:** memory state (save) / file (load).
- **Outputs:** validated memory (load) / durable write (save).
- **Must NOT know:** why signals exist, what a good signal is, or lifecycle rules. It serializes whatever it's given.

### `infra.notifier`
- **Responsibilities:** format and send outbound messages to Telegram; render the dashboard; emit terminal logs.
- **Inputs:** Alert objects and log events.
- **Outputs:** delivered messages (side effects).
- **Must NOT know:** scoring, memory, decision logic, or *whether* an alert should fire. It sends what it's told.

### `infra.clock`
- **Responsibilities:** provide wall-clock time and epoch conversions.
- **Inputs:** none / timestamps.
- **Outputs:** current time, epoch floats.
- **Must NOT know:** anything domain-related. Exists so time is injectable and testable (V1's dependence on `time.time()` inside logic made lifecycle outcomes untestable).

### `analysis.htf`
- **Responsibilities:** compute HTFContext — bias hierarchy, location, weighted direction, veto.
- **Inputs:** MarketSnapshot.
- **Outputs:** HTFContext.
- **Must NOT know:** regime, entry, scoring, memory, or I/O.

### `analysis.regime`
- **Responsibilities:** classify regime and derive execution mode.
- **Inputs:** MarketSnapshot.
- **Outputs:** Regime.
- **Must NOT know:** entry construction or scoring internals.

### `analysis.structure`
- **Responsibilities:** swing detection, sweep detection, FVG detection, OB detection (producing *all* candidates + the first-match selection), displacement, volume spike.
- **OB Selection Policy:** Selection is an explicit policy owned by this module. Detection produces all candidates; selection applies the current first-match policy. Future ranking changes are confined to this policy.
- **Inputs:** MarketSnapshot, HTFContext (for direction).
- **Outputs:** Swings, OrderBlocks (all candidates), FVGs, LiquiditySweep, displacement/volume flags.
- **Must NOT know:** scoring, entry decisions, or memory.

### `analysis.entry`
- **Responsibilities:** construct EntryPlan per mode (continuation micro-pullback engine, retracement OB engine with quality filter and rotational guard). Preserves the exact V1 logic including entry-at-structural-reference (Contract B).
- **Inputs:** MarketSnapshot, HTFContext, Regime, structure objects.
- **Outputs:** EntryPlan.
- **Must NOT know:** how the entry will be scored or persisted.

### `analysis.risk`
- **Responsibilities:** liquidity target selection (EQH/EQL, swing, PDH/PDL, RR-aware, mode-aware, 2R fallback) and RiskPlan construction.
- **Inputs:** EntryPlan, structure objects, MarketSnapshot.
- **Outputs:** RiskPlan.
- **Must NOT know:** scoring thresholds or decision tiers.

### `analysis.scoring`
- **Responsibilities:** compute score, apply mode-aware gates, RR floor hard gate, assemble the final Signal with decision tier. Also the M5 enrichment re-score. Preserves every V1 weight and threshold exactly.
- **Inputs:** SignalCandidate (HTFContext + Regime + EntryPlan + RiskPlan + flags), optional M5 setup.
- **Outputs:** Signal (with decision).
- **Must NOT know:** memory, alerting, or lifecycle.

### `analysis.engine`
- **Responsibilities:** the pure orchestrator of the analysis layer — runs htf → regime → structure → entry → risk → scoring in order for one MarketSnapshot, producing one Signal. This is the decomposed, pure replacement for `build_signal_context`.
- **Inputs:** MarketSnapshot.
- **Outputs:** Signal (immutable).
- **Must NOT know:** memory, I/O, previous signals, or side effects. **Fully stateless and pure.**

### `lifecycle.reconciler`
- **Responsibilities:** the stateful core. Given a fresh Signal and the current memory, decide: kill? stale? insert/replace/cap? decay-increment? evict? which alerts? Produces LifecycleDecision. This is the disciplined replacement for V1's memory-engine branches.
- **Inputs:** Signal, current memory snapshot, live price, current H1 timestamp, clock.
- **Outputs:** LifecycleDecision (mutations + alerts + logs).
- **Must NOT know:** how to fetch data, how to send Telegram, how analysis was computed internally.

### `lifecycle.store`
- **Responsibilities:** hold the in-memory ActiveSignal collection; apply mutations from LifecycleDecision atomically; enforce slot limits. The single owner of `active_signals`.
- **Inputs:** LifecycleDecision mutations.
- **Outputs:** current memory state.
- **Must NOT know:** analysis or delivery.

### `lifecycle.rules`
- **Responsibilities:** pure predicates for lifecycle decisions — invalidation check, decay-gating (H1 timestamp comparison), TTL tiering (including the miss_count tap-proxy), slot-replace eligibility, alert dedup rank. Pure functions, no state.
- **Inputs:** signal/state + thresholds + clock.
- **Outputs:** boolean/enum decisions.
- **Must NOT know:** how decisions are applied or dispatched.

### `app.orchestrator`
- **Responsibilities:** the top-level loop — sequence the phases (fetch → analyze → reconcile → apply → dispatch → persist), handle per-coin errors, sleep. The thin replacement for `run()`'s outer structure, containing *coordination only*, no business logic.
- **Inputs:** config.
- **Outputs:** side effects via lower layers.
- **Must NOT know:** the internals of any layer — only their public contracts.

---

## 4. Dependency Rules

Strict, acyclic, downward-only:

```
app.orchestrator
   │  (may depend on all below)
   ▼
lifecycle.*  ──────►  analysis.*  (may consume analysis OUTPUTS as data, not internals)
   │                     │
   ▼                     ▼
infra.*  ◄───────────────┘  (analysis depends on infra ONLY for the clock, ideally injected)
```

**Allowed:**
- `app.orchestrator` may depend on `lifecycle`, `analysis`, `infra`, `domain`.
- `lifecycle` may depend on `domain`, `infra.clock`, `infra.persistence` contracts, and consume `analysis` *outputs* (Signal) as data.
- `analysis` may depend on `domain` and (injected) `infra.clock` only.
- `infra` may depend on `domain` (for the types it produces/consumes) and nothing else in the app.
- Everything may depend on `domain` (the immutable objects) and `config`.

**Forbidden (hard rules):**
- `analysis.*` MUST NOT depend on `lifecycle.*`, `infra.persistence`, `infra.notifier`, or `infra.market_data`. Analysis is pure — it receives a MarketSnapshot, it does not fetch one.
- `analysis.*` MUST NOT read or reference memory / ActiveSignal / previous state. (This is the single most important rule; V1 violated the spirit of it by computing lifecycle-relevant things inside strategy.)
- `lifecycle.*` MUST NOT call `infra.market_data` or `infra.notifier` directly to *perform* I/O — it emits Alert/mutation objects; the orchestrator routes them.
- `infra.*` MUST NOT depend on `analysis` or `lifecycle`.
- No module may depend on `app.orchestrator`.
- No circular dependencies, ever. The `domain` layer is dependency-free.

---

## 5. Runtime Pipeline

From startup to one scan cycle finishing:

**Startup:**
1. `app.orchestrator` loads config, constructs infra adapters (market_data, persistence, notifier, clock), constructs the analysis engine (stateless), and the lifecycle store + reconciler.
2. Orchestrator asks `infra.persistence` to load memory into `lifecycle.store`.

**One scan cycle:**
3. Orchestrator asks `infra.market_data` for the top-volume universe.
4. **Per symbol** (orchestrator loops):
   a. Orchestrator asks `infra.market_data` for D1/H1 (and later M5 if needed), receiving a **MarketSnapshot** (closed candles + labeled live price + H1 timestamp).
   b. Guard checks (data sufficiency) — orchestrator-level, cheap.
   c. Orchestrator hands the MarketSnapshot to `analysis.engine`, which purely produces a **Signal** (htf → regime → structure → entry → risk → scoring). If the analysis engine determines M5 enrichment is required, it requests it; the orchestrator fetches M5 and analysis re-scores. The eligibility rule belongs to analysis. Analysis returns; it has touched no state.
   d. Orchestrator hands (Signal + current memory view + live price + H1 timestamp + clock) to `lifecycle.reconciler`.
   e. Reconciler consults `lifecycle.rules` (invalidation, decay-gate, slot eligibility, alert rank) and produces a **LifecycleDecision** — but mutates nothing itself.
   f. Orchestrator applies the LifecycleDecision's mutations to `lifecycle.store`, and routes its Alerts + log events to `infra.notifier`.
5. **After the per-symbol loop:** orchestrator asks `lifecycle.rules`/`store` to run TTL cleanup (producing eviction decisions), applies them, then asks `infra.persistence` to save the memory, and `infra.notifier` to render the dashboard.
6. Orchestrator sleeps, returns to step 3.

The essential difference from V1: every arrow above is a **typed handoff of an immutable object**, and no phase reaches sideways into another. Analysis produces a Signal and forgets it. Lifecycle produces decisions and forgets them. The orchestrator is the only thing that knows the sequence.

---

## 6. State Management

**Runtime (in-memory) state — exactly one owner each:**

- **Active signal collection** — owned solely by `lifecycle.store`. The only mutable domain state. Every mutation flows through applying a LifecycleDecision. No other module holds a reference it can mutate.
- **Loop/coordination state** (current symbol, universe list) — owned by `app.orchestrator`, transient, never persisted.
- **Clock** — owned by `infra.clock`, injected everywhere time is needed so it's mockable.

**Persistent state:**

- **Signal memory (JSON)** — written by `infra.persistence` on orchestrator command at cycle end; read once at startup. Contains the serialized ActiveSignal collection with all lifecycle metadata (timestamp, miss_count, last_miss_h1_ts, status, alert_sent_tier/ts). `lifecycle.store` is the logical owner; `infra.persistence` is the mechanical serializer.
- **Killed-thesis memory (JSON)** — the `killed_thesis.json` flat `{thesis_key: killed_ts}` dict enforcing the KILL re-entry cooldown (Decision #010). Written by `infra.persistence` on orchestrator command at cycle end and pruned each cycle; the Lifecycle layer is the logical owner.

**Config (static):** owned by `config`, read-only, injected. Holds operational values (limits, TTLs, thresholds, state file path). Per the V1 boundary rule, config holds *operational* values only, not calibration ratios — those live in the analysis layer as domain constants.

**Ownership principle:** each piece of state has exactly one writer. In V1, `active_signals` was mutated across many inline branches in `run()`; in V2 it is mutated only by `lifecycle.store` applying explicit decisions. This makes every state change traceable to a LifecycleDecision object, which is itself testable in isolation.

**Hidden-state elimination:** the two V1 hidden-coupling problems get explicit homes:
- `miss_count`'s dual role (decay eviction + TTL tiering tap-proxy) is preserved behaviorally but made explicit — `lifecycle.rules` documents both consumers reading the same field, rather than the coupling being implicit across two files.
  - `miss_count` has exactly one writer (`lifecycle.store`). TTL tiering reads the raw `miss_count > 0` value directly; neither consumer recomputes tapped state.
- The `entry_source` magic-string contract becomes a typed field on EntryPlan, so the coupling between entry construction and the scoring gate is a visible data dependency, not a string convention.

---

## 7. Signal Lifecycle (State Machine)

States and every transition. Behavior identical to V1; only the representation is formalized.

**States:**
- `ABSENT` — not in memory.
- `DISCOVERED` — passed analysis + scoring with actionable decision + valid levels; just inserted.
- `ACTIVE` — persisting in memory across cycles, being refreshed.
- `DECAYING` — has accrued one or more misses (miss_count > 0) but below limit.
- `EVICTED` — removed; terminal, returns to ABSENT.

**Transitions:**

- `ABSENT → DISCOVERED`: analysis yields actionable decision (WATCH/VALID/A+) + levels_valid, and a slot is available (or it displaces a weaker signal). Emits new-discovery Alert if VALID/A+.
- `ABSENT → ABSENT` (no-op): actionable but slot full and not strong enough to replace (CAPPED); or decision is SKIP.
- `ABSENT → ABSENT` (cooldown-blocked): actionable + levels_valid, but the thesis (`symbol|direction|entry_mode`) was killed within the cooldown window. No insertion; emits `KILL_COOLDOWN` log. *(Decision #010.)*
- `DISCOVERED → ACTIVE`: survives to next cycle.
- `ACTIVE → ACTIVE` (full refresh): re-analyzed as actionable + valid with a *different* thesis (direction or entry_mode changed) or a *higher* alert tier; payload replaced; miss_count reset to 0, last_miss_h1_ts set to current H1; alert metadata carried over. May emit upgrade Alert if tier increased.
- `ACTIVE → ACTIVE` (same-thesis partial refresh): same direction + entry_mode and alert tier not higher; score = max(old, new), decision never downgraded, entry/sl/tp/regime updated, timestamp reset to now (TTL restart); **miss_count and last_miss_h1_ts unchanged**; no Alert; emits `SAME_THESIS` log. *(Decision #010.)*
- `ACTIVE/DECAYING → DECAYING`: cycle yields SKIP or invalid levels, AND a new H1 candle has closed since last miss (H1-gated). miss_count increments, last_miss_h1_ts updates. No Telegram (decay is silent). Emits terminal-log only when it crosses the limit.
- `DECAYING → ACTIVE`: refreshed successfully before hitting limit; resets to 0.
- `DECAYING → EVICTED`: miss_count reaches MISS_COUNT_LIMIT. Emits `[⏳ DECAY]` log. No Telegram.
- `ACTIVE/DISCOVERED/DECAYING → EVICTED (KILL)`: `is_poi_invalidated` true — live price breaches SL + buffer, **or** D1 and H4 bias have both flipped against the signal's direction. Emits `[☠️ KILL]` log + kill Alert (if previously alerted). Bypasses miss_count. Records a KilledThesisRecord (Decision #010) enforcing the re-entry cooldown.
- `ACTIVE/DISCOVERED/DECAYING → EVICTED (TTL)`: wall-clock age exceeds tier TTL. Emits `[⏰ TTL EXPIRED]` log. No Telegram.
- `ACTIVE/DECAYING → EVICTED (REPLACE)`: a stronger new signal displaces this weakest one when slots are full. Emits `[🔄 REPLACE]` log. No Telegram.
- `EVICTED → ABSENT`: terminal cleanup; if the symbol reappears later it re-enters as a fresh DISCOVERED (no memory of prior life).

**Alert-state invariant:** On any EVICTED transition, alert metadata is discarded. A reappearing symbol is treated as a fresh DISCOVERED and may alert again.

**Invariant:** all transitions are decided by `lifecycle.reconciler` + `lifecycle.rules` and applied by `lifecycle.store`. Analysis cannot cause a transition; it only produces the Signal that the reconciler evaluates.

---

## 8. Data Contracts

Logical schemas for inter-module handoffs. Field-level detail omitted; these define the *shape of the boundary*.

**MarketSnapshot** (infra → analysis): symbol; closed D1 series; closed H1 series; optional closed M5 series; last-closed-H1 timestamp; live forming price (labeled distinct); ATR references.

**Signal** (analysis → lifecycle): the complete immutable analysis outcome — symbol, decision tier, score, notes, and the embedded SignalCandidate (HTFContext, Regime, EntryPlan incl. entry_source label + Contract-B marker, RiskPlan with sl/tp/tp_source/rr, confirmation flags, and the OrderBlock candidate set + selection for auditability). The embedded candidate is the **single source of truth** for entry/risk/structure data — those values are not duplicated as flat Signal fields. `direction`, `entry_mode`, and `regime` are additionally surfaced flat so candidate-less early-exit SKIPs can carry them; when a candidate is present, construction enforces flat == candidate consistency. Contains no lifecycle metadata.

**MemoryView** (lifecycle.store → reconciler): read-only snapshot of current ActiveSignals keyed by symbol, each with lifecycle metadata. Immutable view; reconciler cannot mutate it directly.

**LifecycleDecision** (reconciler → orchestrator → store/delivery): ordered set of memory mutations (insert / replace(target) / partial-refresh(same-thesis) / increment-miss(symbol) / evict(symbol, reason)); zero or more Alerts; zero or more log events; zero or more KilledThesisRecords to store on KILL (Decision #010). Fully declarative — describes *what* to change, not *how*.

**Alert** (lifecycle → delivery): kind (new / upgrade / kill); render payload (symbol, direction, tier, entry/sl/tp, regime, mode, sources); dedup key.

**LogEvent** (lifecycle → delivery): event type (SCAN / KILL / STALE / GUARD / DECAY / TTL / REPLACE / CAPPED / ALERTED / SAME_THESIS / KILL_COOLDOWN); fields for that type.

**PersistenceRecord** (store ↔ persistence): serialized ActiveSignal — the full V1 schema V2 writes to `active_pois.json`: direction, score, decision, entry_price, sl, tp, regime, timestamp, miss_count, status, alert_sent_tier, alert_sent_ts, last_miss_h1_ts, last_ob_audit_h1_ts, entry_mode, entry_source, signal_id. The load contract specifies required fields (score, decision, entry_price, sl, tp, direction — that gate validity) vs optional fields (that default), preserving V1's forward-compatible loading — including V1's behavior of dropping `entry_source` and `signal_id` on load. A second persistence contract covers KilledThesisRecord (`killed_thesis.json`, flat `{thesis_key: killed_ts}` dict; Decision #010).

**Contract discipline:** every boundary object is immutable at the point of handoff. A module receiving one may read it freely but never mutates it; to effect change it produces a *new* object (a decision, a new Signal). This is what makes each phase independently testable — you feed it inputs and assert on its output object, with no shared mutable state to set up or tear down.

---

## 9. Extension Points

Places where future features slot in **without modifying existing modules** — designed as open seams:

- **New structural detectors** (e.g. breaker blocks, mitigation blocks): add a detector in `analysis.structure` producing a new domain object; scoring consumes it as another optional confirmation flag. Existing detectors untouched.
- **New entry modes:** add an entry engine in `analysis.entry` keyed off a regime→mode mapping entry. The mode-dispatch is a lookup, so a new mode is a new entry, not a modified one.
- **New scoring inputs:** scoring reads a SignalCandidate; new confirmation flags extend the candidate and add to the score assembly without touching gate logic. (The diagnostic segmentation work — by mode, volume, TP-source — plugs in here as additional fields.)
- **New lifecycle rules / eviction types:** add a predicate in `lifecycle.rules` and a decision type in LifecycleDecision; the reconciler gains a branch, the store gains a mutation handler. Existing rules unaffected.
- **New delivery channels** (Discord, webhook, DB sink): implement the notifier contract; lifecycle still just emits Alert objects — it doesn't know or care how many channels exist. This is where the dormant executor eventually attaches: as a *consumer of LifecycleDecisions*, not as a modification to lifecycle.
- **New data sources / exchanges:** implement the `infra.market_data` contract; analysis and lifecycle never learn the source changed.
- **OB selection policy change** (the current investigation target): because OB detection now emits *all candidates* as first-class objects and selection is an explicit policy step, changing first-match → rank-by-strength is a change to *one policy function*, with the candidate set already available. The architecture makes the pending diagnostic fix a localized change rather than a surgery on `build_signal_context`.
- **The clock injection point** makes time-dependent behavior (decay, TTL) testable and lets you run historical replay (backtest) by injecting a simulated clock and data source — without touching analysis or lifecycle logic.

---

## 10. Architectural Principles

The non-negotiable engineering rules V2 must always uphold:

1. **Stateless, pure analysis.** The analysis layer is a pure function of its input MarketSnapshot. Same input → same output, always. No memory, no I/O, no clock reads except through injection, no knowledge of previous signals. This is the rule that fixes V1's deepest problem.

2. **Single writer per state.** Every piece of mutable state has exactly one owning module that may mutate it. `active_signals` is mutated only by `lifecycle.store`, only by applying a LifecycleDecision.

3. **Immutable analysis results.** Every object crossing a module boundary is immutable at handoff. Change is expressed by producing new objects, never by mutating received ones.

4. **Side effects isolated at the edges.** I/O — fetching, sending, persisting — lives only in `infra`. The core (analysis + lifecycle) is side-effect-free; it produces *descriptions* of side effects (Alerts, decisions) that the orchestrator routes to infra.

5. **Pure analysis before persistence.** A signal is fully analyzed as an immutable object before any lifecycle or storage logic touches it. Analysis never persists; persistence never analyzes.

6. **No hidden shared state.** Every dependency between modules is an explicit, typed data handoff. No magic strings as cross-module contracts (entry_source becomes a typed field), no field silently serving two masters without documentation (miss_count's dual role is explicit in `lifecycle.rules`).

7. **Strict acyclic dependencies.** Dependencies flow one direction only (§4). `domain` depends on nothing. No module depends on the orchestrator. Circular dependencies are forbidden absolutely.

8. **Single Responsibility per module.** Each module has one reason to change. The monolithic `run()` and `build_signal_context` are decomposed so that a change to scoring never risks breaking lifecycle, and a change to OB detection never risks breaking Telegram.

9. **Decisions separated from execution.** Lifecycle *decides* (emits LifecycleDecision + Alerts); infra *executes*. The layer that knows *whether* something should happen is never the layer that *makes* it happen.

10. **Injectable time and I/O.** Nothing in the core reads the wall clock or the network directly. Both are injected through infra contracts, so the entire core is testable deterministically and replayable for backtesting — the capability V1 structurally lacked.

11. **Behavior preservation is sacred.** V2 is a structural redesign. Any place where clean architecture would suggest changing a behavior (the OB first-match, the dual-purpose miss_count, the Contract-B entry semantics) — the behavior is preserved and the *structure* around it is cleaned. Architectural elegance never justifies a behavior change; those are separate decisions made deliberately, later, with their own evidence.

---

**Closing note on intent:** This architecture's single organizing goal is to make the *pending strategy work* — OB selection diagnosis, scoring calibration, the entry-timing questions — tractable. In V1, every one of those investigations was blocked by the same thing: analysis, state, and side effects were entangled in two giant functions, so nothing could be tested or replayed in isolation. V2's layering exists so that when you finally do change a behavior, you can change exactly one pure function, feed it recorded inputs, and prove the change does what you intend before it ever touches memory or the wire.

12. **Orchestrator holds no branch that changes a signal's outcome.** If removing a conditional would change which signal fires, its tier, or its lifecycle transition, that conditional belongs in analysis or lifecycle. The orchestrator may branch only on operational conditions.