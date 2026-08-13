# scoring_v2 — proposed V2 scoring specification

Family of the `v2_core_rebuild` programme. Supersedes the scoring parts of R1.
Inputs only: no results here, no replay run, no `v2` file modified.

Baseline: `replay-9ec9cd4487f3ba19` / `baseline-daaf6d3a52eebf54`, metrics_hash
`e7f124530b16437d`, comparison_key_digest `0880d4d7557d067f`.

---

## The governing principle

Phase A found three classes of "dead" component, and they demand three different
treatments. The programme spec's original instruction — "remove the dead
components" — is a single action applied to three different problems, and applying
it uniformly would break the score.

| Class | Treatment | Why |
|---|---|---|
| never fires (5) | **weight → 0** | bit-identical on this corpus; a future firing stays visible in `notes` |
| always fires (3) | **keep unchanged** | a constant cannot change a ranking or a tier; removing it buys nothing and costs a threshold re-derivation |
| uninformative (4) | **weight → 0 + re-anchor thresholds** | scores genuinely move; 50% of rows cross a tier boundary |
| wrong sign (1) | **weight experiment** | `momentum_weakening` is a calibration question, not a bug |

The always-fires row is the correction that matters most, and it points the
opposite way from the spec. `weekly_aligned` fires on 373,720 of 373,723 rows. A
term that adds the same 2 points to every score changes **no ordering and no tier
assignment** while thresholds stand. Deleting it therefore delivers zero
behavioural benefit and forces a re-derivation of every threshold — the best
global compensating shift still left 81,195 tier changes, because the constant is
mode-dependent (CONTINUATION −7, RETRACEMENT −4/−5). **Keep the constants.** They
are documentation of what the setup was, and they are free.

---

## Components kept

| Component | Weight | Basis |
|---|---|---|
| `d1_aligned`, `h4_aligned` | 2 / 1 | live and varying |
| `weekly_aligned` | 2 | constant; kept as a tautology marker (direction is derived from weekly_bias) |
| `fresh_ob` | 1 / 2 | constant; free |
| `displacement` | 4 / 1 | constant in CONTINUATION, live in RETRACEMENT where it also gates A+ |
| `fvg` | 1 | the one edge that clears the bar; kept as a score term *and* promoted to a gate |
| `expansion`, `squeeze` | 1 | regime engine, live |
| `weak_volume`, `weak_sweep` | −2 / −1 | penalties whose bonus arm is retained or zeroed deliberately |
| `m5_choch`, `m5_continuation_confirm` | 2 / 1 | live |
| `rr_*` bands | 0/1/2 | live, and the subject of B1 |

## Components zeroed

**Never fires** (`s1-dead-weights-zero`, predicted effect exactly 0):
`discount`, `premium`, `neutral_penalty`, `no_ob`, `m5_watch_confirm`.

Zeroed rather than deleted. They are reachable in code and merely unreached by
this corpus — `neutral_penalty` requires a neutral/compression regime, `no_ob` a
RETRACEMENT without a fresh OB. Deletion would make a regime shift silent;
weight 0 makes it appear in `notes` with no score effect.

**Measured uninformative** (`s2-uninformative-zero`): `sweep` + `weak_sweep`,
`target_clear`, `session_active`, `htf_context_clean`. `sweep` is zeroed together
with its `weak_sweep` arm; zeroing one arm alone would leave a penalty with no
bonus, which is a different experiment nobody proposed.

## Components recalibrated

`momentum_weakening`, currently −1. The flag marks trades that resolve **+0.0617 R
better** than average while the score penalises them. Three arms, tested as a
family: −1 (control), 0 (`s3a`), +1 (`s3b`). Promotion requires a **monotone**
response across the three, not a single significant endpoint — one significant arm
out of two is what a 2-arm family produces by chance often enough to matter, which
is why the family is Holm-corrected.

## The new threshold strategy

Zeroing the uninformative components moves 186,670 of 373,723 rows (50%) across a
tier boundary, and the momentum flip moves another 78,892. The literals 14/11/8
and 15/12/9 were calibrated against a score that contained all of it, so after the
change they no longer denote what they denoted.

**Thresholds become quantile-anchored, derived offline, written into the config as
integers.**

- Basis: per `entry_mode`, choose the integer thresholds whose admitted fraction
  of scored rows on **TRAIN** equals 2.2.0's, ties resolved upward.
- Computed by the existing `rescore` decomposition. No replay, no strategy code.
- The derived integers are written into the variant config, so the strategy never
  computes a threshold at runtime and the variant stays a fixed, hashable object.

Two reasons this is the right anchor rather than "maximise TRAIN expectancy":

