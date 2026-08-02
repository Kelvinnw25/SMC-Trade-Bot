# Strategy Notes

> The permanent technical notebook for the **SMC Signal Radar** trading strategy.
> This is **not** a migration document, **not** a changelog, and does **not**
> duplicate `PROJECT_STATE.md`. It captures strategy findings: confirmed bugs,
> weaknesses, decisions intentionally kept, research ideas, and a running
> experiment log.
>
> - `PROJECT_STATE.md` → *what the project is today*
> - `CHANGELOG.md` → *how it got here (history)*
> - `STRATEGY_NOTES.md` (this file) → *what we know about the strategy, and what we're testing*
>
> **Ground rules for this file:** every claim cites code (`module.py:line`) or an
> experiment. Weaknesses are hypotheses until a backtest confirms them. Nothing
> here authorizes a code change — changes are proposed, approved, measured, then
> logged in the Experiment Log.
>
> Line references are to `v2/` as of 2026-07-26.

---

## Current Strategy Overview

A top-down, single-pass SMC pipeline, run once per symbol per scan cycle
(`v2/analysis`, orchestrated by `v2/app`, state in `v2/lifecycle`). It is
**advisory** — `entry_price` is a *structural reference*, never an executable
order ("Contract B").

**Per-cycle flow:**

1. **HTF context** (`htf.py`) — prior day/week levels; `weekly_bias` = price vs
   prior-week midpoint; `d1_bias`/`h4_bias` = EMA50/EMA200 + 2-swing structure;
   `location` = discount/premium/equilibrium; `momentum_weakening`. **Direction**
   from a weighted vote (weekly 4 · d1 2 · h4 1 · location ±0.5–1.5, H1 fallback),
   a rotational weekly-anchor override, and a hard expansion-vs-direction veto.
2. **Regime** (`regime.py`) — a 3-vote compression/expansion classifier over ATR
   ratios, body ratio and drift → {expansion_up/down, accumulation, distribution,
   rotational, compression, neutral, unknown} → **execution mode** {CONTINUATION,
   RETRACEMENT, WATCH_ONLY, NONE}.
3. **Structure** — swings; **order blocks** (raw origin candle in a 10-bar window,
   size/not-lost/distance quality filter, first-match selection); **FVG** (boolean
   three-candle imbalance); **liquidity sweep** (pierce + reclaim + depth + body).
4. **Entry** (`entry.py`) — CONTINUATION: displacement (≤3 bars back) + micro-
   pullback (≤0.10 ATR) + reclaim → pullback close. RETRACEMENT: fresh quality OB →
   entry at block edge. WATCH_ONLY / NONE otherwise.
5. **Risk** (`risk.py`) — SL: continuation pullback bar ±0.30 ATR; retracement OB
   far edge ±0.70 ATR. TP: RR-aware liquidity target (EQH/EQL cluster → swing →
   PDH/PDL), else a flat **2R** fallback. Hard **1.2 RR floor**.
6. **Scoring** (`scoring.py`) — additive: HTF alignment (weekly 2 · d1 2 · h4 1 ·
   location 2), mode-aware confirmations (sweep/FVG/OB/displacement), structure
   quality (target_clear/session/htf_clean), regime, RR bonus (1.5/2/3), momentum,
   M5 CHoCH; choppy penalty −2 → tiers SKIP/WATCH/VALID/A+ (mode-specific cutoffs).
7. **M5 enrichment** — re-scores with M5 CHoCH when the pre-M5 score ≥ 8.

**Lifecycle** (`v2/lifecycle`) — per symbol: kill (HTF D1+H4 flip **or** SL break
±0.30 ATR), pre-memory stale, kill-cooldown (1h), same-thesis partial refresh,
insert / replace-weakest-by-score / slot cap (10), alert new/upgrade dedup by
tier, H1-gated miss increment → decay eviction at 3 misses, TTL (24h untapped /
6h tapped / 10h manual).

---

## Confirmed Implementation Bugs

*Objectively incorrect behavior — not a matter of strategy taste. Both were
metadata/observability defects: fixing them changed recorded data, not any
trading decision.*

> **STATUS: BOTH FIXED — 2026-07-26** (re-validated against
> `REPLAY_CONTRACT.md` C-1.3/§12 before implementation; `STRATEGY_VERSION`
> bumped to **2.0.1** so datasets segment cleanly). Verification: consumer
> enumeration proved nothing branches on the affected fields; the full suite's
> decision-bearing assertions (scoring/HTF V1 diffs, lifecycle replay
> equivalence, orchestrator tests) pass unchanged; only the three
> quirk-pinning metadata tests were deliberately re-based
> (`test_risk.py`, `test_audit.py`, `test_scoring.py` bridge carve-out).
> Records below kept for history.

