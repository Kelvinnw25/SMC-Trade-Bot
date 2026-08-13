# PROJECT RESEARCH LEDGER — SMC Signal Radar

> **Purpose:** record what has already been tested, so no one repeats it.
> **Audience:** a reviewer with zero historical context.
> **Companion docs:** `STATE_TRANSFER.md` (architecture + system state) · `PROJECT_STATE.md` (engineering checkpoint) · `REPLAY_CONTRACT.md` (binding measurement contract).
> **Last updated:** 2026-08-14.
>
> **Evidence base for everything below:** official baseline replay `replay-9ec9cd4487f3ba19` — 886,954 ledger rows, 118,201 outcomes, **31,209 admitted trades**, 82,954 shadow trades, 30,600 cycles, 2023-01-01 → 2026-06-28, 30 symbols/cycle. Plus full variant replay `replay-a43e3f04e58ec9ad` (s3b).
>
> **Read this first — three facts that reframe every number in this file:**
> 1. **Costs are modelled as zero.** `taker_fee_rate = 0`, `slippage_rate = 0`, `funding = none`. At the measured mean notional-per-R of 88.2, realistic friction is **0.088–0.163 R/trade** — 10–19× the entire measured baseline deficit. No result here is a tradability claim.
> 2. **Trades are not independent.** 31,209 outcomes collapse to **1,561 `thesis_key`s**; 38% overlap a same-symbol sibling; 74% of exits share an hour with another exit. Trade-level σ is inflated. Month-level tests are the defensible unit.
> 3. **The driftless null was misspecified.** `1/(1+RR)` assumes a martingale. Corrected in the 2026-08-14 drift study — several earlier conclusions reversed. See §Falsified and §Supported.

---

## 1 · Strategy Components

Confidence: **High** = replicated across independent partitions with adequate power · **Medium** = measured once with adequate n, confounds identified · **Low** = underpowered, confounded, or structurally untestable.

### POI generation
- **Tested:** whether the system has any higher-timeframe point-of-interest layer at all.
- **Method:** full code read of `v2/analysis/*`; cross-check against ledger fields.
- **Result:** there is **no HTF POI layer**. No D1/H4 order blocks, no HTF FVG, no HTF liquidity map. The only POI generator is a 10-bar H1 candle-colour scan; the only level lookup is an H1 swing/PDH-PDL scan over a 180-bar tail used for targets.
- **Conclusion:** the "top-down SMC" architecture supplies a *direction* and never a *location*. The claimed source of edge is absent from the implementation.
- **Confidence: High** (structural fact, not a measurement).

