# Phase A results — tier 0, no replay

Produced by `python -m backtest.cli phase-a --run <baseline> --experiments experiments/v2_core_rebuild`.
Eight studies, written to `<run>/analytics/research/results/`. Population: **92,627
resolved outcomes** (admitted + slot-suppressed, burn-in excluded). No replay, no
bundle mutation.

Read this before [hypothesis.md](hypothesis.md) — three of its claims are corrected
here.

---

## Which sign test decides, and why the answer changed

Each gate study reports three sign tests over the 10 TRAIN quarters. They answer
different questions and only one of them decides promotion:

| Test | Question | Verdict |
|---|---|---|
| **absolute** | is the kept book profitable each quarter? | too strict here — the baseline loses money in most quarters, so a genuine improvement still fails |
| **vs baseline** | does the gate beat the ungated book? | too weak — any rule that drops the worst trades passes trivially |
| **vs volume-matched control** | does the *gate* carry skill? | **decides** |

The control removes the same trade count by lowest score — the cheapest rule with
no hypothesised skill. This matters because with a −0.0087 R baseline and 41,625
slot-capped rejects, *any* rule that trades less improves net R mechanically.

---

## Results

| Study | n kept | expectancy | recovery | absolute | **vs control** | vs baseline |
|---|---:|---:|---:|:---:|:---:|:---:|
| **A6 FVG mandatory** | 45,497 | +0.0142 | 0.73 | 5/10 | **10/10 PROMOTE** | 10/10 |
| A8 quality filters | 4,558 | +0.0740 | 2.53 | 7/10 | **9/10 PROMOTE** | 7/10 |
| A5 short-only | 38,601 | +0.0435 | 2.04 | 5/10 | 8/10 FLAG | 8/10 |
| A5 asymmetric LONG | 42,299 | +0.0413 | 2.05 | 5/10 | 8/10 FLAG | 9/10 |
| A7 continuation-only | 35,906 | +0.0249 | 1.40 | 6/10 | 8/10 FLAG | 10/10 |

### The sobering headline

**No configuration produces a book that is profitable in more than 7 of 10 TRAIN
quarters.** Every gate improves on the baseline, and every gate still leaves a
strategy that loses money in at least three quarters out of ten. That is the honest
state of V2 and it should temper the programme's ambition — the changes on the table
move expectancy from −0.009 to about +0.04, not into a robustly profitable regime.

### A6 · FVG is the one component that clears the bar

**10/10 quarters beating its volume-matched control**, margin +0.0419 R. Nothing
else in the programme reaches 9/10 on the deciding test. FVG should be the
centrepiece of the rebuild rather than one of six changes — and note its recovery
factor alone is poor (0.73), so it is a *ranking* input, not a standalone strategy.

### A5/A7 · Direction and mode are real but not established

All three land at 8/10 — above chance, below the 9/10 threshold. Short-only and
asymmetric-LONG are statistically indistinguishable here (+0.0435 vs +0.0413,
recovery 2.04 vs 2.05), so **the asymmetry hypothesis is neither confirmed nor
refuted**: it buys 3,698 extra trades at no measurable cost. That is a mild argument
for keeping a long book, not evidence that the extra qualification works.

### A8 · Pruning confirmed, with a twist

The pre-registered negative prediction **holds**: expectancy rises to +0.0740 while
net R falls from +1,049 to +337 against the R2 gate. Verdict: **PRUNING** — excluded
from the replay bundles. The twist is that it passes vs-control at 9/10, so the
filters do carry per-trade skill; they simply shrink the book below where it pays.
Worth revisiting only if capacity ever stops being the binding constraint.

### A4 · The cap's direction is confirmed; its value is not

```
band        n        expectancy   vs-all
0.0-1.5   26,657      −0.0089     8/10 FLAG
1.5-2.0   17,747      −0.0072     6/10 REJECT
2.0-2.5   29,442      −0.0021     6/10 REJECT
2.5-3.0    5,663      −0.0787     3/10 REJECT
3.0-4.0    6,857      −0.1012     2/10 REJECT
4.0+       6,261      −0.1734     2/10 REJECT
```

