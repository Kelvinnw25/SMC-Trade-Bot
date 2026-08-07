# REPLAY_CONTRACT.md — Replay & Outcome Semantics Contract

> **Status: BINDING.** This document is the single source of truth for the Replay
> Engine, the Backtester, the Outcome Engine, the Experiment Engine, and all
> future strategy evaluation. Its purpose is that **two engineers implementing
> replay independently, from this repository alone, produce identical behavior.**
>
> - **Version:** 1.1 (2026-07-27). Supersedes and **replaces** v1.0 entirely.
> - **Authority order:** this contract > `STRATEGY_NOTES.md` > `CHANGELOG.md` >
>   chat history. Architecture rules in `PROJECT_STATE.md` §2 still bind
>   (layering, purity, observability-is-write-only).
> - **Anchoring:** every clause cites the V2 code it derives from. Clause numbers
>   `C-x.y` are permanent identifiers — cite them in code comments, tests, and
>   experiment records. **A clause number is never reused for a different
>   meaning**; §0–§14 keep the exact numbering of v1.0.
> - **Change control:** amendments bump the version and add a `CHANGELOG.md`
>   entry. Every emitted dataset records the `contract_version` that produced it.
> - **No prior context required.** If implementing replay requires a fact that is
>   not in this file or in the cited code, that is a **defect in this contract** —
>   file an amendment rather than deciding locally.

### What changed in v1.1

| Clause | Change | Closes |
|---|---|---|
| **§0** | New: scope, normative language, glossary. Numbered §0 so §1–§14 keep v1.0's identifiers. | — |
| **§5 (C-5.0 … C-5.8)** | Rewritten. Single normative definition of `T`, the scan instant, the reference candle, `h1_last_ts`; ordering guarantees; lifecycle burn-in; worked example. | **B1** (A47) |
| **C-5.2** | Corrected: the v1.0 "intra-hour M5 upgrade" fidelity loss **does not exist**; the real M5 divergence is the opposite and is now stated. | A48 |
| **C-6.2 / C-6.4** | **AMENDED.** `h1_last_ts` is the *open* time of the reference candle (`T − 3600`), not `T`. Selection defined on open times. | **B1** |
| **§6 (C-6.3, C-6.7 … C-6.11)** | New/rewritten: row-window construction with an explicit prohibition list, partial history, outages, universe data scope + tie-break, repository integrity, `data_snapshot_id`. | **B2** (A49) |
| **§7** | Invariants I-1 … I-10 made normative and individually testable. | B1, B2 |
| **§15** | New: complete lifecycle-capture architecture, event schema, TTL attribution with a completeness proof. | **B3** (A50) |
| **§16** | New: Signal Ledger `schema_version` 2 — nine lineage fields, ownership, compatibility, migration, and the exact bounded production change. | **B4** (A53) |
| **C-4.0, C-4.1, C-4.2, C-4.3, C-4.10, C-4.14** | New/amended: walk origin, pre-crossed limits, continuation fill target, missing-M5 fallback, expiry counting, level sanity gate. | D-4 |
| **C-2.2, C-9.6, C-9.7, C-9.8** | New/amended: three explicit shadow classes; R arithmetic; canonical `OutcomeConfig`; deterministic equity ordering. | ambiguity found in §P5 review |
| **§17, §18, §19** | New: testing architecture, proof of determinism, architectural decisions log. | — |

---

## 0. Scope, Language, Glossary

### C-0.1 Scope

This contract governs replay time semantics, historical data construction, the
historical market-data adapter, replay architecture, lifecycle capture, the Signal
Ledger schema, trade instantiation, execution and fill semantics, outcome
labeling, dataset lineage, metrics, testing, and the declared biases of the whole
apparatus.

It does **not** govern strategy quality — strategy findings live in
`STRATEGY_NOTES.md` (items A1–A25). This contract must never be used to justify a
change in analysis or lifecycle behavior; it exists to *measure* the behavior that
already exists.

### C-0.2 Normative language

**MUST / MUST NOT** — a violation makes an implementation non-conforming.
Prohibitions are listed explicitly because each one is a plausible,
well-intentioned mistake that silently corrupts results.
**SHOULD** — deviation requires a recorded reason in the run manifest.
**MAY** — free choice; no result depends on it.

**Citation convention.** `C-x.y` identifies a clause (a `###` heading).
`C-x.y.z` identifies **item z of the numbered list inside clause C-x.y** — e.g.
`C-5.5.2` is the second declared fidelity loss in C-5.5. `§n` identifies a section;
`§n.m` identifies item m of a numbered list in section n (e.g. `§17.5`).

### C-0.3 Glossary

