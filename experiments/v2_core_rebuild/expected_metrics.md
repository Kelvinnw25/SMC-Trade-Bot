# Expected metrics — preregistered

Written **before** any variant runs. Each variant's `preregistration.json` carries
the same prediction in machine-readable form, and its digest travels with every
result, so a prediction edited after seeing an outcome stops matching its own hash.

All figures are on the **admitted scored population** (31,209 trades) — the
population `metrics.json` reports — unless marked otherwise.

## Reference

| | Baseline |
|---|---:|
| Scored trades | 31,209 |
| Total R | −272.7 |
| Expectancy | −0.0087 |
| Win rate | 32.2% |
| Max drawdown (R) | 655.8 |
| Recovery factor | −0.42 |
| Positive years | **1 / 4** (2023 −0.0373, 2024 −0.0226, 2025 −0.0016, 2026 +0.0587) |

---

## Population-only projections

These are what the gates produce **on the existing admitted population**. They are
*not* predictions of replay results, because they ignore substitution: removing
trades frees slots, the cap binds in 51% of cycles, and 41,625 suppressed candidates
are waiting. Tier 0 converts these into book-level estimates; the replay confirms.

| Gate | n | share | Total R | Expectancy | Win | Max DD | Recovery |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline | 31,209 | 100% | −272.7 | −0.0087 | 32.2% | 655.8 | −0.42 |
| `tp_rr ≤ 2.5` | 24,931 | 79.9% | +277.9 | +0.0111 | 35.3% | 332.7 | 0.84 |
| CONTINUATION | 19,691 | 63.1% | +315.5 | +0.0160 | 35.5% | 243.3 | 1.30 |
| FVG | 20,352 | 65.2% | +265.3 | +0.0130 | 33.9% | 332.6 | 0.80 |
| **R2 gate** (CONT ∧ FVG ∧ cap) | 13,649 | 43.7% | **+424.9** | **+0.0311** | 37.2% | **146.5** | **2.90** |
| R3q (+ob≤3 +vol) | 3,388 | 10.9% | +272.0 | +0.0803 | 38.2% | 112.8 | 2.41 |
| **R3b** (R2 ∧ SHORT) | 4,596 | 14.7% | **+580.1** | **+0.1262** | 41.4% | **81.9** | **7.08** |

The cell R1 re-prices: `tp_rr>2.5`, n=6,278 (20.1%), **−550.6 R**, expectancy
−0.0877, win 19.8%, contributing 609.8 R of drawdown. It is larger in magnitude than
the entire book's loss, which is the arithmetic reason R1 is ranked first.

---

## Per-variant predictions

### R1 · Core cleanup — `67c01d47aa327358`

| Metric | Baseline | **Predicted** | Reject if |
|---|---:|---:|---|
| Expectancy | −0.0087 | **+0.015 … +0.035** | CI contains zero |
| Total R | −272.7 | **+400 … +900** | < 0 |
| Max drawdown | 655.8 | **300 … 500** | > 720 |
| Recovery | −0.42 | **1.0 … 2.5** | < 1.0 |
| Positive years | 1/4 | **≥ 3/4** | ≤ 2/4 |
| Trade count | 31,209 | 28,000 … 33,000 | outside ±20% |

Point prediction in the machine-readable file: **+0.030 R**.

Trade count may *rise*: removing 12 score inputs lowers most scores, but four of
them were penalties, and admission depends on tier thresholds that are unchanged.
A large count change in either direction is itself a finding about the tiers.

**Sub-prediction, provable:** R1a (the 8 zero-information removals alone) produces
**exactly 0** score differences over 886,954 rows. Verified by `identity`, not by a
replay. If it fails, stop the program and re-derive the census.

### R2 · Selective continuation — `dc8549addab7f85c`

| Metric | Baseline | **Predicted** | Reject if |
|---|---:|---:|---|
| Expectancy | −0.0087 | **+0.025 … +0.045** | < +0.015 |
| Total R | −272.7 | **+350 … +700** | < baseline |
| Max drawdown | 655.8 | **120 … 250** | > 400 |
| Recovery | −0.42 | **≥ 2.0** | < 1.5 |
| Positive years | 1/4 | **3/4** (2023 expected **negative**) | ≤ 2/4 |
| Trade count | 31,209 | **12,000 … 18,000** | > 22,000 |

Point prediction: **+0.040 R**. 2023 is predicted negative (−0.0217 on the
population view) — a variant that comes back 4/4 positive would be *better* than
predicted and should raise suspicion of an implementation difference, not
celebration.

**Mandatory:** must beat a volume-matched control removing the same count by lowest
score. Predicted margin over that control: **≥ +0.02 R**. Failing this reclassifies
R2 as volume reduction and it does not proceed.

### R3 · Short asymmetry — `aee5f8e177f418a7`

| Metric | Baseline | **Predicted** | Reject if |
|---|---:|---:|---|
| Expectancy | −0.0087 | **+0.045 … +0.075** | < +0.03 |
| Total R | −272.7 | **+400 … +650** | < R2's total R |
| Recovery | −0.42 | **3.0 … 6.0** | < 2.0 |
| Positive years | 1/4 | **4/4** | ≤ 3/4 |
| Trade count | 31,209 | 6,000 … 10,000 | — |

Point prediction: **+0.060 R** — deliberately *below* R3b's +0.1262, because R3
keeps a long book. **The falsifier:** if R3's recovery factor does not exceed R2's
*and* R3b dominates both, the asymmetry hypothesis is refuted — the long side has no
rescue, and the choice becomes a judgement about directional concentration rather
than a further experiment.

### R3b · Short-only bound — `28b5223358a37639` · tier 0 only

Point prediction: **recovery factor ≥ 5.0**, expectancy **+0.120 R**, 4/4 positive
years (observed: +0.0650 / +0.1675 / +0.0889 / +0.2154). Never promoted from tier 0.
It answers "what is the ceiling", and its ceiling is only meaningful as a comparator.

### R3q · Quality filters — `7eb230127fc87ebc` · tier 0 only

**Pre-registered negative prediction:** expectancy rises to ~+0.08 while **total R
falls** from +424.9 to ~+272 and recovery falls from 2.90 to ~2.4. Point prediction
on the primary metric (`total_r`): **−0.005 R equivalent, i.e. worse**.

If confirmed, `max_ob_candidates` and `require_volume_spike` are classified as
**pruning** and excluded from every replay bundle. If refuted — total R rises — then
`ob_candidate_count` is a real edge and belongs in C1 as a weight.

---

## What would falsify the program

Stated in advance, because a thesis that cannot fail is not a thesis:

1. **A2 shows any score difference** from removing the 8 dead inputs → the census is
   wrong, every downstream inference is suspect.
2. **C0 does not reproduce `e7f124530b16437d`** → the seam changed behavior; nothing
   after it is interpretable.
3. **R2 fails its volume-matched control** → the gates are volume reduction, and
   V2's apparent edge is an artifact of trading less.
4. **R1's drawdown worsens** → the target cap is transferring risk rather than
   removing it, and the MFE mechanism is wrong.
5. **Any variant is 4/4-positive on years but fails the holdout** → the per-year
   consistency screen does not generalise, and the whole robustness argument in this
   program needs replacing.

## Standing caveat

Every number here is **in-sample**. The window 2023-01-01 → 2026-06-28 was read in
full during Phase 3 research, and no holdout is declared on `c8542cb360d13207`.
Declaring one is Priority 1 in [hypothesis.md](hypothesis.md) precisely because the
predictions above are otherwise unfalsifiable in the only way that matters.