Monotone as predicted, and the wide bands are consistently worse (2–3/10). But the
tight bands are not consistently *better* than each other (8/10, 6/10, 6/10), so
tier 0 cannot choose between 1.5, 2.0 and 2.5. **B1 must choose it**, paired, on
identical trades. Substitution estimate for capping at 2.5: **+2,120 R**, under the
stated assumption that re-priced trades earn the mean of the ≤2.5 population — an
assumption only tier 1 can discharge.

---

## Three corrections to the programme spec

### 1 · "Remove the dead components" is not one change — it is three

| Class | Components | Score effect | Tier effect |
|---|---|---|---|
| **never fires** | `discount`, `premium`, `neutral_penalty`, `no_ob`, `m5_watch_confirm` | **0, proven** | **0 tier changes** |
| **always fires** | `weekly_aligned`, `fresh_ob`, `displacement` | −2 to −7, mode-dependent | **217,627 tier changes** |
| **uninformative** | `sweep`, `target_clear`, `session_active`, `htf_context_clean` | genuine | **186,670 tier changes** |

A1 confirms the first class exactly: all five fired **0 times in 373,723 scored
rows**, 0 unverified rows, 0 tier changes. That claim in the spec was right.

The spec's claim that *all eight* zero-information components were bit-neutral was
**wrong**. A component that always fires is a constant, and deleting a constant
shifts every score down. Worse, the shift is **mode-dependent** — CONTINUATION loses
7 (weekly 2 + fresh_ob 1 + displacement 4), RETRACEMENT loses 4 or 5 — so no single
threshold shift compensates it. The best global shift (4) still leaves **81,195 tier
changes**.

**Consequence: removing the constants is a re-derivation of the decision tiers, not
a cleanup.** It belongs with the scoring redesign, not in R1's hygiene.

### 2 · R1 is a large behavioural change, not noise removal

Removing the four measured-uninformative components moves **186,670 of 373,723
scored rows (50%) across a tier boundary**. Individually: `target_clear` 83,778,
`sweep` 74,906, `session_active` 43,810, `htf_context_clean` 22,716. Correcting
`momentum_weakening` from −1 to +1 moves another **78,892** (zeroing it moves
37,032).

The tier thresholds (14/11/8 and 15/12/9) were calibrated against a score that
*included* all of this. Strip it out and the thresholds no longer denote what they
did. **R1 must re-derive the thresholds**, which is a fitting exercise — so R1's
"low overfit risk" rating in `expected_metrics.md` needs revising upward, and its
predicted +0.030 R is not a safe floor.

### 3 · The 4/4-year screen was measuring something else

The old screen tested whether a *factor's effect* was positive each year. The
promotion test asks whether the *resulting book* beats a fair control each quarter.
Those diverge sharply: FVG is 4/4 on the old screen and 10/10 on the new one, while
continuation-only is 3/4 on the old and 8/10 on the new. The old screen's ceiling was
p = 0.0625 — it could never reach significance. Ten quarters against a
volume-matched control can.

---

## What this changes about the roadmap

**Promote to first position:** FVG. It is the only component that clears the
deciding test, and it needs no threshold re-derivation.

**Re-scope R1.** Split into:
- **R1a** — remove the five never-fires components. Provably bit-neutral; ships
  with an `identity` proof, no experiment.
- **R1b** — the target cap. Independent of scoring, and B1 chooses the value.
- **R1c** — remove the uninformative components, correct `momentum_weakening`,
  remove the constants, and **re-derive the tier thresholds**. This is the scoring
  redesign under a different name and should be sequenced and risk-rated as such.

**Leave A5's question open.** Short-only and asymmetric-LONG are indistinguishable
at tier 0. Deciding between them on this evidence would be a coin flip dressed as a
decision; carry both to the replay stage or pick the one that keeps a long book on
robustness grounds and say that is why.

**Drop A8's filters** from every bundle, as pre-registered.