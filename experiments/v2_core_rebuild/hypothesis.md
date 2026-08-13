# V2 Core Rebuild — research specification

**Status:** specification only. No strategy code is modified by this document, and
none of these variants is executable yet — see [Blocker: the seam](#blocker-the-seam).

**Program slug:** `v2_core_rebuild` · **Base:** `strategy_version 2.2.0`

---

## 1 · The thesis

V2's measurable edge is narrow and specific:

> **Downside continuation, with an unfilled imbalance, into a target the market
> actually reaches.**

Everything else the strategy trades is either noise or negative. This program tests
that claim in three escalating steps, each of which must earn its place against a
frozen baseline rather than against intuition.

The six agreed hypotheses map onto the variants as follows:

| # | Hypothesis | Variant | Evidence |
|---|---|---|---|
| 1 | Weekly direction authority | R3 | `direction` **is** `weekly_bias` — two cells, zero exceptions in 92,627 trades |
| 2 | Continuation only | R2 | CONTINUATION +0.0160 vs RETRACEMENT −0.0511 (admitted) |
| 3 | FVG mandatory | R2 | +0.0842 R, CI [+0.0610, +0.1074], p_adj 0.0000, 4/4 years |
| 4 | TP cap ~2.5 R | R1 | monotone collapse past 2.5; `tp_rr>2.5` = 20% of volume, **−550.6 R** |
| 5 | Remove no-information components | R1 | 8 structurally dead + 4 measured-uninformative inputs |
| 6 | Fix `momentum_weakening` | R1 | +0.0617 R, p_adj 0.0000, 4/4 years — currently scored **−1** |

Hypothesis 1 needs restating, because the research changed its meaning. There is no
"weekly authority" to grant: [htf.py:263-294](../../v2/analysis/htf.py#L263-L294)
already gives weekly 4.0 against a 4.0 threshold, so no combination of D1, H4 and
location can outvote it. The joint distribution of `(direction, weekly_bias)` over
all resolved trades has exactly two cells — `(LONG, BULLISH)` 54,026 and
`(SHORT, BEARISH)` 38,601. **Weekly authority is the status quo.** What R3 tests is
the consequence: since direction *is* weekly bias, and the bearish side is +0.1230 R
better with 4/4 sign consistency, the two sides should not face the same evidence bar.

---

## 2 · Baseline

Every variant is compared against this and nothing else.

| Field | Value |
|---|---|
| `run_id` | `replay-9ec9cd4487f3ba19` |
| `baseline_id` | `baseline-daaf6d3a52eebf54` |
| `metrics_hash` | `e7f124530b16437d` |
| `comparison_key_digest` | **`0880d4d7557d067f`** |
| `strategy_version` | `2.2.0` |
| `data_snapshot_id` | `c8542cb360d13207` |
| window | 2023-01-01T00:00Z → 2026-06-28T23:00Z |
| `burn_in_h1` / `universe_mode` / `top_coins_limit` | 336 / `volume` / 30 |
| frozen at | `data/baselines/v2.2.0-official/` |

Headline population (`metrics.json`): **31,209 scored trades, net −272.7 R,
expectancy −0.0087 R, win 32.2%, max drawdown 655.8 R, recovery −0.42.**

### Must remain unchanged

Changing any of these invalidates the comparison outright — `ComparisonKey` refuses
the diff, or the numbers stop meaning the same thing:

- `data_snapshot_id` / the corpus — a rebuild is a different dataset (C-6.10)
- window, `burn_in_h1`, `universe_mode`, `top_coins_limit` — C-5.7.2 preconditions
- `OutcomeConfig` and its hash — segments the dataset (C-4.12)
- `requirements.txt` — the C-19 determinism contract; editing it is a
  **re-baselining event** that makes prior baselines incomparable, not diffable
- the replay engine, the audit system, the capture schema and the bundle format
- `max_open_positions = 10` — the cap binds 51% of cycles and `CAPPED` rejects
  average −0.0389 R, *worse* than the admitted book, so the cap is currently doing
  useful selection work. Raising it is a separate experiment, not a free parameter.
- the frozen baseline directory itself

### Must be re-verified before every variant

- `python -m backtest.cli identity --run <baseline>` → **0 mismatches over 886,954
  ledger rows**. A control that has drifted makes every delta meaningless.
- `python -m backtest.cli audit --run <baseline>` → PASS, 0 errors.
- The variant run's `comparison_key_digest` equals `0880d4d7557d067f`.

---

## 3 · Blocker: the seam

**None of these configs can be executed today.** `v2/analysis` has no configuration
surface — every weight and threshold is a literal. The `strategy_inputs` in these
configs are therefore a *specification of what the seam must expose*, and the seam
itself is still awaiting a design decision (three options were tabled; the
recommendation was to extract the literals into one frozen, named weights table).

Two consequences worth stating plainly:

1. **Phase A and most of Phase B can proceed without the seam**, because tier-0 and
   the counterfactual analyses read the existing bundle. Do those first.
2. The seam's acceptance test already exists and is cheap: `identity` must still
   report 0 mismatches over 886,954 rows with default configuration. That is a far
   stronger check than a short replay and costs minutes.

---

## 4 · Variants

`variant_id = sha256(canonical_json(strategy_inputs))[:16]`; metadata is outside the
hash, so editing prose cannot mint a new variant.

| Slug | `variant_id` | Version | Tier |
|---|---|---|---|
| `r0-control` | `44136fa355b3678a` | `2.2.0+exp.r0-control.44136fa3` | 2 (harness) |
| `r1-core-cleanup` | `67c01d47aa327358` | `2.2.0+exp.r1-core-cleanup.67c01d47` | 0 → 1 → 2 |
| `r2-selective-continuation` | `dc8549addab7f85c` | `2.2.0+exp.r2-selective-continuation.dc8549ad` | 0 → 2 |
| `r3-short-asymmetry` | `aee5f8e177f418a7` | `2.2.0+exp.r3-short-asymmetry.aee5f8e1` | 0 → 2 |
| `r3b-short-only` | `28b5223358a37639` | `2.2.0+exp.r3b-short-only.28b52233` | **0 only** |
| `r3q-quality-filters` | `7eb230127fc87ebc` | `2.2.0+exp.r3q-quality-filters.7eb23012` | **0 only** |

`r0-control`'s id is the hash of `{}` — the control constant. A control is therefore
*recognised*, not labelled, and a config claiming to be the control while overriding
something fails `is_control`.

### R0 · Control

Empty `strategy_inputs`. Exists to prove the harness: a replay under R0 must
reproduce `metrics_hash e7f124530b16437d` exactly. If it does not, no other result
from this program means anything, and the correct response is to stop and debug the
seam rather than interpret R1.

### R1 · Core cleanup

**Inputs.** `target.rr_cap = 2.5`; remove 8 zero-information and 4 uninformative
score inputs; `momentum_weakening_weight: −1 → +1`.

**Hypothesis.** Removing noise and capping the target raises expectancy *without
changing which setups are eligible*.

**Mechanism.** Two independent effects. (a) The target cap re-prices 20.1% of
admitted trades that currently average **−0.0877 R** with a 609.8 R drawdown
contribution; the market delivers median MFE 0.918 R (LONG) / 1.083 R (SHORT)
against a median 2.0 RR target, so a wider target lowers hit rate without raising
what is available. (b) The score currently sums 12 inputs that cannot discriminate
plus one with an inverted sign, which is why the weighted score is non-monotone
(score 12–13 is *worse* than 8–9) while the raw confirmation count is monotone
across all 8 buckets.

**Upside.** Expectancy −0.0087 → ~+0.02; total R −273 → +600…+1,000; drawdown
should fall materially, since the re-priced cell carries most of it.

**Downside.** The cap is a hard ceiling: if any real edge exists in the 3R+ tail it
is destroyed. The 4+ band is 6,261 trades at −0.1734 R, so this is unlikely but not
impossible. Removing the 4 measured-uninformative inputs changes scores and
therefore admission, so R1 is not purely additive — the 8 dead ones are.

**Overfit risk: low.** The cap follows from an independently measured quantity
(MFE), not from a subgroup search. The sign fix is defensible in any window. The 8
dead removals are provable.

**Replay cost.** Tier-0 for the admission side, tier-1 for the exit side, then one
bundled tier-2 (~7.8 h).

**Validation.** Split into R1a (the 8 dead inputs only — must be **bit-identical**,
verified by `identity`) and R1b (the rest). Then tier-1 for the cap with selection
frozen, tier-2 for the whole.

### R2 · Selective continuation engine

**Inputs.** R1 + `gate.entry_modes = ["CONTINUATION"]` + `gate.require_fvg = true`.

**Hypothesis.** These gates remove a negative-expectancy population rather than
merely trading less, so expectancy *and* total R *and* recovery all improve.

**Mechanism.** RETRACEMENT is 36.9% of the admitted book at −0.0511 R with a 798.4 R
drawdown — larger than the whole book's 655.8 R. It is the same variable as
`rotational` regime ([regime.py:167-171](../../v2/analysis/regime.py#L167-L171) maps
expansion→CONTINUATION, rotational→RETRACEMENT), i.e. mean-reversion OB entries in
chop. FVG is the only confirmation that raises net R when used as a gate.

**Upside.** On the admitted population the R2 gate holds 43.7% of volume at
**+0.0311 R, net +424.9 R, drawdown 146.5 R, recovery 2.90** — against a baseline
recovery of −0.42.

**Downside.** A 56% volume cut. Frequency matters for two reasons beyond taste: the
slot cap stops binding, so the accidental filtering it currently provides
disappears; and live evidence accumulates more slowly. Also 2023 is **negative**
(−0.0217) for this gate — 3/4 years, not 4/4.

**Overfit risk: low-medium.** Both gates are individually 4/4-stable and each has a
mechanism, but removing over half the book on one window's evidence is a large
structural bet.

**Validation.** Tier-0 with a **volume-matched control** — a rule removing the same
trade count by lowest score. With a −0.0087 R baseline and 41,625 slot-capped
rejects, any rule that trades less improves net R mechanically; beating the
volume-matched control is the only evidence that the *gates* did the work.

### R3 · Short asymmetry — restructured

**Inputs.** R2 + `direction.long_requires = ["volume_spike", "ob_candidates_le_3"]`.

**This deviates from the brief, on evidence.** The proposed R3 was "stronger SHORT
preference + tighter quality filters + possible ranking redesign". Measured on the
admitted population, the tighter quality filters are **pruning, not edge**:

```
R2 gate                        n=13,649 (43.7%)  net +424.9  avg +0.0311  rec 2.90
  + max_ob_candidates=3        n= 4,794 (15.4%)  net +289.6  avg +0.0604  rec 2.16
  + require_volume_spike       n= 3,388 (10.9%)  net +272.0  avg +0.0803  rec 2.41
R2 gate + SHORT only           n= 4,596 (14.7%)  net +580.1  avg +0.1262  rec 7.08
```

Expectancy rises with the filters while **total R and recovery fall** — the
signature of pruning. The directional restriction raises all three. So R3 is built
on the direction asymmetry, and the quality filters are demoted to `r3q`, a tier-0
falsification arm with a pre-registered *negative* prediction.

**Hypothesis.** The direction effect is an asymmetry in required evidence, not a
reason to ban a side. Making LONG clear extra evidence captures most of the SHORT
advantage while keeping a long book — which matters for robustness, because a
short-only system is a directional bet on one market epoch.

**Mechanism.** `direction` is `weekly_bias`. Weekly-bearish continuation
(`expansion_down`, +0.0885 R) is the only strongly working regime; weekly-bullish
continuation is roughly flat. Rather than deleting the bullish side, require it to
present the evidence the bearish side gets for free.

**Upside.** Target ≥ +0.05 R expectancy with 4/4 year consistency and a long book
retained.

**Downside.** May capture little of the asymmetry, landing between R2 and R3b
without either's advantage. If `r3b` dominates on recovery, the asymmetry hypothesis
is **refuted** and the honest conclusion is that the long side has no rescue.

**Overfit risk: medium.** `ob_candidates_le_3` is a single-window discovery, and
using it only on one side is a compound choice.

### R3b · Short-only bound — tier-0 only, never shipped from tier 0

R2 + SHORT. **n=4,596 (14.7%), net +580.1 R, expectancy +0.1262, win 41.4%,
drawdown 81.9 R, recovery 7.08**, and positive in **all four years** (+0.0650 /
+0.1675 / +0.0889 / +0.2154). It is also the variant most exposed to one-window
selection — a strategy that only shorts is a bet that the next epoch resembles
2023–2026. It exists to bound what any directional rule can achieve, and becomes a
holdout candidate only if R3 fails while its own 4/4 consistency holds.

### R3q · Quality filters — falsification arm, tier-0 only

Pre-registered prediction: **total R falls** while expectancy rises. If that holds,
`max_ob_candidates` and `require_volume_spike` are classified as pruning and
excluded from every replay bundle. Registering the negative prediction is the point
— it is how a filter that merely trades less gets caught before it costs 7.8 h.

---

## 5 · Success criteria

A variant is promoted only on the **primary** set. Secondary metrics are reported
for every variant and can veto a promotion, never earn one.

### Primary

| Metric | Baseline | Promote if | Reject if |
|---|---:|---|---|
| Expectancy (R/trade) | −0.0087 | ≥ +0.015 with 95% CI excluding zero | CI contains zero |
| Total R | −272.7 | > 0 and > baseline | falls below baseline |
| Max drawdown (R) | 655.8 | ≤ baseline | worsens by > 10% |
| Recovery factor (total R / max DD) | −0.42 | ≥ 1.5 | < 1.0 |
| Consistency across years | 1/4 positive | ≥ 3/4 positive; **4/4 for anything promoted to holdout** | ≤ 2/4 |

**No single metric promotes anything.** Specifically excluded as objectives: win
rate (a tighter target raises it mechanically), trade count, and profit factor
alone. A variant that improves expectancy while lowering total R is **pruning** and
is recorded as such — that is exactly the `r3q` prediction.

### Secondary (reported, may veto)

- **Trade frequency** — signals/cycle, and the resulting concurrency against the
  10-slot cap. R2/R3-class volumes put concurrency near ~1, meaning the cap stops
  filtering; that changes what the ranking model must do and is a real finding.
- **Slot utilisation** — share of cycles at the cap (baseline: 51%). Falling to near
  zero means capacity is no longer the constraint.
- **Alert quality** — precision by tier. The A+/VALID split currently carries no
  information (p_adj 0.89); a redesigned score should change that or the tiers
  should be collapsed.
- **Signal distribution** — by direction, session, year, regime, symbol. Used to
  detect concentration: an edge carried by few symbols or one session is a
  concentration risk wearing a strategy costume.
- **Volume-matched control delta** — mandatory for R2, R3, R3b, R3q.

### Statistical rules

σ(realized_r) = **1.667 R**, so an unpaired two-arm comparison needs ~35,600 trades
to resolve 0.05 R — more than the entire baseline. Consequences:

- Screen at tier 0 (full 92,627 resolved population) or tier 1 (paired). Never
  screen on a shortened window: a one-year replay resolves only ~0.10 R.
- Every result records tier, n, σ, and the minimum detectable effect at that n.
  "No significant difference" is unreadable without knowing what could be detected.
- Holm-correct within a family of comparisons; report adjusted p.
- The holdout is a consumable: **three looks, logged, then stop.**

---

## 6 · Implementation order

### Priority 1 — do these first, in this order

1. **Declare the holdout on `c8542cb360d13207`.** The existing lock binds
   `15b74e8adc3455e3`, so **there is currently no out-of-sample data at all** and
   every number in this document is in-sample. Take 2026 H1, log it as second-look,
   budget three evaluations. This is blocking and costs nothing.
2. **Phase A (tier 0) in full.** No seam required, hours not days. Produces the
   attribution that later replays cannot.
3. **Design and land the StrategyConfig seam**, accepted by `identity` reporting 0
   mismatches over 886,954 rows.

### Priority 2 — parallelisable with the above

- **Build tier 1.** Copy the bundle, rewrite `tp` in captured payloads, re-resolve
  with the existing `outcome` stage. Independent of the seam.
- **Extend `Clause`** with `entry_mode`, `ob_candidate_count`, `fvg`, `tp_rr` so
  Phase A can express every variant above. Small, additive, tested.
- **Volume-matched control generator** in the tier-0 rig.
- **Decouple RR from scoring** in the seam design (not the behavior): the `rr < 1.2`
  SKIP and the 1.5/2.0/3.0 band bonuses make the target an input to *selection*,
  which is why the cap cannot currently be tested as a pure exit change. Designing
  the seam so these are separable is what makes all future target work tier-1.

### Priority 3 — wait for R1/R2 results

- **R3 vs R3b** — the asymmetry-versus-restriction question only makes sense once
  R2's gates are confirmed.
- **Ranking redesign** — deferred deliberately. At R2/R3 volumes the cap may stop
  binding entirely, which would make explicit ranking unnecessary. Measure
  concurrency in R2 before spending 7.8 h on it.
- **Any refit of the remaining weights.** A refit on 92,627 in-sample trades with no
  holdout is the textbook overfit. Only after R1/R2 have narrowed the surviving
  factor set, and only with the unweighted confirmation count as the benchmark to
  beat.
- **LONG mechanism research.** Core LONG is still −0.0270 inside the best cell, so
  it is a genuine residual rather than an artifact — a research question about the
  entry engine's long branch, not a threshold to tune.

### Not in this program

Score/A+ thresholds (p_adj 0.89), session gating (1/4-year stability — measure, do
not gate), symbol-level rules, raising the slot cap, and any change to the sweep
detector beyond deleting its score contribution.
