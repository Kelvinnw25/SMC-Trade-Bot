# Phase 0 Freeze

**Frozen 2026-08-01.** Written *before* the first official corpus was acquired and
before any five-year replay was executed. Nothing in this document was chosen with
knowledge of a result.

This is the permanent record of what was decided, why, and what it costs to change.
It does not restate the methodology — `REPLAY_CONTRACT.md` is the authority on
replay semantics and this file never overrides it.

---

## 1. How to read this document

Every frozen item falls into exactly one class, and the class determines what
changing it costs.

| Class | Meaning | Cost of changing it |
|---|---|---|
| **P — Production** | Alters what the bot trades or what replay computes | **`STRATEGY_VERSION` bump.** Prior baselines become incomparable (C-5.7.2) |
| **S — Scientific protocol** | A rule about how evidence may be gathered or read | Must be changed *before* seeing the result it governs, or the result is void |
| **I — Implementation** | How something is built | Free, provided behaviour is unchanged and tests pass |

The failure this classification prevents: a contributor changes a "detail",
metrics still look healthy, and nobody can tell that results either side of the
change describe different systems.

---

## 2. Universe Definition — **class P**

Defined in [v2/domain/universe.py](v2/domain/universe.py).
`UNIVERSE_DEFINITION_VERSION = 1`.

### The rule

```
eligible  ==  contractType == "PERPETUAL"
          and underlyingType == "COIN"
          and quoteAsset  == "USDT"
```

All three conditions are required. `contractType` alone is insufficient
(`TRADIFI_PERPETUAL` exists); `underlyingType` alone is insufficient (a COIN can
be quoted in USDC).

### The three universes

| Universe | Content | Applied at |
|---|---|---|
| **Historical** | every symbol the archive has ever held, **delisted included** | acquisition — `discover_symbols(include_archive=True)` |
| **Replay** | the eligible set, ranked per C-6.9 | corpus construction, then adapter ranking |
| **Live** | **identical to Replay** | `v2/infra/market_data.py` |

`Historical ⊇ Replay ≡ Live`.

**Historical must never be narrowed at acquisition time.** Its width is what
removes survivorship bias; a delisted symbol is still evidence about the market
that existed. The narrowing to Replay happens once, at corpus construction, where
the `exchangeInfo` evidence justifying each exclusion is already committed.

**Replay and Live must never drift apart.** C-6.9 requires replay's candidate set
to mirror live exactly. If they diverge, every replay result describes a universe
the bot does not trade, and no metric would reveal it.

### Why the name test was replaced

The previous rule was `name.endswith("USDT") and "_" not in name`. Measured
against a real `exchangeInfo` snapshot (851 entries) it admits:

| `underlyingType` | Count |
|---|---:|
| COIN | 698 |
| **EQUITY** | **131** |
| COMMODITY | 8 |
| HK_EQUITY | 6 |
| INDEX | 3 |
| KR_EQUITY | 3 |
| PREMARKET | 2 |

`AAPLUSDT`, `AMZNUSDT` and `BTCDOMUSDT` all pass a name test; none is a crypto
perpetual. Tokenized equities additionally trade on **exchange hours**, producing
systematic multi-hour flat and zero-volume stretches the analyzer was never
designed for. Binance launched these products recently, so in a 2021–2025 corpus
they appear only at the tail — which is the **holdout period**, creating a
structural break where the holdout contains an instrument class absent from every
training fold.

This exclusion is **scope, not survivorship**. Survivorship bias is selection on
*outcome*, applied after the fact. This is selection on *instrument type*,
declared in advance, independent of any performance.

### The classification gap

`exchangeInfo` describes only currently-listed symbols. The **180 archive-only
(delisted) symbols have no entry and cannot be classified from exchange metadata
at all.** They are reported `unclassified` and are **not** eligible — unknown is
never treated as permitted and never guessed.

**Open Phase 0 item:** the committed classification table resolving those 180
symbols must exist before Stage 2. It is human-adjudicated, and therefore
versioned, hashed, and carries per-symbol provenance. The ambiguity is bounded —
TradFi products are recent, so delisted symbols in an early corpus are almost all
genuine crypto.

### Failure behaviour

If `exchangeInfo` cannot be read and nothing is cached, `eligible_symbols()`
returns `None` and the cycle is **skipped**. It never falls back to the name test:
a transient network error must not silently widen the traded universe.

---

## 3. `STRATEGY_VERSION` — **class P**

**Single definition: [v2/app/ledger.py](v2/app/ledger.py). Current value `2.1.0`.**
[backtest/lineage.py](backtest/lineage.py) re-exports it.

