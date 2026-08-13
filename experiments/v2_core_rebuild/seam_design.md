# StrategyConfig seam — design

**Status: implemented.** `v2/analysis/scoring_config.py` plus the threading
described below. Acceptance gates 1–4 pass (identity 886,954 rows / 0 mismatches,
audit PASS, both weight tables agree, full suite green); gate 5, the prefix
replay, has not been run — no replay has been run at all.

Two blocks of the variant schema are declared but **not implemented**, and the
loader refuses them by name rather than ignoring them: `scoring.gates`
(`require_fvg` is a behaviour change, not a literal) and `scoring.thresholds`
declared as a derived anchor (`{"mode": "quantile_anchored"}` — the anchor's
*output* integers must be derived offline first). So `s0-control` and
`s1-dead-weights-zero` load today; `s2`, `s3a`, `s3b`, `s4` and `s5` name work
that has not been done and say so when asked for.

## The requirement that fixes the design

> default config must produce 0 mismatches against the current ledger

That single acceptance test rules out most of the design space. Any seam that
reorders a branch, re-associates an addition, or changes when a note is appended
will fail it, because `notes` is a recorded field and the ledger has 886,954 rows
of it. So the seam **keeps `score_signal`'s control flow verbatim and replaces
only its literals**. It is a parameterisation, not a rules engine.

## Shape

One frozen dataclass in a new module `v2/analysis/scoring_config.py` (domain-free,
no imports beyond stdlib), plus an optional keyword on the two existing entry
points.

```python
@dataclass(frozen=True)
class ScoringConfig:
    weights: Mapping[str, Mapping[str, int]]      # entry_mode -> note -> weight
    thresholds: Mapping[str, Mapping[str, int]]   # entry_mode -> tier -> min score
    rr_floor: float = 1.2
    rr_bands: tuple[tuple[float, int], ...] = ((3.0, 2), (2.0, 1), (1.5, 0))
    rr_cap: Optional[float] = None
    choppy_penalty: int = 2
    require_fvg: bool = False
    allowed_entry_modes: frozenset[str] = ...
    allowed_directions: frozenset[str] = ...

DEFAULT = ScoringConfig(...)   # exactly scoring_v2/baseline_scoring.json
```

Threading:

```python
def score_signal(htf_map, regime, h1_setup, m5_setup=None, *, config=DEFAULT): ...
def enrich_with_m5(base_signal, m5_df, *, session_active, config=DEFAULT): ...
class ScoringStage:
    def __init__(self, config: ScoringConfig = DEFAULT): ...
```

`AnalysisPipeline.default()` gains `config=DEFAULT` and passes it to
`ScoringStage`. Nothing else in the layer changes — `V2Analyzer` already injects
its pipeline, so an experiment composes `AnalysisPipeline.default(config=variant)`
and never edits scoring code. That is the whole seam.

## The one mechanism that carries the weight

Every scored term today is `score += N; notes.append("x")`. That becomes:

```python
def award(note: str) -> None:
    w = weights.get(note)          # weights for this entry_mode
    if w is None:
        return                     # component disabled -> no score, no note
    nonlocal score
    score += w
    notes.append(note)
```

Two properties follow, and both are deliberate:

- **Weight 0 keeps the note.** The score is unchanged and the audit trail still
  records that the component fired. `rr_1.5` already works exactly this way, so
  it is an existing convention, not a new one.
- **Absent key removes the note.** A component genuinely deleted from the
  vocabulary leaves no trace.

This distinction is what makes the never-fires class safe to neutralise: zeroing
`neutral_penalty` is bit-identical on this corpus *and* makes a future firing
visible in `notes` instead of silently subtracting 2.

## Overrides are merged, never replaced

`strategy_inputs.scoring.weight_overrides` is applied onto `DEFAULT.weights`:

```
weight_overrides = {"ALL": {...}, "RETRACEMENT": {...}}   # ALL applied first
```

Merging rather than substituting means a variant config states its *change*, and
a config that forgets a component inherits 2.2.0 rather than silently scoring it
zero. A key not present in `DEFAULT.weights[mode]` is rejected at construction —
a typo must fail loudly, not disable a component.

## Verification, and why the duplicate table stays

`backtest/experiments/rescore.py` holds an independently hand-read copy of the
weight table and checks, on every row, that the note weights sum to the recorded
score. It would be tempting to make it import `ScoringConfig.DEFAULT` and delete
the duplication. **Do not.** The duplication is the measurement: two independent
readings that must agree is a real check, one shared table is a tautology. Add
instead a test asserting the two tables are equal — that keeps them in sync while
preserving the property that a misread in either is caught.

Acceptance gates before any variant runs:

1. `backtest.cli identity --run replay-9ec9cd4487f3ba19` → 0 mismatches / 886,954 rows.
2. `rescore` census over the ledger → 0 unverified rows.
3. `rescore.WEIGHTS == ScoringConfig.DEFAULT.weights` (new test).
4. Full suite green; `backtest.cli audit` PASS.
5. A ~2,000-cycle **prefix replay** with `DEFAULT` reproduces the official run's
   first 2,000 cycles byte-for-byte. Both start from empty memory, so the prefix
   is exactly comparable — this validates the seam end-to-end for ~30 minutes of
   compute instead of 7.8 hours.

## What the seam deliberately does not do

- No plugin registry, no strategy subclassing, no config file discovery at
  runtime. The config is an argument.
- It does not reach outside scoring. The TP cap is a `RiskPlan` concern and gets
  its own field there (see §4 of the response / `expected_metrics.md`); putting
  `rr_cap` on `ScoringConfig` would misplace ownership even though scoring reads
  `rr`.
- It does not make thresholds dynamic. `thresholds` holds integers. Deriving
  them from a score distribution is an offline step whose *output* is written
  into a variant config — never computed inside the strategy.