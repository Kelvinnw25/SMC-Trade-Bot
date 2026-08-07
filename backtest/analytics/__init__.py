"""backtest.analytics — the research workbench (M14 Phase 1, M15 Phase 2).

    metrics.json ─┐
    outcomes.jsonl├─► analyse_run ─► AnalyticsReport ─► analytics.json
    signal_ledger ┘                                  ├─► analytics.md
                                                     └─► charts/*.csv

## Six modules, one responsibility each

- `core`     — assembly: identity, populations, calendar, sessions, strategy,
               funnel, caveats, and the markdown rendering
- `series`   — path and distribution analysis: drawdown episodes, rolling
               statistics, R and duration histograms, streaks, symbol rankings
- `stats`    — statistical primitives: two-sample intervals, effect sizes,
               Holm-Bonferroni. No metric lives here
- `research` — the pre-declared hypothesis registry and its evaluation
- `exports`  — CSV emission
- (markdown rendering stays in `core`, beside the structure it renders)

## The invariant that shapes all of it

**Nothing here recomputes a metric that `backtest.metrics` already defines.**
Expectancy, win rate, profit factor, drawdown depth, the equity path and the
streak maxima are copied verbatim out of `metrics.json`, and a test compares the
copies against the source. What this package adds is what a *metric* cannot be:
comparisons between two populations, the shape of a path, and the decisions that
never became trades.

## Boundaries

Reader-only, and enforced by test: no module here imports `backtest.replay` or
names a `v2` module. Output is written exclusively under the caller's output
directory, so a replay bundle stays byte-identical and keeps passing
`backtest.cli audit`.
"""
from backtest.analytics.core import (
    ANALYTICS_DIRNAME,
    ANALYTICS_ENGINE_VERSION,
    ANALYTICS_JSON,
    ANALYTICS_MD,
    ANALYTICS_SCHEMA_VERSION,
    DRAWDOWN_EPISODE_MIN_R,
    AnalyticsError,
    AnalyticsReport,
    analyse_run,
)
from backtest.analytics.exports import CHARTS_DIRNAME, EXPORTS, write_csvs
from backtest.analytics.research import HYPOTHESES, Hypothesis, evaluate_hypotheses
from backtest.analytics.series import (
    DURATION_EDGES,
    LEADERBOARD_MIN_N,
    R_EDGES,
    ROLLING_WINDOWS,
)
from backtest.analytics.stats import (
    COMPARISON_RESAMPLES,
    Comparison,
    compare_means,
    compare_proportions,
    holm_bonferroni,
)

__all__ = [
    "ANALYTICS_DIRNAME",
    "ANALYTICS_ENGINE_VERSION",
    "ANALYTICS_JSON",
    "ANALYTICS_MD",
    "ANALYTICS_SCHEMA_VERSION",
    "CHARTS_DIRNAME",
    "COMPARISON_RESAMPLES",
    "DRAWDOWN_EPISODE_MIN_R",
    "DURATION_EDGES",
    "EXPORTS",
    "HYPOTHESES",
    "LEADERBOARD_MIN_N",
    "R_EDGES",
    "ROLLING_WINDOWS",
    "AnalyticsError",
    "AnalyticsReport",
    "Comparison",
    "Hypothesis",
    "analyse_run",
    "compare_means",
    "compare_proportions",
    "evaluate_hypotheses",
    "holm_bonferroni",
    "write_csvs",
]
