# STATE TRANSFER — SMC Signal Radar

> **Audience:** a reviewer with zero prior context who may spend weeks in this codebase.
> **Purpose:** hand over architecture, reasoning, findings, assumptions, risks and open work — not implementation detail.
> **Written:** 2026-08-13. **Reflects:** official baseline `replay-9ec9cd4487f3ba19` and full variant replay `replay-a43e3f04e58ec9ad`.
>
> **Read order:** this file → `PROJECT_STATE.md` (living checkpoint, v3.15) → `REPLAY_CONTRACT.md` (1,885 lines, the binding contract) → `SMC-BOT-DOCS/` (6 frozen specs) → `STRATEGY_NOTES.md` → `MODULE_INTENT.md`.
> **Do not edit `SMC-BOT-DOCS/`.** Amendments are proposed patches awaiting explicit approval.

---

## 1. Project Overview

**What it is.** A Python **advisory** signal generator for Binance USDT-M perpetual futures. It scans a volume-ranked universe once an hour, evaluates Smart-Money-Concepts (SMC) setups, and pushes Telegram alerts. **It places no orders, holds no positions, and has no position sizing.** Every "trade" in this repo is a *counterfactual resolution* of an advisory signal against stored prices.

**Core objective.** Produce advisory signals with positive expectancy measured in R (risk units), verified reproducibly against a frozen historical corpus.

**Trading philosophy (as designed).** Top-down SMC: D1/weekly establishes directional bias → H1 establishes regime, point-of-interest and entry → M5 optionally confirms. Two execution modes: **CONTINUATION** (trade with an expansion) and **RETRACEMENT** (trade a pullback into a zone). A signal is scored on additive confirmations, bucketed into `A+ / VALID / WATCH / SKIP`, and competes for one of 10 memory slots.

**Current production version.** `strategy_version = "2.2.0"`. V1 (`main.py`, `src/`) is **deprecated but still in the tree** as the differential oracle for the migration; V2 (`v2/`) is the only implementation that is optimised or run. The backtest/replay harness lives in `backtest/`.

**Headline state of the strategy:** measured on 31,209 admitted trades, expectancy is **−0.0087 R** with a **32.21%** win rate against a breakeven of **32.50%** — and against a driftless-martingale null for its own geometry (`1/(1+RR)`) it is **−1.20pp**, i.e. the system carries no net directional information. Costs are modelled as exactly zero. See §7.

---

## 2. Locked Architecture

These are invariants. Violating any of them either breaks a frozen contract or silently invalidates every prior measurement.

### 2.1 Layering
`domain ← infra ← analysis ← lifecycle ← app`, strictly acyclic.
- **Rule A8:** `v2/` never imports `backtest/`. `backtest/` may import `v2/` (currently only 4 modules do: `anchor.py`, `identity.py`, `screen.py`, `scoring_variant.py` — pinned by `tests/backtest/test_experiments.py`).
- `v2/domain` depends on nothing. `v2/infra` is the only V2 code allowed to import `src/` (V1).
- Runtime never imports `tests/`.

### 2.2 Analysis purity (`tests/analysis/test_analysis_purity.py`)
`v2/analysis/*` may not perform I/O, read a clock, hold state, cache, or import numpy. Allowed: `pandas`, `math`, `types`. Consequences that look like bugs but are deliberate:
- `session_active` is an **injected input**, never derived from wall clock.
- ATR is **read** from an existing frame column; nothing in analysis computes ATR.
- The bias memo cache from V1 was removed (pure memoisation, behaviour-neutral).

### 2.3 Continuation-first philosophy
CONTINUATION is the privileged mode: `expansion_*` regimes map to it, its displacement gate is a hard requirement, and it is the only arm whose measured information content is non-negative (+0.0036 vs null, ≈1σ). RETRACEMENT is the secondary mode and is measurably anti-predictive (−0.0386, ≈9σ). **Do not propose changes that shift volume from CONTINUATION to RETRACEMENT** without evidence that the retracement POI has been rebuilt.

### 2.4 HTF dominance
Direction is decided first (pipeline stage 1 of 9) and gates everything downstream. Weight hierarchy is fixed: **weekly 4.0 > D1 2.0 > H4 1.0**, location ±1.5/±0.5, H1 fallback +1.0. Thresholds: `min_score_for_direction = 4.0`, `min_gap = 1.0`. `direction is None` ⇒ no signal at all. A hard veto kills SHORT-into-bullish-expansion and LONG-into-bearish-expansion.

### 2.5 Closed-candle analysis rules
- Analysis sees **only closed candles**. V1 strips the forming candle (`iloc[:-1]`); the forming-candle price is carried separately as `live_price` and used only by the lifecycle (kill/stale), never by analysis.
- `C_ref` is the last closed H1 candle, the analysis input. `W0` is the candle *forming at the scan instant*; `open(W0)` is the print one second before the scan — a bounded look-behind, never a look-ahead (`REPLAY_CONTRACT` C-4.0).
- **C-4.13 event ordering:** intra-candle price events precede that candle's close-scan lifecycle events. A fill during `Wk` beats a cancellation issued by the scan at `Wk`'s close.
- The walk never reads through the analysis market adapter (whose 119/499/199 fetch limits and ATR column are analysis concerns only).

### 2.6 `main.py` ownership boundaries
V1's `main.py` is the legacy monolith. V2's `app/orchestrator.py` is **thin coordination only** (Architecture Principle 12):
- It may not branch on any analysis output. Concretely: V1 fetched M5 only when `score >= 8`, but that gate changes the signal tier, so the orchestrator **always** fetches M5 and `analyze()` owns the gate internally. Behaviour-identical, cadence-different.
- It owns operational conditionals and transient operational state only — e.g. the OB-audit dedup set keyed `(symbol, h1_last_ts, "RETRACEMENT")`.
- Error isolation is V1 parity: adapters *surface* errors, the orchestrator *re-swallows* at the edge (per-symbol continue, universe error → skip cycle without saving, notifier/persistence failures swallowed).

