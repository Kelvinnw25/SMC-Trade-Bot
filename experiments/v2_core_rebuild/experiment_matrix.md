# Experiment matrix

Read with [hypothesis.md](hypothesis.md). Tier definitions differ in **what they
hold fixed**, which is why a result must always name its tier:

| Tier | Holds fixed | Cost | Sees |
|---|---|---|---|
| **0** | scoring, entries, exits — re-decides *admission* over already-resolved outcomes | minutes | population + substitution effects |
| **1** | selection — re-resolves *exits* over a frozen decision stream, paired | ~1 h | pure exit effects at low variance |
| **2** | nothing | **~7.8 h** | everything, including lifecycle interaction |

---

## PHASE A — tier 0, no replay, no seam required

Runs against the existing bundle. This is where **attribution** comes from; the
replays later confirm designs, not components.

| A# | Experiment | Question | Method | Blocked on |
|---|---|---|---|---|
| **A1** | TP-cap counterfactual | What does re-pricing `tp_rr>2.5` do to the population? | Subset + band substitution over 92,627 resolved | — |
| **A2** | Dead-input simulation | Do the 8 zero-information inputs move any score? | Re-score all 886,954 ledger rows with each removed; expect **0 diffs** | — |
| **A3** | Uninformative-input simulation | How many rows change decision tier when `sweep`/`target_clear`/`session_active`/`htf_context_clean` are removed? | Re-score + tier-change census | — |
| **A4** | `momentum_weakening` sign | How many rows change tier when −1 → +1? | Re-score + tier-change census | — |
| **A5** | Continuation/retracement split | Does gating RETRACEMENT survive substitution? | Tier-0 slot simulation + volume-matched control | `Clause.entry_mode` |
| **A6** | R2 gate | Same, for CONTINUATION ∧ FVG | Tier-0 slot simulation | `Clause.fvg` |
| **A7** | R3 asymmetry | Does asymmetric LONG qualification beat symmetric gating? | Tier-0, compared against A8 | `Clause` extensions |
| **A8** | R3b bound | What is the ceiling of a directional rule? | Tier-0 | — |
| **A9** | R3q falsification | Do the quality filters prune rather than add edge? | Tier-0; **pre-registered negative** | `Clause.ob_candidate_count` |
| **A10** | Volume-matched controls | For A5–A9, does the gate beat removing the same count by lowest score? | Tier-0 rig extension | control generator |

**A2 is the important one methodologically.** It is the only experiment here whose
result is *provable* rather than statistical: if removing the 8 dead inputs changes
even one score, the census was wrong and everything downstream needs re-derivation.

**Exit criteria for Phase A.** Proceed to B/C only if: A2 shows 0 diffs; A5/A6 beat
their volume-matched controls; A9 confirms or refutes the pruning prediction. If A5
or A6 fails its control, R2 does not go to replay.

---

## PHASE B — tier 1, selection frozen

Requires the tier-1 harness (copy bundle → rewrite `tp` in captured payloads →
re-resolve via the existing `outcome` stage). Independent of the seam.

| B# | Experiment | Question | Why tier 1 |
|---|---|---|---|
| **B1** | TP cap, exit-only | What does the cap do holding selection *exactly* fixed? | Paired: every trade exists in both arms, so variance is a fraction of σ=1.667 |
| **B2** | Cap sensitivity 2.0 / 2.5 / 3.0 | Is 2.5 a boundary or an artifact of band edges? | Same trades, three exits — the only honest way to choose the value |
| **B3** | Excursion-referenced target | Does `min(structural, k·ATR)` beat a flat RR cap? | Exit-only |

**B1/B2 must be read alongside A1**, because the cap is *not* a pure exit change:
`rr` is computed inside `score_signal`, `rr < 1.2` returns a `bad_rr` SKIP
([scoring.py:293](../../v2/analysis/scoring.py#L293)), and bands at 1.5/2.0/3.0 award
score. Tier 1 gives the exit effect; tier 0 gives the selection effect; only tier 2
gives the sum. Report all three or none.

**B2 is how the cap value gets chosen** — not by picking 2.5 because a band boundary
happened to fall there. This is the one place in the program where a parameter value
is selected, and it is selected on paired evidence across three candidates.

---

## PHASE C — tier 2, full replay

**Three replays, not six.** Attribution comes from Phases A and B; a replay confirms
a *design*. Every variant here moves the score and therefore admission, so separate
replays would cost 47 h and still not isolate any component.

| Run | Contents | Cost | Purpose | Gate to run it |
|---|---|---|---|---|
| **C0** | `r0-control` | 7.8 h | Must reproduce `metrics_hash e7f124530b16437d` | seam landed, `identity` clean |
| **C1** | `r1-core-cleanup` | 7.8 h | Does removing noise + capping the target improve expectancy? | C0 exact; A1–A4 done; B1/B2 done |
| **C2** | `r2-selective-continuation` | 7.8 h | Does removing weak setups improve edge? | C1 promoted; A5/A6 beat controls |
| **C3** | `r3-short-asymmetry` | 7.8 h | Does asymmetric qualification beat symmetric gating? | C2 promoted; A7 beats A8 on recovery |

**C0 is not optional and not a formality.** It is the only thing standing between a
seam bug and a program-wide false conclusion. If C0 does not reproduce the hash
exactly, stop.

C3 runs only if A7 shows the asymmetry captures meaningful advantage. If A8 (short-only)
dominates A7 on recovery factor, the asymmetry hypothesis is refuted and the decision
becomes a judgement about directional concentration risk — not another replay.

---

## Bundling — what goes together and why

**Bundle inside one replay** when components are (a) already attributed at tier 0/1,
and (b) irreducibly coupled in the code path. R1's contents satisfy both: the target
cap, the input removals and the sign fix all move the score, hence admission, hence
the lifecycle. No arrangement of replays separates them.

**C1 bundles seven changes:** TP cap · 8 dead-input removals · 4 uninformative-input
removals · `momentum_weakening` sign · scoring simplification · FVG weight promotion
· `ob_candidate_count` as a weight. Each arrives with its own tier-0/tier-1 estimate
and CI. The replay answers one question: *does the joint design work, and does the
lifecycle do anything the cheap tiers could not see?*

**Never bundle:**

| Do not combine | Reason |
|---|---|
| TP cap with any scoring change **when the question is attribution** | `rr` feeds the score; the two are algebraically inseparable in the output |
| R2's gates with R1's reweighting **in the first replay** | both reshape admission through one 10-slot queue; effects compose non-additively |
| A hard gate with a soft reweight in the same arm | population change confounds ranking change |
| Any variant with an `OutcomeConfig` / corpus / `requirements.txt` change | re-baselining event — prior baselines become incomparable, not diffable |
| Any variant whose window, `burn_in_h1`, `universe_mode` or `top_coins_limit` differs from the control | `ComparisonKey` refuses the diff, correctly |
| Ranking redesign with anything | at R2 volumes the cap may stop binding, making it moot |

**Velocity summary.** Phase A: hours, no replay, produces all component attribution.
Phase B: ~3 h, chooses the one free parameter. Phase C: **31 h across four runs**,
of which C3 is conditional — so realistically **23–31 h** for a program that would
cost 47+ h if run one change per replay, and would still attribute nothing.

**Then exactly one holdout look**, on the single winner.
