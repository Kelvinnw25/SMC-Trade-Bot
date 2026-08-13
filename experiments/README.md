# Experiments

Version-controlled **inputs** for strategy experiments. Results are not kept here —
they are written into the run bundle under `<run>/analytics/research/results/`,
where `audit` and `analytics` already look. A second artifact tree that existing
readers cannot see is a tree that drifts.

## Layout

```
experiments/
  <slug>/
    config.json            # the behavioral definition — hashed
    preregistration.json   # what was predicted, written BEFORE any result
    hypothesis.md          # prose: reasoning, prior evidence, what would refute it
```

One directory per experiment rather than parallel `configs/` and `hypotheses/`
trees joined by filename: a per-experiment directory cannot develop a
half-renamed pair, and it reviews as one unit.

## Identity

```
variant_id      = sha256(canonical_json(strategy_inputs))[:16]
variant_version = <base_strategy_version>+exp.<slug>.<variant_id[:8]>
```

`strategy_inputs` is the **only** hashed member of a config. Titles, rationale,
authorship and dates live under `metadata` and provably cannot move the id, so
two configs with the same id produce the same run — which is what makes the id
usable as an equality test on behavior rather than a label.

The base version is embedded on purpose. A future 2.3.0 rebase must invalidate
every 2.2.0 variant id loudly instead of letting an old override ride along on new
code.

The control's id is the hash of `{}` — a constant. A control is therefore
*recognised*, not declared, and a config labelled "control" that overrides
something fails `is_control`.

**No YAML.** `requirements.txt` is the C-19 determinism contract and its own
header states that changing it is a re-baselining event that makes prior baselines
incomparable. Adding a parser dependency to read experiment configs would
invalidate the baseline the experiments are measured against. Configs are
canonical JSON.

## The rules

1. **Preregistration before results.** 31,209 scored trades across 54 registered
   segment dimensions will always yield something significant. The defence is a
   prediction made before looking; its digest is carried into every result, so an
   edited prediction stops matching.
2. **Every result records its control.** `control_run_id` plus
   `comparison_key_digest`. `ComparisonKey` refuses to diff two runs that differ
   on a C-5.7.2 precondition, and a result that cannot name its control cannot be
   checked.
3. **Every result records its tier**, plus n, σ and the minimum detectable effect
   at that n. "No significant difference" is unreadable without knowing what
   could have been detected. σ(realized_r) on the official baseline is **1.667 R**,
   so an unpaired two-arm comparison needs ~35,600 trades to see 0.05 R — more
   than the whole baseline. Screen at tier 0/1, where the comparison is paired or
   the population is the full 93,699 resolved outcomes.
4. **Volume-matched controls for any filter.** With a −0.0087 R baseline and
   41,625 slot-capped rejects, *any* rule that trades less improves net R
   mechanically. A filter must beat a rule that removes the same count with no
   hypothesised skill.
5. **Three tiers, and each says what it holds fixed.**
   - `tier0` — admission re-decided over already-resolved outcomes. Minutes.
     Cannot see score changes. `backtest.cli tier0`.
   - `tier1` — exits re-resolved over a frozen decision stream. Paired.
   - `tier2` — full replay, ~7.8 h. The only tier that holds nothing fixed.
6. **The holdout is a consumable.** Three evaluations, logged, then stop.

## Baseline

The frozen 2.2.0 reference lives at `data/baselines/v2.2.0-official/`:

| | |
|---|---|
| `run_id` | `replay-9ec9cd4487f3ba19` |
| `baseline_id` | `baseline-daaf6d3a52eebf54` |
| `metrics_hash` | `e7f124530b16437d` |
| `comparison_key_digest` | `0880d4d7557d067f` |
| `data_snapshot_id` | `c8542cb360d13207` |
| window | 2023-01-01T00:00Z → 2026-06-28T23:00Z |
| `burn_in_h1` / `universe_mode` / `top_coins_limit` | 336 / volume / 30 |

Before running any variant, confirm the control has not drifted:

```
python -m backtest.cli identity --run data/runs/official/runs/replay-9ec9cd4487f3ba19
```

It re-scores all 886,954 ledger rows and diffs against what they recorded. It
proves the score engine and its thresholds are unchanged; it does **not** prove
the stages that produced those inputs are unchanged. Its own report says so.