### 2.7 Lifecycle ownership boundaries
- `SignalStore` is the **single writer** of active signals and killed theses. Nothing else mutates them (Decision #005).
- Analysis is pure and produces a `Signal`; the `Reconciler` turns `(Signal, MemoryView, HTFContext, live_price, h1_last_ts)` into a `LifecycleDecision` (a list of mutations + alerts + log events). It is a verbatim transcription of V1 `main.py:316-764` and is pinned by an equivalence test driving both implementations over 12 seeds × 60 cycles × 6 symbols.
- Lifecycle predicates are defined **once**, in `v2/lifecycle/rules.py`.

### 2.8 Other locked invariants
| Locked thing | Value / rule |
|---|---|
| Corpus & snapshot | `data/stage1`, `data_snapshot_id = c8542cb360d13207` |
| Replay window | `T_0 = 1672531200` (2023-01-01) → `T_n = 1782687600` (2026-06-28 23:00), **30,600 cycles** |
| `burn_in_h1` | 336 |
| Universe | `volume` mode, `top_coins_limit = 30`, `universe_id = 7fd26361c467d048` |
| Lifecycle config | `max_open_positions 10`, `miss_count_limit 3`, `kill_cooldown 3600s`, `untapped_ttl 24h`, `tapped_ttl 6h`, `manual_action_ttl 10h` |
| `OutcomeConfig` + hash | `39761eb79ae5432e` — changing it segments the dataset (C-4.12) |
| `core` for `run_id` | **A closed list (C-16.2).** Do not extend it. `run_id = "replay-" + sha256(canonical_json(core))[:16]` |
| Frozen artefacts | replay engine, audit system, capture schema, bundle format, `data/baselines/v2.2.0-official/` |
| Dependencies | **None may be added.** `requirements.txt` is the C-19 determinism contract; editing it is a re-baselining event that makes all prior baselines incomparable. No pytest — stdlib `unittest` only. No PyYAML. |
| Test command | `venv/Scripts/python.exe -m unittest discover -s tests -t .` |
| Frozen docs | `SMC-BOT-DOCS/` — never edited; all patches P1–P5 already applied with approval |

---

## 3. Trading System Design

Pipeline order (declared once, in `v2/analysis/pipeline.py`): **htf → regime → structure → liquidity → order_block → fair_value_gap → entry → risk → scoring**.

### 3.1 Thesis lifecycle
- **Purpose:** hold at most 10 live theses, refresh or replace them as new scans arrive, and retire them.
- **Inputs:** fresh `Signal` + `HTFContext`, current memory view, `live_price`, `h1_last_ts`, clock.
- **Outputs:** ordered mutations (`Insert / Replace / PartialRefresh / IncrementMiss / Evict`), alerts, log events.
- **Flow:** KILL check → STALE check → memory engine (kill-cooldown → same-thesis → insert / replace-weakest / cap) → GUARD/decay → end-of-cycle TTL sweep + killed-thesis prune.
- **Critical assumptions:** `miss_count` is H1-gated and serves a dual role (Decisions #006/#007); decay is silent (#008); a same-thesis partial refresh **never** resets `miss_count` and **never** downgrades a decision; an alert fires only if an insert actually happened (a CAPPED signal is silent); `PROMOTED_RESIDENCY_CYCLES = 3` is a **declared assumption**, not a measurement.
- **Known weaknesses:** thesis lifetime is decoupled from *trade* lifetime — a thesis can be evicted while its trade walks for up to 168 H1 candles, so `max_open_positions = 10` does not cap exposure (measured peak 40 concurrent, mean 11.0; see §8 D5).

### 3.2 POI generation
- **Purpose:** locate where price should be engaged.
- **Reality:** there is **no HTF POI layer at all.** No D1/H4 order blocks, no HTF FVG, no HTF liquidity map. The only POI generator is the 10-bar H1 order-block scan (3.3); the only level lookup is an H1 swing/PDH-PDL scan over a 180-bar tail used for targets (3.8).
- **Known weakness:** the stated "top-down" design supplies a *direction* from the HTF and never a *location*. There is no mechanism by which a higher-timeframe level can be the reason for a trade — which is where SMC's claimed edge is supposed to live. **This is the largest architectural gap in the system.**

### 3.3 OB generation
- **Purpose:** identify a demand/supply zone to enter from.
- **Inputs:** H1 frame, direction, `atr_last`, last closed close (`current_price`).
- **Outputs:** the full candidate tuple with one marked `is_selected`, plus derived `fresh_ob`.
- **How:** scan the 10 bars ending one bar before the last, newest-first; register **every candle of the opposing colour** (LONG → down candle, `top=open, bottom=low`); first candidate passing the quality filter wins (Decision #002, frozen). Filter: `size ≥ 0.25·ATR`, `not_lost` within `0.20·ATR`, `|price − mid| ≥ 0.15·ATR`. Freshness = no later close through the zone, recomputed from scratch each cycle.
- **Critical assumptions:** that candle colour within 10 bars identifies institutional interest.
- **Known weaknesses:** **an "order block" here is any opposite-coloured candle** — no impulse/BOS origin requirement, no displacement following it, no inefficiency requirement, no mitigation model, no cross-cycle persistence, no HTF blocks. Freshness is **not** part of selection, so a stale block wins the slot and blocks every fresher candidate behind it, killing the setup (`ob_quality_failed`). This is the mechanism behind the retracement arm's negative edge.

### 3.4 Displacement logic
- **Purpose:** detect an impulsive move worth continuing.
- **Inputs:** one candle, direction, `atr_last`. **Outputs:** boolean.
- **How:** `body ≥ 0.60·ATR` **and** `body/range ≥ 0.50`, correct colour. That is the whole test.
- **Known weaknesses:** it is a **candle-size test, not a structural break** — no level is broken, no swing referenced, no volume or follow-through required. It nonetheless carries the largest single scoring weight (**+4** in CONTINUATION), gates the CONTINUATION mode entirely, and decides the RETRACEMENT A+ tier. There is **no BOS or CHoCH engine on H1** anywhere in the system; `choch` exists only as an M5 flag.

### 3.5 Continuation logic
- **Purpose:** buy the first shallow pullback inside an expansion.
- **Inputs:** H1 frame (last 3 bars), direction, `atr_last`.
- **Outputs:** `entry_ready`, `entry_price = pullback_close`, `entry_source = "continuation_micro_pullback"`, `displacement_index`, `pullback_index`.
- **How:** find a displacement candle in the last 3 bars (skipping the last bar); then scan forward for a bar that retraced to within `0.10·ATR` of the displacement close **and** closed back beyond it. Entry is that bar's close, filled as a **market order** at `open(W0)`.
- **Critical assumptions:** a 1-candle impulse plus a 1-candle reclaim is a continuation; entry at a close is executable.
- **Known weaknesses:** momentum-chase entry with a stop referenced to a single bar; requires no HTF location, no POI, and no confluence. Empirically the *least bad* arm (E +0.016, at-null).

### 3.6 Retracement logic
- **Purpose:** enter a pullback into a zone.
- **Inputs:** direction, H1 frame, `atr_last`, OB candidate set, HTF, regime label.
- **Outputs:** `entry_ready`, `entry_price = block.top` (LONG) / `block.bottom` (SHORT), `entry_source = "retracement_ob"`.
- **How:** a rotational-LONG guard (HTF conflict requires `discount`), then `retracement_ob_valid` = fresh selected block re-passing the quality filter. Effectively `ob_valid == fresh_ob`.
- **Critical assumptions:** the zone is meaningful (3.3); the near edge is the right entry; a limit at the near edge is executable.
- **Known weaknesses:** **sweep and FVG are not entry gates** (verified — they are scoring inputs only), so nothing requires liquidity to have been taken before entry. Nothing checks the POI is still untouched, so signals generated with price already through the edge fill immediately at market (4,651 trades, E −0.078). A successful entry leaves `entry_reason = "no_entry"` (V1 quirk, preserved).

### 3.7 Stop placement
- **Purpose:** define 1R.
- **How:** CONTINUATION → `pullback_low − 0.30·ATR` (LONG). RETRACEMENT → `selected_block.bottom − 0.70·ATR`.
- **Critical assumptions:** the reference bar / zone edge is structurally meaningful; a fixed ATR buffer is sufficient noise protection.
- **Known weaknesses:** no minimum stop distance, no volatility floor, no cap. Stop width ranges from 0.27% to >7% of price (median 2.4%), so **1R is not a comparable unit across trades** — and the R-normalised transaction cost varies by more than an order of magnitude with it. No trailing stop, no break-even, no partials, no time stop below 168 candles, no position sizing (`equity_convention = "1R"` flat). V1 never had any of these and none were invented.

### 3.8 Target placement
- **Purpose:** pick a take-profit at liquidity.
- **Inputs:** H1 frame tail (180 bars), direction, entry, swing sets, ATR, the stop.
- **Outputs:** `tp`, `tp_source ∈ {EQH_cluster, EQL_cluster, SWING_HIGH, SWING_LOW, PDH, PDL, fallback_2r}`, `tp_rr`.
- **How:** collect EQH/EQL clusters (score 3.0, +1 if count ≥2), swings (2.0), PDH/PDL (3.5); pull each back by `0.10·ATR`; require `RR ≥ 1.2`; CONTINUATION prefers the **nearest** passing target, RETRACEMENT prefers the **strongest**. If nothing passes, fall back to a flat **2R**.
- **Critical assumptions:** resting liquidity is reachable; hand-set strength constants rank reach probability; distance needs no ATR normalisation.
- **Known weaknesses:** there is **no reach-probability term anywhere**. Empirically the fallback that ignores liquidity entirely is the best source (+0.031 R) and the upside liquidity targets are the worst (EQH −0.075, PDH −0.081). Note `find_liquidity_target` re-derives PDH/PDL from the H1 frame independently of the D1-based `htf.pdh/pdl`; the two can disagree.

### 3.9 Slot competition
- **Purpose:** allocate 10 memory slots among competing theses.
- **How:** insert if a slot is free; else replace the weakest active thesis (min score) if the new one is stronger; else `CAPPED` (recorded as a shadow, no alert). Alert tiers rank `A+ > VALID > WATCH`; a same-thesis arrival at an equal-or-lower tier triggers a partial refresh instead.
- **Critical assumptions:** score ranks trade quality.
- **Known weaknesses:** the ranking key is a score with almost no rank correlation to outcome (A+ vs VALID = 0.015 R; within RETRACEMENT the relationship is **inverted**). It does add value — same-cycle paired comparison shows admitted −0.030 vs CAPPED −0.055 — but both are negative, and 35.7% of the time the entire open book is on one side. **There is no portfolio construction:** no correlation control, no sector/beta limits, no sizing.

### 3.10 Thesis death causes
| cause | trigger |
|---|---|
| **KILL** | SL breach + `0.30·ATR` buffer, **or** D1+H4 bias both flipped against the direction. Writes a 1h cooldown keyed `symbol\|direction\|entry_mode` |
| **STALE** | pre-memory invalidation check against the fresh HTF reading |
| **DECAY** | `miss_count` reaches `miss_count_limit = 3` (H1-gated increments), silent |
| **REPLACE** | a stronger thesis takes the slot |
| **SUPERSEDED** | same symbol, different thesis |
| **TTL** | untapped 24h / tapped 6h / manual-action 10h |
- **Known weakness:** cancellation ends the *thesis*, not the trade — 3,665 admitted-eligible signals ended `NOT_FILLED` this way (DECAY 1,254, SUPERSEDED 1,195, REPLACED 1,133, TTL 66, KILL 17), and filled trades keep walking after their thesis dies.

---

## 4. Replay Framework

This is the strongest part of the project. Treat it as trustworthy and treat its contract as binding.

### 4.1 Corpus
`data/stage1` — 797 symbols, D1/H1/M5 klines, `data_snapshot_id = c8542cb360d13207`. 30,600 hourly boundaries, 2023-01-01 → 2026-06-28. `T_n` sits exactly 168h (`max_holding_h1`) before the corpus end, so **every trade resolves and there is no censored open tail**. Universe is re-derived per cycle from stored volume (`universe_id` pins it); 640 symbols were guard-skipped at `T_0` for insufficient history. **C-6.10: a rebuilt corpus is a different dataset.**

### 4.2 Replay architecture
`backtest/replay/` drives the real V2 orchestrator cycle-by-cycle against the corpus through a historical adapter. `run_id = "replay-" + sha256(canonical_json(core))[:16]`, where `core` is a closed field list (C-16.2) including `strategy_version` and excluding `universe_id`, `caching_analyzer`, `max_swallowed_errors` and `scoring_config`. `CachingAnalyzer` provides permitted memoisation (C-8.4) at 1.91×; vectorised swings add 4.18× (proven bit-exact over 586,684 comparisons). Full replay ≈ **7.8 hours**. Outputs a 9-file bundle: ledger, lifecycle events, memory snapshots, alerts, OB audit, run health, active/killed state, manifest.

### 4.3 Lifecycle validation
`lineage.py` reconstructs intra-cycle ordering from the event stream (C-16.1) and re-derives memory state at every cycle; the Step-8 shakedown proved exact lifecycle reconstruction for all 30,768 cycles. Shadow tiers (C-2.2) record what the lifecycle *suppressed* — `UNALERTED_INSERT`, `CAPPED`, `KILL_COOLDOWN` — each with a fixed `shadow_pending_h1 = 3` window, so suppressed populations remain measurable (82,954 shadow trades in the baseline).

### 4.4 Identity validation
Determinism is conditioned on version identity (C-19). Two idioms are used:
- **Full identity:** re-derive scores from recorded `notes` and compare to recorded `score` across all 886,954 ledger rows. This works because `score_signal` is a sum of independent terms each appending exactly one note, so the recorded `notes` list **is** the term list. `rescore.WEIGHTS` is deliberately an independent second reading of the weight table, not an import, so the test is a real comparison rather than a tautology.
- **Prefix replay:** ~2,000 cycles (~30 min) compared row-for-row against the equivalent prefix of a reference run. Exactly comparable because both arms start from empty memory. Used as the pre-flight gate before any 7.8-hour run.

### 4.5 Audit system
`retracement_ob_audit.jsonl` (285,546 rows) records the OB candidate set and selection per retracement signal. It is an **observability contract** (Decision #010) — a sibling of `LogEvent`, never consumed by any analysis or lifecycle rule. Assembled by the orchestrator, deduped via orchestrator-owned transient state. Several of its fields are permanently `None` by faithful V1 reproduction (`window_bounds`, `selected_ob_index/rank/top/bottom/size`, `current_price`, `location`) because V1's reader and writer used different key names. **This is preserved deliberately; do not "fix" it.**

### 4.6 Metrics pipeline
Two stages after a replay: `outcome` (walks each signal against stored prices → `outcomes.jsonl`) then `metrics` (aggregates → `metrics.json` + `report.md` + `segments.csv`).
- Populations: `all → evaluated → resolved → scored`. The **admitted book** is `shadow_kind is None`, not `in_burn_in`, `realized_r` present.
- The walker resolves *everything ambiguous against the trade* (C-4.3): SL first inside an M5 candle, SL when the M5 set is incomplete, LOSS on a limit-fill candle that could also have hit SL unless M5 proves otherwise. Measured worst-case resolutions are small: 2.30% of CONTINUATION and 1.38% of RETRACEMENT exits.
- Equity is per-trade 1R (C-9.8), exit-ordered. `metrics_hash` pins the output; `join_rate` must be 1.0.
- Partitions: **TRAIN** (2023-01 → 2025-06) has promotion authority; **DEV** (2025-07 → 2026-06) has **none** — it was read by every Phase 3 study.

### 4.7 Experiment framework
- **`v2/analysis/scoring_config.py`** — the `StrategyConfig` seam. A frozen `ScoringConfig` holds every scoring literal (weights per mode, tier thresholds, RR floor and bands, regime sets, alignment map). `DEFAULT_SCORING_CONFIG` reproduces 2.2.0 exactly. Threaded through `score_signal(config=)`, `ScoringStage(config=)`, `AnalysisPipeline.default(config=)`. **Weight 0 keeps the note; an absent key removes it** — that distinction is what makes neutralising a component corpus-safe and still observable.
- **`backtest/experiments/scoring_variant.py`** — loads a variant from canonical JSON, rejects unknown keys, and returns the base object itself when nothing changes (identity, not an equal rebuild).
- **`backtest/experiments/anchor.py`** — quantile-anchored threshold derivation on TRAIN only, preserving the baseline admitted fraction per mode. Deterministic, reproducible, records `predecessor_variant_id`.
- **`backtest/experiments/screen.py` + `tier0.py`** — tier-0 counterfactual screen (minutes, not hours) that re-decides admission over already-resolved outcomes.
- **Variant injection:** `python -m backtest.cli replay --variant <slug> [--experiment <root>] ...`. Variant identity travels through `strategy_version` (already a `core` member) as `2.2.0+exp.<slug>.<variant_id[:8]>`, so a variant gets a distinct `run_id`. A guard refuses a variant carrying the base `strategy_version` — without it the run would hash to the baseline's `run_id` and `--overwrite` would delete the frozen baseline's sinks.
- **Tier model:** tier 0 = re-decide admission (minutes) · tier 1 = re-resolve exits, paired · tier 2 = full replay (7.8h).

### 4.8 Promotion methodology (respect this; do not invent metrics)
1. **Volume-matched control** — drop the same number of baseline trades by lowest score. The cheapest rule with no hypothesised skill. Any variant must beat *this*, not the baseline.
2. **Exact attribution split** — `Δtotal_R = (n_v − n_b)·E_b` [exposure] `+ n_v·(E_v − E_b)` [quality].
3. **Quarter sign test** — per-quarter ΔE vs the control over TRAIN, one-sided binomial with *derived* thresholds: 10 slices → promote at 9 (p=0.0107), flag at 8 (p=0.0547), reject at ≤7. Slices with n < 10 quarters are underpowered (p-floor 0.0625).
4. **Power** — `n = 21.79/δ²` for 80% power at σ ≈ 1.667 R. At n = 29,146 the MDE is **±0.0273 R**.
5. Preregister direction and magnitude before running. Do not move goalposts.

---

## 5. Completed Work

| Milestone | Goal | Result | Acceptance |
|---|---|---|---|
| **V1→V2 migration (Phases 1–6)** | Behaviour-preserving rewrite into the layered architecture | 18 frozen domain objects, 4 infra adapters, 9 pure analysis stages, lifecycle layer, thin orchestrator. Equivalence proven per phase against unmodified V1 | **ACCEPTED.** V2 is the only implementation |
| **Golden oracle / regression harness** | Diff observable V1↔V2 on identical inputs | `tests/regression/` reads V1's own `debug` dict, so V1 needed no instrumentation. Proven on a committed fixture + 400-snapshot sweep | **ACCEPTED** |
| **Continuation lifecycle refactor** | Single-writer lifecycle with V1-exact semantics | `SignalStore` + `Reconciler`; V1 reference transcription drives both implementations over 12 seeds × 60 cycles × 6 symbols asserting identical memory, killed store, alerts and event sequence | **ACCEPTED — semantics now LOCKED** |
| **Stage-1 corpus + acquisition** | Frozen, verified corpus | 797 symbols, `corpus verify` PASS, 1h invariant preserved, `universe_id` stable | **ACCEPTED** |
| **Phase 0 optimisation + official baseline** | Reproducible reference run | `CachingAnalyzer` 1.91× (9 bundle files byte-identical), vectorised swings 4.18× (586,684 bit-exact comparisons, 0 mismatches). Official replay `replay-9ec9cd4487f3ba19`: 30,600 cycles, 886,954 ledger rows, 118,201 outcomes, 40,587 alerts | **ACCEPTED — frozen at `data/baselines/v2.2.0-official/`** |
| **Scoring seam implementation** | Parameterise every scoring literal without changing behaviour | `scoring_config.py` (~460 lines) + threading; `scoring.py` net *shorter*. Identity: 886,954 rows, **0 score mismatches**; prefix replay 158,456 rows, **0 mismatches** | **ACCEPTED** |
| **Variant framework** | Load experiment configs safely | Canonical JSON only, unknown keys rejected, unimplemented inputs (`gates`, `quantile_anchored`) **refused rather than silently dropped** | **ACCEPTED** |
| **Anchor derivation** | Thresholds that hold admitted volume constant | TRAIN-only, deterministic; ties resolve upward; a_plus ≥ valid ≥ watch clamped | **ACCEPTED with a caveat:** integer thresholds cannot volume-match — achieved ±9pp, and one integer step moves 19–24pp of RETRACEMENT |
| **Screen / Tier-0 framework** | Cheap pre-replay filter | Exposed that tier 0 can only simulate *removals* — SKIP→admit rows were never inserted, so they have no realized R. Observable share: s2 32%, s3a 7%, **s3b 61%** — which is why s3b was the only defensible replay candidate | **ACCEPTED, but calibration failed in practice** (see §7) |
| **Variant replay injection** | Run a variant end-to-end | 4 hunks (`build_analyzer`, `ReplayConfig` + guard, driver, CLI). Prefix validation reproduced all 6,445 changed decisions from the variant's own ladder; 2,262 upgrades all momentum rows, 4,183 downgrades all non-momentum | **ACCEPTED** |
| **Full s3b replay** | Test the momentum hypothesis at tier 2 | `replay-a43e3f04e58ec9ad`: 30,600 cycles, 886,954 ledger rows, 1,154,629 lifecycle events, 37,929 alerts | **RUN COMPLETE — variant REJECTED** |

---

## 6. Experiment Status

Family root: `experiments/v2_core_rebuild/scoring_v2/`. Each arm has `config.json` + `preregistration.json`.

| arm | variant_id | hypothesis | status | result | next action |
|---|---|---|---|---|---|
| **baseline** (2.2.0) | — | reference | **FROZEN** | n=31,209 · E **−0.00874** · total **−272.7 R** · wr 32.21% · maxDD 655.8 · recovery −0.416 · 2/10 TRAIN quarters positive | Never re-run. Re-derive nothing from it without the `OutcomeConfig` hash check |
| **s0-control** | `44136fa355b3678a` | identity — must reproduce baseline | Available, not replayed | — | Use as the null arm if any future variant is replayed |
| **s1-dead-weights-zero** | `af31342b3101d05f` | zeroing never-firing components changes nothing | Not replayed | — | **Superseded.** §7 explains the mechanism: `discount`/`premium` are unreachable *by construction*, so the correct action is to fix the location contradiction, not to zero the weights |
| **s2-uninformative-zero** | `4d08e5afcc31af0f` | zeroing uninformative components + re-anchoring thresholds improves ranking | Anchored + screened; **not replayed** | Tier-0 observability only **32%** — most of its change is invisible at tier 0 | **Do not replay.** Its premise (the score can be improved by reweighting) is falsified by §7 |
| **s3a-momentum-zero** | `f483fac21274f00d` | `momentum_weakening` carries no signal → weight 0 | Anchored + screened; **not replayed** | Observability only **7%** | **Do not replay.** It exists only to supply the monotone middle point of a hypothesis already rejected at its endpoint |
| **s3b-momentum-positive** | `fc05024bb0712c2a` | `momentum_weakening` is *positively* predictive → weight −1 → +1, thresholds CONT 15/12/9, RETR 16/13/10. Preregistered: `increase`, **+0.010 R** | **FULL REPLAY COMPLETE** | See §7. **REJECTED** | Closed. Do not re-litigate |
| **s4-fvg-gate** | `f4733123554dfba3` | requiring FVG as a hard gate improves quality | **BLOCKED — not implementable** | The seam has no gate mechanism; the loader refuses the `gates` key rather than dropping it | Leave blocked. A hard gate is a *structural* change and belongs with the POI rebuild, not with scoring |
| **s5-scoring-v2** | `9eadd803b9b3b0ec` | the full proposed rescoring | **BLOCKED** (depends on s4) | — | Leave blocked |

**Programme-level recommendation:** stop the scoring-experiment programme. Its arms move expectancy by 0.001–0.003 R against an MDE of 0.027 R and an unmodelled cost of 0.088–0.163 R. The infrastructure is sound and will be reused; it is pointed at the wrong variables.

---

## 7. Latest Replay Findings

### 7.1 The s3b promotion gate (full replay `replay-a43e3f04e58ec9ad`)

**What changed.** Exactly two things: `momentum_weakening` weight −1 → +1, and both tier ladders raised by one point (CONT 14/11/8 → 15/12/9; RETR 15/12/9 → 16/13/10). Verified: `notes` unchanged on every row (a weight change moves `score`, not `notes`), every score delta exactly +2 where the flag fired, and 111 rows at +3/+4 explained by the `score >= 8` M5-enrichment gate flipping on.

**What improved.** Total R rose from −272.7 to −267.8 (+4.96 R); win rate +0.56pp; recovery −0.416 → −0.383; maxDD ratio 1.067, inside the ≤1.1 tolerance. Against the volume-matched control over the full window it gained +46.1 R with the exposure term exactly zero.

**What did not improve.** Expectancy **fell** (−0.00874 → −0.00919, Δ **−0.000448**) where +0.010 was predicted — **the preregistered sign failed.** The +4.96 R decomposes to **+18.03 R exposure and −13.07 R quality**: it came from trading 2,063 fewer times, not from better trades. The entire vs-control gain sits in **DEV** (+105.1 R), which has no promotion authority; on **TRAIN** the variant is *worse* than a no-skill control (**−0.0025 R, −59.1 R**) — in both entry modes. The quarter sign test returned **4/10 (p = 0.83)** against a promote line of 9. Only **3 of 8** primary criteria passed. **Verdict: REJECT, confidence High.**

**Why the rejection is robust rather than marginal.** The anchored thresholds were derived *on TRAIN*, so TRAIN results are optimistically biased for s3b — and it fails there anyway. And tier 0 predicted +0.00099 vs control while TRAIN delivered −0.0025: **the screen's sign did not survive on the deciding partition.** Read tier 0 as a feasibility filter, never as directional evidence.

### 7.2 The finding that reframes the programme

A hostile end-to-end audit was run after the rejection. Benchmarking each trade against the driftless-martingale null for its own geometry — `P(+RR before −1R) = 1/(1+RR)` — gives:

| population | n | null wr | observed | Δ |
|---|---:|---:|---:|---:|
| **ALL ADMITTED** | 31,209 | 0.3341 | 0.3221 | **−0.0120** |
| CONTINUATION | 19,691 | 0.3515 | 0.3550 | +0.0036 (≈1σ) |
| **RETRACEMENT** | 11,518 | 0.3043 | 0.2657 | **−0.0386 (≈9σ)** |
| LONG | 19,609 | 0.3339 | 0.3063 | −0.0276 (≈8σ) |
| SHORT | 11,600 | 0.3343 | 0.3486 | +0.0143 (≈3σ) |

**The system carries no net directional information.** Its win rate is *below* what a coin flip with the same stop/target geometry would produce. Maximum adverse-resolution bias is 2.30% (CONT) / 1.38% (RETR), so RETRACEMENT's deficit exceeds its worst-case measurement bias by ~2.8×.

**Why this matters more than any experiment result.** Expectancy = f(information). Under the martingale null, *every* geometry, stop rule, partial, target and position size has expectancy exactly zero pre-cost. Nothing can be calibrated, managed or sized into profit from a base of zero information. And the evaluation model charges **zero cost**: `taker_fee_rate = 0.0`, `slippage_rate = 0.0` (non-zero values are refused because C-9.6 defines no rule for where slippage enters a fill), `funding_rate_model = "none"`. At a measured mean notional/R multiple of **88.2**, realistic friction is **0.088 R/trade** (taker both sides) to **0.163 R/trade** (taker + slippage + funding) — **10× to 19× the entire measured baseline deficit, and 3× to 6× the MDE.**

Concentrated losses, for orientation: trades resolved within one candle **−1,521 R** (16% of the book, E −0.305, 24.5% wr vs 34.9% null); upside liquidity targets (EQH+PDH) **−760 R**; RETRACEMENT×LONG **−663 R**; planned RR ≥ 3.0 **−522 R**; CONTINUATION×LONG **−412 R**. Path asymmetry: mean MFE +1.58 R vs mean MAE −1.40 R; **49.4% of trades reach +1R and only 32.2% finish positive** — 38% of the +1R cohort ends negative. (Arithmetic partial/break-even counterfactuals show +0.06 to +0.14 R but they **peek** — they credit the runner the final R even when the path returned through entry first. Treat them as upper bounds requiring tier-1 re-resolution, not estimates.)

---

## 8. Outstanding Defects

### CRITICAL
- **D1 — No cost model.** Fees, slippage and funding are all zero, and no fee-sensitivity run exists in the official bundle (`realized_r_net == realized_r`). Every expectancy figure in the project, including every experiment verdict, is 10–19× smaller than the friction it ignores. *Nothing measured before this is fixed can be interpreted as tradability.*
- **D2 — The premium/discount framework is dead code.** `location == "discount"` requires `price < weekly_eq`; `weekly_bias == "BULLISH"` requires `price > weekly_eq` — the same price and the same variable, so the two are **mutually exclusive by construction**. Confirmed empirically: `weekly_aligned ∧ discount` occurs **0 times in 31,209 admitted trades**, and the joint distribution contains no `BULLISH×discount` or `BEARISH×premium` at all. Consequences: the `+1.5` location branches in `_select_direction` are unreachable; the `discount`/`premium` scoring terms (+2 each) never fire; `htf_context_clean` is reachable only at `equilibrium`; and **LONGs can only be generated above the weekly midpoint and SHORTs only below it** — the inverse of the stated positional thesis. LONG expectancy is negative in all four years.
- **D3 — Order blocks are not order blocks.** Any opposite-coloured candle in the last 10 H1 bars qualifies; no impulse origin, no BOS, no inefficiency, no mitigation state, no HTF blocks. This is the mechanism behind a 9σ-negative retracement arm.

### HIGH
- **D4 — No HTF POI layer.** The "top-down" architecture supplies direction only, never location. The claimed source of edge is absent from the implementation.
- **D5 — Exposure is uncapped.** `max_open_positions = 10` limits *theses*; trades keep walking for up to 168 candles after their thesis dies. Measured: time-weighted mean **11.0** concurrent positions, **48.3% of open time above the declared cap**, peak **40**, 35.7% of observations entirely one-sided, `max_consecutive_losses = 51`. Reported drawdown and recovery therefore understate the risk of any capital-based implementation.
- **D6 — RR geometry is rewarded in the wrong direction.** Hit rate falls faster than RR rises (RR<1.5 → E +0.024; RR≥6 → E −0.180) while scoring pays +2 for RR ≥ 3.0 and `target_clear` pays +1 for a distant target (marginal delta −0.035). Three independent mechanisms all push toward the losing geometry.
- **D7 — No trade management.** No partials, break-even, trailing stop, time stop below 168 candles, or position sizing. This is faithful to V1, and it is also the only lever that operates in units of 0.1 R.
- **D8 — Ledger `strategy_version` is not threaded** (deferred by explicit instruction). Ledger rows stamp the module constant `"2.2.0"` while the manifest carries the variant version, so `metrics.json` lineage reports `strategy_versions: ["2.2.0"]` for the s3b run. **No number is affected** — `ComparisonKey` excludes `strategy_version`, and `run_id`/`core`/manifest are all correct — but the metrics lineage block mislabels variant runs and should be fixed before any variant's metrics are archived as evidence.

### MEDIUM
- **D9 — Freshness is not part of OB selection.** A stale block wins the slot and blocks every fresher candidate behind it, killing the setup. The suppression is non-random: it correlates with recent trending moves, biasing retracement entries toward chop.
- **D10 — Entries are taken into breached POIs.** Nothing requires the level to be untouched at signal time; `IMMEDIATE` fills are 4,651 trades at E −0.078.
- **D11 — `slippage_rate` is recorded but unimplementable** — C-9.7 lists the field, C-4.12 zeroes it, and C-9.6 defines net R only in terms of `taker_fee_rate`. The code refuses a non-zero value rather than ignoring it (correct), which means **modelling slippage requires a contract amendment.**
- **D12 — Tier 0 can only simulate removals.** SKIP→admit rows were never inserted so have no realized R. Observability was 32% / 7% / 61% for s2 / s3a / s3b, and on s3b the screen's *sign* did not survive to TRAIN.
- **D13 — Integer thresholds cannot volume-match.** Achieved ±9pp; one integer step moves 19–24pp of RETRACEMENT. Any "volume-matched by construction" claim about an anchored variant is false at that tolerance.
- **D14 — `PROMOTED_RESIDENCY_CYCLES = 3` is a declared assumption**, not a measurement, and it feeds slot-pressure reasoning.
- **D15 — Four of the promotion slices are underpowered** (p-floor 0.0625 — they cannot reach α=0.05 at any outcome).
- **D16 — s4/s5 are unimplementable in the current seam.** The `gates` key and `{"mode": "quantile_anchored"}` are refused, not silently dropped — correct behaviour, but it means the two most structural arms have never been testable.
- **D17 — Universe selection points at the most competitive symbols.** `universe_rank ≤ 10` is the worst bucket (E −0.029, −296 R).

### LOW — deliberate V1 fidelity, do not "fix" without a decision record
- A successful retracement entry leaves `entry_reason = "no_entry"`.
- `entry_source` carries the *failure* reason on the CONTINUATION path (which is why scoring keys its displacement bonus off the exact string `"continuation_micro_pullback"`).
- The OB quality filter is applied twice; the second call always returns True, so `ob_valid == fresh_ob`.
- `window_bounds` is computed and never published; several OB-audit fields are permanently `None`.
- V1's `load_memory` drops `entry_source` and `signal_id` — a lossy round-trip in V1 itself.
- `last_ob_audit_h1_ts` is vestigial; audit dedup is the in-memory set.
- **One deliberate deviation from V1:** the CONTINUATION `tp_source`/`tp_rr` scoping bug is fixed (BUG-1, C-1.3). Both fields are metadata; nothing branches on them.
- The regime classifier emits 7 labels that collapse to 3 modes; its scoring terms are mode proxies (`expansion` has n exactly equal to the CONTINUATION count).
- One known wall-clock-dependent test flake: `test_scoring.test_corpus_covers_every_decision_tier` (no A+ tier at STRICT session hours).

---

## 9. Review Priorities

Ranked by expected impact on expectancy.

1. **Make the yardstick honest.** Add a fee/slippage/funding sensitivity to the metrics pipeline (a contract amendment is required for slippage — D11). Until a cost-inclusive expectancy exists, no result in this repo distinguishes a strategy from a rounding error.
2. **POI / entry generation.** Where all the missing information lives (D2, D3, D4). Nothing downstream can compensate for zero information upstream.
3. **The premium/discount contradiction (D2).** A localised, provable defect that inverts the system's core positional premise and explains the LONG side's persistent negative edge.
4. **The retracement arm (D3, D9, D10).** The only statistically overwhelming effect in the dataset, and its sign is negative. Either rebuild its POI or remove the arm.
5. **RR/target geometry (D6, target placement).** Three mechanisms reward the losing side of the RR/hit-rate trade-off; the naive 2R fallback beats the entire liquidity-targeting engine.
6. **Trade management at tier 1 (D7).** Re-resolve partials and break-even *in the walker* so the number is real rather than a peeking upper bound.
7. **Exposure and portfolio construction (D5).** Correct the reported risk first, then decide whether correlation control belongs in the lifecycle or in a new layer.
8. **Metrics lineage threading (D8).** Cheap, and it prevents mislabelled evidence from being archived.
9. **Universe construction (D17).**
10. **Scoring weights and thresholds.** Last. Their effect size is two to three orders of magnitude below the leaks above.

---

## 10. Questions a New Reviewer Should Investigate

1. **Is there any subpopulation with genuine information?** Re-run the `1/(1+RR)` null test across every context slice available (regime, session, ATR percentile, symbol beta, time since last swing). If nothing clears the null on TRAIN with credible n, the honest conclusion is that no SMC component in this implementation predicts anything — and the project's premise, not its parameters, needs revisiting.
2. **Is the SHORT edge skill or beta?** +0.0143 vs null, stable across all four years, versus LONG at −0.0276. If the corpus's alt-heavy tail simply bled against USDT, the system's only positive number is an unhedged short exposure that will invert. This is answerable by comparing per-symbol signal expectancy to per-symbol buy-and-hold drift over the same windows.
3. **Do the order blocks mark anything at all?** Test the POI in isolation: does price react at a selected block more than at an ATR-distance-matched random level? This single experiment decides whether the retracement arm is repairable or should be deleted.
4. **What survives a real cost model?** At 88× notional/R, which RR band and stop-width regime could *ever* be cost-viable? This may prove that a large fraction of current signals are structurally untradable regardless of skill.
5. **Is the fast-resolution cohort a timing defect or a selection defect?** 16% of trades resolve within one candle at −10.4pp versus null. Do those signals differ observably at *signal time* (they would be filterable), or only in hindsight (they would not)?
6. **How much of the +1R MFE asymmetry survives honest re-resolution?** Requires the walker to model a partial and a break-even stop with the same adverse tie-breaks it applies to SL/TP. If little survives, D7 drops several places in §9.
7. **Would a real capital curve change the verdicts?** Every drawdown and recovery figure assumes flat 1R per trade with no concept of ~11 concurrent correlated positions. Rebuild the equity curve with concurrency and see whether any historical decision would have been made differently.
8. **Is `displacement` (weight +4) doing anything a volatility filter would not?** It is a 0.6-ATR body test. Compare it head-to-head against "ATR percentile above X" as a predictor.
9. **Does the M5 layer earn its place?** It is gated at `score >= 8`, so it changes tiers, and `m5_choch` shows a negative marginal delta. Its cost is a fetch for every symbol every cycle.
10. **Is the corpus wide enough to conclude anything?** 30 symbols per cycle, one 3.5-year window, one exchange, one bar interval. The four TRAIN years give 1 positive year out of 4 and 2 positive quarters out of 10 — before any variant is considered. Establish what effect size this corpus can actually resolve *before* the next experiment is designed.