1. It makes every variant **volume-matched to the baseline by construction**,
   which is the comparison Phase A established as the only one with teeth.
2. It is a one-parameter-free rule. Fitting thresholds to expectancy is where
   this programme would overfit fastest — 10 quarters and 6 free integers is not
   a defensible ratio.

The anchor is still an in-sample derivation, and it is declared as such: it is
selection on TRAIN, it is covered by the holdout rule, and it is the single
largest overfitting exposure in this family.

### Measured: "volume-matched by construction" holds only to ±9pp

The derivation has now been run ([derivation/](derivation/)), and claim 1 above
needs qualifying. **An integer score cannot be volume-matched exactly.** One
integer step in RETRACEMENT moves 19–24 percentage points of the population, so
the two candidate thresholds around a target rate can both miss it badly. The
worst case is `s2`'s RETRACEMENT admission rate: target 0.4816, `watch = 7` gives
0.5739 (+9.2pp) and `watch = 8` gives 0.3379 (−14.4pp). Seven is the closer
integer and it is still 9.2pp too permissive.

| variant | worst cumulative-rate deviation |
|---|---:|
| `s2-uninformative-zero` | 0.0922 |
| `s3a-momentum-zero` | 0.0652 |
| `s3b-momentum-positive` | 0.0376 |

Consequence for the analysis: the anchor keeps a variant in the baseline's
operating regime, which is what stops a hypothesis test from silently becoming a
selectivity test. It does **not** deliver equal trade counts, so the per-variant
**volume-matched control remains mandatory** at analysis time — it is what
absorbs the residual. Every result must report its own admitted count next to the
baseline's rather than assuming the anchor equalised them.

The report records the bracket (the rate at the chosen threshold and both
neighbours) for exactly this reason: a 9pp residual should be visibly the best
available integer, not an unexplained number.

## Gate change

`require_fvg`. FVG is the only component that beat its volume-matched control in
**10 of 10** TRAIN quarters (margin +0.0419 R). It is implemented as a gate rather
than a larger weight because the gate is what was tested; raising the weight is a
different, unmeasured intervention. No threshold re-anchor accompanies it — a gate
removes rows, it does not shift the scores of the rows that remain.

## Risks

1. **Threshold anchoring is fitting.** Mitigated by the volume anchor (no free
   optimisation target), by declaring it before the runs, and by the holdout.
2. **The never-fires class is corpus-conditional.** "0 in 373,723 rows" is not
   "unreachable". Weight 0 rather than deletion is the mitigation, and a firing
   count belongs in the variant's result payload.
3. **The FVG gate halves the book** (45,497 of 92,627 resolved). With the slot cap
   binding 51% of cycles, freed slots refill with the next-ranked candidate, so
   the realised replay effect will be **smaller than the tier-0 figure** — the
   prediction is set at +0.030 against a tier-0 measurement of +0.0428 for that
   reason.
4. **Sub-additivity.** S5 is not the sum of its arms, for the same slot-cap reason.
   Its prediction is below the sum deliberately.
5. **The honest ceiling.** No configuration in Phase A is profitable in more than
   7 of 10 TRAIN quarters. This family is expected to move expectancy from −0.0087
   toward +0.03, not to produce a robustly profitable strategy.

## Variants

| slug | variant_id | tier | predicted |
|---|---|---|---|
| `s0-control` | `44136fa355b3678a` | tier0 | 0 — harness identity |
| `s1-dead-weights-zero` | `af31342b3101d05f` | tier0 | exactly 0 score/tier deltas |
| `s2-uninformative-zero` | `4d08e5afcc31af0f` | tier2 | none (falsification arm) |
| `s3a-momentum-zero` | `f483fac21274f00d` | tier2 | +0.005 |
| `s3b-momentum-positive` | `fc05024bb0712c2a` | tier2 | +0.010 |
| `s4-fvg-gate` | `f4733123554dfba3` | tier2 | +0.030 |
| `s5-scoring-v2` | `9eadd803b9b3b0ec` | tier2 | +0.035 |

`s0-control`'s id is `CONTROL_VARIANT_ID` by construction — its `strategy_inputs`
are empty, so it is recognisable as a control rather than merely labelled one.

The three anchored variants carry **new ids**: their configs originally declared
`thresholds: {"mode": "quantile_anchored"}` and now carry the integers that
declaration resolved to. Thresholds are strategy inputs, so that is a different
variant by definition — the predecessor id is recorded in each config's
`metadata.threshold_derivation`, and the derivation evidence is in
[derivation/](derivation/). Their preregistrations are untouched and still valid:
a prediction binds to a hypothesis and a control run, not to a threshold.

The TP cap is **not** in this family. See `expected_metrics.md` §B1.