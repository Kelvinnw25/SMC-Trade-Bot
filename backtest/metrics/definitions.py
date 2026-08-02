"""Every metric, defined once — formula, population, edge cases (Task 5).

REPLAY_CONTRACT.md §12 (the metrics inventory), C-2.2, C-3.1, C-4.10, C-4.12,
C-4.14, C-5.7.1, C-9.6, C-9.8.

## Why the catalogue is data

A metric whose definition lives only in the function that computes it cannot be
audited, and a report that does not state its own denominators is not evidence. So
each metric is declared here — name, formula, which states are counted, which are
excluded, what it does with zero trades — and `report.py` emits the catalogue
alongside the numbers. The same table is the milestone's documentation and the
report's glossary; they cannot drift apart because there is only one of them.

## The four populations

Everything below is computed over one of these, and every metric names which.

| Population | Definition | Why |
|---|---|---|
| `all` | every record supplied | the only population that counts shadows and burn-in |
| `evaluated` | `shadow_kind is None` **and** `not in_burn_in` **and** `state != INVALID_LEVELS` | the headline population. C-2.2 keeps shadows out of headline metrics; C-5.7.1 excludes burn-in from *every* headline metric; C-4.14 excludes `INVALID_LEVELS` from every performance metric |
| `resolved` | `evaluated` and terminal (C-3.1's six terminal states) | trades whose story finished. `OPEN` is excluded — C-4.10 |
| `scored` | `resolved` and `realized_r is not None` | trades that produced an R. Excludes `NOT_FILLED` (a cancelled order has no R) |

`scored` is exactly `resolved` ∩ {WIN, LOSS, INVALIDATED_EXIT, EXPIRED}: those four
are filled, so they have a fill price and therefore an R (C-9.6).

## Zero-trade and NaN behaviour (normative for this engine)

- A **count** over an empty population is `0`.
- A **sum** over an empty population is `0.0`.
- A **rate, mean, median or ratio** over an empty population is `None`, never `0.0`.
  Zero is a measurement; `None` is the absence of one, and a baseline that reports
  `win_rate: 0.0` for a segment with no trades would be read as a catastrophe.
- **`NaN` and `Infinity` never appear.** `canonical_json` is constructed with
  `allow_nan=False` (C-9.7), so emitting either would raise rather than serialize.
  A ratio with a zero denominator is `None` and carries a companion
  `*_undefined: true` flag where the distinction matters (profit factor).
- A metric that requires a field some records leave `None` (`holding_candles`,
  `mae_r`) is computed over the records that have it, and the count that
  contributed is reported beside it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "BREAKEVEN_R",
    "METRICS",
    "METRICS_BY_NAME",
    "METRIC_ENGINE_VERSION",
    "MetricDefinition",
    "POPULATIONS",
    "catalogue",
]

#: Bumped when a metric's *definition* changes, so a baseline frozen under an older
#: definition is never silently compared against a newer one. Part of the baseline
#: comparison key.
METRIC_ENGINE_VERSION = 1

#: C-3.1: the derived break-even bucket. Mirrored from `outcome.record` rather than
#: imported so the catalogue documents the number it uses.
BREAKEVEN_R = 0.1

POPULATIONS = {
    "all": "every record supplied, including shadows and burn-in",
    "evaluated": "shadow_kind is None and not in_burn_in and state != "
                 "INVALID_LEVELS (C-2.2 / C-5.7.1 / C-4.14)",
    "resolved": "evaluated and terminal; OPEN excluded (C-4.10)",
    "scored": "resolved and realized_r is not None; NOT_FILLED excluded",
    "filled": "evaluated and filled",
    "fill_decided": "evaluated minus trades still PENDING at end of data",
}


@dataclass(frozen=True)
class MetricDefinition:
    """One metric's contract with its reader."""

    name: str
    kind: str                       # count | sum | rate | mean | median | ratio | path
    population: str
    formula: str
    definition: str
    included_states: str = "—"
    excluded_states: str = "—"
    edge_cases: str = "—"
    zero_trades: str = ""
    clause: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "population": self.population,
            "formula": self.formula,
            "definition": self.definition,
            "included_states": self.included_states,
            "excluded_states": self.excluded_states,
            "edge_cases": self.edge_cases,
            "zero_trades": self.zero_trades or _default_zero(self.kind),
            "clause": self.clause,
        }


def _default_zero(kind: str) -> str:
    if kind == "count":
        return "0"
    if kind == "sum":
        return "0.0"
    return "None"


_ALL_TERMINAL = "WIN, LOSS, NOT_FILLED, INVALIDATED_EXIT, EXPIRED"
_R_STATES = "WIN, LOSS, INVALIDATED_EXIT, EXPIRED"

