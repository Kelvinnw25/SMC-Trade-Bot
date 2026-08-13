# scoring_v2 — expected metrics, decision rules, and B1 integration

Predictions written before any run. Every figure below is a *prediction* except
the two blocks explicitly labelled measured.

---

## Measured, for reference

**Baseline, admitted book** (`metrics.json`, 31,209 scored trades):
total R −272.7 · expectancy −0.0087 · win 32.2% · maxDD 655.8 R · recovery −0.42.

**Baseline, resolved population** (92,627 outcomes, burn-in excluded):
total R −2,653.4 · expectancy −0.0286 · σ(realized_r) 1.667 R.

**Phase A tier-0, kept sets** (resolved population):

| gate | n kept | expectancy | recovery | vs control |
|---|---:|---:|---:|:---:|
| FVG mandatory | 45,497 | +0.0142 | 0.73 | 10/10 |
| momentum flag present | — | +0.0617 (factor effect) | — | — |

---

## Predicted, per variant

Primary metric is **expectancy_r per admitted trade**. Reported on the admitted
book, because that is what the strategy trades.

| variant | tier | predicted Δ expectancy | predicted admitted n | predicted total R | notes |
|---|---|---:|---:|---:|---|
| `s0-control` | 0 | 0.000 exactly | 31,209 | −272.7 | must reproduce `metrics_hash e7f124530b16437d` |
| `s1-dead-weights-zero` | 0 | 0.000 exactly | 31,209 | −272.7 | 0 score deltas, 0 tier changes, or A1 is falsified |
| `s2-uninformative-zero` | 2 | 0.000 (null) | ≈31,200 (anchored) | ≈−270 | falsification arm; a null promotes the simpler config |
| `s3a-momentum-zero` | 2 | +0.005 | ≈31,200 (anchored) | ≈ −100 | |
| `s3b-momentum-positive` | 2 | +0.010 | ≈31,200 (anchored) | ≈ +40 | monotone response required across −1 → 0 → +1 |
| `s4-fvg-gate` | 2 | +0.030 | 20,000–26,000 | +350 to +600 | tier-0 measured +0.043; discounted for slot refill |
| `s5-scoring-v2` | 2 | +0.035 | 20,000–26,000 | +400 to +700 | deliberately **below** the sum of its arms |

Why the admitted counts stay near baseline for S2/S3: the quantile anchor holds
the per-mode admitted rate fixed by construction. Only S4/S5 move volume, because
only a gate removes rows.

Why S4's replay prediction (+0.030) is below its tier-0 measurement (+0.043): the
slot cap binds 51% of cycles. Every trade the gate removes frees a slot that
refills with the next-ranked candidate, whose expectancy is near the population
mean rather than zero. Tier 0 measures the gate; tier 2 measures the gate *plus*
what replaces what it removed.

## Power

σ = 1.667 R, so 80% power at δ needs n ≈ 21.79/δ² per arm unpaired:

| δ | n needed |
|---:|---:|
| +0.050 | 8,716 |
| +0.040 | 13,617 |
| +0.030 | 24,208 |
| +0.010 | 217,900 |

**S3a and S3b are underpowered by construction** at their predicted effects and are
run as directional/monotonicity evidence, not as confirmations. S4 and S5 are
adequately powered on the admitted book only if the admitted n lands at the upper
end of its range; both must report their own minimum detectable effect alongside
the result.

## Decision rules

Promotion requires **all** of:

1. ≥9 of 10 TRAIN quarters positive in the per-quarter delta **vs the
   volume-matched control** (p = 0.0107). 8/10 flags (p = 0.0547); ≤7/10 rejects.
2. Bootstrap CI on the expectancy delta excluding zero.
3. Total R above baseline **and** positive.
4. maxDD no more than 1.1× baseline; recovery factor ≥ 1.5.
5. Not classified PRUNING — expectancy up while total R falls is pruning, not
   edge, and is excluded from every bundle.

Holm–Bonferroni within each family: `{s3a, s3b}` is one family of 2; `{s2, s4}`
are separate single tests; `s5` is the composite and is not corrected against its
own arms.

---

## §B1 — how the TP cap integrates later

### The dependency that decides the design

**TP is not independent of scoring.** `rr` is computed *inside* `score_signal`
(scoring.py:283-311) from `entry_price`/`sl`/`tp`, and it feeds the score twice:

- the **hard floor** — `rr < 1.2` returns a SKIP with reason `bad_rr`, before any
  tier is assigned;
- the **bands** — `rr ≥ 3.0` adds +2, `rr ≥ 2.0` adds +1.

There is a third, weaker path: `target_clear` is `|tp − close| ≥ 0.75·ATR`, so a
lowered TP can flip that flag too.

Consequence: capping TP at 2.5R **changes which trades are admitted**, not merely
what they earn. A naive cap silently deletes the +2 rr band from every capped
trade and pushes a slice of the book below its tier. Measuring "the cap" that way
measures the cap *and* a scoring change at once.

### Two designs, and which one to measure with

| | where the cap applies | decision stream | measurable at |
|---|---|---|---|
| **(i) risk-stage cap** | in `RiskStage`, before scoring | changes (rr bands + floor) | tier 2 only |
| **(ii) post-scoring clamp** | after the tier is assigned | **identical** | tier 1, paired |

**Measure with (ii), ship (i) only if (ii) wins.** Under (ii) the admitted set is
byte-identical to the control, so the cap is measured on *the same trades* — a
paired test, which at n≈31,000 pairs is far stronger than the unpaired arms above,
and it is what discharges the tier-0 substitution assumption behind the +2,120 R
estimate. Under (i) the cap and a scoring change are confounded and the result
cannot attribute either.

If (ii) wins, (i) is then run once at tier 2 as the production form, and the
difference between (i) and (ii) is itself the measurement of the rr-band coupling.

### What must be frozen before B1 runs

1. **The scoring config.** B1 must run against one fixed scoring configuration —
   the winner of this family. Otherwise the cap is measured against a moving
   admitted set and cannot be attributed. This is the hard ordering constraint:
   **scoring_v2 completes before B1 starts.**
2. **`OutcomeConfig` and its hash** — the cap changes TP, which changes exits;
   changing the exit rules at the same time would segment the dataset (C-4.12).
3. **`max_open_positions = 10`** and the slot policy.
4. **The admitted decision stream**, captured from the frozen scoring winner —
   this is the tier-1 input.
5. **`rr_floor = 1.2`.** The floor and the cap interact: together they define the
   admitted rr window. Moving both at once is two experiments.
6. **The partition** (TRAIN 10 quarters) and the holdout declaration.

### B1 arms

Cap ∈ {none (control), 3.0, 2.5, 2.0, 1.5}, paired on identical trades, Holm
across 4 tests. Tier 0 established the *direction* (wide bands 2–3/10 on the
absolute test, monotone collapse: 2.5–3.0 → −0.0787, 3.0–4.0 → −0.1012, 4.0+ →
−0.1734) but could not separate 1.5 / 2.0 / 2.5 (8/10, 6/10, 6/10). Choosing among
four values on 10 quarters is exactly where a cap value gets overfitted, so the
decision rule is: **pick the least aggressive cap whose paired delta is
significant**, not the best-performing one.

### What B1 must not be bundled with

Anything that changes trade count. The slot cap is binding, so two volume-changing
edits are not additive and their bundle cannot be decomposed.