### BUG-1 — Continuation signals drop `tp_source` and `tp_rr` ✅ FIXED
- **ID:** BUG-1 (audit ref A12)
- **Description:** On the CONTINUATION path, `plan_risk` forced `tp_source=None`
  and `tp_rr=None` before returning (formerly risk.py:509-518; now publishes
  the computed values).
- **Why objectively incorrect:** it faithfully reproduces a V1 Python scoping bug
  — V1 assigned these inside a nested `_calc_continuation_entry` without
  `nonlocal`, so the assignments never reached the outer scope. The real values
  exist (a target *was* selected); they are discarded by accident, not by design.
- **Impact:** every continuation signal's payload, alert, and any downstream
  analysis carry null TP provenance/RR. Continuation exit quality cannot be
  evaluated from the collected data.
- **Risk of fixing:** Low. The `tp` value itself is unaffected; only two
  previously-null metadata fields get populated.
- **Recommended fix:** return the `tp_source`/`tp_rr` that `find_liquidity_target`
  (or the 2R fallback) actually produced, same as the retracement path.
- **Changes strategy behavior?** **No** — no score/decision/entry/sl/tp changes;
  only fills in metadata. (It does change observable payload/alert text.)

### BUG-2 — OB-audit records omit the selected-OB metadata (all None) ✅ FIXED
- **ID:** BUG-2
- **Description:** `assemble_ob_audit` hard-coded `selected_ob_index/rank/top/
  bottom/size`, `window_bounds`, `current_price` to `None` and `location` to `""`,
  even though the selected `OrderBlock` and the `MarketSnapshot` carried every
  one of those values on the same call. Now populated; per the contract,
  `current_price` = the OB-filter reference price (last **closed** H1 close),
  distinct from `market_price` (live forming close).
- **Why objectively incorrect:** it reproduces a V1 key-name mismatch (V1's
  `main.py` read keys under names `strategy.py` never set), so the fields were
  always null in V1 too. The data is present and simply not written.
- **Impact:** `retracement_ob_audit.jsonl` — the primary artifact for evaluating
  retracement OB entries — is missing OB index/rank/size/depth, current price,
  scan window, and location. (`ob_meta` top/bottom and the full `ob_candidates`
  set *are* recorded, so the loss is partial but material.)