METRICS: tuple[MetricDefinition, ...] = (
    # -- population ------------------------------------------------------
    MetricDefinition(
        "total_trades", "count", "evaluated",
        "|evaluated|",
        "Trades in the headline population.",
        included_states="all states, terminal or not",
        excluded_states="shadows (C-2.2), burn-in (C-5.7.1), INVALID_LEVELS (C-4.14)",
        clause="§12 Population",
    ),
    MetricDefinition(
        "completed_trades", "count", "resolved",
        "|resolved|",
        "Trades that reached a terminal state.",
        included_states=_ALL_TERMINAL,
        excluded_states="PENDING, ACTIVE (the OPEN label — C-4.10)",
        clause="C-3.1",
    ),
    MetricDefinition(
        "pending_trades", "count", "evaluated",
        "|{r : r.state ∈ {PENDING, ACTIVE}}|",
        "Trades still live when the repository's data ended. Reported, never "
        "counted in performance.",
        included_states="PENDING, ACTIVE",
        excluded_states=_ALL_TERMINAL,
        edge_cases="Mark-to-market R exists for ACTIVE trades but is excluded "
                   "from expectancy by C-4.10.",
        clause="C-4.10",
    ),
    MetricDefinition(
        "scored_trades", "count", "scored",
        "|{r ∈ resolved : r.realized_r ≠ None}|",
        "Trades carrying an R. The denominator of every R metric.",
        included_states=_R_STATES,
        excluded_states="NOT_FILLED (no fill, therefore no R — C-9.6)",
        clause="C-9.6",
    ),
    MetricDefinition(
        "shadow_trades", "count", "all",
        "|{r : r.shadow_kind ≠ None}|",
        "Research-tier trades, reported separately and never mixed in.",
        clause="C-2.2",
    ),
    MetricDefinition(
        "burn_in_trades", "count", "all",
        "|{r : r.in_burn_in}|",
        "Trades excluded because lifecycle state had not warmed.",
        clause="C-5.7.1",
    ),
    MetricDefinition(
        "invalid_levels_trades", "count", "all",
        "|{r : r.state = INVALID_LEVELS}|",
        "Trades whose frozen levels failed the sanity gate. A strategy-health "
        "signal, not dead code.",
        clause="C-4.14",
    ),

    # -- fill ------------------------------------------------------------
    MetricDefinition(
        "fill_rate", "rate", "fill_decided",
        "|filled| / |fill_decided|",
        "Share of trades whose limit or market order was filled.",
        included_states=f"{_ALL_TERMINAL}, ACTIVE",
        excluded_states="PENDING at end of data — the fill question is unsettled, "
                        "so counting it as a miss would understate the rate",
        edge_cases="CONTINUATION always fills at the walk origin (C-4.2), so a "
                   "population of pure continuations has fill_rate 1.0 by "
                   "construction.",
        clause="§12 Population",
    ),
    MetricDefinition(
        "immediate_fill_share", "rate", "filled",
        "|{r : r.fill_kind = IMMEDIATE}| / |filled|",
        "Share of fills that happened because the limit was already crossed at "
        "the walk origin. §12 calls this out specifically because a high share "
        "means the structural entry was not actually a limit.",
        clause="C-4.1, §12",
    ),
    MetricDefinition(
        "avg_time_to_fill", "mean", "filled",
        "mean(candles_to_fill)",
        "Mean H1 candles from the walk origin to the fill. 0 means filled in W0.",
        clause="§12 Risk/path",
    ),
    MetricDefinition(
        "median_time_to_fill", "median", "filled",
        "median(candles_to_fill)",
        "Median H1 candles to fill.",
        clause="§12 Risk/path",
    ),

    # -- state rates -----------------------------------------------------
    MetricDefinition(
        "win_rate", "rate", "scored",
        "|WIN| / |scored|",
        "Share of R-bearing trades that reached the target. The conventional win "
        "rate: conditioned on having been filled.",
        included_states="WIN",
        excluded_states="NOT_FILLED (not in the denominator), OPEN",
        edge_cases="win_rate + loss_rate ≤ 1: INVALIDATED_EXIT and EXPIRED are "
                   "also R-bearing and occupy the remainder.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "loss_rate", "rate", "scored",
        "|LOSS| / |scored|",
        "Share of R-bearing trades stopped out.",
        included_states="LOSS",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "breakeven_rate", "rate", "scored",
        "|{r : |realized_r| < 0.1}| / |scored|",
        "The derived break-even bucket. V2 has no break-even logic, so this is a "
        "reporting bucket that overlaps the state rates rather than partitioning "
        "them.",
        edge_cases="Not part of the state partition — a BE trade is still WIN or "
                   "LOSS by state.",
        clause="C-3.1",
    ),
    MetricDefinition(
        "not_filled_rate", "rate", "resolved",
        "|NOT_FILLED| / |resolved|",
        "Share of completed trades cancelled before filling. Denominator is "
        "`resolved`, not `scored`, because a NOT_FILLED trade has no R.",
        included_states="NOT_FILLED",
        clause="C-4.8, §12",
    ),
    MetricDefinition(
        "invalidated_rate", "rate", "resolved",
        "|INVALIDATED_EXIT| / |resolved|",
        "Share of completed trades closed early by an HTF-flip kill.",
        included_states="INVALIDATED_EXIT",
        clause="C-4.9",
    ),
    MetricDefinition(
        "expired_rate", "rate", "resolved",
        "|EXPIRED| / |resolved|",
        "Share of completed trades that hit the holding cap.",
        included_states="EXPIRED",
        clause="C-4.10",
    ),
    MetricDefinition(
        "win_rate_of_completed", "rate", "resolved",
        "|WIN| / |resolved|",
        "Win rate with cancellations in the denominator — the per-signal view. "
        "Reported alongside `win_rate` because the two answer different "
        "questions and confusing them is the commonest way to overstate an edge.",
        clause="§12 Performance",
    ),

    # -- R --------------------------------------------------------------
    MetricDefinition(
        "expectancy", "mean", "scored",
        "mean(realized_r)",
        "Expectancy in R per filled trade. §12's headline number.",
        included_states=_R_STATES,
        excluded_states="NOT_FILLED, OPEN",
        edge_cases="R is measured from the actual fill: R_unit = |fill_price − sl| "
                   "(C-9.6/R-16), so a gap-filled trade reports the risk actually "
                   "taken, not the structural risk.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "expectancy_per_signal", "mean", "resolved",
        "Σ(realized_r or 0) / |resolved|",
        "Expectancy per *instantiated* trade, counting a cancellation as 0R. The "
        "capital-deployment view: an unfilled order costs nothing but also earns "
        "nothing. Derived from named quantities; the contract names only "
        "`expectancy`.",
        edge_cases="Always ≤ |expectancy| in magnitude when the fill rate is "
                   "below 1. Reported so the choice of denominator is visible "
                   "rather than assumed.",
        clause="§12 Performance (denominator made explicit)",
    ),
    MetricDefinition(
        "expectancy_net", "mean", "scored",
        "mean(realized_r_net)",
        "Expectancy after the configured fee and cost model. Equals `expectancy` "
        "under the zero-cost baseline.",
        clause="C-4.12",
    ),
    MetricDefinition(
        "median_r", "median", "scored",
        "median(realized_r)",
        "Median realized R.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "avg_win", "mean", "scored",
        "mean({realized_r : realized_r > 0})",
        "Mean R of profitable trades.",
        edge_cases="None when no trade was profitable.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "avg_loss", "mean", "scored",
        "mean({realized_r : realized_r < 0})",
        "Mean R of losing trades, reported as a negative number.",
        edge_cases="None when no trade lost. Trades at exactly 0R belong to "
                   "neither set.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "largest_win", "sum", "scored",
        "max(realized_r)",
        "Best single trade in R.",
        edge_cases="None on an empty population. May be negative if every trade "
                   "lost — it is the maximum, not the best winner.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "largest_loss", "sum", "scored",
        "min(realized_r)",
        "Worst single trade in R. Can be below −1: a candle opening through the "
        "stop exits at that open (C-4.5).",
        clause="C-4.5",
    ),
    MetricDefinition(
        "gross_profit", "sum", "scored",
        "Σ max(realized_r, 0)",
        "Total R gained.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "gross_loss", "sum", "scored",
        "Σ |min(realized_r, 0)|",
        "Total R lost, as a positive magnitude.",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "profit_factor", "ratio", "scored",
        "gross_profit / gross_loss",
        "Gross profit per unit of gross loss.",
        edge_cases="gross_loss = 0 → None with `profit_factor_undefined: true`. "
                   "Infinity is never emitted: canonical JSON forbids it (C-9.7).",
        clause="§12 Performance",
    ),
    MetricDefinition(
        "profit_factor_net", "ratio", "scored",
        "gross_profit_net / gross_loss_net",
        "Profit factor after costs — §12's mandated fee sensitivity.",
        clause="C-4.12",
    ),
    MetricDefinition(
        "payoff_ratio", "ratio", "scored",
        "avg_win / |avg_loss|",
        "Mean winner per unit of mean loser.",
        edge_cases="None when either side is empty.",
        clause="§12 Performance",
    ),

    # -- holding --------------------------------------------------------
    MetricDefinition(
        "avg_holding_candles", "mean", "scored",
        "mean(holding_candles)",
        "Mean H1 candles held, counted from the fill candle inclusive, so a trade "
        "resolved in its fill candle holds 1.",
        edge_cases="Computed over records where holding_candles is not None; the "
                   "contributing count is reported as "
                   "`avg_holding_candles_n`.",
        clause="C-4.10",
    ),
    MetricDefinition(
        "median_holding_candles", "median", "scored",
        "median(holding_candles)",
        "Median H1 candles held. One candle is one hour, so hours equal candles.",
        clause="C-4.10",
    ),

    # -- path / risk ----------------------------------------------------
    MetricDefinition(
        "max_drawdown_r", "path", "scored",
        "max over i of (max_{j≤i} E_j − E_i), E = cumulative Σ realized_r",
        "Largest peak-to-trough decline of the 1R-per-trade equity curve, in R. "
        "Path-dependent, so the trade order is fixed by contract: "
        "(exit_T, cycle_index, universe_rank, symbol).",
        edge_cases="Reported as a non-negative number; 0.0 when the curve never "
                   "declines. Unfilled and OPEN trades contribute nothing.",
        clause="C-9.8",
    ),
    MetricDefinition(
        "max_consecutive_losses", "count", "scored",
        "longest run of realized_r < 0 in C-9.8 order",
        "Longest losing streak, in the same fixed order as the drawdown.",
        clause="§12 Risk/path",
    ),
    MetricDefinition(
        "avg_mae_r", "mean", "scored",
        "mean(mae_r)",
        "Mean maximum adverse excursion in R. Directly powers the SL research "
        "A14.",
        edge_cases="Measured from M5 extremes between fill and exit inclusive, so "
                   "the exit candle's post-exit movement is included and a "
                   "stopped trade can report worse than −1R (M8 finding O6).",
        clause="C-9.6, §12",
    ),
    MetricDefinition(
        "avg_mfe_r", "mean", "scored",
        "mean(mfe_r)",
        "Mean maximum favourable excursion in R. Powers the TP research A13.",
        clause="C-9.6, §12",
    ),
    MetricDefinition(
        "excursion_degraded_share", "rate", "scored",
        "|{r : r.excursion_degraded}| / |scored|",
        "Share of trades whose excursions were measured without a complete M5 "
        "set. Mandatory data-health output: it bounds how far MAE/MFE can be "
        "trusted.",
        clause="§12 Data health",
    ),
    MetricDefinition(
        "worst_case_share", "rate", "resolved",
        "|{r : exit_reason ∈ {SL_WORST_CASE_M5, SL_WORST_CASE_NO_M5}}| / |resolved|",
        "Share of exits resolved by a worst-case tie-break. Every such exit "
        "biases expectancy downward, so this bounds the pessimism.",
        clause="C-4.3, §13.2",
    ),
    MetricDefinition(
        "kill_exit_rate", "rate", "resolved",
        "|{r : exit_reason = HTF_KILL}| / |resolved|",
        "Share of completed trades closed by an HTF-flip kill.",
        clause="C-4.9, §12",
    ),
    MetricDefinition(
        "kill_exit_mean_r", "mean", "scored",
        "mean({realized_r : exit_reason = HTF_KILL})",
        "Mean R of kill exits. May be positive — a kill can close a winner.",
        clause="C-4.9, §12",
    ),
    MetricDefinition(
        "expiry_mean_r", "mean", "scored",
        "mean({realized_r : state = EXPIRED})",
        "Mean mark-to-market R of trades that hit the holding cap.",
        clause="C-4.10, §12",
    ),
    MetricDefinition(
        "avg_structural_rr", "mean", "evaluated",
        "mean(structural_rr)",
        "Mean advertised reward-to-risk, |tp − entry| / |entry − sl|, frozen at "
        "instantiation.",
        clause="C-9.6",
    ),
    MetricDefinition(
        "structural_vs_realized_gap", "mean", "scored",
        "mean(structural_rr) − mean(realized_r)",
        "The gap between what the strategy advertised and what it delivered. "
        "§12 names it explicitly; it is the single most diagnostic number in the "
        "report.",
        edge_cases="Computed over `scored` for both terms so the two means share "
                   "a population.",
        clause="C-9.6, §12",
    ),
)

METRICS_BY_NAME = {definition.name: definition for definition in METRICS}


def catalogue() -> list[dict]:
    """The catalogue as plain data, for `metrics.json` and the report glossary."""
    return [definition.to_dict() for definition in METRICS]


def describe(name: str) -> Optional[MetricDefinition]:
    return METRICS_BY_NAME.get(name)
