"""backtest.metrics — the Metrics & Baseline Engine (Milestone M10).

REPLAY_CONTRACT.md §11 step 5, §12, C-9.8, C-5.7.2, C-16.6.

    OutcomeRecords ──► compute_metrics ──► MetricSet
                   └─► segment_all     ──► Segmentation
                              │
                              ▼
                       build_report ──► metrics.json + report.md + segments.csv
                              │
                              ▼
                          freeze ──► baseline.json   (the permanent reference)
                              │
                              ▼
                         compare ──► deltas, or a refusal (C-5.7.2)

## What it consumes

`OutcomeRecord`s, and an opaque table of labels. Nothing else. This package opens
**no file for reading**, reads no candle, no capture artifact, no ledger and no
clock — all asserted structurally by the test suite. It imports nothing from
`backtest.replay`, so it holds no replay-shaped assumption and a future live outcome
feeds through the identical path (Task 9).

The strategy-context labels §12 wants segmentations for live in the signal ledger,
which `OutcomeRecord` does not carry. `backtest.dataset` performs that join — §11
step 4 — and hands the result here as a plain mapping. That module is the only place
in the evaluation stack that opens a ledger.

## Six modules, one responsibility each

- `definitions` — every metric's formula, population, exclusions and edge cases
- `core`        — the aggregator
- `equity`      — the C-9.8 equity path, drawdown and streaks
- `bootstrap`   — §12's normative percentile CI (the only stochastic step)
- `segments`    — the dimension registry and reusable grouping
- `report`      — assembly and the three artifacts
- `baseline`    — freeze, verify, compare

## What it never does

No strategy logic, no parameter tuning, no refinement, and no write to any replay or
outcome artifact. Metrics never influences replay; replay never computes metrics.
"""
from backtest.metrics.baseline import (
    BASELINE_FILENAME,
    Baseline,
    BaselineError,
    Comparison,
    ComparisonKey,
    IncomparableBaseline,
    MetricDelta,
    compare,
    freeze,
    load_baseline,
)
from backtest.metrics.bootstrap import (
    BOOTSTRAP_SEED,
    CONFIDENCE,
    ConfidenceInterval,
    MIN_SEGMENT_N,
    MULTIPLE_COMPARISON_THRESHOLD,
    N_RESAMPLES,
    bootstrap_mean,
    bootstrap_statistic,
)
from backtest.metrics.core import MetricSet, Populations, compute_metrics
from backtest.metrics.definitions import (
    METRICS,
    METRICS_BY_NAME,
    METRIC_ENGINE_VERSION,
    MetricDefinition,
    POPULATIONS,
    catalogue,
)
from backtest.metrics.equity import EquityCurve, build_equity_curve, c98_order_key
from backtest.metrics.report import (
    HEADLINE,
    METRICS_FILENAME,
    REPORT_FILENAME,
    SEGMENTS_FILENAME,
    BaselineReport,
    ReportInputs,
    build_report,
)
from backtest.metrics.segments import (
    DIMENSIONS,
    DIMENSIONS_BY_NAME,
    Dimension,
    SegmentSet,
    Segmentation,
    group_by,
    register,
    segment_all,
)

__all__ = [
    "BASELINE_FILENAME",
    "BOOTSTRAP_SEED",
    "CONFIDENCE",
    "DIMENSIONS",
    "DIMENSIONS_BY_NAME",
    "HEADLINE",
    "METRICS",
    "METRICS_BY_NAME",
    "METRICS_FILENAME",
    "METRIC_ENGINE_VERSION",
    "MIN_SEGMENT_N",
    "MULTIPLE_COMPARISON_THRESHOLD",
    "N_RESAMPLES",
    "POPULATIONS",
    "REPORT_FILENAME",
    "SEGMENTS_FILENAME",
    "Baseline",
    "BaselineError",
    "BaselineReport",
    "Comparison",
    "ComparisonKey",
    "ConfidenceInterval",
    "Dimension",
    "EquityCurve",
    "IncomparableBaseline",
    "MetricDefinition",
    "MetricDelta",
    "MetricSet",
    "Populations",
    "ReportInputs",
    "SegmentSet",
    "Segmentation",
    "bootstrap_mean",
    "bootstrap_statistic",
    "build_equity_curve",
    "build_report",
    "c98_order_key",
    "catalogue",
    "compare",
    "compute_metrics",
    "freeze",
    "group_by",
    "load_baseline",
    "register",
    "segment_all",
]