- **Risk of fixing:** Low. `OBAuditRecord` is an **observability contract**
  (Decision #010) — never read back into any analysis or lifecycle decision.
- **Recommended fix:** populate the fields from `selected` (index/rank/top/bottom/
  size), `snapshot.live_price` (current/market price), the OB scan window bounds,
  and `signal.candidate.htf.location`.
- **Changes strategy behavior?** **No** — audit is write-only; no decision reads it.

---

## Strategy Weaknesses

*Every A1–A25 audit item, grouped. Each is a **hypothesis** until measured.
"Confidence" = how sure the issue is real; "Expected impact" = likely effect on
signal quality if addressed; "Difficulty" = implementation size (XS/S/M/L). None
of these are Fix-Now — all change trading decisions and must wait for a baseline +
backtester (see Triage).*

### HTF & Bias — weak assumptions
- **A1 — EMA200 is noise on short history.** `_derive_simple_bias` runs
  `ewm(span=200)` on D1 frames as short as ~12 rows ([htf.py:99](v2/analysis/htf.py#L99));
  with <200 bars EMA200 ≈ recent price, so "price vs EMA200" (+2) and "price vs
  EMA50" (+1.5) are effectively the same input twice. *Evidence:* span-200 EMA on
  12 rows. *Confidence:* High. *Impact:* Med. *Difficulty:* M. *Experiment:*
  correlate the two votes across history; replace with a structure/BOS bias and
  compare tier distribution.
- **A2 — Weekly bias is a midpoint coin-flip.** `weekly_bias` = price above/below
  the prior-week midpoint ([htf.py:197](v2/analysis/htf.py#L197)), yet it is the
  dominant weight (4.0 direction, +2 score). *Confidence:* High. *Impact:* Med.
  *Difficulty:* M. *Experiment:* measure how often a single midpoint crossing
  flips direction; test a structure-based weekly bias.

### Regime & Timing — fragile heuristics
- **A3 — Regime is a magic-number cliff.** Hard thresholds (`range_ratio` 3/4,
  `atr` ×0.95/1.03, `body_ratio` 0.42/0.46, drift 0.008/0.012) with no hysteresis
  ([regime.py:109-146](v2/analysis/regime.py#L109)); a tiny ATR wobble flips regime
  → flips execution mode, a large discontinuity. *Confidence:* High. *Impact:*
  High. *Difficulty:* M. *Experiment:* add hysteresis / smoothing; measure
  mode-flip frequency and its effect on outcomes.
- **A11 — Continuation micro-pullback is 0.10 ATR tight.** Entry only triggers on a
  ≤0.10 ATR retrace ([entry.py:105,148](v2/analysis/entry.py#L105)); deeper healthy
  pullbacks yield no entry. *Confidence:* Med. *Impact:* Med. *Difficulty:* S.
  *Experiment:* sweep the multiple; measure fill-plausibility vs realized follow-through.

### Scoring — inflation & tiering
- **A5 — HTF counted ~4×.** The same weekly/d1/h4/location read drives direction
  weighting ([htf.py:267](v2/analysis/htf.py#L267)), scoring HTF-alignment +5–7
  ([scoring.py:179](v2/analysis/scoring.py#L179)), `htf_context_clean` +1
  ([scoring.py:260](v2/analysis/scoring.py#L260)), and `location` again. A
  trend-aligned setup banks a large score before any structural evidence.
  *Confidence:* High. *Impact:* High. *Difficulty:* M. *Experiment:* collapse HTF
  into one factor; recalibrate tiers; compare precision.
- **A6 — Continuation "displacement" is circular.** The +4 bonus fires iff
  `entry_source == "continuation_micro_pullback"` ([scoring.py:214](v2/analysis/scoring.py#L214))
  — i.e. iff an entry was found — so it rewards the entry existing, not an
  independent confirmation, and it sets the tier. *Confidence:* High. *Impact:*
  Med. *Difficulty:* S. *Experiment:* separate "a displacement candle exists" from
  "an entry triggered"; re-weight.
- **A7 — A+ reachable with zero liquidity confirmation.** Continuation A+ needs ≥14
  ([scoring.py:332](v2/analysis/scoring.py#L332)); HTF(5)+displacement(4)+
  target_clear+session+htf_clean+expansion = 14 with **no** sweep/FVG/OB.
  *Confidence:* High. *Impact:* High. *Difficulty:* M. *Experiment:* require ≥1
  liquidity confirmation for VALID/A+; measure hit-rate change.
- **A8 — WATCH_ONLY is near-unreachable.** Fires only in `compression` with strong
  HTF alignment, then needs ≥12 with no sweep/OB penalties available
  ([scoring.py:239-355](v2/analysis/scoring.py#L239)). *Confidence:* Med (suspected
  near-dead). *Impact:* Low. *Difficulty:* S. *Experiment:* count WATCH_ONLY VALIDs
  in the live dataset; decide to fix the branch or remove it.
- **A15 — RR floor 1.2 is low; two RR numbers.** The hard 1.2 floor
  ([scoring.py:293](v2/analysis/scoring.py#L293)) admits marginal setups; separately
  `tp_rr` (risk) and `rr` (scoring) are computed against possibly-different stops
  and can drift. *Confidence:* Med. *Impact:* Med. *Difficulty:* S. *Experiment:*
  sweep the floor against realized RR; unify the two RR computations.

### Entry Quality
- **A9 — A stale OB wins the slot.** Selection is first-match by quality filter;
  **freshness is not part of selection** ([order_block.py:207](v2/analysis/order_block.py#L207)),
  so a stale block is selected and suppresses fresher ones behind it → `fresh_ob=
  False` → −1 and no +2. *Confidence:* High. *Impact:* High. *Difficulty:* S.
  *Experiment:* prefer fresh passing blocks in selection; measure entry quality.
- **A10 — Retracement entry has no trigger.** Entry = OB edge
  ([entry.py:252](v2/analysis/entry.py#L252)); nothing confirms price reacted, and
  sweep/OB/FVG are scored independently, never required to be confluent.
  *Confidence:* Med-High. *Impact:* High. *Difficulty:* M. *Experiment:* require
  sweep- or FVG-confluence with the OB; measure precision.
- **A23 — OB has no impulse-origin requirement.** Any down/up candle in the 10-bar
  window is a "zone" ([order_block.py:196](v2/analysis/order_block.py#L196)); no
  BOS/displacement-origin check. *Confidence:* Med. *Impact:* Med-High.
  *Difficulty:* M. *Experiment:* require the OB to precede a displacement/BOS;
  compare selected-OB quality.

### Confirmation Value — Liquidity / FVG
- **A22 — The core SMC concept is a minor input.** A sweep is +2 (retracement) /
  +1 (continuation), its absence only −1 / nothing
  ([scoring.py:221](v2/analysis/scoring.py#L221)) — a VALID can fire with no sweep;
  inducement/IDM is unmodeled. *Confidence:* High. *Impact:* High. *Difficulty:* M.
  *Experiment:* make a sweep a stronger gate; add IDM detection as a spike.
- **A24 — FVG is near-decorative.** A boolean +1, never gates entry, bounds
  discarded, no fill/mitigation ([fair_value_gap.py:120](v2/analysis/fair_value_gap.py#L120)).
  *Confidence:* Med. *Impact:* Low-Med. *Difficulty:* M. *Experiment:* make FVG a
  confluence/mitigation trigger, or drop it — decide on measured value.

### Exit Quality — TP / SL
- **A12 — Continuation TP metadata dropped.** See **BUG-1** (promoted to a
  confirmed bug; **FIXED 2026-07-26**). Listed here for completeness.
- **A13 — Single TP, no partials/trail/BE.** Nearest RR-passing liquidity −0.10 ATR
  or a flat 2R fallback ([risk.py:504](v2/analysis/risk.py#L504)); no scaling or
  structure trail. *Confidence:* High (as a limitation). *Impact:* High.
  *Difficulty:* L. *Experiment:* model partial TPs / trailing against history.
- **A14 — Fixed, uncalibrated SL multiples.** 0.30 ATR (continuation) vs 0.70 ATR
  (retracement) ([risk.py:98-99](v2/analysis/risk.py#L98)); not regime- or
  liquidity-aware; the stop can sit on an obvious pool. *Confidence:* High.
  *Impact:* High. *Difficulty:* M. *Experiment:* liquidity-anchored stops; sweep
  the multiples against stop-out rate.

### Kill & Cooldown
- **A4 — Kill on a forming-candle wick.** `is_poi_invalidated` compares the **live**
  (unclosed) price to SL ±0.30 ATR ([rules.py:147](v2/lifecycle/rules.py#L147)); a
  wick kills before any H1 closes. *Confidence:* High. *Impact:* High.
  *Difficulty:* S. *Experiment:* close-confirmed kill; measure false-kill rate.
- **A16 — HTF-flip kill trusts crude EMA bias.** Kill on D1+H4 both flipping
  ([rules.py:145,150](v2/lifecycle/rules.py#L145)) uses the same weak EMA reads
  (A1); weekly isn't consulted. *Confidence:* Med-High. *Impact:* Med.
  *Difficulty:* M. *Experiment:* require weekly agreement or a structural flip.
- **A17 — Zero kill-buffer on candidate-less cycles.** The buffer uses the *fresh*
  cycle's `atr_last`, which is `0.0` when the fresh signal has no candidate
  ([reconciler.py:214](v2/lifecycle/reconciler.py#L214)) → buffer 0 → kill on any
  wick past raw SL. *Confidence:* High (inconsistency). *Impact:* Med.
  *Difficulty:* S. *Experiment:* derive the buffer from the saved signal's ATR;
  measure kill-frequency change. **Affects evaluation** — may cause spurious kills
  during collection; measure its frequency in the baseline.
- **A18 — Fixed 1h cooldown.** [config.py:33](v2/lifecycle/config.py#L33); a false
  kill suppresses a genuine re-form for an hour; not volatility-aware. *Confidence:*
  Med. *Impact:* Med. *Difficulty:* S. *Experiment:* volatility-scaled cooldown.

### Lifecycle & Memory
- **A19 — `miss_count` is overloaded.** One counter drives both TTL tiering (>0 →
  6h) and decay eviction (≥3 → evict) ([rules.py:171](v2/lifecycle/rules.py#L171),
  [reconciler.py:580](v2/lifecycle/reconciler.py#L580)) — conflating "tapped" with
  "decaying". *Confidence:* High. *Impact:* Med. *Difficulty:* M. *Experiment:*
  split into two fields; measure lifespan/quality of kept signals.
- **A20 — The decay clock evicts refreshed valids.** Same-thesis refresh does not
  reset `miss_count` ([reconciler.py:400](v2/lifecycle/reconciler.py#L400)), so a
  continuously re-confirmed VALID is evicted after 3 H1 candles of not *upgrading*.
  *Confidence:* High. *Impact:* High. *Difficulty:* M. *Experiment:* reset/So
  decay only on genuine quality loss; measure premature evictions.
- **A21 — Eviction inherits the scoring bias.** Replace-weakest-**by-score**
  ([reconciler.py:517](v2/lifecycle/reconciler.py#L517)); since score is HTF-inflated
  (A5), the slot cap favors trend-aligned setups over higher-quality counter-trend
  structure. *Confidence:* Med. *Impact:* Med. *Difficulty:* S (follows A5).
  *Experiment:* re-evaluate once scoring is de-inflated.

### Portfolio Risk Management
- **A25 — No sizing, no correlation cap.** 10 slots can be 10 correlated alts, same
  direction ([reconciler.py:509](v2/lifecycle/reconciler.py#L509)); no per-trade
  risk %, portfolio heat, per-direction cap, or daily loss switch. *Confidence:*
  High (as a gap). *Impact:* High (for a real system). *Difficulty:* M-L.
  *Experiment:* add a correlation/direction cap; measure diversification of the
  active set. *(New strategy surface, not a change to existing decisions.)*

---

## Architecture & Evaluation Findings (A26–A58)

*From the Phase-8-preparation audits (2026-07-26). Distinct from A1–A25: these
are architecture/evaluation findings, not strategy-quality items. Replay-facing
ones are resolved by `REPLAY_CONTRACT.md` (clause refs below); the rest are
deferred cleanups or measure-first strategy items.*

| # | Finding (code ref) | Class | Status |
|---|---|---|---|
| A26 | WATCH_ONLY is a dead tier end-to-end: always 0.0 levels → guaranteed GUARD, can never enter memory ([scoring.py:757](v2/analysis/scoring.py#L757), [reconciler.py:288](v2/lifecycle/reconciler.py#L288)) | Dead branch | Defer (fix or remove in Phase 9) |
| A27 | `LifecycleState` enum is dead vocabulary — zero runtime consumers ([lifecycle.py:22](v2/domain/lifecycle.py#L22)) | Dead code | Defer cleanup |
| A28 | `Alert.dedup_key` produced, never consumed ([reconciler.py:430](v2/lifecycle/reconciler.py#L430)) | Dead field | Defer cleanup |
| A29 | `signal_id` written, dropped on load, never consumed — **no durable signal identity** ([active_signal.py:102,162](v2/domain/active_signal.py#L102)) | Missing concept | Revisit before long-horizon lineage matters |
| A30 | `MANUAL_ACTION` status set by nothing (hand-edit-only operator channel); wiped by upgrade re-insert ([rules.py:169](v2/lifecycle/rules.py#L169)) | Hidden workflow | Defer formalization |
| A31 | Kill-switch blind spot: symbols that exit the top-30 stop being scanned; resident records unmonitored until TTL ([orchestrator.py:181](v2/app/orchestrator.py#L181)) | Strategy/ops risk | Measure first; replay preserves it (contract C-4.11) |
| A32 | `kill_htf` derivation is an orchestrator-owned implicit contract ([orchestrator.py:222](v2/app/orchestrator.py#L222)) | Contract leak | Mitigated: replay must reuse Orchestrator (C-8.1) |
| A33 | No cooldown after decay/TTL/replace evictions (only KILL) → oscillation re-alerts ([reconciler.py:251](v2/lifecycle/reconciler.py#L251)) | Strategy smell | Measure first |
| A34 | Ledger join key `(symbol, h1_last_ts)` one-to-many at live cadence; no canonical-row rule; volume growth | Evaluation gap | Resolved for replay by C-9.4; live rows via lifecycle reconstruction (C-9.1) |
| A35 | Double pipeline evaluation per symbol ([orchestrator.py:215](v2/app/orchestrator.py#L215), [engine.py:93](v2/analysis/engine.py#L93)) | Compute/coupling risk | Defer (safe while stages stay pure) |
| A36 | 2R fallback can emit non-positive TP for SHORT with a wide stop → silent guaranteed GUARD ([risk.py:462](v2/analysis/risk.py#L462)) | Silent edge case | Measure first |
| A37 | Legacy-load `alert_sent_tier` defaults to `decision` → legacy records suppress their first alert ([active_signal.py:157](v2/domain/active_signal.py#L157)) | Quirk | Defer |
| A38 | TTL's real semantics = universe-exit GC (continuously-refreshed signals never TTL; decay reaps in-universe) | Semantic insight | Documented; keep |
| A39 | (folded into A30 — status wiped on upgrade re-insert) | — | — |
| A40 | OB-audit vs Signal-Ledger overlap for retracements (two sinks, two schemas) | Duplication | Consolidation question post-Phase-8 |
| A41/A45 | Indicators are fetch-window-relative (ATR RMA warmup, EMA on the 120-bar D1, swing prominence = window-mean ATR) — window shape is part of the strategy definition | Replay fidelity / research hazard | Bound by contract C-6.2/C-6.3 |
| A42 | (= A37) | — | — |
| A43 | Direction/mode flip is a silent overwrite with inherited alert state; cross-direction "upgrade" alerts possible ([reconciler.py:471-545](v2/lifecycle/reconciler.py#L471)) | Strategy risk (top-ranked for eventual fix) | Measure first; trades handled via C-4.7 |
| A44 | Kill reacts every ~60s to forming wicks; confirmation only on closes — asymmetric reactivity | Evaluation risk | Replay policy fixed by C-5.2 |
| A46 | Live records no durable alert/insertion stream — trade instantiation for live data requires deterministic lifecycle reconstruction from the ledger | Lineage gap | Contract C-9.1; a lifecycle-event ledger is a deferred addition |

### Replay-architecture findings (A47–A58)

*From the final Phase-8 architecture review (2026-07-27). These are replay-engine
findings, not strategy items. Four of them were blockers; all four are closed by
`REPLAY_CONTRACT.md` v1.1.*

| # | Finding (evidence) | Class | Status |
|---|---|---|---|
| A47 | **`h1_last_ts` is the *open* time of the last closed H1 (`T − 1h`), not the close boundary** — kline field 0 is the open time ([market_data.py:170-178](v2/infra/market_data.py#L170)) read after the strip ([market_data.py:270](v2/infra/market_data.py#L270)); confirmed empirically by `retracement_ob_audit.jsonl` (`h1_last_ts = 02:00Z` written ≈`03:00Z`). v1.0 said `h1_last_ts = T`. Anchoring the clock wrongly also shifts `session_active`, a **scoring input** → different tiers at 07:00/22:00 UTC | **Blocker B1** — correctness | ✅ Closed by C-5.1–C-5.3 + R-1 |
| A48 | **M5 enrichment is reachable only in `[hh:00, hh:05)`** (guard [market_data.py:321](v2/infra/market_data.py#L321)); v1.0's claimed "intra-hour M5 upgrade" fidelity loss does not exist, and the real divergence is the reverse — replay is *more* M5-complete than live | Contract error | ✅ Corrected in C-5.5.2; declared bias §13.5 |
| A49 | **Windows are row slices, not time ranges** — live gets the N most recent *existing* klines; `safe_frame` drops NaN rows ([frames.py:37](v2/analysis/frames.py#L37)) so grid-filling shortens frames and shifts every positional index | **Blocker B2** | ✅ Closed by C-6.3 (with an 8-item prohibition list) |
| A50 | **`LifecycleDecision` never leaves `_process_symbol`; `SignalStore` is not injectable** — TTL decisions reach the outside world only as rendered strings, so `cancel_reason` was unattributable | **Blocker B3** | ✅ Closed by §15 (`RecordingReconciler` + TTL-by-elimination proof + writer cross-check) |
| A51 | **Per-symbol exceptions are swallowed silently** ([orchestrator.py:242](v2/app/orchestrator.py#L242)) — a broken adapter would look like "no signals" | Misleading-research risk | ✅ Closed by C-15.8 (per-cycle count reconciliation, run fails on breach) |
| A52 | **Replay is compute-bound: 214 ms/symbol-scan measured, ~90 % in `identify_swings`** ([structure.py:98](v2/analysis/structure.py#L98)); 5 y × 30 symbols ≈ 78 h | Program-level risk | Bounded by C-8.4 (memoization, → 39 h) + C-8.5 (optional vectorization, → 6–8 h) |
| A53 | **Ledger rows carry no run/source lineage** — live and replay rows are structurally identical; lineage cannot be backfilled | **Blocker B4** | ✅ Closed by §16 (schema 2, nine fields, zero-cost migration — the ledger file does not exist yet) |
| A54 | **Replay results depend on the start date** via lifecycle memory (slot cap, insertion-order tie-breaks, cooldowns, decay counters) | Methodology | ✅ Closed by C-5.7 (336-H1 burn-in, comparability rules, no sharding for the baseline) |
| A55 | `MarketDataAdapter` / `PersistenceAdapter` are concrete classes, not `Protocol`s (unlike `Notifier`/`Clock`); no conformance test | Missing abstraction | Milestone M2 (type-only extraction + §17.1 conformance suite) |
| A56 | `_ob_audit_seen` grows for a whole replay run; per-cycle memory saves must be in-memory | Perf trap | Documented in C-15.6 (behaviorally inert; must **not** be cleared mid-run) |
| A57 | The forming-candle strip assumes a forming candle exists; if Binance returns none, live silently analyzes an hour-stale frame | Unreproducible quirk | Declared divergence §13.9 |
| A58 | **Universe reconstruction needs `qv` for all listed perps** — downloading only today's top 30 bakes in survivorship + look-ahead permanently | Dataset design | ✅ Closed by C-6.9 (data-scope requirement + eligibility + name tie-break) |

---

## Validated Design Decisions

*Intentionally kept. Documented so a future contributor does not "clean these up"
by accident.*

- **Contract B — `entry_price` is a structural reference, not an order price**
  (Decision #001). The system is advisory. Do **not** "fix" this by treating entry
  as a fill or adding execution — that is a separate, deliberate Phase-12 choice.
- **Layered purity: analysis is pure, the clock is injected, lifecycle owns all
  state.** Keeps the strategy deterministic and testable/backtestable. Any new
  logic must respect the layer that owns it; never read the wall clock in
  analysis/lifecycle.
- **Immutable domain objects + `LifecycleDecision` (declarative) + single-writer
  `SignalStore`.** Eliminates hidden-state bugs and makes replay/backtesting sound.
- **`OBAuditRecord` is observability-only** (Decision #010) — never read into a
  decision. Enrich it freely (BUG-2) without fear of altering behavior.
- **H1-gated miss increment** (Decision #006) — a miss accrues only once a new H1
  has closed, preventing intra-candle thrash. Keep the *gating* even when the decay
  *semantics* are reworked (A19/A20).
- **Choppy-market suppression exists on purpose** — the −2 penalty exists to demote
  low-quality ranges. Keep the concept; only its calibration is open (Phase 10).
- **Session bonus is an injected flag, not a clock read** — preserves analysis
  purity. Keep it injected.
- **First-match OB determinism** (Decision #002) — kept for determinism, but **under
  review**: A9 shows freshness should factor into *selection*. Change selection
  deliberately (with a measurement), not casually.

---

## Future Research Ideas

*Experimental concepts only. No approval, no implementation implied.*

- **X1 — Inducement / IDM detection** feeding entry confluence (absent in V1; a
  core modern-SMC concept).
- **X2 — FVG mitigation / fill tracking** as an entry trigger (or a decision to
  drop FVG if it proves valueless — A24).
- **X3 — Multi-timeframe order blocks** (D1 zones gating H1 entries) for higher-TF
  confluence.
- **X4 — Probabilistic / ML score calibration** over engineered features — only
  after a measured baseline exists (depends on the scoring re-derivation).
- **X5 — Regime detection via a continuous score** (instead of discrete votes) to
  remove the cliff (A3) at the source.
- **X6 — Outcome-labeled signal store** — every emitted signal tracked to a
  resolved outcome (TP-first / SL-first / expired), the substrate for all
  supervised calibration.

---

## Evaluation Infrastructure & Workflow

*How the strategy gets measured. Built incrementally; the Signal Ledger (Phase
7.1) is the first piece.*

> **Authoritative spec:** the full execution/fill/outcome semantics, replay
> clock, historical-data requirements, lifecycle capture, ledger lineage,
> backtester architecture, testing architecture and metrics inventory are
> formalized in **`REPLAY_CONTRACT.md` v1.1** (2026-07-27) — that contract is
> **binding** and supersedes the sketch below wherever they differ. The sections
> below remain only as narrative rationale; **do not implement from them.**
> In particular the outcome-labeling sketch below predates the contract: the
> normative rules are C-4.0–C-4.14 (fill/exit), C-9.6 (R arithmetic) and
> C-9.7 (`OutcomeConfig`), and the join key is C-9.4, not `(symbol, h1_last_ts)`
> alone.

### The Signal Ledger (built — Phase 7.1)

Every completed analysis (one record per analyzed symbol per cycle, **all tiers**
including SKIP) is appended to `signal_ledger.jsonl` — a permanent, append-only,
write-only dataset. It is an observability contract (schema in
`domain.signal_ledger.SignalLedgerRecord`, sink in
`infra.persistence.append_signal_ledger`, assembled by `app.ledger`), never read
back into a runtime decision. It is **separate** from the Telegram alerts,
`active_pois.json`, `killed_thesis.json`, and `retracement_ob_audit.jsonl`, all of
which remain.

Each record carries the full decision context: decision/reason/score/**notes**,
direction/entry_mode/regime, HTF (weekly/d1/h4/location, momentum, direction
source, bull/bear, veto), the confirmation booleans (**sweep/fvg/displacement/
fresh_ob/volume_spike/target_clear/htf_context_clean**), levels
(entry/sl/tp/entry_source/tp_source/**rr**/tp_rr), the selected OB
(top/bottom/index/rank/fresh + candidate count), session, live_price, atr_last,
and provenance (`schema_version` + `strategy_version`). This is enough to answer
the feature-value questions (does sweep matter? does fresh_ob? which notes predict
winners? which regime performs best? which skip reasons are good?) **without ever
reopening a chart** — once outcomes are labeled.

### Outcome labeling (NOT in the ledger — derived later)

The ledger stores *what the strategy decided*, never an outcome. WIN / LOSS / BE /
EXPIRED / NOT_FILLED are computed **offline** by replaying forward price against
`entry_price` / `sl` / `tp` / `direction`, and joined to the ledger by the
*canonical row* rule — `(run_id, symbol, h1_last_ts)`, disambiguated for live data
by `cycle_index` (contract C-9.4; the walk starts at `W0`, the candle opening at
`T`, per C-4.0 — **not** at `h1_last_ts`). Keeping labels out of the append-only file is
deliberate — the ledger stays an immutable record of decisions; the labeled
dataset is a separate, reproducible derivation that can be regenerated whenever the
labeling rules improve.

Most of labeling is **fully automatable** (SL/TP-first resolution, expiry via the
168-H1 holding cap — *not* the lifecycle TTL, which cancels *pending* entries
(C-4.8) rather than closing filled ones (C-4.10) — and NOT_FILLED when price never
reaches the structural entry before the thesis is evicted; Contract B means entries
are limits, not fills). What needs judgment is
only the **fill/slippage model** (assumptions about whether a limit at a structural
level would realistically fill, and by how much) and the **holding-window / BE
policy** — these are modeling choices to fix once and version, not per-signal chart
reading.

### The workflow (evidence-driven; each stage gates the next)

1. **Collect** — run V2 live, accumulating ledger records (operational validation +
   signal-distribution sanity; live collection is *not* the statistical baseline —
   too slow, too few, no counterfactuals).
2. **Label** — replay forward price → outcome-labeled dataset (automated).
3. **Baseline** — the **backtester** (Phase 8), replaying `analysis`+`lifecycle`
   over long history and emitting the **same `SignalLedgerRecord` schema**, is the
   statistical baseline. Build it against out-of-sample history, do not tune it to
   the small live sample. Freeze + version the baseline metrics (git tag + a
   committed metrics file).
4. **Refine** — one change at a time, each with a pre-registered hypothesis and
   success criterion, measured out-of-sample against the frozen baseline, logged in
   the Experiment Log below (successes and failures). Bump `strategy_version` so
   records segment cleanly by strategy.

Because the live ledger and the backtester share one schema and one analysis
pipeline, a query like "expectancy of VALID retracements with a sweep vs without"
runs identically over live or simulated records — that shared schema is the whole
point of building the ledger first.

**At 500 signals:** enough to sanity-check distribution (tier mix, regime mix,
frequency, obvious bugs) but **not** enough to conclude on expectancy. Do not
optimize on it — its role is to confirm the pipeline is sound and to shake out
operational issues while the backtester (the real evaluation engine) is built.

---

## Experiment Log

*One row per attempted change, success or failure. Negative results are kept —
they stop us re-treading dead ends. Populate from Phase 9 onward.*

| Date | Change (ID) | Hypothesis / Reason | Baseline | Backtest result | In/Out-of-sample | Decision |
|------|-------------|---------------------|----------|-----------------|------------------|----------|
| _(empty — first entry lands when Phase 9 begins)_ | | | | | | |

---

*Maintain this file continuously: log every bug found, every weakness confirmed
or refuted, and every experiment run. This is the strategy's institutional memory.*
