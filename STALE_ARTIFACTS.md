# Stale artifact register — market-fill admissibility amendment

Every artifact listed here was generated under **`outcome_config_hash = 39761eb79ae5432e`** (outcome schema 2), which predates the C-4.2a / C-4.14 market-fill admissibility amendment. Under C-4.12 outcomes produced by different configs are **never comparable without segmentation**, so nothing below may be quoted alongside, mixed with, or diffed against the current baseline.

Nothing here is deleted. The old run and the old baseline remain byte-intact as the evidence for the snapshot they were derived from; they are simply no longer current.

## Identity

| | superseded | current |
|---|---|---|
| baseline id | `baseline-daaf6d3a52eebf54` | **`baseline-d03b8b319ce0551e`** |
| label | `official-2.2.0` | **`admissibility-2.2.0`** |
| `outcome_config_hash` | `39761eb79ae5432e` | **`c6d2c3489e850bcb`** |
| `outcome_schema_version` | 2 | **3** |
| `metrics_hash` | `e7f124530b16437d` | **`d30ce03c4f62ffd6`** |
| `comparison_key_digest` | `0880d4d7557d067f` | **`ae92bab42faa8884`** |
| baseline directory | `data/baselines/v2.2.0-official/` | **`data/baselines/v2.2.0-admissibility/`** |
| run directory | `data/runs/official/runs/replay-9ec9cd4487f3ba19/` | **`data/runs/official-admissibility/runs/replay-9ec9cd4487f3ba19/`** |

`run_id` (`replay-9ec9cd4487f3ba19`), `data_snapshot_id` (`c8542cb360d13207`), `strategy_version` (`2.2.0`) and `contract_version` (`1.1`) are unchanged: the replay itself was not re-run, and no strategy code was touched. Only the outcome projection changed.

## STALE — superseded by a recomputation

Under `data/runs/official/runs/replay-9ec9cd4487f3ba19/analytics/research/`:

- `performance_census/`
- `exit_census` (and root `exit_census.json`)
- `be_path/`
- `episode_reconstruction/`
- `lifecycle_analysis/`
- `lifecycle_duration/`
- `session_decomposition/`
- `risk_analysis.{json,md}`
- `direction_analysis.{json,md}`
- `drawdown_analysis.{json,md}`
- `score_component_analysis.{json,md}`
- `setup_quality_analysis.{json,md}`
- `strategy_research_priority.{json,md}`
- `results/`, `charts/`, `analytics.{json,md}`

Plus, at the repository root: `episode_inference.json`, `exit_census.json` — ad-hoc dumps written by the two study modules whose `__main__` blocks default to the **old** run directory and ignore `--run`. See the caveat below.

Every one of these has a current counterpart under `data/runs/official-admissibility/runs/replay-9ec9cd4487f3ba19/analytics/research/`, each stamping `run_id`, `outcome_config_hash`, `metrics_hash`, `data_snapshot_id` and its own specification hash where it has one.

## STALE — numeric constants derived from the old population

- **`FRICTION_LEG_CONT_SHORT = 0.0683`** in [exit_census.py:93](backtest/analytics/studies/exit_census.py#L93), documented as "136.6 notional/R × 5 bp". The CONT×SHORT filled mean notional-per-R is **82.29** under the current baseline, not 136.8 — the old figure was inflated by the inadmissible fills this amendment removes. The constant is **not** updated here: it is a frozen threshold, and changing it is a threshold decision requiring its own approval, not a side effect of a recomputation. Until then, `exit_census`'s fee-tier band and its `K1`/`J1` gates are evaluated against a stale friction figure.
- **`FRICTION_LEG_BOOK = 0.0441`** ("88.2 notional/R × 5 bp"), same defect: the book-wide filled mean is now **64.32**.
- The ledger's headline "realistic friction is **0.088–0.163 R/trade** at mean notional-per-R 88.2" (`PROJECT_RESEARCH_LEDGER.md`) rests on the same superseded quantity.

## FIXED — the two study entry points that read the wrong run

`episode_inference.__main__` and `exit_census.__main__` called `main()` with no run directory, and `main` then defaulted to the first run under `data/runs/official/runs` — the superseded bundle. Neither parsed `--run`, so passing it was silently ignored, and `python -m ...` produced normal-looking output stamped with the wrong lineage.

Both now require `--run` and **raise** rather than guess when the run is unstated. `main(run_dir, out_path)` is unchanged for programmatic callers. No analytics semantics, constant, or gate was touched. Pinned by `tests/backtest/test_study_run_selection.py`.

## Resolution of the stale friction constants

Provenance verified against the superseded baseline: CONT×SHORT mean notional/R 136.81 × 5 bp = 0.0684 ≈ the recorded 0.0683; book-wide 88.25 × 5 bp = 0.0441 exactly. The formula is `mean(fill_price / |fill_price − sl|) × fee_per_leg`.

Under `baseline-d03b8b319ce0551e` the same formula gives **CONT×SHORT 82.29 → 0.0411** and **book-wide 64.32 → 0.0322**. Medians barely moved (49.80 → 49.06; 42.11 → 41.76), confirming the old means were tail-driven.

- `FRICTION_LEG_BOOK` is **descriptive only** — exported and documented "for context only", used by no gate. Its docstring input is stale.
- `FRICTION_LEG_CONT_SHORT` is a **live decision threshold**, read by `BAND`, `J1`, and `K1`. **Not rebased here**, and rebasing is proven inert at the current measurement: `max_delta_e_lower = 0.00815` sits below `MATERIAL_R = 0.02`, so `band=False`, `J1=False`, `K1=True` at 0.0683, 0.0411, and 0.0322 alike. The `INCONCLUSIVE` verdict is produced by `K2` and `K3`, neither of which reads friction.

## Obsolete justification in `exit_census`'s own docstring

"After excluding `no_race`, `mfe_r` still reaches +286.53 and `mae_r` −3538.13. One −3538 row moves a 31,209-row mean by −0.113 R" — the −3538 row was one of the four `SUB_TICK_RISK` fills and is gone. Excluding `no_race` under the current baseline: `mfe_r` max **+286.53** (unchanged), `mae_r` min **−199.95**. The module's refusal of raw means remains correct on the `mfe` side; the `mae` figure quoted is superseded.