### A defect this freeze found and fixed

The two files previously declared the constant **independently and disagreed**:
run manifests recorded `"v2.0"` while the ledger rows *inside those same runs*
recorded `"2.0.1"`. One definition makes that class of disagreement
unrepresentable.

### History

| Version | Change |
|---|---|
| 2.0.0 | The frozen V2 baseline — behaviour the migration reproduced from V1 |
| 2.0.1 | BUG-1 + BUG-2 metadata fixes. No decision/score/level/lifecycle change |
| **2.1.0** | **Phase 0 Universe Freeze.** Live universe moved from the name test to the exchange-declared rule, removing 150 of 851 instruments. Changes which symbols are scanned, therefore which signals exist |

### When it MUST change

Any change to analysis, scoring, entry, risk, lifecycle, or the Universe
Definition. Editing that code without bumping this constant silently mixes two
strategies under one identity, with no way to separate them afterwards. It is the
single most damaging routine mistake available in this repository, because it
happens during ordinary work.

---

## 4. Falsification protocol — **class S**

**Frozen 2026-08-01, before any five-year replay.** Its purpose is to make it
impossible to move the goalposts after seeing results.

### The decision it governs

*Should A1–A25 begin?* — **not** "should this be traded". The bar is on
**structure**, not profitability: A1–A25 exists to improve the strategy, so
requiring the un-improved baseline to already be profitable sets the bar in the
wrong place; requiring nothing would licence researching a random number
generator.

### Tier 1 — apparatus validity (hard gates)

If any fails, no performance number is interpretable.

| Check | Hard failure when |
|---|---|
| `health.clean` | `false` |
| `metrics_hash` reproducibility | differs on re-run |
| `worst_case_share` | > 0.50 |
| Corpus gap share | above the declared tolerance |
| B′ carryover test | fails while A-slicing is intended |

### Tier 2 — the six pre-registered segmentation dimensions

Chosen now, without knowledge of any result:

1. `entry_mode`
2. `alert_tier`
3. `regime_class`
4. `htf_alignment`
5. `session_active`
6. `score_band`

### The primary criterion

**Split-half stability of segment ordering.** For each dimension, compute
segment-level `expectancy_per_signal` separately on the first and second halves of
the folds, then take the rank correlation between them.

Stable ordering across disjoint halves means the strategy contains **persistent
structure** — differences that are properties of the market rather than of the
sample — which is exactly what a filtering programme can exploit. Random ordering
means every segment difference is noise, and twenty-five experiments would be
twenty-five opportunities to curve-fit.

> **If ordering is unstable in half or more of the six dimensions, A1–A25 does
> not begin.** That is the protocol working, not failing.

Chosen as primary because it is distribution-free (no assumption about the
heavy-tailed R distribution), robust to the design-effect problem that makes
trade-level intervals untrustworthy, and cannot be gamed by re-running — folds
are calendar-defined and strategy-independent.

### The secondary criterion

**Fold sign-consistency of `expectancy_per_signal`** — note *per signal*, not
`expectancy`, which conditions on being filled, and fill rate is itself something
A1–A25 will change.

Under a no-edge null, positive folds ~ Binomial(20, 0.5):

| Positive folds | *p* (one-sided) | Class |
|---:|---:|---|
| ≥ 15 / 20 | 0.021 | Healthy |
| 14 / 20 | 0.058 | Healthy (marginal) |
| 11–13 / 20 | > 0.25 | Warning — indistinguishable from a coin flip |
| ≤ 10 / 20 | — | Warning — driven by a minority of folds |

Deliberately **not** a hard failure: a negative-expectancy baseline with stable
structure is a legitimate starting point for a filtering programme, while a
positive-expectancy baseline with no stable structure is not.

### Declared UNKNOWN

Recorded so that silence is never mistaken for a clean result:

- **All confidence intervals** — i.i.d. bootstrap on dependent data; true coverage unknown
- **Effective sample size** — not computed
- **Max drawdown as a risk statement** — single realization, no sampling distribution
- **Anything cost-adjusted** — zero-cost baseline (`taker_fee_rate = 0.0`)
- **Live fidelity** — no paired data

### The pre-commitment

The pilot returned expectancy **−0.0801R** at zero cost. If the full baseline
lands similarly negative *and* split-half ordering proves unstable, **the correct
conclusion is that A1–A25 does not begin.** The temptation at that moment will be
to reinterpret this protocol rather than accept it. That is precisely why it is
dated and frozen here.

---

