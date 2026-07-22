# Decision Log

---

## #001

### Decision

Entry Price uses Contract B.

### Reason

The system is a market radar, not an execution engine.

`entry_price` represents a structural reference rather than an executable order price.

This preserves V1 behavior exactly and keeps risk/reward calculations behaviorally compatible.

### Alternative Considered

Use executable market-entry semantics (Contract A).

Rejected because it changes RR semantics and trading behavior.

### Impact

- Structural RR remains identical to V1.
- Consumers must treat RR as structural, not executable.
- Future execution support may introduce a separate execution entry without changing this contract.

### Status

Frozen

---

## #002

### Decision

Order Block selection policy remains **First Match**.

### Reason

Although shallow-OB bias is currently under investigation, there is not yet sufficient evidence that the selection policy itself is responsible.

Behavior preservation takes priority over hypothesis-driven redesign.

### Alternative Considered

Replace First Match with Rank-by-Strength.

Rejected because it changes behavior before evidence exists.

### Impact

- V1 behavior remains identical.
- All OB candidates are still surfaced, allowing future investigation without changing current behavior.

### Status

Frozen

---

## #003

### Decision

Behavior compatibility with V1 is mandatory.

### Reason

The purpose of V2 is architectural modernization, not strategy redesign.

Behavioral changes must be deliberate future decisions supported by evidence.

### Alternative Considered

Improve strategy during migration.

Rejected because migration and strategy evolution are separate concerns.

### Impact

Every implementation decision is evaluated against behavioral equivalence first.

### Status

Frozen

---

## #004

### Decision

Analysis Engine remains completely pure.

### Reason

Separating analysis from lifecycle, persistence and delivery is the architectural foundation of V2.

Pure analysis guarantees deterministic outputs and enables replay, testing and future experimentation.

### Alternative Considered

Allow analysis to access memory or infrastructure directly.

Rejected because it recreates the coupling that existed in V1.

### Impact

Analysis remains deterministic and independently testable.

### Status

Frozen

---

## #005

### Decision

Lifecycle exclusively owns mutable signal state.

### Reason

Every mutable state must have exactly one owner.

Centralizing ownership eliminates hidden mutations and makes every state transition traceable.

### Alternative Considered

Allow multiple modules to update signal state.

Rejected because it recreates hidden coupling and unpredictable behavior.

### Impact

All lifecycle mutations pass through the Lifecycle layer.

### Status

Frozen

---

## #006

### Decision

miss_count increments only after a new H1 candle closes.

### Reason

Confidence decay should measure new structural information, not polling frequency.

Per-scan decay tied lifecycle behavior to operational settings instead of market structure.

### Alternative Considered

Increment miss_count every scan cycle.

Rejected because scan interval should never influence signal lifetime.

### Impact

Signal confidence reflects market evolution rather than scan frequency.

### Status

Active

---

## #007

### Decision

miss_count intentionally serves two consumers.

### Reason

The same field drives both decay eviction and TTL tiering.

Although architecturally imperfect, preserving this behavior is safer than splitting it during migration.

### Alternative Considered

Separate decay state and tap-state into different fields.

Rejected because it risks changing lifecycle behavior.

### Impact

The coupling is documented explicitly instead of hidden.

### Status

Active

---

## #008

### Decision

Decay expiration is silent on Telegram.

### Reason

Decay represents gradual confidence loss rather than a discrete market event.

Operator notifications should remain focused on actionable events.

### Alternative Considered

Send Telegram notifications for every decay expiration.

Rejected because it increases notification noise without improving decision quality.

### Impact

Decay remains visible through logs and dashboards only.

### Status

Active

---

## #009

### Decision

DECAY log fields intentionally originate from different sources.

### Reason

Historical tier and current decay reason answer different questions.

Using one source for every field would make the log less accurate.

### Alternative Considered

Read every field from one source for consistency.

Rejected because correctness is more important than uniformity.

### Impact

Log semantics remain historically accurate.

### Status

Active

---

## #010

### Decision

The domain model is extended with two objects and two log-event types that exist in V1 behavior but were absent from the original Architecture Specification: `KilledThesisRecord`, `OBAuditRecord`, and LogEvent types `SAME_THESIS` and `KILL_COOLDOWN`.

`OBAuditRecord` is classified as an **observability contract** — a sibling of `LogEvent`, not a peer of `Signal`. It is created by `app.orchestrator`, its schema is owned by `domain`, and it is executed by `infra` as an append-only JSONL sink. It is never consumed by any analysis or lifecycle rule.

### Reason

Direct V1 code verification confirmed behavior the legacy documentation had flagged as unverified:

- A killed-thesis cooldown: on KILL, `symbol|direction|entry_mode` is recorded in `killed_thesis.json` and blocks re-insertion for one closed H1 candle (`KILL_COOLDOWN_SECONDS`), emitting `[🚫 KILL COOLDOWN]`.
- A same-thesis partial-refresh branch emitting `[🟰 SAME THESIS]`.
- A retracement OB audit trail appended to `retracement_ob_audit.jsonl`.

Behavior compatibility with V1 is mandatory (#003). Modeling these in Phase 1 avoids reopening frozen domain modules in Phase 4.

### Alternative Considered

Defer modeling until Phase 4, or exclude the subsystems from V2. Deferral rejected because it guarantees reopening completed Phase-1 modules; exclusion rejected because it is a behavior change (killed signals would reappear within cooldown).

`OBAuditRecord` was also considered for the Infrastructure layer as an audit DTO. Rejected: infra must not own an SMC-vocabulary schema (§3 "must NOT know" clauses), and the object matches the `Alert`/`LogEvent` precedent of a core-emitted, side-effect-describing domain contract.

### Impact

- The Phase 1 domain model comprises 18 objects (the original 16 plus these two).
- The LogEvent contract includes `SAME_THESIS` and `KILL_COOLDOWN`.
- `killed_thesis.json` is recognized as a second persistent store owned by the Lifecycle layer.
- The OB audit trail remains observability-only: never read back into a decision.

### Status

Active