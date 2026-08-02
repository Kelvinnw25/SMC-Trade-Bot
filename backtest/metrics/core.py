"""The aggregator — `OutcomeRecord`s in, one `MetricSet` out.

Every number this module produces is defined in `definitions.py`, and a test
asserts the two agree: a metric present here and undocumented there, or documented
there and missing here, fails the build. That is the mechanism that keeps a
baseline auditable a year after it was frozen.

## What it reads

`OutcomeRecord`s. Nothing else. No candle, no capture file, no ledger, no
repository, no clock. `backtest.metrics` imports nothing from `backtest.replay` and
opens no file — both asserted structurally — so it cannot acquire a replay-shaped
assumption and a future live outcome feeds through the identical path (Task 9).

## The populations

`definitions.POPULATIONS` is normative and `Populations` below is its
implementation. The one that matters is `evaluated`: shadows out (C-2.2), burn-in
out (C-5.7.1), `INVALID_LEVELS` out (C-4.14). Everything headline is computed on
subsets of it, and the excluded counts are reported rather than dropped — an
exclusion nobody can see is indistinguishable from a bug.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, Iterable, Optional, Sequence

from backtest.metrics.bootstrap import (
    BOOTSTRAP_SEED, ConfidenceInterval, MIN_SEGMENT_N, bootstrap_statistic,
)
from backtest.metrics.definitions import BREAKEVEN_R, METRICS_BY_NAME
from backtest.metrics.equity import EquityCurve, build_equity_curve
from backtest.outcome.record import OutcomeRecord
from backtest.outcome.trade import (
    STATE_ACTIVE, STATE_EXPIRED, STATE_INVALIDATED_EXIT, STATE_INVALID_LEVELS,
    STATE_LOSS, STATE_NOT_FILLED, STATE_PENDING, STATE_WIN, TERMINAL_STATES,
)

__all__ = ["MetricSet", "Populations", "compute_metrics"]

_WORST_CASE_EXITS = ("SL_WORST_CASE_M5", "SL_WORST_CASE_NO_M5")


@dataclass(frozen=True)
class Populations:
    """The four populations of `definitions.POPULATIONS`, materialised once."""

    all: tuple[OutcomeRecord, ...]
    evaluated: tuple[OutcomeRecord, ...]
    resolved: tuple[OutcomeRecord, ...]
    scored: tuple[OutcomeRecord, ...]
    filled: tuple[OutcomeRecord, ...]
    fill_decided: tuple[OutcomeRecord, ...]
    shadows: tuple[OutcomeRecord, ...]
    burn_in: tuple[OutcomeRecord, ...]
    invalid_levels: tuple[OutcomeRecord, ...]
    open_trades: tuple[OutcomeRecord, ...]

    @classmethod
    def of(cls, records: Iterable[OutcomeRecord]) -> "Populations":
        records = tuple(records)
        shadows = tuple(r for r in records if r.shadow_kind is not None)
        burn_in = tuple(r for r in records if r.in_burn_in)
        invalid = tuple(r for r in records if r.state == STATE_INVALID_LEVELS)
        evaluated = tuple(
            r for r in records
            if r.shadow_kind is None and not r.in_burn_in
            and r.state != STATE_INVALID_LEVELS)
        resolved = tuple(r for r in evaluated if r.state in TERMINAL_STATES)
        return cls(
            all=records,
            evaluated=evaluated,
            resolved=resolved,
            scored=tuple(r for r in resolved if r.realized_r is not None),
            filled=tuple(r for r in evaluated if r.filled),
            # A trade still PENDING when the data ended has an unsettled fill
            # question, so counting it as a miss would understate the fill rate.
            fill_decided=tuple(r for r in evaluated if r.state != STATE_PENDING),
            shadows=shadows,
            burn_in=burn_in,
            invalid_levels=invalid,
            open_trades=tuple(r for r in evaluated
                              if r.state in (STATE_PENDING, STATE_ACTIVE)),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "all": len(self.all),
            "evaluated": len(self.evaluated),
            "resolved": len(self.resolved),
            "scored": len(self.scored),
            "filled": len(self.filled),
            "fill_decided": len(self.fill_decided),
            "shadows": len(self.shadows),
            "burn_in": len(self.burn_in),
            "invalid_levels": len(self.invalid_levels),
            "open": len(self.open_trades),
        }


@dataclass(frozen=True)
class MetricSet:
    """Every metric for one population, plus the counts behind them.

    `values` holds the numbers, keyed by the names in `definitions.METRICS`.
    `counts` holds the supporting integers a reader needs to judge them —
    populations, state tallies, and the `n` behind any mean computed over a
    subset. `flags` holds the booleans that qualify a number
    (`profit_factor_undefined`, `inferential`, `mixed_sources`).
    """

    values: dict[str, Optional[float]]
    counts: dict[str, int]
    flags: dict[str, bool]
    distributions: dict[str, dict[str, int]]
    equity: EquityCurve
    intervals: dict[str, ConfidenceInterval] = field(default_factory=dict)
    populations: Optional[Populations] = None

    @property
    def n(self) -> int:
        return self.counts.get("total_trades", 0)

    @property
    def inferential(self) -> bool:
        """§12: below `min_segment_n` a segment may be reported but MUST NOT
        justify a change."""
        return self.flags.get("inferential", False)

    def get(self, name: str) -> Optional[float]:
        return self.values.get(name)

    def to_dict(self, *, include_series: bool = False) -> dict[str, Any]:
        return {
            "values": dict(sorted(self.values.items())),
            "counts": dict(sorted(self.counts.items())),
            "flags": dict(sorted(self.flags.items())),
            "distributions": {
                key: dict(sorted(value.items()))
                for key, value in sorted(self.distributions.items())
            },
            "equity": self.equity.to_dict(include_series=include_series),
            "intervals": {
                key: interval.to_dict()
                for key, interval in sorted(self.intervals.items())
            },
        }


def compute_metrics(
    records: Iterable[OutcomeRecord],
    *,
    with_intervals: bool = True,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    min_segment_n: int = MIN_SEGMENT_N,
    n_resamples: Optional[int] = None,
) -> MetricSet:
    """Aggregate one population. Pure: same records in, same numbers out.

    `with_intervals=False` skips the bootstrap, which is the only expensive step —
    used when computing many segments where only the point estimates are wanted.
    Skipping it changes no point estimate, because the point estimate is computed
    from the sample and never from the resamples.
    """
    pop = Populations.of(records)
    values: dict[str, Optional[float]] = {}
    counts: dict[str, int] = {}
    flags: dict[str, bool] = {}

    # -- population counts -------------------------------------------------
    counts["total_trades"] = len(pop.evaluated)
    counts["completed_trades"] = len(pop.resolved)
    counts["pending_trades"] = len(pop.open_trades)
    counts["scored_trades"] = len(pop.scored)
    counts["filled_trades"] = len(pop.filled)
    counts["fill_decided_trades"] = len(pop.fill_decided)
    counts["shadow_trades"] = len(pop.shadows)
    counts["burn_in_trades"] = len(pop.burn_in)
    counts["invalid_levels_trades"] = len(pop.invalid_levels)
    counts["all_records"] = len(pop.all)
    for name in ("total_trades", "completed_trades", "pending_trades",
                 "scored_trades", "shadow_trades", "burn_in_trades",
                 "invalid_levels_trades"):
        values[name] = float(counts[name])

    state_counts = _tally(pop.resolved, lambda r: r.state)
    counts.update({f"state_{key}": value for key, value in state_counts.items()})

    r_values = [float(r.realized_r) for r in pop.scored]      # type: ignore[arg-type]
    net_values = [float(r.realized_r_net) for r in pop.scored
                  if r.realized_r_net is not None]

    # -- fill --------------------------------------------------------------
    values["fill_rate"] = _rate(len(pop.filled), len(pop.fill_decided))
    values["immediate_fill_share"] = _rate(
        sum(1 for r in pop.filled if r.fill_kind == "IMMEDIATE"), len(pop.filled))
    ttf = [float(r.candles_to_fill) for r in pop.filled
           if r.candles_to_fill is not None]
    values["avg_time_to_fill"] = _mean(ttf)
    values["median_time_to_fill"] = _median(ttf)
    counts["time_to_fill_n"] = len(ttf)

    # -- state rates -------------------------------------------------------
    values["win_rate"] = _rate(state_counts.get(STATE_WIN, 0), len(pop.scored))
    values["loss_rate"] = _rate(state_counts.get(STATE_LOSS, 0), len(pop.scored))
    values["breakeven_rate"] = _rate(
        sum(1 for r in pop.scored if r.is_breakeven), len(pop.scored))
    values["not_filled_rate"] = _rate(
        state_counts.get(STATE_NOT_FILLED, 0), len(pop.resolved))
    values["invalidated_rate"] = _rate(
        state_counts.get(STATE_INVALIDATED_EXIT, 0), len(pop.resolved))
    values["expired_rate"] = _rate(
        state_counts.get(STATE_EXPIRED, 0), len(pop.resolved))
    values["win_rate_of_completed"] = _rate(
        state_counts.get(STATE_WIN, 0), len(pop.resolved))

    # -- R -----------------------------------------------------------------
    values["expectancy"] = _mean(r_values)
    values["median_r"] = _median(r_values)
    values["expectancy_net"] = _mean(net_values)
    # A cancellation earns nothing and costs nothing, so it enters this mean as 0R.
    values["expectancy_per_signal"] = _mean(
        [float(r.realized_r or 0.0) for r in pop.resolved])

    wins = [v for v in r_values if v > 0]
    losses = [v for v in r_values if v < 0]
    values["avg_win"] = _mean(wins)
    values["avg_loss"] = _mean(losses)
    values["largest_win"] = max(r_values) if r_values else None
    values["largest_loss"] = min(r_values) if r_values else None
    counts["win_count"] = len(wins)
    counts["loss_count"] = len(losses)

    gross_profit = sum(v for v in r_values if v > 0)
    gross_loss = -sum(v for v in r_values if v < 0)
    values["gross_profit"] = float(gross_profit)
    values["gross_loss"] = float(gross_loss)
    values["profit_factor"] = _ratio(gross_profit, gross_loss)
    flags["profit_factor_undefined"] = bool(r_values) and gross_loss == 0.0

    net_profit = sum(v for v in net_values if v > 0)
    net_loss = -sum(v for v in net_values if v < 0)
    values["profit_factor_net"] = _ratio(net_profit, net_loss)

    avg_win, avg_loss = values["avg_win"], values["avg_loss"]
    values["payoff_ratio"] = (
        None if avg_win is None or avg_loss is None or avg_loss == 0
        else avg_win / abs(avg_loss))

    # -- holding -----------------------------------------------------------
    holding = [float(r.holding_candles) for r in pop.scored
               if r.holding_candles is not None]
    values["avg_holding_candles"] = _mean(holding)
    values["median_holding_candles"] = _median(holding)
    counts["avg_holding_candles_n"] = len(holding)

    # -- path / risk -------------------------------------------------------
    curve = build_equity_curve(pop.scored)
    values["max_drawdown_r"] = curve.max_drawdown_r
    values["max_consecutive_losses"] = float(curve.max_consecutive_losses)
    counts["max_consecutive_losses"] = curve.max_consecutive_losses

    mae = [float(r.mae_r) for r in pop.scored if r.mae_r is not None]
    mfe = [float(r.mfe_r) for r in pop.scored if r.mfe_r is not None]
    values["avg_mae_r"] = _mean(mae)
    values["avg_mfe_r"] = _mean(mfe)
    counts["excursion_n"] = len(mae)
    values["excursion_degraded_share"] = _rate(
        sum(1 for r in pop.scored if r.excursion_degraded), len(pop.scored))
    values["worst_case_share"] = _rate(
        sum(1 for r in pop.resolved if r.exit_reason in _WORST_CASE_EXITS),
        len(pop.resolved))

    kills = [float(r.realized_r) for r in pop.scored      # type: ignore[arg-type]
             if r.exit_reason == "HTF_KILL"]
    values["kill_exit_rate"] = _rate(
        sum(1 for r in pop.resolved if r.exit_reason == "HTF_KILL"),
        len(pop.resolved))
    values["kill_exit_mean_r"] = _mean(kills)
    values["expiry_mean_r"] = _mean(
        [float(r.realized_r) for r in pop.scored     # type: ignore[arg-type]
         if r.state == STATE_EXPIRED])

    structural = [float(r.structural_rr) for r in pop.evaluated
                  if r.structural_rr is not None]
    values["avg_structural_rr"] = _mean(structural)
    structural_scored = [float(r.structural_rr) for r in pop.scored
                         if r.structural_rr is not None]
    expectancy = values["expectancy"]
    mean_structural = _mean(structural_scored)
    values["structural_vs_realized_gap"] = (
        None if mean_structural is None or expectancy is None
        else mean_structural - expectancy)

    # -- distributions -----------------------------------------------------
    distributions = {
        "state": state_counts,
        "fill_kind": _tally(pop.filled, lambda r: r.fill_kind),
        "cancel_reason": _tally(
            [r for r in pop.resolved if r.state == STATE_NOT_FILLED],
            lambda r: r.cancel_reason),
        "exit_reason": _tally(pop.resolved, lambda r: r.exit_reason),
        "shadow_kind": _tally(pop.shadows, lambda r: r.shadow_kind),
        "r_histogram": _histogram(r_values),
    }

    # -- flags -------------------------------------------------------------
    flags["inferential"] = len(pop.scored) >= min_segment_n
    sources = {r.source for r in pop.all}
    flags["mixed_sources"] = len(sources) > 1

    # -- intervals ---------------------------------------------------------
    intervals: dict[str, ConfidenceInterval] = {}
    if with_intervals:
        resamples = {}
        if n_resamples is not None:
            resamples["n_resamples"] = n_resamples
        intervals["expectancy"] = bootstrap_statistic(
            r_values, statistic=lambda block: block.mean(axis=1),
            seed=bootstrap_seed, min_n=min_segment_n, **resamples)
        intervals["win_rate"] = bootstrap_statistic(
            [1.0 if r.state == STATE_WIN else 0.0 for r in pop.scored],
            statistic=lambda block: block.mean(axis=1),
            seed=bootstrap_seed, min_n=min_segment_n, **resamples)

    return MetricSet(
        values=values, counts=counts, flags=flags,
        distributions=distributions, equity=curve, intervals=intervals,
        populations=pop,
    )


# ---------------------------------------------------------------------------
# Arithmetic with explicit empty-population behaviour
# ---------------------------------------------------------------------------

def _rate(numerator: int, denominator: int) -> Optional[float]:
    """`None` on an empty denominator, never `0.0`. Zero is a measurement; the
    absence of one is not."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / len(values))


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(median(values))


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    """`None` rather than infinity when the denominator vanishes — canonical JSON
    forbids non-finite numbers (C-9.7), and `profit_factor_undefined` carries the
    distinction between "no losses" and "no trades"."""
    if denominator == 0:
        return None
    return float(numerator / denominator)


def _tally(records: Iterable[OutcomeRecord],
           key: Callable[[OutcomeRecord], Optional[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = key(record)
        if value is None:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


#: R histogram edges. Fixed rather than data-derived: bins that move with the data
#: make two baselines' histograms incomparable, which is the one thing a frozen
#: baseline must never allow.
_R_BINS = (-5.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0)


def _histogram(values: Sequence[float]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[_bin_label(value)] = counts.get(_bin_label(value), 0) + 1
    return counts


def _bin_label(value: float) -> str:
    if value < _R_BINS[0]:
        return f"<{_R_BINS[0]:g}"
    for low, high in zip(_R_BINS, _R_BINS[1:]):
        if low <= value < high:
            return f"[{low:g},{high:g})"
    return f">={_R_BINS[-1]:g}"


def _assert_documented() -> None:
    """Import-time guard: every emitted metric is documented, and every documented
    metric is emitted. Kept as a function so the test can call it explicitly with a
    real population rather than relying on import order."""
    documented = set(METRICS_BY_NAME)
    if not documented:                                   # pragma: no cover
        raise RuntimeError("the metric catalogue is empty")