| Term | Definition |
|---|---|
| **H1 grid** | The sequence of one-hour intervals aligned to UTC epoch hours; an interval's **open time** `o` satisfies `o mod 3600 == 0` (seconds). |
| **Candle** | One kline. Its `timestamp` column is its **open time** ([market_data.py:178](v2/infra/market_data.py#L178) converts Binance kline field 0; field 6 `ct` is the close time and is discarded). |
| **Closed candle** | A candle whose interval has fully elapsed at the instant under consideration. |
| **Scan boundary `T`** | The instant an H1 candle closes; the anchor of one replay cycle (C-5.1). |
| **Reference candle `C_ref`** | The H1 candle that closed exactly at `T`; its open time is `T − 3600`. |
| **Scan instant** | `T + 1s` — the clock reading during the cycle anchored at `T` (C-5.2). |
| **Cycle** | One execution of `Orchestrator.run_cycle()` ([orchestrator.py:167](v2/app/orchestrator.py#L167)) at one `T`. |
| **Walk candle `Wk`** | The H1 candle with open time `T + k·3600`; the fill/exit timeline (C-4.0). |
| **Signal** | The pure analysis opinion for one symbol at one `T` (C-1.1). |
| **ActiveSignal** | The lifecycle's advisory memory record (C-1.2). |
| **Trade** | An evaluation-only construct created by the Outcome Engine (C-2.1). Nothing in `v2/` knows trades exist. |
| **Run** | One replay execution or one live process lifetime, identified by `run_id` (C-16.2). |
| **Repository** | The immutable local kline store (§6). |
| **`data_snapshot_id`** | Content identity of the repository (C-6.10). |

---

## 1. Signal Semantics — what exists today

### C-1.1 A Signal is an analysis opinion, not a trade

A `Signal` ([v2/domain/signal.py](v2/domain/signal.py)) is the pure analysis
layer's opinion about one symbol at one `T`: score, decision tier
(`SKIP`/`WATCH`/`VALID`/`A+`), direction, entry mode, notes, and an embedded
`SignalCandidate` carrying structure/entry/risk detail. It is immutable,
regenerated from scratch every cycle, and **is not a trade**: nothing in `v2/`
fills, holds, sizes, or closes anything. `EntryPlan.contract = "B"`
([entry.py:43](v2/domain/entry.py#L43)) states it: *entry_price is a STRUCTURAL
REFERENCE, not an executable order price.*

`Signal.candidate` is `None` for early-exit SKIPs (invalid input, no direction,
veto). **Discriminator for reconstruction:** in a ledger row,
`atr_last is None ⇔ candidate was None` ([ledger.py:129](v2/app/ledger.py#L129)
sets `atr_last` from the candidate or `None`). This is the only reliable
discriminator and MUST be the one used.

### C-1.2 An ActiveSignal is advisory memory, not a position

An `ActiveSignal` ([v2/domain/active_signal.py](v2/domain/active_signal.py)) is a
watch-list record with lifecycle metadata (`status` is always `"WATCH"` when V2
writes it — [reconciler.py:491](v2/lifecycle/reconciler.py#L491)). Its insertion,
refresh, and eviction express *advisory attention*, not order state.

### C-1.3 Field meanings (normative)

| Field | Meaning under this contract | Source |
|---|---|---|
| `entry_price` | The **structural level** the operator is advised to trade at. RETRACEMENT: the selected OB's near edge (top for LONG, bottom for SHORT — [entry.py:251-254](v2/analysis/entry.py#L251)). CONTINUATION: the pullback bar's close — **already historical at signal time** ([entry.py:153](v2/analysis/entry.py#L153)). | `EntryPlan` |
| `entry_source` | Provenance of the entry decision (`retracement_ob`, `continuation_micro_pullback`) or the failure reason on unready paths. Segmentation key; never an execution instruction. | `EntryPlan` |
| `sl` | The advised invalidation level: continuation = pullback bar low/high ∓ 0.30·ATR; retracement = selected OB far edge ∓ 0.70·ATR ([risk.py:450-459](v2/analysis/risk.py#L450)). Under this contract a **hard exit** (no trailing, no break-even logic exists in V2). | `RiskPlan` |
| `tp` | The advised target: RR-passing liquidity level ∓ 0.10·ATR, else flat 2R fallback ([risk.py:504-507](v2/analysis/risk.py#L504)). A **single, full exit** (no partials exist in V2). | `RiskPlan` |
| `RiskPlan.tp_rr` | Target-side RR from liquidity targeting. Metadata only — no decision consumes it (verified by consumer enumeration; published since `STRATEGY_VERSION` 2.0.1). | `RiskPlan` |
| `RiskPlan.rr` | Scoring-side RR, gated against the 1.2 floor ([scoring.py:293](v2/analysis/scoring.py#L293)). Computed from the structural entry (Contract B). | `RiskPlan` |
| `live_price` | Live: the close of the **forming** H1 candle ([market_data.py:262](v2/infra/market_data.py#L262)). Replay: the close of `C_ref` (C-5.3). Consumed **only** by the lifecycle kill/STALE predicate and recorded in observability — never by analysis (verified: no `live_price` reference exists in `v2/analysis/` outside a docstring). | `MarketSnapshot` |

### C-1.4 Per-mode semantics

| Mode | Analysis meaning | Trade meaning under this contract |
|---|---|---|
| `RETRACEMENT` | Price is expected to return to the selected OB edge | **Resting limit order** at `entry_price` (C-4.1) |
| `CONTINUATION` | A displacement + micro-pullback already completed; momentum expected to continue | **Market order** at the walk origin (C-4.2) — the published price is historical and cannot be traded |
| `WATCH_ONLY` | Compression-regime observation | **Never a trade.** Levels are 0.0, so `levels_valid` always fails and the record can never enter memory (finding A26; [scoring.py:757](v2/analysis/scoring.py#L757), [reconciler.py:288](v2/lifecycle/reconciler.py#L288)). Ledger-only. |
| `NONE` | No actionable regime / veto / no direction | Never a trade. Ledger-only. |

---

## 2. Trade Instantiation

### C-2.1 A Trade is born from an alert-bearing insertion

A **Trade** is instantiated when the lifecycle inserts (or replace-inserts) an
`ActiveSignal` **and the same `LifecycleDecision` carries a `new` or `upgrade`
Alert** ([reconciler.py:541-557](v2/lifecycle/reconciler.py#L541)). The trade
freezes, at instantiation: `symbol, direction, entry_mode, entry_price, sl, tp,
tier, score, T, h1_last_ts, run_id, cycle_index, universe_rank`.

Rationale: the system is advisory — the operator acts on alerts, so what was
alerted is what is tradable.

**Source of truth (AMENDED in v1.1):** trade instantiation is derived from the
captured `LifecycleDecision` (§15), **not** from the notifier stream. The notifier
capture is a cross-check only (C-15.7). Reason: an exception in `_render_symbol`
would skip dispatch after the decision was already applied
([orchestrator.py:239-241](v2/app/orchestrator.py#L239)), and `Alert` carries no
symbol-time identity of its own.

### C-2.2 The shadow population (research tier) — AMENDED in v1.1

Three **disjoint** classes of shadow trade exist. All are evaluated with the
mechanics of §4, reported separately from each other, and **never mixed into
headline metrics**. Each record carries `shadow_kind`.

| `shadow_kind` | Instantiated when | Pending lifetime | Question it answers |
|---|---|---|---|
| `UNALERTED_INSERT` | the decision inserts an `ActiveSignal` with **no** alert (WATCH tier) | the real lifecycle (C-4.8) | does the sub-alert tier carry edge? |
| `CAPPED` | the decision carries `LogEvent("CAPPED")` and **no** mutation ([reconciler.py:528-536](v2/lifecycle/reconciler.py#L528)) — the alert was computed, the slot cap suppressed the insertion | fixed `OutcomeConfig.shadow_pending_h1` (default 3), then `NOT_FILLED(SHADOW_WINDOW)` | what does the 10-slot cap cost? |
| `KILL_COOLDOWN` | the decision carries `LogEvent("KILL_COOLDOWN")` and no mutation ([reconciler.py:361-376](v2/lifecycle/reconciler.py#L361)) **and** the canonical ledger row's tier is `VALID`/`A+` | same fixed window | what does the 1 h cooldown cost (findings A18/A33)? |

Levels for the two suppressed classes come from the canonical ledger row of that
scan (C-9.4), which always exists. The fixed window is 3 H1 because that is the
decay limit an accepted insertion would have had
(`LifecycleConfig.miss_count_limit = 3`) — it mirrors the real lifetime instead of
inventing one. A `PENDING` shadow of a fixed-window class is never cancelled by a
lifecycle event, because no memory record exists for it to be evicted from.

### C-2.3 One live trade per thesis

At most one non-terminal trade exists per thesis key `symbol|direction|entry_mode`
(mirroring [killed_thesis.py:36](v2/domain/killed_thesis.py#L36)). A new alert for
a thesis with a live trade supersedes it (C-4.6/C-4.7).

---

## 3. Trade Lifecycle — states and transitions

### C-3.1 State vocabulary (normative)

| State | Definition | Entry condition | Terminal |
|---|---|---|---|
| `PENDING` | Order intent exists, not filled | Trade instantiated (C-2.1) | no |
| `ACTIVE` | Position open | Fill rule met (C-4.1/C-4.2) | no |
| `NOT_FILLED(reason)` | Cancelled before fill | Thesis evicted or superseded (C-4.6–C-4.8), or shadow window elapsed (C-2.2) | **yes** |
| `WIN` | TP reached before SL | TP touch per C-4.3 | **yes** |
| `LOSS` | SL reached before TP | SL touch per C-4.3 | **yes** |
| `INVALIDATED_EXIT` | Closed early by an HTF-flip kill while ACTIVE | C-4.9 | **yes** |
| `EXPIRED` | Holding cap reached | C-4.10 | **yes** |
| `INVALID_LEVELS` | Rejected before any walk | C-4.14 | **yes** |
| `OPEN` | Reporting label, not a state | Data ends while PENDING or ACTIVE | no |

`NOT_FILLED` carries
`cancel_reason ∈ {KILL, DECAY, TTL, REPLACED, SUPERSEDED, SHADOW_WINDOW}`.
**`BREAKEVEN` is not a state** — V2 has no BE logic; BE is a derived reporting
bucket (`|realized_r| < 0.1`). **`PARTIAL` does not exist** — V2 has no partial
exits.

### C-3.2 State diagram

```
                       alert-bearing insertion (C-2.1)
                                   │
                          ┌────────┴─────────┐
                          │ level sanity     │ fail → INVALID_LEVELS (C-4.14)
                          └────────┬─────────┘
                                   ▼
                              ┌─────────┐
     eviction of the thesis   │ PENDING │   fill rule met (C-4.1/C-4.2)
   ┌──────────────────────────┤         ├────────────────────────┐
   ▼                          └────┬────┘                        ▼
 NOT_FILLED(reason)                │ upgrade alert          ┌────────┐
 reason ∈ {KILL, DECAY, TTL,       └──► NOT_FILLED(SUPER-   │ ACTIVE │
 REPLACED, SUPERSEDED,                  SEDED) + new PENDING└───┬────┘
 SHADOW_WINDOW}                                                 │
                     ┌──────────────┬───────────────┬───────────┴─┬───────────┐
                     ▼              ▼               ▼             ▼           ▼
                    WIN           LOSS      INVALIDATED_EXIT   EXPIRED      OPEN
                 (TP first)    (SL first)   (HTF-flip kill    (holding     (end of
                                             while filled)     cap)         data)
```

---

## 4. Execution and Fill Model

### C-4.0 The walk origin (new in v1.1)

For a trade instantiated at scan boundary `T`, the **price walk** is the ordered
sequence of H1 candles `W0, W1, W2, …` where

```
W0 = the H1 candle whose open time == T          (the candle forming at the scan instant)
Wk = the H1 candle whose open time == T + k*3600
```

`W0` is the first candle in which any fill or exit may occur. It is **not** the
reference candle `C_ref`, which is already closed and was the analysis input.

```
 ── C_ref ──────────┼── W0 ─────────────┼── W1 ─────────────┼──
 open T-3600        │  open T           │  open T+3600      │
 close T            │  close T+3600     │  close T+7200     │
                    ▲
                    T (scan boundary).  Scan instant = T+1s.
                    Analysis input = frames ending at C_ref.
                    Fills/exits may occur from W0 onward.
```

`W0`'s open price is the print at instant `T` — one second **before** the scan
instant. A bounded look-behind, never a look-ahead.

### C-4.1 RETRACEMENT fill (limit)

A pending RETRACEMENT trade fills when a walk candle **touches** the entry level:
LONG — `low <= entry_price`; SHORT — `high >= entry_price`. Fill price =
`entry_price`, **or better on a gap**: if a candle *opens* through the level
(LONG: `open < entry_price`; SHORT: `open > entry_price`) the fill price is that
open. Zero slippage on limit fills (C-4.12).

`fill_kind` is `"LIMIT_TOUCH"`, or `"LIMIT_GAP"` when the gap rule improved the
price.

**Pre-crossed levels (new in v1.1).** If at instantiation the limit sits on the
already-reachable side of the reference close — LONG with
`entry_price >= close(C_ref)`, or SHORT with `entry_price <= close(C_ref)` — the
trade is an **immediate fill**: `fill_price = open(W0)`,
`fill_kind = "IMMEDIATE"`. An order already at or through market fills at once;
modeling patience would fabricate a favourable entry. These trades are counted
separately in the fill-rate breakdown (§12).

### C-4.2 CONTINUATION fill (market) — AMENDED in v1.1

A pending CONTINUATION trade fills at **`open(W0)`**.
`fill_kind = "MARKET_WALK_ORIGIN"`.

It is **prohibited** to fill at `open(W1)` (a full hour of invented delay) or at
`entry_price` (the pullback close, already historical —
[entry.py:153](v2/analysis/entry.py#L153)).

`OutcomeConfig.continuation_fill` selects the model: `"w0_open"` (default) or
`"w0_close"` (pessimistic sensitivity). The chosen value is stamped into every
outcome record. Consequence: realized R is computed from the actual fill, so
**structural RR ≠ realized RR** — both are recorded (C-9.4, C-9.6).

### C-4.3 SL/TP resolution while ACTIVE — AMENDED in v1.1

Walk forward candle by candle from the fill candle onward. In each candle: LONG —
SL hit if `low <= sl`, TP hit if `high >= tp` (mirrored for SHORT).

1. If exactly one is hit, that is the exit.
2. If **both** are hit in the same H1 candle, sub-resolve on that candle's M5
   candles in ascending open-time order, applying the same per-candle rule.
3. If both are hit within a **single M5 candle**, apply **worst case: SL first**
   (`LOSS`, `exit_reason = "SL_WORST_CASE_M5"`).
4. **If the M5 set for that H1 candle is missing or incomplete**, do not guess:
   apply worst case (`LOSS`, `exit_reason = "SL_WORST_CASE_NO_M5"`) and increment
   the run's `m5_gap_resolutions` counter, which the metrics report MUST surface.

The M5 candles **covering** an H1 candle whose open time is `h` are exactly those
with `h <= m5_open_time < h + 3600`, ascending. The set is **complete** only when
it contains 12 candles; any other count (including 0) is incomplete and triggers
rule 4.

Rationale: deterministic and conservative — every ambiguity biases expectancy
*downward*, never upward.

### C-4.4 The fill candle

On the candle where a limit fills, SL/TP are evaluated **from the fill onward**
using the same M5 sub-resolution. Where M5 cannot order the events, worst case
applies: a LONG fill candle whose `low <= sl` is a `LOSS` unless M5 proves TP was
struck first *after* the fill. For `fill_kind ∈ {"IMMEDIATE",
"MARKET_WALK_ORIGIN"}` the fill is at `W0`'s open, so the whole of `W0` is
evaluated for exits.

### C-4.5 Gap exits

If a candle **opens** beyond SL (LONG: `open <= sl`), the exit price is the open,
not SL — realized R may be worse than −1. Symmetrically, a gap through TP exits at
the open (better than TP). Crypto perps gap rarely; the rule exists so the walk is
total.

### C-4.6 Same-thesis refresh before fill

A non-alerted partial refresh ([reconciler.py:401-412](v2/lifecycle/reconciler.py#L401))
does **not** modify a pending or active trade. The operator was not re-alerted;
their order stands as placed. Memory levels and trade levels are allowed to
diverge — the trade is the alert's frozen copy (C-1.3).

### C-4.7 Upgrade and supersession

- An **upgrade alert** ([reconciler.py:443-467](v2/lifecycle/reconciler.py#L443))
  on a thesis with a `PENDING` trade cancels it (`NOT_FILLED(SUPERSEDED)`) and
  instantiates a new trade at the upgrade's levels.
- An upgrade on an **ACTIVE** trade changes nothing (the position is held; the
  upgrade is informational).
- A **silent overwrite** of the resident record by a different thesis
  (direction/mode flip with no alert — finding A43,
  [reconciler.py:471-545](v2/lifecycle/reconciler.py#L471)) cancels a `PENDING`
  trade of the old thesis (`SUPERSEDED`) and instantiates **nothing** (no alert →
  no trade, C-2.1).

### C-4.8 Pre-fill cancellations (entry lifetime)

**The lifecycle IS the order-lifetime model** — no separate order TTL is invented.
A `PENDING` trade is cancelled when its thesis is evicted:

| Lifecycle event | Trade result | Attribution source |
|---|---|---|
| KILL of the resident thesis ([reconciler.py:228-262](v2/lifecycle/reconciler.py#L228)) | `NOT_FILLED(KILL)` | `EvictMutation(reason="kill")` |
| Decay eviction (3 H1-gated misses, [reconciler.py:575-582](v2/lifecycle/reconciler.py#L575)) | `NOT_FILLED(DECAY)` | `EvictMutation(reason="decay")` |
| TTL eviction ([store.py:132-153](v2/lifecycle/store.py#L132)) | `NOT_FILLED(TTL)` | C-15.5 attribution |
| REPLACE eviction (weakest slot, [reconciler.py:511-527](v2/lifecycle/reconciler.py#L511)) | `NOT_FILLED(REPLACED)` | `ReplaceMutation.target_symbol` |
| Supersession (C-4.7) | `NOT_FILLED(SUPERSEDED)` | new `InsertMutation` + alert on the same symbol |

Consequence: typical entry lifetime is ≤ 3–4 candles (decay) for monitored symbols
and ≤ 24 h (TTL) for symbols that leave the universe (finding A31).

### C-4.9 Kill while ACTIVE

Price-breach kills are subsumed by the SL exit (the kill threshold sits beyond the
SL — [rules.py:147](v2/lifecycle/rules.py#L147)). An **HTF-flip kill** (D1 and H4
both flipped against the direction) while ACTIVE closes the trade at the **close of
`C_ref` for the cycle whose scan produced the kill** → `INVALIDATED_EXIT`, realized
R signed from that exit (may be positive). The kill alert tells the operator to
exit; at H1 replay cadence that close is the honest actionable price.

### C-4.10 Expiry and end-of-data — AMENDED in v1.1

`holding_candles` counts walk candles **from the fill candle inclusive**: a trade
filled in `W_f` has `holding_candles = 1` at the close of `W_f`. When
`holding_candles` reaches `OutcomeConfig.max_holding_h1` (default 168 = 7 days)
without SL/TP/kill, the trade closes mark-to-market at **that candle's close** —
i.e. at the close of `W_{f + max_holding_h1 − 1}` → `EXPIRED`.

Trades still ACTIVE when the repository's data for that symbol ends are labeled
`OPEN`, reported mark-to-market at the last available close, and **excluded from
expectancy**. Trades still `PENDING` when data ends are also `OPEN`, with
`filled = false`.

### C-4.11 Symbol leaves the universe

Fills and SL/TP resolution continue on price data regardless of scanning — the
operator's order or position rests on the exchange. Lifecycle cancellations require
scans, so an unmonitored `PENDING` trade's only cancellation path is the TTL sweep,
which runs against memory every cycle
([orchestrator.py:325](v2/app/orchestrator.py#L325)). This mirrors live behavior
(finding A31) and is preserved, not fixed.

### C-4.12 Cost model

Baseline `OutcomeConfig`: **zero limit-fill slippage, zero spread, zero market
slippage, `taker_fee_rate = 0`, funding = 0**. Every metrics report shows gross and
net at 0.05 %/side as a sensitivity. Funding is a documented omission (advisory
horizon ≤ 7 d; revisit if `EXPIRED` rates are material). All cost parameters live
in `OutcomeConfig`, whose canonical hash is stamped into every outcome record —
outcomes from different configs are never comparable without segmentation.

### C-4.13 Event ordering within a candle

Intra-candle **price events** (fills, SL, TP — resolved via M5) precede that
candle's **close-scan lifecycle events** (kill, decay, TTL, replace). A fill during
`Wk` beats a cancellation produced by the scan at `Wk`'s close.

### C-4.14 Level sanity gate (new in v1.1)

Before any walk, a trade's frozen levels are checked:

- LONG requires `tp > entry_price > 0` and `sl < entry_price`;
- SHORT requires `tp < entry_price` and `sl > entry_price`, all `> 0`;
- all three finite; `|fill_price − sl| > 0` once filled (C-9.6).

A trade failing this is terminal `INVALID_LEVELS`, excluded from every performance
metric, and **counted** in the population report. It can only fire if analysis
emits inverted levels that still pass `levels_valid`
([rules.py:76](v2/lifecycle/rules.py#L76), which checks positivity and distinctness
but not side) — see finding A36. The count is therefore a strategy-health signal,
not dead code.

---

## 5. Replay Clock and Cadence — **resolves B1**

This section has one job: make the replay clock so precisely defined that no
second reading exists.

### C-5.0 Run window parameters

`T_0` (first boundary) and `T_n` (last boundary) are **explicit run
configuration**, never inferred from the data. The driver validates
`T_0 mod 3600 == 0`, `T_n mod 3600 == 0`, `T_0 <= T_n`, and that the repository
covers `[T_0 − warmup, T_n + max_holding_h1·3600]` for at least one symbol;
otherwise it refuses to start.

Warmup (C-6.11) is enforced **per symbol by the existing guards**, not globally: a
symbol with fewer than 50 closed H1 or 10 closed D1 rows at `T` is skipped that
cycle exactly as in live. The manifest records how many symbols were guard-skipped
at `T_0`, so an accidentally-early window is visible rather than silent.

### C-5.1 The scan boundary `T`

A replay **cycle** is anchored at a scan boundary `T`, an epoch-second value with

```
T mod 3600 == 0
```

`T` is the instant an H1 candle **closes**. The candle that closed at `T` is the
**reference candle** `C_ref`, whose open time is `T − 3600`.

The replay window is the finite, strictly increasing sequence
`T_0 < T_1 < … < T_n` with `T_{k+1} − T_k = 3600` exactly. **No boundary is ever
skipped**, including boundaries at which the universe is empty (such a cycle still
runs and returns `False` — [orchestrator.py:179](v2/app/orchestrator.py#L179)).

### C-5.2 The scan instant and the clock

```
scan_instant = T + 1 second
```

The driver seeds a `FakeClock` ([clock.py:62](v2/infra/clock.py#L62)) at
`T_0 + 1s` and advances it by **exactly 3600 s** per cycle. `FakeClock` is
timezone-aware UTC and forward-only ([clock.py:83](v2/infra/clock.py#L83)); no new
clock class is required or permitted.

**The clock does not move within a cycle.** Every value the cycle stamps —
`ActiveSignal.timestamp`, `alert_sent_ts`, `KilledThesisRecord.killed_ts`,
`SignalLedgerRecord.recorded_at` — equals `scan_instant` for that cycle. This is
*more* deterministic than live (where `SystemClock` drifts across the ~45 s a real
cycle takes) and is neutral for every threshold that consumes it: TTL 6/10/24 h,
cooldown 1 h, and decay is H1-gated rather than seconds-gated
([rules.py:181](v2/lifecycle/rules.py#L181)).

Why `+1 s` and not `+0 s`: at exactly `T` the closing candle is not yet
observable, and live never scans at a boundary instant. One second is the smallest
unambiguous "after the close" offset; it keeps `hour(scan_instant) == hour(T)` and
`floor(scan_instant, 1h) == T`. Why not `+30 s` or `+60 s`: every consumer either
reads the hour/weekday (C-5.4) or compares differences of hours, so all offsets in
`(0, 3600)` behave identically — `+1 s` is therefore chosen as the canonical,
minimal value, and implementations **MUST** use it so `recorded_at` is
bit-comparable across runs.

### C-5.3 Snapshot time fields — AMENDED in v1.1 (this is finding A47)

For the cycle anchored at `T`, the adapter MUST produce:

| Field | Value | Why |
|---|---|---|
| `h1_last_ts` | `float(T − 3600)` | It is the **open time** of `C_ref`. Live reads `h1["timestamp"].iloc[-1]` *after* the forming-candle strip ([market_data.py:266-272](v2/infra/market_data.py#L266)), and the `timestamp` column is Binance's kline open time ([market_data.py:170-178](v2/infra/market_data.py#L170) — the separate close-time column `ct` is dropped). |
| `h1_last_ts_dt` | `pd.Timestamp` of `h1_last_ts`, **tz-naive**, wall value = the UTC time of `T − 3600` | Live builds it with `pd.to_datetime(kline_ms, unit="ms")`, which is tz-naive UTC ([market_data.py:271](v2/infra/market_data.py#L271)). It MUST equal the frame's last `timestamp` value. |
| `live_price` | `close(C_ref)` | The forming-candle price is unreconstructible at H1 cadence. Declared substitution (C-5.5.3). |

> **`h1_last_ts != T`. It is `T − 3600`.** Any implementation that sets
> `h1_last_ts = T` is non-conforming. The v1.0 text said otherwise and was wrong.

**Empirical confirmation (not inference).** Production
`retracement_ob_audit.jsonl` carries `h1_last_ts = 2026-07-26T02:00:00Z` on the
rows appended by the cycle that ran at ≈`03:00Z`. Under open-time semantics the
value at a `03:00Z` boundary is `02:00Z` ✓; under the rejected reading it would
have been `03:00Z` ✗.

**Why this matters beyond keys.** `session_active` is derived from the clock
([orchestrator.py:315](v2/app/orchestrator.py#L315)) and is a **scoring input**
([scoring.py:257](v2/analysis/scoring.py#L257)). An implementation that anchored
the clock to `h1_last_ts + 1s` instead of `T + 1s` would shift the session flag by
one hour and produce **different decision tiers** at the 07:00 and 22:00 UTC
edges. B1 was a correctness ambiguity, not a metadata one.

### C-5.4 `session_active` derivation

```python
now = clock.now()                      # == scan_instant, tz-aware UTC
session_active = now.weekday() < 5 and 7 <= now.hour < 22
```

This is [orchestrator.py:312-316](v2/app/orchestrator.py#L312) unchanged; the
driver MUST NOT compute or inject it separately. Because
`hour(T + 1s) == hour(T)`, the flag is a pure function of `T`:
`session_active(T) = weekday(T) < 5 and 7 <= hour(T) < 22` (UTC), with `T` read as
the *closing* boundary.

### C-5.5 Accepted fidelity losses — corrected in v1.1

1. **Intra-candle kill/cancel timing.** Live can cancel a pending thesis mid-candle
   on a wick; replay cancels at the close. Trades may therefore fill in replay
   where live would have cancelled first, and vice versa. Bounded to one candle.
   Trade *outcomes* are unaffected — SL/TP resolution is M5-accurate (C-4.3).
2. **M5 enrichment coverage — corrected.** v1.0 claimed replay loses intra-hour
   M5-driven upgrades. **That loss does not exist.** The live attachment guard is
   `m5_last_open.floor("h") <= h1_last_open.floor("h")`
   ([market_data.py:321](v2/infra/market_data.py#L321)). At wall time `W` the last
   closed M5 opens at `floor(W,5m) − 5m` and `C_ref` opens at `floor(W,1h) − 1h`,
   so the guard passes **only for `W ∈ [hh:00, hh:05)`**. Live can therefore only
   enrich with M5 in the first five minutes of an hour, and only for the symbols a
   cycle reaches before `hh:05`. The replay scan at `T + 1s` reproduces exactly
   that one M5-eligible scan — and gives M5 to **all** symbols, whereas live gives
   it only to the early ones. The real divergence is therefore *replay is more
   M5-complete than live*, and since M5 only adds score, replay may show marginally
   more `VALID`/`A+` upgrades. Declared and measurable via the parity test (§17.6).
3. **`live_price` := `close(C_ref)`.** Kill/STALE checks in replay react to closes,
   not wicks — this *reduces* false wick-kills relative to live (interacts with
   findings A4/A17; measured, not hidden).
4. **Timestamp refresh granularity.** Live refreshes `ActiveSignal.timestamp` every
   ~60 s while a thesis stays actionable; replay refreshes once per candle. TTL
   semantics are preserved because TTL only reaps records that stopped being
   refreshed (finding A38).
5. **Cycle wall duration.** Live spends ~45 s walking 30 symbols, so late symbols
   see slightly later prices; replay is instantaneous. This is what makes loss (2)
   asymmetric and is otherwise inert (analysis reads only closed frames).

### C-5.6 Ordering guarantees (normative)

An implementation MUST guarantee, and its tests MUST assert:

1. **Cycle order.** Strictly increasing `T`, step 3600 s, no gaps, no repeats.
2. **Symbol order within a cycle.** Symbols are processed in the order the universe
   tuple returns them — **descending 24 h quote volume**
   ([market_data.py:136](v2/infra/market_data.py#L136),
   [orchestrator.py:182](v2/app/orchestrator.py#L182)). This order is
   **semantically load-bearing**: it decides which symbol reaches a free slot
   first, and therefore the outcome of the 10-slot cap and the REPLACE branch
   ([reconciler.py:507-536](v2/lifecycle/reconciler.py#L507)). It MUST NOT be
   re-sorted, shuffled, parallelized, or deduplicated.
3. **Intra-cycle event order.** Per symbol: `reconcile` → `store.apply` → render →
   notifier dispatch → OB audit
   ([orchestrator.py:230-241](v2/app/orchestrator.py#L230)). Events inside one
   `LifecycleDecision` keep their tuple order.
4. **End-of-cycle order.** TTL sweep → apply → TTL logs → killed-thesis prune →
   persist active → persist killed → dashboard
   ([orchestrator.py:320-339](v2/app/orchestrator.py#L320)). End-of-cycle events
   sort **after** every per-symbol event of the same cycle.
5. **Empty universe.** `run_cycle` returns `False` and performs **no** end-of-cycle
   maintenance ([orchestrator.py:179-186](v2/app/orchestrator.py#L179)). The driver
   MUST still advance the clock and proceed to `T + 3600`.
6. **Monotone adapter.** `set_time` is called with strictly increasing `T`; the
   adapter MUST reject a non-increasing `T` with `ReplayDataError`.

### C-5.7 Lifecycle burn-in (new in v1.1 — finding A54)

Lifecycle state carries across cycles (10-slot cap, insertion-order tie-breaks in
`weakest_symbol` — [memory.py:64](v2/lifecycle/memory.py#L64) — miss counters, kill
cooldowns). Therefore **replay results depend on the replay start date.**

1. Every run declares `burn_in_h1` (default **336** = 14 days). Trades instantiated
   at `T < T_0 + burn_in_h1·3600` are emitted, labeled, and stored, but flagged
   `in_burn_in = true` and **excluded from every headline metric**.
2. Two runs are comparable only if `T_0`, `T_n`, `burn_in_h1`, `universe_mode` and
   `data_snapshot_id` are all identical. The metrics engine MUST refuse to diff
   runs that differ on any of these.
3. Replay MUST start from empty lifecycle memory (`ReplayPersistence` returns
   `{}` / `{}` at startup) — never from live `active_pois.json`.
4. Time-window sharding is permitted for parallel exploration, each shard applying
   its own burn-in; the **canonical baseline MUST be a single unsharded run**,
   because shard boundaries reset lifecycle state.

### C-5.8 Worked example (normative reference)

Cycle anchored at `T = 2025-03-11T12:00:00Z` (epoch `1741694400`):

| Quantity | Value |
|---|---|
| `T` | `1741694400` = 2025-03-11 12:00:00 UTC |
| `scan_instant` (clock) | `1741694401` = 12:00:01 UTC |
| `C_ref` open / close | 11:00:00 / 12:00:00 UTC |
| `h1_last_ts` | `1741690800` = 11:00:00 UTC |
| `h1_last_ts_dt` | `Timestamp('2025-03-11 11:00:00')`, tz-naive |
| `live_price` | close of the 11:00–12:00 candle |
| `session_active` | Tuesday, hour 12 → `True` |
| H1 frame | 499 rows, open times 2025-02-18 17:00 … 2025-03-11 11:00 |
| D1 frame | 119 rows, open times 2024-11-12 … 2025-03-10 (last closes at 2025-03-11 00:00) |
| M5 frame | 199 rows, open times 2025-03-10 19:25 … 2025-03-11 11:55 |
| M5 guard | `11:55.floor('h') = 11:00 <= 11:00 = h1_last_ts_dt.floor('h')` ✓ |
| Walk origin `W0` | open 12:00:00, close 13:00:00 |
| Next cycle | `T = 1741698000` = 13:00:00 UTC |

Midnight edge case, `T = 2025-03-11T00:00:00Z`: `h1_last_ts` = 2025-03-10 23:00;
the D1 candle of 2025-03-10 closed exactly at `T` and **is included** as the last
D1 row; `session_active = False` (hour 0).

---

## 6. Historical Data Contract — **resolves B2**

### C-6.1 Repository content

Raw Binance USDT-M perpetual klines, **all 12 columns retained on disk** (the
quote-volume column `qv` is required for universe reconstruction, C-6.9), per
symbol per timeframe: **1h** (signals + universe), **1d** (HTF), **5m**
(enrichment + outcome resolution). Open times stored as UTC epoch **milliseconds**
(Binance-native, `int64`).

### C-6.2 Window shapes — AMENDED in v1.1

Live requests raw windows of **120 D1 / 500 H1 / 200 M5** *including the forming
candle*, then strips the last row
([market_data.py:53-58, 252-266](v2/infra/market_data.py#L53)). Replay therefore
serves **closed-only** frames of at most:

| Timeframe | Rows | Selection rule (normative) |
|---|---|---|
| D1 | **119** | last 119 rows with `open_time_ms + 86_400_000 <= T_ms` |
| H1 | **499** | last 499 rows with `open_time_ms + 3_600_000 <= T_ms` |
| M5 | **199** | last 199 rows with `open_time_ms + 300_000 <= T_ms` |

Selection is expressed on **open time plus interval** — never on Binance's `ct`
column, whose value is `open_time + interval − 1 ms` and would invite
off-by-one-millisecond errors.

### C-6.3 Row-window construction (new in v1.1 — this is finding A49)

**The window is the last *N* rows present in the repository, not the last *N* time
units.**

```
window(symbol, tf, T, N):
    i = index of the last row with open_time_ms + interval_ms(tf) <= T_ms
    if no such row:  return empty
    lo = max(0, i - N + 1)
    return rows[lo : i+1]              # a contiguous slice of stored rows
```

If the symbol's history contains a gap (exchange outage, a missing kline), the
returned window spans **more wall-clock time than N intervals** and contains
**fewer rows than the calendar implies** — exactly as live's `limit=N` request
does, because Binance returns the *N most recent existing* klines.

**Prohibited** (each is a plausible mistake that silently changes every result):

1. **MUST NOT** reindex to a complete time grid, forward-fill, back-fill, or
   interpolate missing candles. `safe_frame` drops rows with any null
   OHLC/timestamp ([frames.py:37](v2/analysis/frames.py#L37)), so NaN padding
   *shortens* the analyzed frame and shifts every positional index — the OB scan
   window ([order_block.py:95-98](v2/analysis/order_block.py#L95)), the swing loop
   ([structure.py:98](v2/analysis/structure.py#L98)), and the EMA seeds.
2. **MUST NOT** select by time range (`open_time >= T - N*interval`).
3. **MUST NOT** pad a short window to `N` rows by any means.
4. **MUST NOT** resample a finer timeframe to synthesize a coarser one.
5. **MUST NOT** compute any indicator on longer history and then slice (C-6.5).
6. **MUST NOT** sort by anything other than ascending `open_time`, nor deduplicate
   on anything other than `open_time`.
7. **MUST NOT** include a candle whose interval has not fully elapsed at `T`.
8. **MUST NOT** vary `N` per symbol or per experiment — the window shape is part of
   the strategy definition (C-6.5).

**Required:** windows shorter than `N` are served as-is (C-6.7), and the served row
counts are recorded on every ledger row (C-16.1) so truncation is always auditable.

### C-6.4 Frame shape and dtypes — AMENDED in v1.1

The served DataFrame has **exactly seven columns, in this order**:

```
timestamp, open, high, low, close, v, atr
```

matching live ([market_data.py:176-179](v2/infra/market_data.py#L176)) and the
harness's `FRAME_COLUMNS` (`tests/regression/snapshot.py`). `timestamp` is
`datetime64[ns]`, tz-naive, built as `pd.to_datetime(open_time_ms, unit="ms")`; the
other six are `float64`. The 12-column raw form is a **storage** concern:
`ct, qv, nt, tb, tq, i` MUST be dropped before the frame reaches analysis, because
live drops them and any future column-order-sensitive code would diverge.

The index MUST be a fresh `RangeIndex` from 0 (live achieves this via
`pd.DataFrame(bars, …)` then `.iloc[:-1].copy()`; analysis re-indexes again inside
`safe_frame`, but the adapter MUST NOT rely on that).

The snapshot's time fields are those of C-5.3.

### C-6.5 Indicator computation windows (the window-relativity law)

- **ATR** is the **only** column the adapter computes. It MUST be computed **after**
  slicing, on the served rows, as
  `ta.atr(high, low, close, length=ATR_PERIOD)` with `ATR_PERIOD = 14`
  ([config.py](v2/infra/config.py), [market_data.py:179](v2/infra/market_data.py#L179)).
  The first 13 values are `NaN` and MUST be preserved as `NaN`. ATR's RMA is
  **causal** (row *i* depends only on rows ≤ *i*), so computing it on the
  closed-only slice is bit-identical to live's compute-then-strip; this equivalence
  MUST be pinned by a test (§17.4).
- **EMA50 / EMA200** are computed *inside* analysis on the served D1/H1 frames
  (`ewm(span=…, adjust=False)` — [htf.py:69-70](v2/analysis/htf.py#L69)). On a
  119-row D1 frame EMA200 is dominated by its seed; **that is the strategy as
  defined** (findings A1/A45), not something the adapter may "improve".
- **Volume SMA(20)** ([structure.py:195](v2/analysis/structure.py#L195)), **swing
  prominence** (= mean ATR of the served window,
  [structure.py:88-96](v2/analysis/structure.py#L88)), the regime's `tail(20)`
  statistics ([regime.py:75](v2/analysis/regime.py#L75)) and the liquidity target's
  `tail` window ([risk.py:201](v2/analysis/risk.py#L201)) are all computed inside
  analysis on the served frames.

**Law:** identical served frames ⇒ identical indicator values ⇒ identical Signal.
Feeding longer history "for a better warmup" changes the strategy and is prohibited
(C-6.3 item 5). The environment (`pandas`, `pandas_ta`, `numpy` versions) is part of
dataset lineage (C-16.2).

### C-6.6 MarketSnapshot reconstruction

```
MarketSnapshot(
    symbol         = "<BASE>/USDT",              # live format, market_data.py:142
    d1             = window(symbol,"1d",T,119),
    h1             = window(symbol,"1h",T,499),
    h1_last_ts     = float(T - 3600),            # C-5.3
    h1_last_ts_dt  = tz-naive Timestamp(T-3600), # C-5.3, == h1.timestamp.iloc[-1]
    live_price     = float(h1["close"].iloc[-1]),# close(C_ref)
    m5             = m5_frame_or_None,           # C-6.7
)
```

### C-6.7 Partial history, outages, listing start, truncation

| Situation | Required behavior | Live equivalence |
|---|---|---|
| No H1 rows satisfy the selection | adapter returns `None` | live returns `None` when the H1 frame is empty after the strip ([market_data.py:267](v2/infra/market_data.py#L267)) |
| No D1 rows satisfy the selection | adapter returns `None` | live would return a snapshot with an empty `d1`, which the orchestrator then skips via `len(d1) < 10` ([orchestrator.py:212](v2/app/orchestrator.py#L212)). **Outcome identical** (symbol skipped, no ledger row, no reconcile, no state change); the simpler rule is chosen and the equivalence is declared here (decision R-5). |
| `1 <= closed D1 rows < 10` or `1 <= closed H1 rows < 50` | adapter returns the snapshot **unchanged**; the orchestrator's guard skips the symbol | identical — the guard belongs to `app`, not the adapter ([orchestrator.py:212](v2/app/orchestrator.py#L212)) |
| Fewer than 19 closed M5 rows | `m5 = None` | live requires `len(raw) >= 20` before the strip ([market_data.py:314](v2/infra/market_data.py#L314)), i.e. ≥ 19 closed rows |
| M5 hour-alignment guard | assert it passes; if it does not, `m5 = None` and increment `m5_guard_failures` | at `T + 1s` the guard always passes (C-5.5.2); a failure indicates a repository defect |
| Exchange outage inside the window | window spans extra wall time, fewer rows; **no filling** | identical (Binance omits nonexistent klines) |
| Symbol listed mid-window | windows grow naturally from the listing candle; guards skip the symbol until 50 H1 / 10 D1 exist | identical |
| Symbol delisted mid-window | data ends; the universe stops ranking it; open trades resolve per C-4.10 (`OPEN`) or C-4.11 | live: the symbol vanishes from the ticker |
| Repository holds *more* history than `N` | slice to exactly `N` (C-6.3) | identical |
| Requested `T` precedes all data | adapter returns `None` | identical |

### C-6.8 Window ownership (single owner per concern)

| Concern | Owner | Everyone else |
|---|---|---|
| Bytes on disk, integrity, `data_snapshot_id` | `backtest/data` repository | MUST NOT re-derive |
| Row positioning and slicing (C-6.3) | repository | adapter MUST NOT re-implement |
| Frame construction, dtypes, column set, **ATR** | `HistoricalMarketDataAdapter` | analysis MUST NOT recompute ATR |
| Every other indicator | `v2/analysis` | adapter MUST NOT precompute |
| Sufficiency guards (D1 < 10, H1 < 50) | `v2/app.Orchestrator` | adapter MUST NOT apply them |
| M5 attachment guards | adapter (mirroring live) | orchestrator already passes `include_m5=True` unconditionally |

### C-6.9 Universe reconstruction and data scope (finding A58)

At each `T`: rank **every** listed USDT-M perpetual by **trailing 24-closed-H1
quote volume** (`Σ qv` over the 24 rows selected by C-6.3 for `tf="1h"`), take the
top `top_coins_limit` (default 30), and emit `SymbolVolume(symbol, volume)` in
descending order. Symbol filtering mirrors live: the name ends with `USDT` and
contains no `_` ([market_data.py:131-135](v2/infra/market_data.py#L131)); the
emitted symbol is `f"{name[:-4]}/USDT"`.

**Ranking eligibility.** A symbol participates at `T` only if it has **24 closed H1
rows** at `T` (a full trailing day). Symbols with fewer rows are excluded from the
ranking entirely — never ranked on a partial sum, which would systematically
under-rank new listings and make their entry into the universe depend on the
window's start date. They enter naturally once a full day exists.

**Tie-break (normative).** Live sorts the exchange's ticker list by volume with
Python's stable sort ([market_data.py:136](v2/infra/market_data.py#L136)), so equal
volumes retain the API's response order — which is not reproducible. Replay MUST
therefore sort candidates by **symbol name ascending first**, then apply the stable
descending-volume sort. Equal volumes are thus ordered by name and the universe is
a deterministic function of the data.

**Data scope requirement.** The candidate set MUST be all perpetuals in the
inventory, **not** the symbols that happen to rank today. Restricting the download
to today's top 30 bakes survivorship and look-ahead into every result permanently.
Consequently:

- **H1 + `qv`:** required for *all* perpetuals in the inventory.
- **D1 and M5:** required only for symbols that rank at least once in the window
  (plus M5 forward data through `max_holding_h1` candles past the last fill).

Approximation vs. live: live ranks by Binance's rolling 24 h ticker, which updates
continuously and includes the forming hour; the reconstruction is hourly and
closed-only. Declared bias (§13). Where the inventory is known to be incomplete
(symbols delisted before the archive was built), the manifest MUST declare
`survivorship_incomplete: true` and list the affected periods. A pinned
static-universe mode (`universe_mode: "static"`) exists for controlled experiments
and MUST declare its own bias.

### C-6.10 Integrity, corruption detection, `data_snapshot_id`

The repository has two tiers: an **immutable raw tier** (exactly what was
downloaded, plus a SHA-256 per artifact) and a **canonical tier** derived from it.
The canonical tier MUST be rebuildable from the raw tier by a versioned builder.

Per `(symbol, timeframe)` the builder MUST verify and record: row count; strictly
increasing `open_time`; every gap (start, end, missing count) written to a gap
inventory; `high >= max(open, close)`; `low <= min(open, close)`; `high >= low`; no
duplicate `open_time`; no `NaN` in OHLCV. A violation is a **hard error** — never a
silent repair.

```
data_snapshot_id = sha256( canonical_json({
    builder_version, symbol_inventory_sorted,
    { (symbol, tf): { rows, first_open_ms, last_open_ms, raw_sha256_list } }
}) )[:16]
```

It is stamped into every ledger row (C-16.1), every outcome record (C-9.4) and the
run manifest. Two runs with different `data_snapshot_id` are **never** comparable
without segmentation. Re-downloading a replayed period yields a new
`data_snapshot_id`; prior results remain valid for their own snapshot and are
simply not comparable.

### C-6.11 Warmup

The first replayable boundary `T_0` requires, *before* it: ≥ 500 H1 rows
(~21 days), ≥ 120 D1 rows (**the binding constraint: ~120 days**), ≥ 200 M5 rows,
plus M5 *forward* of any fill through `max_holding_h1` (+ slack) for outcome
resolution. Warmup is a **data** requirement, distinct from the lifecycle
**burn-in** of C-5.7; both apply.

---

## 7. HistoricalMarketDataAdapter — interface specification

A **duck-type of the live adapter**: the orchestrator calls exactly two methods
([orchestrator.py:175, 209](v2/app/orchestrator.py#L175)).

| Member | Contract |
|---|---|
| `set_time(T)` | Position the adapter at scan boundary `T` (C-5.1) and reset the per-cycle counters (C-8.3). Driver-only; the orchestrator never sees it. Strictly increasing (C-5.6.6). |
| `get_top_volume_symbols(limit)` | The reconstructed universe as of `T` (C-6.9), as `tuple[SymbolVolume, ...]` in descending volume order. Empty tuple when nothing qualifies. |
| `get_market_snapshot(symbol, *, include_m5)` | The C-6.6 snapshot as of `T`, or `None` per C-6.7. Raises `ReplayDataError` (a subclass of `MarketDataError`) **only** for repository corruption — never for ordinary missing data. |

### C-7.1 Invariants (each MUST have a test)

| # | Invariant |
|---|---|
| **I-1** | Columns exactly `(timestamp, open, high, low, close, v, atr)`, that order; `datetime64[ns]` + 6 × `float64`; fresh `RangeIndex` (C-6.4). |
| **I-2** | Row counts ≤ (119, 499, 199), selected per C-6.2/C-6.3; short history truncates, never pads. |
| **I-3** | `atr` computed after slicing with `length=14`; first 13 rows `NaN`. |
| **I-4** | `h1_last_ts == T − 3600`; `h1_last_ts_dt` tz-naive and equal to `h1["timestamp"].iloc[-1]`. |
| **I-5** | `live_price == float(h1["close"].iloc[-1])`. |
| **I-6** | M5 attached only when ≥ 19 closed rows exist **and** the hour-alignment guard passes (C-6.7). |
| **I-7** | Guard failure ⇒ `None`; corruption ⇒ `ReplayDataError`. Never any other exception type — the orchestrator would swallow it invisibly (finding A51). |
| **I-8** | Determinism: identical `(data_snapshot_id, T, symbol, include_m5)` ⇒ bit-identical frames, ATR included. No network, no wall clock, no mutation of repository state, no cache that can return a frame built for another `T`. |
| **I-9** | One-way data flow: the adapter never reads `active_pois.json`, the ledger, outcomes, or anything downstream. |
| **I-10** | Frames handed out are never mutated afterwards; each call returns freshly-sliced copies (`MarketSnapshot`'s documented immutability-by-contract). |

### C-7.2 Excluded responsibilities

No signal logic, no lifecycle logic, no outcome logic, no sufficiency guards
(C-6.8), no universe policy beyond C-6.9 — policy variants are constructor
parameters, never conditionals inside a method.

---

## 8. Replay Architecture — Orchestrator reuse

### C-8.1 The replay engine IS the production Orchestrator

Replay MUST drive `v2.app.Orchestrator` **unchanged**. Re-implementing the loop is
**prohibited**. The reason is enumerable: nine conventions exist only inside
`_process_symbol` / `_end_of_cycle`, and each silently changes results if
re-derived differently.

1. `kill_htf = context.htf if signal.direction is not None else None`
   ([orchestrator.py:229](v2/app/orchestrator.py#L229)) — finding A32; a pure
   convention documented nowhere else.
2. The order of the snapshot check and the `D1 < 10 / H1 < 50` guard
   ([orchestrator.py:210-213](v2/app/orchestrator.py#L210)).
3. `include_m5=True` always, with the score ≥ 8 gate living inside
   `V2Analyzer.analyze` ([engine.py:103](v2/analysis/engine.py#L103)) — Principle 12.
4. Ledger append **before** reconcile, so a completed analysis is recorded even if
   a later step fails ([orchestrator.py:225](v2/app/orchestrator.py#L225)).
5. OB-audit suppression on KILL/STALE, plus dedup-key insertion even when the
   append fails ([orchestrator.py:277-289](v2/app/orchestrator.py#L277)).
6. Alert dispatch strictly **after** `store.apply`
   ([orchestrator.py:237-240](v2/app/orchestrator.py#L237)).
7. End-of-cycle ordering (C-5.6.4).
8. Empty universe ⇒ no maintenance, no save (C-5.6.5).
9. Per-symbol error isolation ([orchestrator.py:242](v2/app/orchestrator.py#L242)).

### C-8.2 Dependency swap table

| Constructor slot | Live | Replay |
|---|---|---|
| `clock` | `SystemClock` | `FakeClock`, advanced 3600 s per cycle by the driver (C-5.2) |
| `market_data` | `MarketDataAdapter` (native Binance) | `HistoricalMarketDataAdapter` (§7) |
| `persistence` | `PersistenceAdapter(".")` | `ReplayPersistence` (C-15.6) |
| `notifier` | `TelegramNotifier` | `RecordingNotifier` (cross-check only, C-15.7) |
| `reconciler` | default `Reconciler(clock, config)` | `RecordingReconciler` wrapping a real `Reconciler` (C-15.2) |
| `analyzer` | `V2Analyzer()` | `V2Analyzer()`, optionally wrapped by `CachingAnalyzer` (C-8.4) |
| `writer` | `print` | capturing sink (validation channel, C-15.5) |
| `sleeper` | `time.sleep` | no-op |
| `config` | `LifecycleConfig()` | `LifecycleConfig()` — **defaults only** in the baseline; any change is a strategy experiment and is recorded in the manifest |
| `top_coins_limit` | 30 | 30 unless the experiment declares otherwise |

No other injection point exists, and replay code MUST NOT add one.

### C-8.3 The driver loop

`FakeClock` exposes only `advance(seconds)`
([clock.py:83](v2/infra/clock.py#L83)), so the seed is set at construction and
every step is a fixed advance:

```
clock = FakeClock(datetime.fromtimestamp(T_0 + 1, tz=timezone.utc))
orch  = Orchestrator(clock=clock, market_data=adapter, persistence=replay_persistence,
                     notifier=recording_notifier, analyzer=analyzer,
                     reconciler=RecordingReconciler(Reconciler(clock, config), recorder),
                     writer=capturing_writer, sleeper=lambda _s: None, config=config,
                     top_coins_limit=30,
                     source="replay", run_id=run_id, data_snapshot_id=snapshot_id)

for k, T in enumerate(window):        # C-5.1: strictly increasing, step 3600, no gaps
    if k > 0:
        clock.advance(3600.0)         # clock now reads T + 1
    adapter.set_time(T)               # also resets the adapter's per-cycle counters
    recorder.begin_cycle(k, T)        # resets `seq`; brackets the writer capture
    processed = orch.run_cycle()      # public API, orchestrator.py:167
    recorder.end_cycle(processed)     # flushes the cycle's buffered events
    check_health(k, adapter.cycle_counters(), recorder)   # C-15.8; raises on breach
```

Rules:

- `run_forever` MUST NOT be used.
- The driver MUST NOT call `orch.startup()`: `run_cycle` performs the lazy startup
  on its first call ([orchestrator.py:171-172](v2/app/orchestrator.py#L171)), and a
  second call would rebuild the store and discard lifecycle memory.
- The driver MUST NOT touch `orch.store` except read-only, and MUST NOT reach into
  any private attribute.
- `cycle_index` is the loop index `k`, assigned to **every** boundary including
  empty-universe ones (C-5.6.5).
- The driver owns iteration, the cycle index, the manifest and the health checks —
  nothing else. Per-run outputs are listed in C-16.4.

**Adapter per-cycle counters (normative).** `set_time(T)` resets, and the adapter
accumulates during the cycle: `universe_size`; `universe_error` (the exception type
name if `get_top_volume_symbols` raised, else `None`); `snapshot_none_skips`;
`guard_skips` (snapshots served with closed D1 < 10 or closed H1 < 50 — the adapter
knows the row counts even though the *guard* belongs to the orchestrator, C-6.8);
`m5_attached`; `m5_guard_failures`. The driver reads them after `run_cycle`.

**Universe-level failure.** `run_cycle` swallows `MarketDataError` from the universe
call and returns `False` ([orchestrator.py:176-180](v2/app/orchestrator.py#L176)),
so a corrupt repository would look like an empty market. The driver MUST fail the
run if any cycle reports a non-`None` `universe_error`, and MUST record every
genuinely-empty universe in `run_health.jsonl`.

### C-8.4 Analysis memoization (permitted, behavior-neutral)

The orchestrator evaluates the pipeline twice per symbol
([orchestrator.py:216-219](v2/app/orchestrator.py#L216) — finding A35): once for
the observability/kill context, once inside `analyze`. Both are pure functions of
`(snapshot, session_active)`.

A `CachingAnalyzer` memoizing the analysis pipeline is therefore **semantically
transparent** and permitted as a dependency swap. Measured effect: ~214 ms →
~107 ms per symbol-scan (implementation measured 207.82 ms → 106.86 ms on the
top-30 universe at 2024-07-01).

**Amendment, approved 2026-08-03 — the key.** This clause originally specified
`(symbol, h1_last_ts, session_active)`. That key is **unsafe** and MUST NOT be
used: it under-determines the snapshot. During an H1 gap `h1_last_ts` freezes for
hours while `d1`, `m5` and `live_price` advance, and the Stage 2 corpus holds
6,204 gap hours across 52 symbols. Two cycles with genuinely different snapshots
therefore collide on it and the cache serves a Signal built for another `T` —
violating I-8 silently. Step 4's T4.1 control demonstrates the collision on a
corpus-derived fixture (`DYDX/USDT` across the 2023-03-15T12:00Z gap): the
original key collides on all three cycles and returns a wrong Signal.

The **required** key is every snapshot component analysis can read:

```
(data_snapshot_id, symbol, h1_last_ts, len(h1), d1_last_ts, len(d1),
 m5_last_ts, len(m5), bit-pattern of live_price, session_active)
```

`live_price` enters as its IEEE **bit pattern**, not its value, so `0.0` and
`-0.0` stay distinct and NaN compares equal to itself — neither of which `==`
provides. `MarketSnapshot` cannot serve as its own key: `d1`/`h1`/`m5` are
declared `field(compare=False)` ([market.py:39-44](v2/domain/market.py#L39)), so
the determinant is built explicitly.

The memo wraps **`AnalysisPipeline`**, not `analyze`. The orchestrator calls both
seams ([orchestrator.py:265-268](v2/app/orchestrator.py#L265)), so memoizing
`analyze` alone would leave the first evaluation unpaid; wrapping the pipeline
puts the entry underneath both. It also keeps V1's M5 re-score gate (`score >= 8`
and an M5 frame present) inside `v2/`, executed every call, rather than
reimplemented in `backtest/` where it could drift.

Requirements: keyed exactly as above; keyed per `data_snapshot_id` (satisfied by
its presence in the key); the `prior` argument bypasses the cache; covered by a
test asserting cached and uncached runs produce byte-identical output bundles.
Verified: a 169-cycle `corpus-pilot` replay, all 9 bundle files identical, and
the same result at 30,768 cycles in Shakedown B. The flag is
`ReplayConfig.caching_analyzer` / `--caching`, **default off**, and is
deliberately **absent from C-16.2 `core`** — the claim being made is that
memoization cannot change a result, so two runs differing only in it must share a
`run_id`; a comparison therefore requires different `output_root`s.

### C-8.5 Performance envelope (measured, informative)

Measured on the development machine with realistic frames (119/499/199):
`pipeline.run + analyze` = **213.7 ms** per symbol-scan; `analyze` alone 106.4 ms,
of which ~90 % is `identify_swings`
([structure.py:98-125](v2/analysis/structure.py#L98) — scalar `.iloc` access in a
Python loop); ATR 0.64 ms; row slice 0.05 ms.

Implication: 30 symbols × 5 years = 1,314,000 symbol-scans ≈ **78 h** as-is, ≈ 39 h
with C-8.4, ≈ 6–8 h if `identify_swings` is vectorized. Vectorization is a
**separate, optional milestone**, permitted only behind a golden-master test proving
bit-identical Signals, and `atr_mean` MUST keep using `df["atr"].dropna().mean()`
(pandas' summation order) rather than a numpy equivalent. This clause is
informative — no result depends on it — but a design that ignores it produces an
evaluation loop too slow for Phases 9–10.

---

## 9. Outcome Engine

### C-9.1 Position in the pipeline

A separate **offline** stage in `backtest/outcome/`. Nothing in `v2/` knows it
exists (§10).

Inputs:

- **Replay data:** the capture bundle of C-16.4.
- **Live data:** the signal ledger plus a deterministic **lifecycle
  reconstruction** — replay the ledger rows through `Reconciler` + `SignalStore`
  with a `FakeClock` driven by `recorded_at`, grouped into cycles by
  `(run_id, cycle_index)` and ordered within a cycle by `universe_rank` (both new
  in schema 2 — C-16.1). Required because live records no durable
  alert/insertion stream (finding A46).
- The kline repository (forward H1 + M5).

**Reconstruction sufficiency (normative).** A ledger row rehydrates into a `Signal`
sufficient for `Reconciler.reconcile`, because the reconciler reads only:

| Reconciler input | Ledger source |
|---|---|
| `signal.symbol / decision / score / direction / regime / reason / entry_mode` | direct fields |
| candidate presence | `atr_last is None ⇔ candidate is None` (C-1.1) |
| `candidate.atr_last`, `candidate.volume_spike` | `atr_last`, `volume_spike` |
| `candidate.entry.{entry_mode, entry_price, entry_ready, entry_source}` | `entry_mode`, `entry_price`, `entry_ready`, `entry_source` |
| `candidate.risk.{sl, tp, tp_source}` | `sl`, `tp`, `tp_source` |
| `htf.d1_bias`, `htf.h4_bias` (kill predicate) | `d1_bias`, `h4_bias` |
| `live_price`, `h1_last_ts` | direct fields |
| alert cosmetics via `_htf_pairs` ([reconciler.py:161-179](v2/lifecycle/reconciler.py#L161)) | `weekly_bias`, `location`, `direction_source`, `htf_bull`, `htf_bear`, `anchor_override` |

Fields the constructors require but lifecycle **never reads** —
`HTFContext.{pdh, pdl, pwh, pwl, daily_eq, weekly_eq, current_price}`, the
structure tuples, `EntryPlan.entry_reason` — are filled with neutral placeholders
(`0.0`, `()`, `""`). The single genuinely unreconstructible field,
`HTFContext.fallback_h1_bias`, appears **only** in the alert's cosmetic `htf`
payload and is never consumed by the Outcome Engine; the rehydrator sets it to
`None` and the manifest declares `htf_fallback_reconstructed: false`.
`Signal.__post_init__` consistency (`direction`, `regime`, `entry_mode` must match
the candidate — [signal.py:100-122](v2/domain/signal.py#L100)) is satisfied by
construction.

### C-9.2 Trade building

The captured `LifecycleDecision` stream (§15) → trades per §2: an
`InsertMutation`/`ReplaceMutation` carrying a `new`/`upgrade` Alert instantiates a
trade; an unalerted insert instantiates an `UNALERTED_INSERT` shadow; CAPPED and
KILL_COOLDOWN decisions instantiate fixed-window shadows (C-2.2); evictions and
supersessions close pending trades per C-4.8.

### C-9.3 Price walk

Per C-4.0 – C-4.5, C-4.13, C-4.14: H1 walk from `W0` with M5 sub-resolution and
worst-case tie-breaks.

### C-9.4 `OutcomeRecord` (own file, own `schema_version`)

One line per trade:

- **identity:** `run_id`, `source`, `thesis_key`, `symbol`, `direction`,
  `entry_mode`, instantiation `T`, `h1_last_ts`, `cycle_index`, `universe_rank`,
  `alert_tier`, `shadow_kind` (`null` for the alert population — C-2.2),
  `in_burn_in`;
- **frozen levels:** `entry_price`, `sl`, `tp`, `structural_rr`, `tp_source`,
  `entry_source`, `score`;
- **fill:** `filled`, `fill_kind`, `fill_T`, `fill_price`, `candles_to_fill`;
- **exit:** `state`, `cancel_reason`, `exit_reason`, `exit_T`, `exit_price`,
  `realized_r`, `realized_r_net`, `holding_candles`;
- **path:** `mae_r`, `mfe_r`, `excursion_degraded`;
- **lineage:** `contract_version`, `strategy_version`, `ledger_schema_version`,
  `outcome_schema_version`, `outcome_config_hash`, `data_snapshot_id`, `env`.

**Canonical ledger row (resolves finding A34).** A trade joins to exactly one ledger
row: `(run_id, symbol, h1_last_ts)` — the row of the scan that instantiated it. At
replay cadence this is unique by construction (one row per symbol per cycle, one
cycle per `h1_last_ts`). For live data, `(run_id, cycle_index, symbol)`
disambiguates the multiple rows sharing an `h1_last_ts`, and the canonical row is
the one whose cycle produced the alert-tier transition.

### C-9.5 One-way derivation

Outcomes MUST NOT be written into the signal ledger, and no runtime module may read
them. The ledger records *decisions*; outcomes are a re-derivable projection that
can be regenerated whenever the labeling rules improve.

The Outcome Engine reads the kline repository **directly**, by time range — never
through `HistoricalMarketDataAdapter`. The adapter's 119/499/199 limits and its ATR
column are analysis concerns with no meaning for a forward price walk.

### C-9.6 R arithmetic (normative)

Two engineers would otherwise choose different denominators.

```
structural_rr = |tp - entry_price| / |entry_price - sl|   # decision-time, frozen levels
R_unit        = |fill_price - sl|                         # realized risk, from the actual fill
realized_r    = (exit_price - fill_price) / R_unit        # LONG
              = (fill_price - exit_price) / R_unit        # SHORT
```

- `R_unit` uses the **fill price**, not `entry_price`. A CONTINUATION trade filled
  at `open(W0)` genuinely risks `|fill − sl|`; using the structural entry would
  report a risk the operator never had.
- `R_unit == 0` or non-finite ⇒ terminal `INVALID_LEVELS` (C-4.14).
- `structural_rr` is recorded alongside `realized_r` precisely so the gap between
  advertised and achievable RR is measurable (§12).
- `mae_r` (≤ 0) and `mfe_r` (≥ 0) are the worst adverse and best favourable
  excursions between the fill and the exit **inclusive**, divided by `R_unit`,
  measured from M5 extremes. For any H1 candle whose M5 set is incomplete (C-4.3)
  that candle's own `high`/`low` are used and the record is flagged
  `excursion_degraded = true`.
- Fees, when enabled (C-4.12), give
  `realized_r_net = realized_r − 2·taker_fee_rate·fill_price / R_unit`; the gross
  value is always also recorded.

### C-9.7 `OutcomeConfig` (canonical field list)

Its hash segments every dataset, so the field set is fixed by contract. Adding a
field is an amendment.

| Field | Default | Meaning |
|---|---|---|
| `max_holding_h1` | `168` | expiry cap in walk candles (C-4.10) |
| `continuation_fill` | `"w0_open"` | `"w0_open"` \| `"w0_close"` (C-4.2) |
| `limit_fill_rule` | `"touch"` | fixed; the only implemented rule (C-4.1) |
| `tie_break` | `"sl_first"` | fixed; worst case (C-4.3) |
| `no_m5_policy` | `"worst_case"` | fixed (C-4.3 rule 4) |
| `taker_fee_rate` | `0.0` | per side (C-4.12) |
| `slippage_rate` | `0.0` | market-fill slippage per side |
| `funding_rate_model` | `"none"` | fixed; documented omission |
| `shadow_pending_h1` | `3` | fixed window for suppressed shadow classes (C-2.2) |
| `burn_in_h1` | `336` | lifecycle burn-in (C-5.7) |
| `equity_convention` | `"1R"` | per-trade unit for drawdown (C-9.8) |

```
outcome_config_hash = sha256(canonical_json(config))[:16]
canonical_json(x)   = json.dumps(x, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False)
```

The same `canonical_json` is used for `run_id` (C-16.2) and `data_snapshot_id`
(C-6.10). Python's float `repr` round-trips exactly, so numeric values hash stably.

### C-9.8 Equity curve and drawdown ordering

Max drawdown is path-dependent, so the trade order is fixed: trades are ordered by
`(exit_T, cycle_index, universe_rank, symbol)` — the first three taken from the
trade's *exit* event, the last as a final tie-break — and each contributes
`realized_r` to a running sum starting at 0. Unfilled and `OPEN` trades contribute
nothing. Max drawdown is the largest peak-to-trough decline of that sum, in R.

---

## 10. Backtester Architecture

```
backtest/                      top-level package; imports v2 freely.
│                              v2 NEVER imports backtest (mirror of Rule A8).
├─ data/        repository: download (raw tier), build (canonical tier),
│               integrity + gap inventory, data_snapshot_id, universe table
├─ adapter.py   HistoricalMarketDataAdapter (§7)
├─ capture.py   ReplayPersistence, RecordingReconciler, RecordingNotifier,
│               CaptureRecorder, memory-snapshot differ (§15)
├─ driver.py    the C-8.3 loop + run manifest + health checks
├─ rehydrate.py ledger row → Signal (C-9.1), live data only
├─ outcome/     trade builder (C-9.2), price walker (C-9.3), OutcomeConfig,
│               OutcomeRecord writer
├─ metrics/     aggregation + segmentation (§12), report emitter
└─ cli.py       entry points (replay window, universe mode, config)
tests/backtest/ hermetic tests (synthetic repositories, no network)
```

Dependency direction: `cli → driver → {adapter, capture} → v2`;
`outcome, metrics → data + v2.domain`. Replay work never modifies the production
tree, with exactly two sanctioned exceptions: the additive ledger fields of C-16.3.1
and the optional performance work of C-8.5.

---

## 11. Evaluation Workflow

1. **Signal Ledger** — every completed analysis, live and replay, one schema (§16).
2. **Replay** — regenerate history at C-5 cadence; emit the capture bundle.
3. **Outcome Engine** — trades and labels per §9 (fully automated; the only human
   inputs are the frozen `OutcomeConfig` choices).
4. **Labeled dataset** — ledger ⋈ outcomes on the canonical row (C-9.4).
5. **Statistics** — §12 metrics; **freeze the baseline** (git tag + committed
   `metrics.json` + manifest).
6. **Experiment** — one change, pre-registered hypothesis and success criterion in
   the `STRATEGY_NOTES.md` Experiment Log; `strategy_version` bumped.
7. **Backtest** — the change replayed over the training window against the frozen
   baseline: same `data_snapshot_id`, same window, same burn-in (C-5.7).
8. **Walk-forward** — rolling out-of-sample windows plus a permanent holdout,
   declared before the first experiment and never optimized against.
9. **Production** — promote only after walk-forward and holdout pass; forward
   testing (Phase 11) confirms live.

---

## 12. Metrics Inventory

**Population:** trade count; fill rate overall and by `fill_kind` (including the
`IMMEDIATE` share, C-4.1); `NOT_FILLED` breakdown by `cancel_reason`;
`INVALID_LEVELS` count (C-4.14); alert vs. each `shadow_kind` (C-2.2); burn-in
exclusions.

**Performance:** win rate, loss rate, BE bucket, expectancy (mean `realized_r`),
median R, R distribution/histogram, profit factor, payoff ratio, gross vs. net (fee
sensitivity), kill-exit rate and its mean R, expiry rate and mark-to-market R,
`structural_rr` vs. realized R gap.

**Risk/path:** max drawdown and the drawdown curve (C-9.8), max consecutive losses,
MAE/MFE distributions (these directly power the SL/TP research A13/A14),
time-to-fill, time-in-trade.

**Data health (mandatory in every report):** universe coverage per cycle (ledger
rows vs. universe size), swallowed-error counts (C-15.8), adapter `None`-skips,
guard skips, `m5_gap_resolutions` (C-4.3), `m5_guard_failures`,
`excursion_degraded` share, repository gap count inside the window.

**Statistical reproducibility (normative).** Bootstrap confidence intervals are the
only stochastic step in the whole pipeline, so their parameters are fixed by
contract: **percentile method, `n_resamples = 10_000`, `numpy.random.default_rng`
seeded with `bootstrap_seed` (default `20260727`)**, resampling trades with
replacement within the segment, reporting the 2.5 / 97.5 percentiles. The seed is
recorded in `metrics.json`. Segments with `n < min_segment_n` (default **30**) are
printed with their `n` but flagged `inferential: false` and MUST NOT be used to
justify a change. A multiple-comparison caution is mandatory whenever more than 20
segments are reported. `scipy` is **not** available and MUST NOT be introduced —
the bootstrap is a `numpy` reduction.

**Segmentations** (each metric × each dimension, with per-segment `n` and bootstrap
CIs): decision tier · score bands · `entry_mode` · direction · regime ·
weekly/D1/H4 bias combinations · `location` · `session_active` · weekday ·
hour-of-day · symbol · `universe_rank` bands · every `notes` token (per-token lift)
· sweep / fvg / displacement / fresh_ob / volume_spike / target_clear /
htf_context_clean (true vs. false) · `entry_source` · `tp_source` ·
structural-RR bands · `tp_rr` bands · selected-OB rank / freshness / candidate count
· `sweep_reclaim_strength` bands · ATR-relative volatility bands ·
`htf_bull − htf_bear` margin · `m5_choch` · window-truncation flag (C-16.1) ·
`strategy_version` · `contract_version` · `outcome_config_hash` ·
`data_snapshot_id`.

---

## 13. Declared Biases and Divergences

Uniform across experiments, therefore comparison-safe; each is a revisit candidate,
not a hidden assumption.

1. **Touch-fill optimism** (C-4.1) — queue position is not modeled.
2. **Worst-case tie-break pessimism** (C-4.3), including the no-M5 fallback.
3. **Continuation fill at the walk origin** (C-4.2) — assumes execution at the
   top-of-hour print, ~1 s before the alert existed; `w0_close` is the pessimistic
   sensitivity.
4. **Close-cadence kill timing** (C-5.5.1) and **close-based `live_price`**
   (C-5.5.3) — fewer wick-kills than live.
5. **Replay is more M5-complete than live** (C-5.5.2) — slightly more upgrades.
6. **Universe reconstruction is hourly and closed-only** (C-6.9), with survivorship
   declared where the inventory is incomplete.
7. **Zero-cost baseline** with fee sensitivity (C-4.12); **funding omitted**.
8. **Single position per thesis; 1R equity convention** (no sizing exists — A25).
9. **Forming-candle assumption** (finding A57): live silently analyzes a
   one-hour-stale frame if Binance returns no forming candle (no trades in the
   interval). Replay always constructs the ideal window. Immaterial at top-30
   liquidity; declared for completeness.
10. **Lifecycle burn-in dependence** (C-5.7).

---

## 14. Non-blocking Research Questions

Recorded so they are not mistaken for gaps in this contract. None blocks
implementation.

1. Queue-realistic limit fills (replacing the touch-fill assumption).
2. Funding costs for long holds, if `EXPIRED` rates prove material.
3. Cadence-sensitivity study: replay at M5 cadence vs. C-5.1 on a sample window, to
   bound fidelity loss C-5.5.1 empirically.
4. Break-even / trailing-stop modeling — only if A13/A14 experiments motivate it.
5. Portfolio-level metrics once position sizing exists (A25).
6. Persistent cross-run `Signal` cache (C-8.4 extended to disk), which would make
   lifecycle-only experiments nearly free.
7. First-class lifecycle-event emission in production (decision R-7's rejected
   alternative), if live ever needs the observability replay has.

---

## 15. Lifecycle Capture Architecture — **resolves B3**

**Goal:** every lifecycle transition becomes observable, with zero change to
production behavior.

### C-15.1 What must become observable

The complete set of lifecycle-relevant emissions in one cycle, with producers:

| # | Transition / event | Produced by | Carries |
|---|---|---|---|
| 1 | KILL (evict + optional kill Alert + `KilledThesisRecord`) | `Reconciler.reconcile` ([reconciler.py:228-262](v2/lifecycle/reconciler.py#L228)) | `EvictMutation("kill")`, `LogEvent("KILL")`, maybe `Alert(kind="kill")` |
| 2 | STALE (no state change) | `Reconciler.reconcile` ([reconciler.py:269-279](v2/lifecycle/reconciler.py#L269)) | `LogEvent("STALE")` |
| 3 | KILL_COOLDOWN suppression | `Reconciler._actionable` ([reconciler.py:361-376](v2/lifecycle/reconciler.py#L361)) | `LogEvent("KILL_COOLDOWN")` |
| 4 | SAME_THESIS partial refresh | `Reconciler._actionable` ([reconciler.py:378-413](v2/lifecycle/reconciler.py#L378)) | `PartialRefreshMutation`, `LogEvent("SAME_THESIS")` |
| 5 | Insert (+ `new`/`upgrade` Alert) | `Reconciler._actionable` ([reconciler.py:541-557](v2/lifecycle/reconciler.py#L541)) | `InsertMutation`, `Alert`, `LogEvent("ALERTED")` |
| 6 | REPLACE (evict weakest + insert) | `Reconciler._actionable` ([reconciler.py:511-550](v2/lifecycle/reconciler.py#L511)) | `ReplaceMutation`, `LogEvent("REPLACE")` |
| 7 | CAPPED suppression | `Reconciler._actionable` ([reconciler.py:528-536](v2/lifecycle/reconciler.py#L528)) | `LogEvent("CAPPED")` |
| 8 | GUARD (invalid levels) | `Reconciler.reconcile` ([reconciler.py:296-313](v2/lifecycle/reconciler.py#L296)) | `LogEvent("GUARD")` + miss/decay |
| 9 | Miss increment / DECAY eviction | `Reconciler._decay_miss` ([reconciler.py:561-582](v2/lifecycle/reconciler.py#L561)) | `IncrementMissMutation`, `LogEvent("DECAY")`, `EvictMutation("decay")` |
| 10 | **TTL eviction** | `SignalStore.ttl_sweep` ([store.py:132-153](v2/lifecycle/store.py#L132)) | `EvictMutation("ttl")`, `LogEvent("TTL")` |
| 11 | Killed-thesis prune | `SignalStore.prune_killed_theses` ([store.py:155-165](v2/lifecycle/store.py#L155)) | nothing (silent by design) |
| 12 | Symbol not analyzed | adapter `None`, sufficiency guard, or swallowed exception ([orchestrator.py:209-243](v2/app/orchestrator.py#L209)) | nothing |

Items 1–9 come from the **injectable** `Reconciler` and are captured directly
(C-15.2). Item 10 comes from the **non-injectable** `SignalStore` and is attributed
by C-15.5. Item 11 changes no trade state and needs no capture — it only removes
expired cooldown entries, and a cooldown that mattered already appears as item 3.
Item 12 is attributed by C-15.8.

### C-15.2 `RecordingReconciler`

```
class RecordingReconciler:            # backtest/capture.py
    def __init__(self, inner: Reconciler, recorder: CaptureRecorder): ...
    def reconcile(self, signal, memory, *, htf, live_price, h1_last_ts):
        decision = self.inner.reconcile(signal, memory,
                                        htf=htf, live_price=live_price,
                                        h1_last_ts=h1_last_ts)
        self.recorder.record_decision(signal.symbol, h1_last_ts, decision,
                                      memory_size=len(memory))
        return decision
```

It is passed through the **existing** `reconciler=` constructor slot
([orchestrator.py:102](v2/app/orchestrator.py#L102)). It adds no branch, mutates
nothing, and returns the inner decision unchanged, so production behavior is
provably identical. It MUST record *before* returning, so a later exception in
rendering or dispatch cannot lose the decision.

### C-15.2.1 `CaptureRecorder` owns every output file

Single-owner rule, so two components can never disagree about a run's contents:

- `CaptureRecorder` is the **sole writer** of every file in C-16.4 except
  `manifest.json` and `run_health.jsonl` (driver-owned) and `outcomes.jsonl` /
  `metrics.*` (later stages).
- `RecordingReconciler`, `ReplayPersistence`, `RecordingNotifier` and the capturing
  `writer` are thin forwarders: they call `record_decision`, `record_ledger_row`,
  `record_memory_snapshot`, `record_alert`, `record_writer_line`, and hold no files
  of their own.
- Events are **buffered in memory for one cycle** and flushed by `end_cycle`.
  Buffering is required, not optional: `seq` assignment, the `universe_rank` lookup
  and the TTL residue of C-15.5 all need cycle-complete information (the universe
  and the end-of-cycle snapshot) that exists only after `run_cycle` returns. Buffer
  size is bounded by `top_coins_limit`.
- `universe_rank` is resolved at flush time from the universe the adapter recorded
  for that cycle (C-8.3) — **not** from the order of `record_decision` calls, which
  omits symbols skipped before reconcile.

### C-15.3 Lifecycle event schema (`lifecycle_events.jsonl`)

One JSON line per emitted event, flattened from the decision:

```
run_id            str      C-16.2
source            str      "replay" | "live"
cycle_index       int      monotonic within the run
scan_T            int      the boundary T (epoch seconds)
h1_last_ts        float    T - 3600
seq               int      0-based, monotonic within the cycle across all symbols
symbol            str
universe_rank     int|null 1-based rank in this cycle's universe; null for
                           end-of-cycle events
phase             str      "symbol" | "end_of_cycle"
kind              str      "MUTATION" | "LOG" | "ALERT" | "KILLED_THESIS"
mutation_type     str|null InsertMutation | ReplaceMutation |
                           PartialRefreshMutation | IncrementMissMutation |
                           EvictMutation
mutation_payload  obj|null the mutation dataclass as a dict, verbatim, with any
                           nested ActiveSignal rendered via to_v1_dict(); this is
                           the complete record and the two fields below are
                           convenience projections of it
evict_reason      str|null "kill" | "decay" | "ttl" | "replace"
target_symbol     str|null ReplaceMutation's evicted symbol
log_event_type    str|null KILL|STALE|GUARD|DECAY|TTL|REPLACE|CAPPED|ALERTED|
                           SAME_THESIS|KILL_COOLDOWN
log_details       obj|null the LogEvent.details pairs as an object
alert_kind        str|null "new" | "upgrade" | "kill"
alert_tier        str|null the Alert.dedup_key (current tier)
alert_payload     obj|null the full Alert as a dict; its `htf` tuple-of-pairs is
                           serialized as a list of [name, value] lists
attribution       str      "reconciler" | "inferred_ttl"   (C-15.5)
schema_version    int      1
```

Ordering key: `(run_id, cycle_index, seq)`. `seq` is assigned by the recorder in
emission order — `universe_rank` order for `phase="symbol"`, then end-of-cycle
events last (C-5.6.3/4).

One `LifecycleDecision` expands to one line per mutation, per log event, per alert
and per killed-thesis record, in that order — mirroring the tuple order of
`LifecycleDecision` ([lifecycle.py:171-184](v2/domain/lifecycle.py#L171)). A
decision with no content (a legitimate outcome, e.g. a SKIP for a symbol with no
memory) produces **one** line with `kind="LOG"`, `log_event_type=null` and
`mutation_type=null`, so "reconcile ran and decided nothing" stays distinguishable
from "reconcile never ran" — required by C-15.8.

### C-15.4 Memory snapshots

`ReplayPersistence.save_active_signals` is called exactly once per processed cycle,
**after** the TTL sweep ([orchestrator.py:333](v2/app/orchestrator.py#L333)). Each
call is recorded as one `memory_snapshots.jsonl` line:
`{run_id, source, cycle_index, scan_T, active: {symbol: to_v1_dict()},
killed: {thesis_key: killed_ts}}`. Cycles with an empty universe produce **no**
snapshot (C-5.6.5); a reader MUST treat a missing snapshot as "unchanged".

### C-15.5 TTL attribution and its completeness proof

TTL evictions come from the store, which is not injectable. They are attributed by
**elimination**, and the attribution is *complete* — not heuristic.

**Lemma 1.** A symbol can leave `SignalStore._active` only through
`_apply_mutation` ([store.py:93-128](v2/lifecycle/store.py#L93)). Exhaustively, that
method removes a symbol in exactly two branches: `ReplaceMutation` (pops
`target_symbol`) and `EvictMutation` (pops `symbol`). No other branch and no other
method removes a key — `view`, `active` and `killed_theses` return copies, and
`prune_killed_theses` touches only `_killed`.

**Lemma 2.** `EvictMutation` instances originate in exactly four places: KILL
([reconciler.py:259](v2/lifecycle/reconciler.py#L259)), DECAY
([reconciler.py:582](v2/lifecycle/reconciler.py#L582)), TTL
([store.py:152](v2/lifecycle/store.py#L152)), and — as `ReplaceMutation` — REPLACE
([reconciler.py:550](v2/lifecycle/reconciler.py#L550)). Three of the four travel in
a `LifecycleDecision` returned by `Reconciler.reconcile` and are therefore captured
verbatim by C-15.2.

**Therefore:** for cycle `k`, let `R_k = keys(snapshot_{k−1}) \ keys(snapshot_k)`
(C-15.4) and let `A_k` be the symbols removed by a captured reconciler mutation in
cycle `k` (an `EvictMutation` with reason `kill` or `decay`, or a
`ReplaceMutation`'s `target_symbol`). Then

```
TTL_k  =  R_k \ A_k
```

Every element of `TTL_k` is emitted as a `lifecycle_events` line with
`attribution="inferred_ttl"`, `mutation_type="EvictMutation"`,
`evict_reason="ttl"`, `phase="end_of_cycle"`.

No third term is needed: a symbol popped **and** re-inserted within cycle `k`
(possible when a REPLACE targets a symbol processed later in the same cycle, which
then inserts itself) is present in both snapshots and therefore never in `R_k`,
while its cancellation is still attributed from the captured `ReplaceMutation` in
`A_k`.

**Edge cases, all closed:**

- *A symbol inserted and TTL'd in the same cycle?* Impossible: insertion sets
  `timestamp = now` ([reconciler.py:489](v2/lifecycle/reconciler.py#L489)) and the
  sweep requires `now − timestamp > ttl` with `ttl ≥ 6 h`
  ([store.py:143-144](v2/lifecycle/store.py#L143)); the clock does not move within
  a cycle (C-5.2), so the age is exactly 0.
- *A symbol both decay-evicted and TTL'd?* The decay eviction removes it before the
  sweep runs, so the sweep cannot see it; it is in `A_k` and excluded.
- *A REPLACE target that was also over TTL?* It is in `A_k` and attributed to
  REPLACE — matching production, where the pop happened first.
- *An empty-universe cycle?* No snapshot, no reconciler events, no sweep
  (C-5.6.5); the differ skips the cycle.
- *The first cycle?* `snapshot_{−1}` is the startup memory, which in replay is
  empty (C-5.7.3).

**Mandatory cross-check.** The captured `writer` sink (C-8.2) receives one rendered
`[⏰ TTL EXPIRED]` line per TTL eviction
([rendering.py:252-259](v2/app/rendering.py#L252)). The driver MUST assert
`|TTL_k| == count of TTL lines in cycle k`. The writer is a **validation channel
only** — never a data source, because rendered text is not a schema. A mismatch
fails the run.

### C-15.6 `ReplayPersistence`

Implements exactly the six methods the orchestrator uses
([persistence.py](v2/infra/persistence.py)):
`load_active_signals(*, default_timestamp)`, `load_killed_theses`,
`save_active_signals`, `save_killed_theses`, `append_ob_audit`,
`append_signal_ledger`. Behavior:

- loads return `{}`, `{}` (empty lifecycle memory — C-5.7.3);
- `save_*` forward the C-15.4 snapshot to the recorder; they MUST NOT touch
  `active_pois.json` or `killed_thesis.json` — production live state is never
  written by a replay;
- `append_ob_audit` and `append_signal_ledger` forward to run-scoped sinks
  (`runs/{run_id}/ob_audit.jsonl`, `runs/{run_id}/signal_ledger.jsonl(.gz)`);
- failures raise `PersistenceError`, the type the orchestrator catches
  ([orchestrator.py:307](v2/app/orchestrator.py#L307)).

**OB-audit dedup note.** `Orchestrator._ob_audit_seen`
([orchestrator.py:125](v2/app/orchestrator.py#L125)) lives for the life of the
instance — one whole replay run rather than one live process. This is behaviorally
inert: the key is `(symbol, h1_last_ts, "RETRACEMENT")` and replay produces exactly
one scan per `h1_last_ts`, so the set never suppresses anything (in live it
suppresses the other ~59 scans of the same hour, which is why the audit is
one-line-per-candle in both). The set does grow for the run's duration (one tuple
per eligible retracement — on the order of 10⁵ over five years, needing no
intervention), and an implementation MUST NOT "optimize" it by clearing it mid-run,
which would change the audit file.

### C-15.7 `RecordingNotifier` (cross-check only)

Implements `Notifier.send(alert)`
([notifier.py:303](v2/infra/notifier.py#L303)) and forwards each `Alert` with the
current `(run_id, cycle_index, seq)`. It never raises. Its content MUST equal the
`kind="ALERT"` events derived from captured decisions, and the driver asserts this.
`Alert` carries no time or symbol-cycle identity of its own, and dispatch happens
after two intermediate steps that could raise, so the **decision stream remains the
source of truth** for trade instantiation (C-2.1).

### C-15.8 Coverage and failure visibility (finding A51)

`_process_symbol` swallows every exception with no output
([orchestrator.py:242-243](v2/app/orchestrator.py#L242)). A systematically broken
adapter would therefore yield an empty dataset that reads as "the strategy found
nothing". Replay MUST make that impossible by reconciling counts per cycle:

```
universe_size          = the size of the universe the adapter served
adapter_none_skips     = symbols for which get_market_snapshot returned None
guard_skips            = snapshots with closed D1 < 10 or closed H1 < 50
ledger_rows            = rows appended this cycle
decisions              = symbols with a captured LifecycleDecision

expected_analyzed        = universe_size - adapter_none_skips - guard_skips
swallowed_before_ledger  = expected_analyzed - ledger_rows
swallowed_after_ledger   = ledger_rows       - decisions
```

Both `swallowed_*` values MUST be 0 in a healthy run. The driver records all of
these per cycle in `run_health.jsonl` (one line per cycle:
`{run_id, cycle_index, scan_T, processed, universe_size, universe_error,
adapter_none_skips, guard_skips, ledger_rows, decisions, ttl_inferred,
ttl_log_lines, m5_attached, m5_guard_failures}`), the metrics report surfaces the
totals (§12), and the run **fails** if either `swallowed_*` exceeds
`max_swallowed_errors` (default 0). This is why I-7 forbids the adapter from raising
unexpected exception types.

---

## 16. Signal Ledger `schema_version` 2 — **resolves B4**

### C-16.1 The new fields

`SignalLedgerRecord` ([v2/domain/signal_ledger.py](v2/domain/signal_ledger.py))
gains **nine** fields. All are additive with defaults, no existing field changes
meaning, and `LEDGER_SCHEMA_VERSION` becomes `2`.

| Field | Type / default | Owner | Why it must exist |
|---|---|---|---|
| `source` | `str = "live"` | `app.ledger` (orchestrator constructor arg) | Live and replay rows are otherwise **structurally indistinguishable**. A merged file, a mis-pointed `base_dir` or a stray copy silently corrupts the permanent dataset. The default is `"live"` precisely because only live could have written a schema-1 row. |
| `run_id` | `Optional[str] = None` | `app.ledger` (constructor arg) | Identifies one execution. Without it, rows from two replay runs over the same window are indistinguishable and no result is reproducible. |
| `cycle_index` | `Optional[int] = None` | `app.ledger` (orchestrator counts cycles) | Groups rows into cycles. Required by C-9.1 live reconstruction (cycle boundaries drive the TTL sweep and the killed prune) and by C-15.5 snapshot diffing. Recoverable from nothing else: in replay every row of a cycle shares `recorded_at`. |
| `universe_rank` | `Optional[int] = None` | `app.ledger` (1-based enumeration index of the universe tuple) | The processing order **decides** slot-cap and REPLACE outcomes (C-5.6.2), so reconstruction without it is not deterministic. Doubles as a liquidity-tier segmentation dimension. |
| `contract_version` | `str = "1.1"` | `app.ledger` constant | A row's outcome semantics are only meaningful against the contract in force; rows must state it themselves rather than inherit it from a sibling file. |
| `data_snapshot_id` | `Optional[str] = None` | driver → orchestrator → `app.ledger`; `None` for live | Ties a replay row to the exact kline bytes that produced it (C-6.10). Without it a re-download silently makes old and new rows non-comparable with no way to detect it. |
| `d1_rows` | `Optional[int] = None` | `app.ledger`, `len(snapshot.d1)` | Window shape is part of the strategy definition (C-6.5). Near listing start or after an outage the window truncates and behavior legitimately differs; recording the shape makes that visible instead of inexplicable. Also the cheapest possible detector for a window-construction regression — a wrong `N` shows up on every row. |
| `h1_rows` | `Optional[int] = None` | `app.ledger`, `len(snapshot.h1)` | Same. |
| `m5_rows` | `Optional[int] = None` | `app.ledger`, `0` when `m5 is None` | Distinguishes "no M5 attached" from "M5 attached but the score gate failed" — otherwise ambiguous, and exactly the field needed to measure divergence C-5.5.2. |

No other field is added; nothing is renamed, repurposed, reordered in
`to_jsonl_dict`, or removed. New keys are appended at the end of the emitted object
so a diff of two lines stays readable.

### C-16.2 `run_id` construction

- **Replay (deterministic):**
  `run_id = "replay-" + sha256(canonical_json(core))[:16]`, where `core` is
  `{contract_version, strategy_version, ledger_schema_version, data_snapshot_id,
  T_0, T_n, burn_in_h1, universe_mode, static_universe, top_coins_limit,
  lifecycle_config, outcome_config_hash, env}`. Identical inputs ⇒ identical
  `run_id`, so re-runs are idempotent and any drift surfaces immediately as a new
  id.

**Amendment, approved 2026-08-03 — what `core` does and does not contain.**
`static_universe` is named above because a pinned universe changes which symbols
are scanned and therefore the result; it was present in the implementation from
M13 and is recorded here so the closed list matches it exactly.

Three values are recorded in the run manifest **outside** `core`, and their
exclusion is deliberate rather than an omission:

| Manifest key | Why outside `core` |
|---|---|
| `universe_id` | The identity of the Universe Definition resolution (C-6.9), resolved by `backtest.universe` and injected. It *can* change which symbols are ranked, so a stricter contract would include it — but `core` is a closed list this clause owns, and widening it would retroactively change every recorded `run_id`. The consequence is stated rather than hidden: **two runs differing only in `universe_id` produce the same `run_id` and therefore the same output directory.** The mitigation is that `universe_id` is written to the manifest and to every ledger row's lineage, so a mismatch is detectable after the fact. Gate B verified the value is stable across Stage 1 → Stage 2 (`7fd26361c467d048`, `added=0 removed=0 changed=0`), so no baseline is exposed to it. |
| `optimizations.caching_analyzer` | C-8.4. The claim is that memoization cannot change a result; putting it in `core` would assert the opposite. |
| `optimizations.vectorized_swings` | C-8.5. The state of `analysis.structure.SWING_IMPL`, not run configuration; same reasoning. |

A cached-vs-uncached or v1-vs-vectorized comparison therefore points the two arms
at different `output_root`s and excludes `optimizations` from the manifest diff
along with `generated_at` and `wall_seconds` — those keys *are* the variable under
test. Changes to the ranking **rule** are not covered by this exclusion: they are
strategy behaviour and surface through `strategy_version`, which *is* in `core`
(see finding A1 and `STRATEGY_VERSION` 2.2.0).
- **Live (not deterministic, and need not be):**
  `run_id = "live-" + iso8601_utc(process_start) + "-" + pid`, generated once per
  process and stamped on every row of that process.

**Environment capture (normative).** `env` is
`{"python": platform.python_version(), "pandas": v("pandas"),
"pandas_ta": v("pandas_ta"), "numpy": v("numpy")}` with
`v = importlib.metadata.version`. `importlib.metadata` is required because
`pandas_ta.__version__` is empty in this environment — reading the attribute would
silently drop the version from the lineage hash. A patch-level upgrade therefore
changes `run_id`; that is intended, because float results are only guaranteed
identical within one environment (§18).

### C-16.3 Ownership

Unchanged from schema 1 and non-negotiable: the **schema** belongs to
`v2/domain/signal_ledger.py`; the **assembly** to `v2/app/ledger.py`, from
already-computed domain objects; the **sink** to
`v2/infra/persistence.append_signal_ledger`. The record is **write-only at
runtime** — no `analysis` or `lifecycle` module may import, read, or condition on
it. The lineage values are *inputs to the orchestrator* (constructor arguments with
live-safe defaults), never values it derives from market data — deriving them would
be a Principle-12 leak.

### C-16.3.1 The exact production change schema 2 requires (Milestone M1)

Lineage means the orchestrator must *carry* values it does not currently hold. The
complete, bounded change set — additive, behavior-neutral, and the only production
edit this contract authorizes besides the optional performance work of C-8.5:

1. `SignalLedgerRecord` gains the nine fields of C-16.1;
   `LEDGER_SCHEMA_VERSION` → `2`.
2. `assemble_ledger_record` gains keyword-only parameters `source`, `run_id`,
   `cycle_index`, `universe_rank`, `contract_version`, `data_snapshot_id`, and
   derives `d1_rows` / `h1_rows` / `m5_rows` from the snapshot it already receives
   (`len(snapshot.d1)`, `len(snapshot.h1)`,
   `0 if snapshot.m5 is None else len(snapshot.m5)`).
3. `Orchestrator.__init__` gains `source: str = "live"`,
   `run_id: Optional[str] = None`, `data_snapshot_id: Optional[str] = None`.
   Defaults leave live behavior identical.
4. `Orchestrator` maintains `self._cycle_index`, initialized to `-1` and incremented
   **at the start of `run_cycle`**, so every call consumes an index including
   empty-universe cycles. In replay, `cycle_index == k` of C-8.3.
5. `run_cycle` enumerates the universe 1-based and passes the rank down:
   `for rank, entry in enumerate(universe, 1): self._process_symbol(entry.symbol, rank)`.
   `_process_symbol` forwards it to `_record_ledger` only.
6. The `domain.signal_ledger` module docstring is corrected: it currently states
   that outcomes are "joined to these records by `(symbol, h1_last_ts)`", which
   contradicts C-9.4. It must cite the canonical-row rule instead. Docstring only —
   no behavior.
7. Nothing else changes. `universe_rank` and the lineage values are inputs and
   observability outputs; **no branch reads them**, so Principle 12 holds and no
   signal outcome, tier, or lifecycle transition can differ.

Verification required for M1: the existing 784 tests stay green; a new test asserts
a schema-1 line parses into a schema-2 object with `source == "live"` and the other
eight fields `None`; a new orchestrator test asserts the emitted record carries the
injected lineage and the correct `universe_rank` for the second symbol of a
two-symbol universe.

### C-16.4 Per-run output bundle

| File | Written by | Contents |
|---|---|---|
| `runs/{run_id}/manifest.json` | driver | the `core` of C-16.2, plus declared biases, burn-in, the window's gap inventory, tool versions, git commit |
| `runs/{run_id}/signal_ledger.jsonl(.gz)` | recorder | schema-2 rows (§16) |
| `runs/{run_id}/lifecycle_events.jsonl` | recorder | C-15.3 |
| `runs/{run_id}/memory_snapshots.jsonl` | recorder | C-15.4 |
| `runs/{run_id}/alerts.jsonl` | recorder | C-15.7 (cross-check) |
| `runs/{run_id}/ob_audit.jsonl` | recorder | the production OB-audit record, unchanged |
| `runs/{run_id}/run_health.jsonl` | driver | C-15.8, one line per cycle |
| `runs/{run_id}/outcomes.jsonl` | Outcome Engine | C-9.4 |
| `runs/{run_id}/metrics.json`, `report.md`, `segments.csv` | metrics engine | §12 |

Measured row size is ~1.2 KB, so a 5-year × 30-symbol ledger is ≈ 1.6 GB raw.
Replay ledgers SHOULD be gzipped (~10× smaller); the live ledger stays uncompressed
for tail-ability.

**Byte-determinism of compressed output.** `gzip` embeds an mtime by default, which
would break the §17.5 determinism test. Compressed artifacts MUST be written with
`gzip.GzipFile(filename="", mtime=0, compresslevel=6)`, and the determinism test
compares **decompressed** content in any case. `manifest.json` carries the run's own
`run_id` and is compared after removing wall-clock-only fields (`generated_at`,
elapsed seconds), which MUST be the only non-reproducible keys it contains.

### C-16.5 Compatibility and migration

- **Backward (reading old rows):** `from_jsonl_dict`
  ([signal_ledger.py:182-191](v2/domain/signal_ledger.py#L182)) already filters to
  known fields and falls back to dataclass defaults, so schema-1 lines parse
  unchanged into schema-2 objects: `source="live"`, everything else `None`.
- **Forward (old readers, new rows):** an unknown key is ignored by the same
  filter, so a schema-1 reader tolerates schema-2 lines.
- **Migration cost: zero.** `signal_ledger.jsonl` **does not exist** in the
  repository — verified 2026-07-27. No production ledger row has ever been written,
  so schema 2 is adopted with no backfill. **This is the last moment at which that
  is true**; lineage cannot be reconstructed after the fact, which is why B4 blocks
  the first live *or* replay run rather than the first analysis.
- **Never:** rewrite a written line; repurpose a key; reorder existing keys; reuse
  `schema_version` 2 for a different field set.

### C-16.6 Replay/live compatibility rules

One schema, one reader, two sources. Consequently:

1. Replay MUST NOT write to the live `signal_ledger.jsonl` (C-15.6).
2. Any aggregate mixing `source` values MUST declare it in its output.
3. `run_id` is the mandatory first grouping key of every analysis; `source`,
   `strategy_version`, `contract_version` and `data_snapshot_id` are mandatory
   segmentation dimensions (§12).
4. The metrics engine MUST refuse to diff two runs whose `data_snapshot_id`,
   window, `burn_in_h1` or `universe_mode` differ (C-5.7.2).

---

## 17. Testing Architecture (normative minimum)

An implementation is conforming only if all of the following exist and pass.

1. **Port conformance** — one suite run against both the live and the replay
   implementations of the market-data and persistence surfaces (method names,
   return types, `None` semantics, error types).
2. **Repository** — synthetic arrays: boundary positioning (C-6.2/C-6.3), short
   history truncation, gap windows return fewer rows and are never padded,
   integrity violations raise.
3. **Adapter parity** — the test that mechanically closes B1 and B2. Load an
   `InputSnapshot` (`tests/regression/snapshot.py`), seed a repository with its
   rows, set `T = h1_last_ts + 3600` (the C-5.3 relation, exercised on real data),
   and assert the adapter's frames are `DataFrame.equals`-identical to the
   snapshot's — including the `atr` column and its `NaN`s — and that `h1_last_ts`,
   `h1_last_ts_dt` and `live_price` match exactly.
   **Prerequisite:** the repository currently ships only a *synthetic* fixture
   (`tests/regression/fixtures/synthetic_short.json`). A **live** capture must be
   committed before the adapter milestone via the existing manual tool —
   `python -m tests.regression.generate live --symbol BTC/USDT --out
   tests/regression/fixtures/btcusdt_live.json --m5` — which performs the single
   network round-trip. The test itself reads only the committed file and stays
   hermetic.
4. **ATR parity** — ATR on the closed-only slice equals live's compute-then-strip,
   on real captured data (pins C-6.5).
5. **Replay determinism** — the same `(data_snapshot_id, window, config)` run twice
   produces identical output bundles (decompressed comparison), under varied
   `PYTHONHASHSEED`.
6. **Live↔replay parity** — feed the replay adapter the snapshots that produced
   existing `retracement_ob_audit.jsonl` rows and assert identical audit records;
   every discrepancy must map to a §13 divergence.
7. **Golden replay** — a committed small synthetic repository (3 symbols,
   ~200 hours) plus an expected output bundle. Any semantic drift in `v2/` or
   `backtest/` fails this test. This is the regression harness for Phases 9–12.
8. **Lifecycle capture completeness** — for a scripted scenario exercising KILL,
   DECAY, TTL, REPLACE, SUPERSEDED, CAPPED and SAME_THESIS, assert every memory
   removal is attributed and the C-15.5 residue equals the TTL log count.
9. **Outcome engine** — table-driven cases: same-candle SL+TP → LOSS; M5 resolves TP
   first → WIN; gap through SL → R < −1; pre-crossed limit → IMMEDIATE; missing M5 →
   worst case; each `cancel_reason`; expiry at exactly `max_holding_h1`; `OPEN` at
   end of data; inverted levels → `INVALID_LEVELS`; each `shadow_kind`.
10. **Metrics** — hand-computed fixtures for expectancy / profit factor / max
    drawdown; a property test that segment `n` sums to the population.
11. **Architecture** — no `v2` module imports `backtest`; no wall-clock read on any
    replay path; the adapter performs no I/O beyond the repository.

All tests remain stdlib `unittest`, hermetic (no network, no real filesystem outside
temp dirs, no wall clock).

---

## 18. Proof of Determinism

Every source of nondeterminism available to this codebase was enumerated and
checked against the source.

| Source | Status | Evidence |
|---|---|---|
| Wall clock | eliminated | `SystemClock` is the only wall-clock reader; replay injects `FakeClock`. A repository-wide search for `time.time` / `datetime.now` / `utcnow` in `v2/` matches only `infra/clock.py` and docstrings. |
| Network | eliminated | Only `infra/market_data.py` and `infra/notifier.py` perform network I/O; both are swapped (C-8.2). |
| Filesystem | eliminated | Only `infra/persistence.py`; swapped for `ReplayPersistence`. |
| `random` / PRNG | absent | No `random` reference exists in `v2/`. |
| Threads / processes | absent | No `threading` / `multiprocessing` reference exists in `v2/`. |
| `id()` / `hash()` in logic | absent | No occurrence in `v2/`. |
| Set iteration order | not output-affecting | The only set is `Orchestrator._ob_audit_seen`, used for membership only ([orchestrator.py:282-289](v2/app/orchestrator.py#L282)); `LOG_EVENT_TYPES`, `ALERT_KINDS`, `EVICTION_REASONS` are membership constants. |
| Dict iteration order | deterministic | Python dicts are insertion-ordered; `SignalStore._active` insertion order is a pure function of the cycle history, itself a pure function of the window and the data. |
| Tie-breaks | deterministic | `MemoryView.weakest_symbol` = `min` over an insertion-ordered dict ([memory.py:64](v2/lifecycle/memory.py#L64)); the `sorted()` calls in `risk.py` and `rendering.py` use explicit keys and Python's sort is stable. |
| Universe tie-breaks | pinned by contract | C-6.9 sorts by name before the stable volume sort, removing live's dependence on API response order. |
| Float arithmetic | deterministic per environment | Same operation order and same `numpy`/`pandas`/`pandas_ta` versions ⇒ identical IEEE-754 results. The environment is pinned in the manifest (C-16.2) and is part of lineage. |
| Universe order | pinned by contract | C-5.6.2 forbids re-sorting or parallelizing symbols. |
| Cycle order | pinned by contract | C-5.6.1 plus the adapter's monotonicity check (I-8, C-5.6.6). |
| Lifecycle carry-over | pinned by contract | C-5.7 (burn-in, empty startup memory, no sharding for the baseline). |
| Analysis memoization | proven transparent | C-8.4 keys the memo on the full determinant of a pure function, and a test asserts equality with the uncached run. |
| Compressed output | pinned by contract | C-16.4 mandates `mtime=0`. |

**Conclusion.** Given a fixed `data_snapshot_id`, window, config and environment, a
conforming replay is a deterministic function of its inputs, and the `run_id` of
C-16.2 is a content hash of exactly those inputs. Test §17.5 pins this; §17.7 pins
it against semantic drift over time.

---

## 19. Architectural Decisions Log

Decisions taken while closing B1–B4, with the rejected alternatives, recorded so
they are not silently reopened.

| # | Decision | Rejected alternative | Reason |
|---|---|---|---|
| **R-1** | `T` = the **close** boundary; `h1_last_ts = T − 3600`; clock = `T + 1s`. | Anchor everything on the reference candle's open time and scan at `open + 3600 + 1s`. | Arithmetically equivalent but invites the A47 error: anchoring the *clock* to `h1_last_ts` shifts `session_active` by an hour and changes scores. Boundary-anchoring keeps `hour(clock) == hour(T)`. |
| **R-2** | Scan-instant offset is exactly **+1 s**. | +30 s or +60 s (closer to live). | Every consumer reads the hour or compares hour differences, so all offsets in `(0, 3600)` behave identically; one canonical value makes `recorded_at` bit-comparable across runs. |
| **R-3** | Windows are **row slices** (C-6.3). | Reindex to a complete time grid. | Live receives "the N most recent existing klines"; `safe_frame` drops NaN rows, so grid-filling shortens the analyzed frame and shifts every positional index. |
| **R-4** | Selection expressed as `open_time + interval <= T`. | Use Binance's `ct` close-time column. | `ct` is `open + interval − 1 ms`; using it invites off-by-one-ms boundary bugs, and `ct` is dropped before analysis anyway. |
| **R-5** | Adapter returns `None` when there are **no** closed D1 rows. | Return a snapshot with an empty D1 frame, exactly as live. | Provably identical outcome (the `len(d1) < 10` guard skips the symbol; no state changes either way), and the simpler rule cannot be implemented two ways. |
| **R-6** | Lifecycle capture via a **`RecordingReconciler`** through the existing injection slot. | Add an `on_decision` hook to `Orchestrator`. | Zero production change, therefore zero risk of altering behavior; the slot already exists ([orchestrator.py:102](v2/app/orchestrator.py#L102)). |
| **R-7** | TTL attributed by **elimination**, with a writer-based cross-check (C-15.5). | (a) Parse rendered `[⏰ TTL EXPIRED]` lines as data; (b) add an injectable `store_factory`. | (a) rendered text is not a schema and would couple the dataset to presentation; (b) is a production change that is not yet necessary — the elimination argument is *complete*, not heuristic. If a future need defeats the proof, (b) is the sanctioned change and requires an amendment. |
| **R-8** | Trade instantiation from the **decision stream**; notifier is a cross-check. | Instantiate from the notifier stream (v1.0's reading). | An exception in `_render_symbol` skips dispatch after the decision was applied, and `Alert` carries no symbol-time identity. The decision stream is complete and self-identifying. |
| **R-9** | CONTINUATION fills at **`open(W0)`**, parameterized. | `open(W1)` (an hour later) or `entry_price` (the historical pullback close). | `open(W1)` invents an hour of delay; `entry_price` is unreachable by construction. `w0_close` exists as the pessimistic sensitivity. |
| **R-10** | Pre-crossed limits fill **immediately** at `open(W0)`. | Treat them as ordinary limits. | An order already at or through market fills at once; modeling patience would fabricate a favourable entry. |
| **R-11** | Missing M5 ⇒ **worst case**, counted. | Fall back to H1-only ordering assumptions, or drop the trade. | Dropping biases the sample; assuming biases upward. Worst-case-plus-a-counter is conservative and auditable. |
| **R-12** | Ledger schema 2 adds lineage **now**, before the first run. | Add lineage later, when replay exists. | Lineage cannot be backfilled; `signal_ledger.jsonl` does not exist yet (C-16.5), so the cost is exactly zero today and unbounded tomorrow. |
| **R-13** | Nine lineage fields; no more. | Also add `scan_instant`, `signal_id`, outcome fields. | `recorded_at` + `cycle_index` already determine the cycle instant; durable signal identity (finding A29) is a separate concern; outcomes must stay out of the ledger (C-9.5). |
| **R-14** | Baseline is a **single unsharded run** with a 336-H1 burn-in. | Parallel shards for the baseline. | Shard boundaries reset lifecycle memory, changing slot-cap outcomes. Sharding remains available for exploration only. |
| **R-15** | Three explicit shadow classes with a fixed 3-H1 window for the suppressed ones (C-2.2). | One undifferentiated "shadow" population (v1.0). | v1.0 both defined shadows as *insertions without an alert* and included CAPPED, which produces **no** insertion — a contradiction. Splitting the classes also makes the slot-cap and cooldown costs directly measurable. |
| **R-16** | `R_unit` = `|fill_price − sl|` (C-9.6). | `|entry_price − sl|` (structural risk). | The operator's actual risk is measured from the actual fill; using the structural entry would report a risk nobody took. `structural_rr` is recorded separately so the gap is visible. |
| **R-17** | Universe tie-break by **symbol name**, ranking requires 24 closed H1 rows. | Preserve live's API-response tie order; rank on partial sums. | API order is not reproducible; partial sums make a new listing's universe entry depend on the window's start date. |
| **R-18** | Capture events are **buffered per cycle** and flushed at `end_cycle`. | Stream events as they occur. | `seq`, `universe_rank` and the TTL residue all need cycle-complete information; streaming would require a second pass or a partial schema. |
| **R-19** | `.npy` + JSON sidecar for klines; JSONL for records; SQLite only for small derived tables. | Parquet everywhere. | `pyarrow`/`fastparquet` are **not installed** and a new dependency needs approval; the hot path (1.3 M window reads) wants zero parse cost, which memory-mapped `.npy` provides. Parquet stays available for interchange if the dependency is approved. |
| **R-20** | v1.0 clause numbers are **preserved**; changed clauses are marked AMENDED; new material is appended as §15–§19. | Renumber for a cleaner narrative. | [risk.py:452](v2/analysis/risk.py#L452), [audit.py:18](v2/app/audit.py#L18), `STRATEGY_NOTES.md`, `PROJECT_STATE.md` and `CHANGELOG.md` already cite C-1.3, C-4.7, C-4.11, C-5.2, C-6.2, C-6.3, C-8.1, C-9.1, C-9.4 and §12. Stable identifiers beat tidy numbering. |

---

*End of contract v1.1. Implementation MUST cite clause numbers in code comments and
tests. Amendments bump the version and add a CHANGELOG entry.*