## 5. Fold, holdout and split specification — **class S**

| Item | Frozen value | Why |
|---|---|---|
| Fold definition | **Quarterly calendar**, rolling, non-overlapping | Deterministic, strategy-independent, ~2,200 evaluated trades/fold, stable across every A1–A25 variant |
| Fold count | 20 over five years | |
| `embargo_h1` | **168** | Exactly `OutcomeConfig.max_holding_h1`, so no trade can straddle a boundary. 24 h would cover only lifecycle carryover (max TTL); 72 h leaks the longest-held trades *selectively*, which is worse than uniform leakage; 336 h is the burn-in and would discard 7.7 % of the corpus for nothing |
| Canonical baseline | **Approach A** — one unsharded run (C-5.7.4) | Realistic: lifecycle state crosses boundaries in live too |
| Fold independence evidence | **Approach B′** — five independently burned-in folds | A licensing experiment, not a methodology |
| Holdout | **Fixed, most-recent 12 months** | 4 quarterly folds — enough to check sign-consistency *within* the holdout. Six months gives 2, which cannot separate an edge from one good quarter |

**Embargo is derived, not conventional: `embargo ≡ max_holding_h1`.** If a future
experiment changes `max_holding_h1`, the embargo moves with it — which changes the
folds and invalidates cross-variant comparison. Treat `max_holding_h1` as
near-frozen for the duration of A1–A25.

**Holdout rule:** opened **once**, on exactly one candidate, with the promotion
criterion written down before it is opened, binding in both directions. If the
candidate fails, you do not get to try a second one on the same holdout. That
property *is* what makes it a holdout.

**The canonical run is scoped to the non-holdout window**, so holdout records
cannot exist in `outcomes.jsonl` and accidental evaluation is structurally
impossible rather than procedurally discouraged.

---

## 6. Replay restart policy — **class I**

**No resume in Version 1.** A crash means restart from zero.

Resume code is by construction the least-exercised path in the system, running at
the moment of maximum stress, on unusual state. A subtle resume bug produces a run
that *looks* complete but has a discontinuity at the seam, and nothing downstream
would detect it — a far worse failure than losing 37 hours.

This is acceptable **because** Shakedowns A and B establish a low crash
probability first. The two decisions are coupled; without the shakedown, no-resume
would be reckless rather than simple.

Mitigations instead of resume: `preserve_failed_run` archives a crashed bundle
rather than letting `--overwrite` delete it, and `JobRecorder` captures the
traceback, the configuration and how far the job got.

---

## 7. Observability policy — **class I**

Four tiers, discriminated by one question: *if this file were deleted, would the
result change, become unreproducible, or merely become harder to explain?*

| Tier | Answer | Lifetime |
|---|---|---|
| Progress reporting | no effect | transient, stderr |
| Operational logging | harder to explain | persistent, **disposable** |
| Scientific evidence | **result changes** | immutable, append-only |
| Audit trail | **unidentifiable** | permanent; the only tier backed up off-machine |

Enforced mechanically by `assert_operational`:

> **Counts are permitted. Ratios and R-denominated quantities are not.**

An operator must never watch expectancy accumulate during a run — that is how a
conclusion gets formed before the falsification protocol is applied.

---

## 8. Coverage Report policy — **class S**

The Coverage Report **decides nothing.** It never selects a start date, never
picks `top_coins_limit`, never names a recommended window.

It *does* score each candidate window PASS/WARN/FAIL against thresholds supplied
as arguments and recorded in the report:

> A threshold fixed before the data is seen is a **protocol**.
> A recommendation computed after the data is seen is the report **deciding**.

Declared defaults: `min_rankable_symbols=30`, `min_union_completeness=0.995`,
`max_gap_hours=24`, `max_gap_ratio=0.01`.

The binding constraint is usually **not** data age but `min_rankable_symbols` — an
hour with fewer than 30 rankable symbols does not achieve `top_coins_limit=30`, so
the universe is silently truncated there.

**Shakedown selection** is the union of per-dimension extremes plus liquid
controls — never a single weighted score, which needs arbitrary weights and lets
whichever component has the widest numeric range silently dominate.

---

## 9. What is NOT frozen

Recorded so nobody mistakes silence for a decision:

- The corpus window start date — Stage 1 evidence decides it
- Holdout length if the usable window proves shorter than five years
- Stage 2 universe size (Top-30 vs Top-50) — the Coverage Report's
  `promotion_rate` decides it
- Every A1–A25 experiment
- Cost model, slippage, dependence-aware inference (Priority 1)
