# Benchmark Fix — Frozen Specification v4

**Status:** FROZEN. Binding requirements for implementation.
**Supersedes:** v3 (adjudicated). v4 applies the six implementation-level amendments
authorised after the implementation-readiness review; it changes no methodology.
**Scope:** Steps 1 and 2 of the agreed sequence — fix the benchmark, recompute the
findings. No continuation decision is made or implied by this document.

**Do not re-open:** A1, A2, A3, benchmark philosophy, estimands, placebo calibration,
R1/R2, multiplicity, unconditional estimands. These were adjudicated and are final.

---

## §A — Benchmark Audit (findings, retained from v3)

### A.1 The defect

[`backtest/outcome/walker.py:156`](backtest/outcome/walker.py#L156) computes

```python
structural_rr = abs(self._tp - self._entry) / abs(self._entry - self._sl)
```

from the **planned** entry, before `_find_fill()` runs. Realised R is then measured
from the **fill**, at [`walker.py:173`](backtest/outcome/walker.py#L173):

```python
r_unit = abs(fill_price - self._sl)
```

Every null comparison to date has used `1/(1 + structural_rr)` — a null derived from a
price the trade never traded at — against outcomes measured in R units derived from the
price it did. The two are not commensurate.

### A.2 Magnitude

| mode | mean fill slippage | direction |
|---|---|---|
| CONTINUATION | **+0.083 R** | adverse — market entry chases |
| RETRACEMENT | **−0.109 R** | favourable — limit entry improves |

Because the modes slip in opposite directions, the error does not cancel; it biases the
between-mode comparison, which is the comparison the research programme rests on.

Recomputing RETRACEMENT against a fill-based null moves its Δ from **−0.0386 (z = −9.4)**
to **−0.0045 (z = −1.1)**. A finding reported as strongly negative is, on the corrected
benchmark, indistinguishable from zero.

### A.3 What the fix is, and is not

The fix is: compute the null from `fill_price` instead of `entry_price`.

It is **not** a claim that market-entry continuation and limit-entry retracement should
fill alike. Those are legitimate strategy differences and are preserved exactly. The
defect is in the evaluation framework, not in the walker.

The walker is **not modified**. `structural_rr` remains in the record as produced. All
correction happens in the analytics layer, downstream of the frozen dataset.

---

## §B — Fill-Based Null Specification

### §B1 — Geometry

```
geometry(direction, fill_price, sl, tp) -> Geometry | None
```

Returns `None` unless the fill sits **strictly between** the barriers:

- **LONG:**  `sl < fill_price < tp`
- **SHORT:** `tp < fill_price < sl`

This ordering predicate is the definition of eligibility. It is evaluated on **ordering,
never on `abs()` magnitudes.** An `abs()`-based implementation returns a finite,
plausible, and entirely wrong number for a fill that sits beyond a barrier — see §D0-S1.

`fill_price` must be present and finite. Rows without a fill (`NOT_FILLED`, pending,
invalid levels) carry no `realized_r` and are outside the admitted book by construction.

When `geometry()` returns a value:

```
effective_rr        = |tp - fill_price| / |fill_price - sl|
R_unit              = |fill_price - sl|
adverse_slippage_r  = dir_sign * (fill_price - entry_price) / |entry_price - sl|
                      dir_sign = +1 (LONG), -1 (SHORT); positive = worse than planned
```

`effective_rr > 0` is guaranteed by the ordering predicate, not assumed.

### §B2 — Nulls

```
fill_null(row)  = 1 / (1 + effective_rr)     requires geometry() is not None
null_bound(row) = 1 / (1 + structural_rr)    always defined; see §B5
```

`fill_null` **raises a typed error** whenever `geometry()` returns `None`. It never
returns `nan`, never returns `inf`, and never propagates `ZeroDivisionError`.

`structural_rr > 0` is guaranteed for every admitted row by `_levels_sane`
([`walker.py:212-220`](backtest/outcome/walker.py#L212-L220)), which enforces
`tp > entry > sl` (LONG) or `tp < entry < sl` (SHORT) with all values finite and
positive. `null_bound` is therefore total on the admitted book.

### §B3 — Populations

```
admitted          = scored, non-shadow, realized_r is not None
                    (exactly what studies/common.labelled_trades() returns)
no_race           = admitted rows where geometry() is None
barrier_eligible  = admitted \ no_race
censored          = barrier_eligible with exit_reason in {HTF_KILL, MAX_HOLDING}
barrier_resolved  = barrier_eligible with exit_reason in
                    {TP, SL, GAP_TP, GAP_SL, SL_WORST_CASE_M5, SL_WORST_CASE_NO_M5}
```

Exit-reason names are taken verbatim from `EXIT_REASONS` at
[`outcome/trade.py:91-101`](backtest/outcome/trade.py#L91-L101). Two points that are easy
to get wrong and would silently drop real resolutions:

- The barrier-hit reasons are **`TP`** and **`SL`**, not `FILL_TP` / `FILL_SL`.
- **`SL_WORST_CASE_M5`** and **`SL_WORST_CASE_NO_M5`** are barrier resolutions — both
  barriers were struck and the tie was broken to SL under C-4.3 rules 3 and 4. They belong
  in `barrier_resolved`. Omitting them drops genuine losses from the primary population
  and biases Δ upward.
- **`END_OF_DATA` cannot appear in the admitted book.** It resolves to `STATE_PENDING`
  ([`walker.py:169`](backtest/outcome/walker.py#L169)) or `STATE_ACTIVE`
  ([`walker.py:362`](backtest/outcome/walker.py#L362)); neither is in `TERMINAL_STATES`
  ([`trade.py:74-75`](backtest/outcome/trade.py#L74-L75)), so `Populations.resolved`
  excludes it upstream. It is therefore not a censored class here and requires no
  handling. This is a fact about the pipeline, not a choice made by this specification.

Two identities hold by construction and are asserted in D0:

```
barrier_resolved  ⊎  censored  ==  barrier_eligible
|barrier_eligible| + |no_race| ==  |admitted|
```

`GAP_TP` / `GAP_SL` are **not** coextensive with `no_race`. A gap exit means a candle
opened beyond a level during the walk; the fill itself may still have been interior.
`no_race` is the narrower set where the **fill** was at or beyond a barrier — in practice
the `MARKET_WALK_ORIGIN` fills at W0 open that gap out on the same candle, producing
exactly 0.0 R.

Observed on the official run: **CONTINUATION `no_race` n = 628**; **RETRACEMENT
`no_race` n = 0**.

### §B4 — Primary estimator

**Estimand (retained from v3, adjudicated):** the edge *conditional on a completed
two-barrier race under system rules*. Not an unconditional no-kill counterfactual.

Primary population: **`barrier_resolved`**.

```
Δ = observed_win_rate(barrier_resolved) - mean(fill_null over barrier_resolved)
```

`effective_rr` is **heavy-tailed** — as a fill approaches SL, `|fill - sl| → 0` and
`effective_rr → ∞`. On the official run the arithmetic mean of `effective_rr` for
CONTINUATION is **2.975** against a structural mean of **2.007**, which is directionally
backwards from the known adverse chase and is an artifact of the tail, not a finding.

Therefore: **`effective_rr` is reported as median and 10% trimmed mean. A bare arithmetic
mean of `effective_rr` is never emitted.** `fill_null` is bounded on (0, 1) and its mean
is safe.

### §B5 — `no_race` sensitivity bounds

`no_race` rows have no valid fill geometry, so **`fill_null` is undefined on them** — the
fill sits at or beyond a barrier, so there is no race left to price. They are excluded
from the primary population at source (§C5 rule 1).

Their exclusion is entry-time-knowable: it follows from `fill_price` relative to the
levels, with no hindsight conditioning.

The bound reinstates them using **`null_bound = 1/(1 + structural_rr)`** — the only null
that remains defined — and brackets their unknown outcome by count:

```
delta_bound_lower_no_race = Δ recomputed over (barrier_resolved ∪ no_race),
                            every no_race row counted as a LOSS,
                            every no_race row priced at null_bound
delta_bound_upper_no_race = same, every no_race row counted as a WIN
```

**Rationale for count-based bounds:** `mfe_r` / `mae_r` were considered and rejected. On
`no_race` rows `R_unit → 0`, which inflates them past usability — observed range
**+1403.44** to **−815.51**. They bound nothing.

**What is knowable from H1 data:** that the fill was non-interior, and the trade closed at
0.0 R. **What is unknowable:** how a race would have resolved had the fill been interior.
The bounds encode exactly that ignorance and nothing more.

### §B6 — Censoring sensitivity bounds

**Population, explicitly:**

```
barrier_resolved  ∪  {HTF_KILL, MAX_HOLDING}   ==  barrier_eligible
```

`no_race` is **excluded**. Censoring bounds never reinstate `no_race`.

Censored rows **are** `barrier_eligible` — they filled with interior geometry and were
terminated by system rules before a barrier was struck — so `fill_null` **is** defined on
them and is the null used here. This is the deliberate asymmetry against §B5:

| bound family | reinstated rows | null used |
|---|---|---|
| §B5 `no_race` | `no_race` | **`null_bound`** = 1/(1+`structural_rr`) |
| §B6 censoring | `HTF_KILL`, `MAX_HOLDING` | **`fill_null`** = 1/(1+`effective_rr`) |

```
delta_bound_lower_censored = Δ over barrier_eligible, every censored row a LOSS
delta_bound_upper_censored = Δ over barrier_eligible, every censored row a WIN
```

These are **robustness diagnostics presented alongside the primary estimator, not a
replacement for it.** The primary estimand remains conditional on a completed race.

### §B7 — As-traded outputs

The as-traded population is the **admitted book minus `no_race`**:

- retained: `TP`, `SL`, `GAP_TP`, `GAP_SL`, `SL_WORST_CASE_M5`, `SL_WORST_CASE_NO_M5`,
  `HTF_KILL`, `MAX_HOLDING` — i.e. every exit reason that can reach the admitted book
- excluded: `no_race` (per §C5 rule 1)
- excluded: rows lacking `realized_r` — never in the admitted book
- `END_OF_DATA` does not occur here; see §B3

As-traded outputs report **R-space metrics only**: mean R, median R, win rate, total R,
equity path, drawdown, streaks.

**No null comparison of any kind may be attached to an as-traded output.**
`effective_rr`, `fill_null`, `null_bound`, and Δ are undefined in the as-traded channel.

**Note for readers comparing against prior work.** Excluding 628 zero-R CONTINUATION rows
leaves total R unchanged (315.5) but changes n from **19,691 → 19,063**, and therefore
mean R from **+0.01602 → +0.01655**, and the win rate. Any as-traded figure quoted before
this specification used the larger denominator.

---

## §C — Re-Derivation Scope

### §C1 — Contaminated by benchmark

Every finding whose statement contains a null comparison, a Δ, a z-score, or a
significance claim derived from `1/(1 + structural_rr)`. All must be re-derived:

- mode × direction cell table (the CONTINUATION / RETRACEMENT × LONG / SHORT grid)
- the drift-adjusted null results (CONT×SHORT +0.0305, CONT×LONG +0.0143)
- the RETRACEMENT negative-edge finding (−0.0386, z = −9.4)
- every per-regime, per-session, per-score-tier null comparison
- the empirical direction-bias estimate (+0.026 to +0.032 short-side)

### §C2 — Shadow populations: OUT OF SCOPE

**Shadow populations are out of scope for this phase.** The fill-price correction is
defined and validated on the admitted book only.

Shadow re-derivation requires two artifacts that do not exist:

- **(a) a shadow-capable loader.** [`studies/common.py:81-94`](backtest/analytics/studies/common.py#L81-L94)
  is the studies layer's only loader and returns `Populations.of(...).scored`, filtered to
  `shadow_kind is None` at [`metrics/core.py:66-75`](backtest/metrics/core.py#L66-L75).
  The studies layer structurally cannot see a shadow record.
- **(b) a committed volume matcher.** No volume-matching, matched-control, or equivalent
  implementation exists anywhere under `backtest/`. The prior match was ad-hoc and was
  never committed; there is no artifact to re-derive.

Tracked as separate future work. See §H.

### §C3 — Contaminated by `realized_r`

Findings that read `realized_r` but attach no null are **not** benchmark-contaminated.
They are affected only by the §B7 population change (the 628-row exclusion):

- expectancy and win-rate tables
- equity curve, drawdown, streak analysis
- exposure and slot-competition studies

These are re-derived for population consistency, not for benchmark correction.

### §C4 — Uncontaminated

Findings that touch neither nulls nor `realized_r` — corpus coverage, universe
composition, signal counts, lifecycle transition counts — are unaffected and are not
re-derived.

### §C5 — Population interaction rule

One rule governs every **primary** output:

1. `no_race` rows are removed once, at source, for both channel (a) and channel (b).
   No **primary** output sees them under any output type.
2. Censored classes (`HTF_KILL`, `MAX_HOLDING`) are removed only for null-comparison
   primary outputs; they are retained for as-traded primary outputs.

**The sensitivity diagnostics of §B5 and §B6 are the sole exceptions to rules 1 and 2.**
They deliberately reinstate rows that the primary populations exclude, and this is their
purpose, not a violation. Every diagnostic is subject to four constraints:

- it is **never** a primary output;
- it is emitted in its **own dedicated column**, never merged into or substituted for a
  primary column;
- its column name **must contain the infix `_bound_`** (e.g. `delta_bound_lower_no_race`,
  `delta_bound_upper_censored`), so a primary and a diagnostic are never confusable by
  name alone;
- **diagnostics are never combined.**

**`no_race` bounds and censoring bounds are computed independently against the primary
estimate.** No output stacks them. There is no combined bound, and none may be derived by
a reader from the emitted columns without being labelled as such.

---

## §D — Validation Plan

### §D0 — Unit and invariant gate (authoritative)

D0 is the authoritative correctness gate. It runs on synthetic fixtures and analytic
truth, independent of production data.

#### D0-S1 — Ordering predicate, not magnitude

`barrier_eligible` is defined by ordering only — LONG: `sl < fill < tp`; SHORT:
`tp < fill < sl`. `geometry()` returns `None` otherwise.

| case | dir | entry | sl | tp | fill | expected | naive `abs()` bug value |
|---|---|---|---|---|---|---|---|
| GAP-SL-S | SHORT | 100 | 104 | 88 | 105 | `None` | **17.0** |
| GAP-TP-S | SHORT | 100 | 104 | 88 | 87 | `None` | 0.0588 |
| GAP-SL-L | LONG | 100 | 98 | 106 | 97 | `None` | **9.0** |
| GAP-TP-L | LONG | 100 | 98 | 106 | 107 | `None` | 0.111 |
| AT-SL-S | SHORT | 100 | 104 | 88 | 104 | `None` | `ZeroDivisionError` |
| AT-TP-S | SHORT | 100 | 104 | 88 | 88 | `None` | 0.0 |

The two bolded rows are why this test is mandatory: both are finite, positive, and would
flow into a mean without raising anything.

#### D0-S2 — Asymmetric LONG/SHORT fixture

Identical `structural_rr`, both filled adversely, **different** `fill_null`. A SHORT
branch copy-pasted from LONG passes a mirrored fixture and fails this one.

| id | dir | entry | sl | tp | fill | `structural_rr` | `effective_rr` | `fill_null` | `adverse_slippage_r` |
|---|---|---|---|---|---|---|---|---|---|
| LONG-A | LONG | 100 | 98 | 106 | 101 | 3.0 | 5/3 = 1.666667 | **3/8 = 0.375** | +0.50 |
| SHORT-A | SHORT | 100 | 104 | 88 | 97 | 3.0 | 9/7 = 1.285714 | **7/16 = 0.4375** | +0.75 |

Assert all four quantities for both rows, and assert explicitly:

```python
assert fill_null(LONG_A) != fill_null(SHORT_A)
```

so the asymmetry cannot be silently lost to a later refactor.

#### D0-S3 — SHORT `LIMIT_TOUCH` invariance

| dir | entry | sl | tp | fill | assertion |
|---|---|---|---|---|---|
| SHORT | 100 | 104 | 88 | 100 | `effective_rr == structural_rr == 3.0` and `fill_null == 0.25` |

**Exact equality, not `assertAlmostEqual`.** All values are exactly representable in
binary floating point, so exact comparison is correct here and catches drift that a
tolerance would hide.

#### D0-S4 — Typed raise

`fill_null` raises a typed error — not `ZeroDivisionError`, not `nan`, not `inf` — for
every case in D0-S1, in both directions.

#### D0-S5 — Bound-path routing

Assert the §B5 bound path uses `null_bound = 1/(1 + structural_rr)` and **not**
`fill_null`. Fixture: LONG-A gives `null_bound = 0.25` against `fill_null = 0.375`.

The fixture deliberately uses a `barrier_eligible` row so that both quantities are
computable and demonstrably differ. On a real `no_race` row `fill_null` raises, so the
substitution error would be caught by D0-S4 there — but not in the bound path, which is
what this test covers.

Assert also that the §B6 bound path uses `fill_null`, per the §B6 asymmetry table.

#### D0-S6 — Censored-class eligibility

Assert **every** `HTF_KILL` and `MAX_HOLDING` row in the run is `barrier_eligible`. §B6
states this as fact; this test verifies it. If any row fails, §B6's population is wrong
and its bounds are invalid.

Assert also the two §B3 identities:

```python
assert barrier_resolved | censored == barrier_eligible
assert len(barrier_eligible) + len(no_race) == len(admitted)
```

**Vocabulary closure.** Assert every admitted row's `exit_reason` falls in the union of
the `barrier_resolved` and `censored` lists:

```python
assert {t.exit_reason for t in admitted} <= BARRIER_REASONS | CENSORED_REASONS
```

Without this, a future addition to `EXIT_REASONS` — or a name misread, which already
happened once during drafting — silently drops rows from every population instead of
failing. The partition identities above would still pass, because both sides would lose
the same rows.

#### D0-S7 — Output-type exclusivity

Assert:

- no emitted row carries both a Δ and an as-traded tag (§B7);
- no column whose name lacks `_bound_` reinstates an excluded row (§C5);
- no emitted column combines a `no_race` bound with a censoring bound (§C5).

#### D0-S8 — Descoped-shadow guard

Shadow analysis is descoped (§C2). Assert that `fill_null.py` exposes **no** shadow entry
point, and that any shadow-filtered call raises `NotImplementedError`.

**It must never silently return an empty result.** A shadow filter over
`labelled_trades()` returns `[]` by construction, which would be reported as "no effect"
and is indistinguishable in a D3 table from a genuine null.

#### D0-S9 — Analytic truth

Synthetic driftless random walk; recovered Δ within **±0.005** of analytic zero. Dead
branches covered by synthetic fixtures. Study output byte-identical across two runs on
identical input (C-19 practice).

### §D1 — Consistency check (not authoritative)

Reproduce the corrected Δ with an independent scratch script. This is a **consistency
check**, not an oracle. **D0 synthetic and analytic truth tests remain authoritative.**
Any discrepancy requires investigation of both implementations; it does not license
assuming the production code is wrong.

### §D2 — `LIMIT_TOUCH` invariance on real data

On the 6,864 `LIMIT_TOUCH` rows, `fill_price == entry_price` by definition, so
`effective_rr == structural_rr` and the corrected Δ must equal the uncorrected Δ exactly.
This is a **consistency check** framed so it cannot falsely fail: it verifies that the
correction is inert where it must be inert.

### §D3 — Output table

One row per cell (mode × direction). Columns:

| # | column | population | null |
|---|---|---|---|
| 1 | cell | — | — |
| 2 | `n_barrier_resolved` | `barrier_resolved` | — |
| 3 | `delta_primary` | `barrier_resolved` | `fill_null` |
| 4 | `delta_bound_lower_no_race` | `barrier_resolved ∪ no_race` | `null_bound` on reinstated |
| 5 | `delta_bound_upper_no_race` | `barrier_resolved ∪ no_race` | `null_bound` on reinstated |
| 6 | `delta_bound_lower_censored` | `barrier_eligible` | `fill_null` |
| 7 | `delta_bound_upper_censored` | `barrier_eligible` | `fill_null` |
| 8 | `median_effective_rr` | `barrier_resolved` | — |
| 9 | `trimmed_mean_effective_rr` | `barrier_resolved` | — |

**Where a bound's reinstated population is empty — RETRACEMENT has `no_race` n = 0 —
columns 4 and 5 emit `null`. They never duplicate the primary value.** A degenerate column
must not be readable as a computed one.

**All D3 outputs are labelled "benchmark-consistent, not edge estimates."** No empirical
calibration is in scope.

No shadow row appears in D3.

### §D3b — As-traded output table (channel b)

Emitted by §E step 10. Population: §B7 (admitted minus `no_race`) — **not**
`barrier_resolved`. One row per cell.

| # | column |
|---|---|
| 1 | cell |
| 2 | `n_as_traded` |
| 3 | `mean_r` |
| 4 | `median_r` |
| 5 | `win_rate` |
| 6 | `total_r` |
| 7 | `max_drawdown_r` |
| 8 | `max_consecutive_losses` |

No column in D3b may be a Δ, a null, a z-score, or a significance claim (§B7). Equity-path
columns are computed in C-9.8 order as delivered by `labelled_trades()`; see §E step 10.

### §D4 — Diagnostic panel

Influence, clustering, and resolution-time panels, on `barrier_resolved`.

### §D5 — Ledger diff

Record in `PROJECT_RESEARCH_LEDGER.md`: every finding whose statement changed, with the
before/after value and the reason (benchmark correction vs. population change vs. both).

---

## §E — Implementation Order

| # | step | gate |
|---|---|---|
| 1 | Confirm baseline-lock surface: `HEADLINE` membership and `metrics/definitions.py` | blocking |
| 2 | Record official baseline digest | blocking |
| 3 | Create `backtest/analytics/studies/fill_null.py` | blocking |
| 4 | Create `tests/backtest/test_fill_null.py` — D0-S1 … D0-S9 | blocking |
| 5 | Run D0 gate | blocking |
| 6 | Run D1 consistency check | non-blocking, investigate on mismatch |
| 7 | Run D2 `LIMIT_TOUCH` invariance | blocking |
| 8 | Re-verify baseline digest unchanged | blocking |
| 9 | Emit channel (a) null-comparison outputs — §D3 | blocking |
| 10 | Emit channel (b) as-traded outputs — §D3b | blocking |
| 11 | Re-derive drift-adjusted null with fill-based `s` | blocking |
| 12 | Emit D4 diagnostic panel | non-blocking |
| 13 | Write D5 ledger diff, including the §H future-work note | blocking |
| 14 | Full suite (2,401 tests) + digest + residual reference sweep | blocking |

Step 3 must follow the existing study pattern: the `Study` protocol and `LabelledTrade`
in [`studies/common.py:62-94`](backtest/analytics/studies/common.py#L62-L94), consuming
`labelled_trades(run_dir)`.

Step 10 must preserve the C-9.8 ordering that `labelled_trades()` already applies via
`c98_order_key`. Re-sorting or filtering after load silently changes drawdown, streak
length, and lag-1 autocorrelation with no error raised.

The new study is **opt-in**. It registers nothing into `HEADLINE` or
`metrics/definitions.py` and does not alter `report.md` for any future run.

---

## §F — Risk Ledger

| id | risk | control |
|---|---|---|
| R1 | SHORT-branch sign error corrupts 40% of the book | D0-S1, D0-S2 |
| R2 | `abs()`-based geometry accepts beyond-barrier fills | D0-S1 |
| R3 | `fill_null` used in the §B5 bound path | D0-S5 |
| R4 | Bare `mean(effective_rr)` emitted; heavy tail inverts the reading | §B4, D3 columns 8–9 |
| R5 | C-9.8 order lost in channel (b) | §E step 10 note |
| R6 | §C5 applied inconsistently between channels | D0-S7 |
| R7 | Silent empty shadow result reported as "no effect" | §C2 descope, D0-S8 |
| R8 | Degenerate bound column read as a computed value | §D3 `null` rule |
| R9 | Study leaks into the frozen baseline surface | §E steps 1, 2, 8; opt-in registration |
| R10 | Baseline-lock misidentification | **`structural_vs_realized_gap` is in `HEADLINE`. `avg_structural_rr` is NOT in `HEADLINE`** — verified at [`report.py:64-73`](backtest/metrics/report.py#L64-L73), 26 entries |
| R11 | Exit-reason names misread; `SL_WORST_CASE_*` silently dropped from `barrier_resolved`, biasing Δ upward | §B3 verbatim list, D0-S6 vocabulary closure |

---

## §G — Out of Scope

Explicitly excluded from this phase, and not to be reintroduced:

- any continuation keep/retire/recalibrate decision
- any change to the walker, replay engine, `OutcomeConfig`, or its hash
- any change to `data_snapshot_id`, the corpus, window, `burn_in_h1`, `universe_mode`,
  `top_coins_limit`, `max_open_positions`, or `requirements.txt`
- any change to `data/baselines/v2.2.0-official/`
- empirical null calibration
- unconditional (no-kill) estimands
- multiplicity correction
- shadow populations (see §C2, §H)
- any architecture change

---

## §H — Future Work (recorded, not scheduled)

**Shadow re-derivation under the fill-based null.** Blocked on two missing artifacts:

- **(a) shadow-capable loader.** The studies layer has no path to shadow records;
  `labelled_trades()` filters them out upstream via `Populations.scored`
  (`shadow_kind is None`). Enabling it requires a new loader crossing the
  `metrics` / `analytics` boundary.
- **(b) committed volume matcher.** No volume-matching implementation exists under
  `backtest/`. The prior matched control was ad-hoc and uncommitted; its methodology
  would itself need specifying before any re-derivation could be trusted.

Until both exist, no shadow-population claim may be made under the corrected benchmark.