### Order blocks
- **Tested:** does price react at a selected order block?
- **Method:** `LIMIT_TOUCH` fills — the first touch of an unbreached zone — scored against the geometric null. n = 6,864.
- **Result:** **Δ = −0.0026 (0.5σ).** Exactly zero. A well-powered null, not an absence of data.
- **Also:** an OB here is *any* opposite-coloured candle in the last 10 H1 bars — no impulse origin, no BOS, no inefficiency requirement, no mitigation state, no cross-cycle persistence. Selection is first-match by recency (Decision #002, frozen), and freshness is **not** part of selection, so a stale block blocks fresher candidates behind it.
- **Conclusion:** the zone carries no information. **Do not re-test this OB definition.** A genuine order block (impulse-origin, BOS-anchored) has never been tested and remains open.
- **Confidence: High** for the null; the general SMC claim is untested.

### Displacement
- **Tested:** does the displacement requirement improve outcomes?
- **Method:** `body ≥ 0.60·ATR and body/range ≥ 0.50` is the definition. Isolated where it varies without changing mode — the A+ tier requirement.
- **Result:** CONTINUATION A+ Δ +0.0105 vs VALID −0.0010 (≈1.6σ on the difference); RETRACEMENT A+ −0.015 vs VALID −0.040 (n=433, ≈1.2σ). Right sign in both modes, never resolvable.
- **Conclusion:** a candle-size test, not a structural break. Carries the largest single weight in the system (+4) on no demonstrated basis. Never tested against a plain ATR-percentile filter.
- **Confidence: Medium** (consistent sign, insufficient power).

### Continuation
- **Tested:** trend persistence, repeatedly, and most recently under a drift-adjusted null.
- **Method:** mode-level partition; then per-direction; then drift-adjusted first-passage benchmark (2026-08-14).
- **Result:** driftless Δ +0.0036 (≈1σ, n=19,691). **Drift-adjusted Δ ≈ +0.0201**, with SHORT +0.0305 (5.24σ) and LONG +0.0143 (3.41σ).
- **Conclusion:** the strongest premise in the project. Two-sided once drift is controlled, contrary to the earlier short-only reading. Still small, still concentrated in market-drawdown months, still negative at taker fees.
- **Confidence: Medium.**

### Retracement
- **Tested:** pullback-into-zone entry.
- **Method:** mode partition, n = 11,518; adverse-resolution bias bounded at 1.38%.
- **Result:** **Δ = −0.0386 (≈9σ)**, E −0.0511, −588 R. RETR×LONG −0.0539 (≈9.7σ), −663 R. Negative in 2023/24/25; positive only in DEV 2026. Replicated on the CAPPED population (Δ −0.0250, −3.57σ for CONT×LONG mirror).
- **Decomposition:** the loss is concentrated in `IMMEDIATE` fills — entries taken after price already traded through the zone — Δ −0.0917 on n=4,651. Clean first touches are at zero (see Order blocks).
- **Conclusion:** the only statistically overwhelming effect in the dataset, and its sign is negative. Jointly attributable with the OB definition; not separable.
- **Confidence: High.**

### Liquidity targets
- **Tested:** does referencing resting liquidity produce better targets than arithmetic placement?
- **Method:** internally controlled — same entry, same stop, differing only in target source. Repeated within a single mode *and* direction to remove the long/short confound.
- **Result:** **falsified.** CONT×LONG: `fallback_2r` Δ +0.0031 (at null) vs EQH −0.0223, PDH −0.0383, SWING_HIGH −0.0431. SHORT: fallback +0.0616 vs EQL −0.0065, PDL +0.0172, SWING_LOW +0.0118. Book-wide: upside liquidity targets −760 R, fallback_2r +323 R.
- **Conclusion:** the ~430-line `find_liquidity_target` is net-negative against `entry ± 2×risk`. There is no reach-probability term anywhere in it.
- **Confidence: High.**

### Premium / discount
- **Tested:** attempted; **structurally impossible to test.**
- **Method:** co-occurrence count in the admitted book.
- **Result:** `location == "discount"` requires `price < weekly_eq`; `weekly_bias == "BULLISH"` requires `price > weekly_eq` — same price, same variable, **mutually exclusive by construction**. `weekly_aligned ∧ discount` occurs **0 times in 31,209 trades**. The `+1.5` location branches in `_select_direction` are dead code; the `discount`/`premium` scoring terms never fire; `htf_context_clean` is reachable only at `equilibrium`.
- **Consequence:** LONGs can only be generated above the weekly midpoint and SHORTs only below it — the inverse of the stated positional thesis.
- **Conclusion:** never tested, and cannot be until the contradiction is fixed. **Do not "zero the never-firing weights" (variant s1) — that treats the symptom.**
- **Confidence: High** (deductive + 0/31,209).

### Momentum
- **Tested:** three independent ways — continuation entries, the weekly-midpoint direction rule, and the s3b momentum-weakening replay.
- **Result:** the best-evidenced premise. See Continuation. The s3b arm (treating momentum *fade* as positive) was rejected at tier 2, which is weak indirect support for momentum over reversion.
- **Conclusion:** supported, small, regime-concentrated.
- **Confidence: Medium.**

### Mitigation
- **Tested:** partially; the freshness flag itself is untestable.
- **Method:** `fresh_ob` distribution; `LIMIT_TOUCH` vs `IMMEDIATE` fills.
- **Result:** `fresh_ob` fires on **99.1%** of the admitted book — a near-constant, so freshness never varies among traded signals. But the adjacent comparison is large: untouched zone Δ −0.0026 vs already-breached zone Δ **−0.0917** — an **8.9pp gap**.
- **Conclusion:** what is supported is the *negative* form — a breached level is destructive. "A fresh zone works" is unsupported (it is measured at zero).
- **Confidence: High** for the breach effect; **Low** for mitigation as a concept.

### Session effects
- **Tested:** the `session_active` +1 bonus (Mon–Fri 07:00–22:00 UTC).
- **Method:** partition within CONT×SHORT; marginal note delta book-wide.
- **Result:** book-wide marginal +0.009. Within CONT×SHORT: `session_active=False` Δ **+0.0579 (6.43σ)** vs `True` +0.0160. **Off-session trades are markedly better** — the opposite of what the scoring bonus assumes.
- **Conclusion:** the session model is an FX/equities assumption applied to a 24/7 market, and the data contradicts its sign.
- **Confidence: Medium** (one partition, in-sample, but large and well-powered).

### Score calibration
- **Tested:** exhaustively — this consumed the largest single share of research effort in the project.
- **Method:** Phase A component analysis → `StrategyConfig` seam → quantile-anchored thresholds → tier-0 screen → full tier-2 replay of s3b → promotion gate.
- **Result:** A+ vs VALID differ by 0.015 R raw. Null-adjusted and tier-level the ordering is correct but ≈1.2–1.6σ. Of ~20 terms: `weekly_aligned` fires on **100%** of the admitted book (a constant), `fresh_ob` on 99.1%, `discount`/`premium` never, and the largest apparent note deltas are mode proxies (`expansion` has n exactly equal to the CONTINUATION count).
- **Conclusion:** the score decides *how many* trades and *in what order*, never *which* trade — every level is fixed before `ScoringStage` runs. Its measurable ranking value is ~0.015–0.035 R and partly regime confound. **Effect sizes here are two to three orders of magnitude below the leaks above. Stop tuning it.**
- **Confidence: High.**

### Risk engine (drawdown-adaptive sizing)
- **Tested:** fixed 1% · 0.75% after 10% DD · 0.50% after 15% DD · reset only at a new high.
- **Method:** event-driven portfolio simulation, sizing at **fill** time on equity known then (~11 concurrent positions make exit-time sizing look-ahead).
- **Result:** rules 3 and 4 are **numerically identical** — equity crosses 15% DD in the first weeks and never makes a new high, so the ratchet never releases and 99.9% of trades size at the floor. CAGR: −85.6% / −71.6% / −51.0% / −51.0%. Worst recovery 1,265 days under all four. A **constant** 0.50% gives −49.76%, i.e. *better* than the adaptive rule — the adaptation has slightly negative in-sample value.
- **Predictive test:** drawdown state does carry information (rolling-250-trade mean R < −0.10 → E −0.035 vs +0.020 in middle bands), but the relationship is a hump, not a slope, and the specific 10%/15% equity thresholds are one-way doors here.
- **Clustering:** genuine but half-artefact. Exit-ordered lag-1 autocorrelation +0.272; on a strictly non-overlapping single-slot book **+0.135**. Longest losing streak 49.
- **Scale finding:** at 24.5 trades/day, 1% risk gives drift term −2.727 and **variance drag +4.035** — over-sizing costs more than the bad edge. Moving to a constant 0.10% improves CAGR by 77 points (−85.6% → −8.7%).
- **Conclusion:** a risk engine is a monotone scaler of a negative-drift process; both terms scale with risk, so **no sizing rule can change the sign**. Its ceiling is zero. Build it only after an edge exists.
- **Confidence: High.**

### Stop geometry
- **Tested:** stop width vs outcome; and the interaction with cost.
- **Result:** book-wide, E by stop-width-in-ATR is **non-monotone** and mostly a mode proxy (<0.8 ATR +0.028; 1.0–2.0 ATR −0.02…−0.04; ≥2.0 ATR +0.030) — "stops are too tight" is **not** supported as a first-order cause. Within CONT×SHORT, wider is clearly better: ≥0.9 ATR Δ +0.042/+0.043 vs <0.6 ATR **+0.0007**.
- **Critical:** stop width sets the notional-per-R multiple (mean 88.2 book-wide, **136.8** for CONT×SHORT), so a tight stop inflates R-measured edge *and* R-measured cost identically. R-normalisation concealed the cost structure for the entire research programme.
- **Confidence: High.**

### Target placement / RR geometry
- **Tested:** hit rate vs planned RR.
- **Result:** hit rate falls faster than RR rises — RR<1.5 E +0.024 (wr 40.9%); RR≥6 E −0.180 (wr 12.6%). RR ≥3.0 in aggregate **−522 R**. Scoring pays +2 for RR ≥3.0 and `target_clear` pays +1 for a distant target (marginal −0.035). Three independent mechanisms all reward the losing side.
- **Confidence: High.**

### Trade management
- **Tested:** measured, not simulated.
- **Result:** no partials, break-even, trailing, or time stop below 168 candles exist. Mean MFE **+1.58** vs mean MAE **−1.40**; **49.4% of trades reach +1R MFE, only 32.2% finish positive** — 38% of the +1R cohort ends negative.
- **Caveat that matters:** arithmetic counterfactuals (½ off at +1R, remainder to BE → E +0.101) **peek** — they credit the runner the final R even when the path returned through entry first. Under the martingale null every stop/partial rule has EV exactly 0. Treat +0.10 R as an upper bound requiring tier-1 re-resolution, never an estimate.
- **Confidence: Low** on exploitability; **High** on the path asymmetry existing.

### Portfolio construction / exposure
- **Tested:** real concurrent exposure vs the declared cap.
- **Result:** `max_open_positions = 10` caps **theses**, not risk. Time-weighted mean concurrent positions **11.0**, peak **40**, **48.3% of open time above the cap**, 35.7% of observations entirely one-sided, `max_consecutive_losses = 51`. Trades keep walking up to 168 candles after their thesis is evicted.
- **Conclusion:** reported drawdown and recovery understate the risk of any capital-based implementation. No correlation control, sector limits, or sizing exist anywhere.
- **Confidence: High.**

### Universe construction
- **Tested:** expectancy by volume rank.
- **Result:** rank ≤10 is the **worst** bucket (E −0.029, −296 R); 20–30 ≈ 0.000; ≥30 +0.017. Within CONT×SHORT, rank 11–30 beats rank 1–10 (+0.042/+0.037 vs +0.020).
- **Confidence: Medium.**

---

## 2 · Experiment Registry

Family root: `experiments/v2_core_rebuild/scoring_v2/`. Each arm has `config.json` + `preregistration.json`.

| arm | variant_id | hypothesis | implementation | outcome | status | lesson |
|---|---|---|---|---|---|---|
| **baseline 2.2.0** | — | reference | official replay | n=31,209 · E **−0.00874** · −272.7 R · wr 32.21% · maxDD 655.8 · recovery −0.416 · 2/10 TRAIN quarters positive | **FROZEN** | Breakeven wr is 32.50%; the system is a fair coin at 2:1 payoff, pre-cost |
| **s0-control** | `44136fa355b3678a` | identity — must reproduce baseline | resolved config | not replayed | available | The loader returns the base object itself when nothing changes, so a control cannot silently become a second definition of baseline |
| **s1-dead-weights-zero** | `af31342b3101d05f` | zeroing never-firing components changes nothing | config only | not replayed | **superseded** | The components never fire because of the premium/discount contradiction. Zeroing them treats a symptom |
| **s2-uninformative-zero** | `4d08e5afcc31af0f` | zeroing uninformative components + re-anchored thresholds improves ranking | anchored + screened | tier-0 observability only **32%** | **do not replay** | Its premise (the score can be improved by reweighting) is falsified independently |
| **s3a-momentum-zero** | `f483fac21274f00d` | `momentum_weakening` carries no signal → weight 0 | anchored + screened | observability only **7%** | **do not replay** | Exists only to supply the monotone midpoint of a hypothesis already rejected at its endpoint |
| **s3b-momentum-positive** | `fc05024bb0712c2a` | weight −1 → +1, thresholds CONT 15/12/9, RETR 16/13/10. Preregistered: `increase`, **+0.010 R** | full tier-2 replay `replay-a43e3f04e58ec9ad` | ΔE **−0.000448** (sign FAIL, magnitude FAIL) · quarter sign test **4/10, p=0.83** · 3/8 primary criteria · all vs-control gain in DEV (+105 R) while TRAIN is −59 R · Δtotal R = +18.03 exposure, **−13.07 quality** | **REJECTED** (High confidence) | Thresholds were derived on TRAIN, so TRAIN was optimistically biased — and it failed there anyway. Tier-0 predicted +0.00099 and TRAIN delivered −0.0025: **the screen's sign did not survive to the deciding partition** |
| **s4-fvg-gate** | `f4733123554dfba3` | requiring FVG as a hard gate improves quality | — | **BLOCKED**: the seam has no gate mechanism; the loader refuses the `gates` key rather than dropping it | blocked | A hard gate is a structural change; it belongs with a POI rebuild, not with scoring |
| **s5-scoring-v2** | `9eadd803b9b3b0ec` | the full proposed rescoring | — | **BLOCKED** (depends on s4) | blocked | — |

**Non-variant studies run (do not repeat):** prefix-replay seam validation (158,456 rows, 0 mismatches) · identity verification (886,954 rows, 0 score mismatches) · quantile-anchored threshold derivation · tier-0 counterfactual screen · volume-matched control + quarter sign test methodology · drawdown-adaptive risk engine simulation · hostile end-to-end expectancy audit · CONT×SHORT falsification suite · month-episode environment study · **drift-adjusted first-passage benchmark (2026-08-14)**.

---

## 3 · Falsified Hypotheses

Evidence currently rejects these. Do not re-open without new data or a new mechanism.

1. **Liquidity targeting beats arithmetic target placement.** Falsified within mode *and* direction. `fallback_2r` is the best target source in the system.
2. **`momentum_weakening` is positively predictive (s3b).** Rejected at tier 2 on its own preregistration, its sign test, and TRAIN.
3. **The scoring engine's weights/thresholds are where expectancy lives.** Effect sizes 0.001–0.003 R against an MDE of 0.027 R and unmodelled costs of 0.088–0.163 R.
4. **This OB definition marks a reaction zone.** Δ −0.0026 on n=6,864 clean first touches.
5. **Mean reversion (pullback entry).** RETRACEMENT Δ −0.0386, ≈9σ.
6. **A drawdown-adaptive risk engine can rescue the economics.** Its ceiling is zero; it degenerates to a constant here and underperforms the equivalent constant.
7. **"Stops are too tight" is the first-order cause of losses.** Non-monotone and mode-confounded book-wide.
8. **The slot cap limits risk.** It limits theses; real exposure peaks at 40.
9. **The session bonus has the right sign.** Off-session trades are markedly better within the best cell.
10. **~~The long/short asymmetry is a structural market property.~~** *Falsified 2026-08-14:* under a drift-adjusted null the gap collapses from +0.0470 to +0.0162 and CONT×LONG flips positive (+0.0143, 3.41σ). It was largely the traded universe's downward forward drift.
11. **~~The short-side edge is market beta.~~** Also false — it is not *simple* beta (positive in all trailing-drift bands and all four years), but it is substantially drift once the null is corrected. Both naive readings are wrong; see Open Questions.

---

## 4 · Supported Findings

1. **The system carries no net directional information under the original benchmark.** Overall Δ −0.0120 vs the geometric null; win rate 32.21% against a 32.50% breakeven. **High.**
2. **Continuation beats retracement**, and after drift adjustment continuation is positive on both sides (Δ ≈ +0.0201; SHORT +0.0305, LONG +0.0143). **Medium.**
3. **Entering a level price has already breached is destructive** — Δ −0.0917 on n=4,651 versus −0.0026 for clean first touches. **High.**
4. **A fixed 2R target beats every liquidity-derived target**, in both directions, within mode. **High.**
5. **Planned RR and hit rate trade off unfavourably** — RR ≥3.0 is −522 R. **High.**
6. **Path asymmetry is large** — mean MFE +1.58 vs MAE −1.40; 49.4% reach +1R and 32.2% finish positive. **High** (existence), **Low** (exploitability).
7. **Genuine serial dependence exists** — lag-1 +0.135 on a non-overlapping series; return clustering is ~2× what a shuffle produces (top-5-month share 72.9% vs shuffled median 37.2%, p≈0). **High.**
8. **Over-sizing dominates bad edge at current turnover** — at 1%/trade the variance drag (4.035) exceeds the negative drift term (2.727). **High.**
9. **Market state explains ~29% of monthly expectancy variation** (best single covariate: universe median return, R² 0.267). **Medium.**
10. **Effects are concentrated in market-drawdown months**: CONT×SHORT Δ +0.0726 (drift-adjusted) when BTC < −10%, versus +0.014 in flat months. **High.**

---

## 5 · Open Questions

1. **Does any subpopulation clear the drift-adjusted null on TRAIN with credible power?** The residual (+0.0305, upper bound) is concentrated exactly where the Gaussian benchmark is still misspecified for skew.
2. **Would a skew/jump-adjusted benchmark eliminate the residual?** The episode months have BTC H1 skew −0.497 vs −0.040 elsewhere and 33% more down-tails. This is the single highest-value unresolved measurement.
3. **Can these entries be filled passively?** The whole economic verdict turns on it: maker 2bp → CONT×SHORT E **+0.049**; taker 5bp → **−0.033**. The replay fills continuations at market, so taker is the honest current assumption.
4. **Do real order blocks (impulse-origin, BOS-anchored) mark anything?** One controlled test against ATR-distance-matched random levels decides whether the retracement arm is repairable.
5. **What is the effective sample size?** 31,209 trades → 1,561 theses → ~5 return episodes. Every published CI and power figure in this project assumes independence it does not have.
6. **Is TRAIN/DEV a generalisation test?** It is one contiguous timeline split in two, with the friendlier tape in the reserved half. There is no true out-of-sample estimate.
7. **How much of the MFE asymmetry survives honest re-resolution** with a partial and a break-even stop modelled in the walker (tier 1)?
8. **What is `ATR_PERIOD`'s blast radius?** Every threshold is a multiple of a 14-period pandas_ta H1 ATR set in a V1 config constant. Never treated as a strategy parameter, never sensitivity-tested.
9. **Is the universe reconstruction point-in-time correct?** 797 symbols, 640 guard-skipped at T₀, membership re-derived per cycle from stored volume.
10. **What expression of any behaviour could clear friction at all?** At 88–137× notional/R the cost floor is 0.09–0.22 R. This is a question about holding period, stop width and turnover, answerable from data already on disk.
11. **Why does 2024-07 fit no environmental pattern** yet produced +96 R? It is the one episode month with flat BTC, 50% breadth and near-zero skew.

---

## 6 · Current Priority List

1. **Make the yardstick honest** — add fee/slippage/funding sensitivity to the metrics pipeline. (`slippage_rate` is recorded but unimplementable: C-9.6 defines net R only in terms of `taker_fee_rate`, so this requires a **contract amendment**.) Until then no result distinguishes a strategy from a rounding error.
2. **Skew/jump-adjusted benchmark** — the cheapest test that could retire or promote the one surviving finding.
3. **POI / entry generation** — the only subsystem where information can enter (Q4, plus the premium/discount contradiction).
4. **Independence and effective sample size** — revises every interval the project has published.
5. **Fix the premium/discount contradiction** — localised, provable, and it inverts the system's core positional premise.
6. **The retracement arm** — rebuild its POI or delete it (−588 R).
7. **RR/target geometry** — three mechanisms reward the losing side.
8. **Trade management at tier 1** — the only lever operating in units of 0.1 R.
9. **Exposure accounting** — correct the reported risk before deciding where correlation control belongs.
10. **Scoring weights and thresholds** — last, and only if 1–9 are exhausted.

---

## 7 · Reviewer Warnings

**Measurement traps**
- **Never compare raw expectancy across populations with different RR.** Use Δ vs a null benchmark; geometry alone moves win rate by 20+ points.
- **The driftless null `1/(1+RR)` is only valid unconditionally.** The moment you condition on realised market direction it is misspecified, and the error runs opposite for longs and shorts. Use the first-passage form with drift.
- **Trade-level σ is inflated.** Use month-level sign tests as the defensible unit.
- **Conditioning on hindsight partitions proves nothing.** Holding time, exit reason and MFE are not knowable at entry. E by holding-time bucket looks dramatic (<4h: −3,011 R) and is largely geometric tautology.
- **Note-level marginal deltas are mode proxies.** `expansion` has n exactly equal to the CONTINUATION count. `momentum_weakening` showed the largest univariate delta (+0.087) and failed at tier 2 — that is the empirical proof these are confounded.
- **Removing top-N months chosen by outcome is a biased procedure.** It guarantees a reduction. Use symmetric trims, jackknife, and a shuffle test for whether concentration is excessive.
- **Multiplicity is severe.** ~39 entry-knowable partitions were scanned in one study; ~2 at |σ|>1.96 are expected by chance.

**Infrastructure traps**
- **The canonical key omits `cycle_index`.** Including it makes every tier-0 join return zero matches, which reads as a clean null rather than a bug. This exact failure occurred (118,201 of 118,201 unjoined).
- **`scoring_config` is deliberately outside `core` (C-16.2).** Variant identity travels through `strategy_version`. A variant carrying the base version hashes to the baseline `run_id`, and `--overwrite` then deletes the frozen baseline's sinks. A guard exists — do not remove it.
- **`identify_swings`'s `atr_mean` line is frozen by C-8.5.** Pandas' pairwise summation differs from a sequential sum in 4,506 of 5,000 realistic arrays. Refactoring it changes the swing set, hence bias, hence everything.
- **`rescore.WEIGHTS` is an intentional duplicate.** Collapsing it to an import turns the 886,954-row identity test into a tautology.
- **Do not add dependencies.** `requirements.txt` is the C-19 determinism contract; editing it is a re-baselining event that makes all prior baselines incomparable. stdlib `unittest` only; no pytest, no PyYAML.
- **Never edit `SMC-BOT-DOCS/`.** Amendments are proposed patches awaiting explicit approval. (P1–P5 are already applied.)
- **Known defect, deferred:** ledger rows stamp `strategy_version = "2.2.0"` even for variant runs, so `metrics.json` lineage mislabels them. No number is affected (`ComparisonKey` excludes it) but fix before archiving variant metrics as evidence.

**Dead ends already walked**
- Tuning scoring weights or thresholds. Three arms, one full 7.8-hour replay, zero resolvable effect.
- Tier-0 screening as *directional* evidence. It is a feasibility filter — it can only simulate removals (SKIP→admit rows were never inserted), and its sign did not survive to TRAIN on the one arm tested at tier 2.
- Zeroing the never-firing weights (s1). The cause is upstream.
- Expecting integer thresholds to volume-match. Achieved ±9pp; one integer step moves 19–24pp of RETRACEMENT.
- Adding a risk engine before an edge exists. Its ceiling is zero.
- Reading DEV as evidence. It has no promotion authority and is the most favourable regime in the corpus.
