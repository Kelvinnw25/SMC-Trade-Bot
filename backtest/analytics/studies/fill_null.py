"""Fill-based null — the benchmark correction. BENCHMARK_FIX_SPEC.md v4.

Every null comparison in the research programme to date has priced a trade's
difficulty from its **planned** entry:

    structural_rr = |tp - entry_price| / |entry_price - sl|      walker.py:156

while its outcome was measured in R units taken from the **fill**:

    r_unit = |fill_price - sl|                                   walker.py:173

The two are not commensurate, and because CONTINUATION chases (+0.083 R adverse)
while RETRACEMENT improves (-0.109 R favourable), the error does not cancel — it
biases exactly the between-mode comparison the programme rests on.

This module recomputes the null from the fill and nothing else:

    effective_rr = |tp - fill_price| / |fill_price - sl|
    fill_null    = 1 / (1 + effective_rr)

## What this module does not do

It does not touch the walker, the engine, `OutcomeConfig`, or any frozen metric.
`structural_rr` stays in the record exactly as produced; the correction lives
entirely downstream of the frozen dataset. The study is **opt-in** — it is not a
member of `studies.STUDIES`, registers nothing into `HEADLINE` or
`metrics.definitions`, and therefore cannot move a baseline.

## Eligibility is an ordering question, not a magnitude question

The single most dangerous implementation of `effective_rr` is the obvious one:
take `abs()` of both distances and divide. That returns a finite, plausible,
entirely wrong number for a fill that sits *beyond* a barrier — a SHORT gapped
through its stop yields 17.0 rather than an error. `geometry()` therefore
decides on **ordering** (§B1), and every downstream quantity is defined only
where it returns a value.

## Two nulls, deliberately

`fill_null` requires interior geometry. On `no_race` rows there is no race left
to price, so §B5's sensitivity bounds fall back to `null_bound`, computed from
`structural_rr`, which `_levels_sane` guarantees is positive for every admitted
row. §B6's censoring bounds reinstate rows that *do* have interior geometry and
so keep using `fill_null`. Mixing the two up is silent, which is why D0-S5 tests
the routing rather than the arithmetic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from backtest.analytics.studies.common import LabelledTrade, Study

__all__ = [
    "BARRIER_REASONS",
    "CENSORED_REASONS",
    "Bounds",
    "CellResult",
    "FillNullError",
    "Geometry",
    "Populations",
    "TRIM_FRACTION",
    "adverse_slippage_r",
    "cell_result",
    "effective_rr",
    "fill_null",
    "geometry",
    "null_bound",
    "partition",
    "run",
]


class FillNullError(Exception):
    """A fill-based quantity was asked for where it is not defined."""


#: §B3. Verbatim from `outcome.trade.EXIT_REASONS`. `SL_WORST_CASE_*` are
#: barrier resolutions — both barriers were struck and C-4.3 broke the tie to
#: SL — so omitting them would drop genuine losses and bias Δ upward.
BARRIER_REASONS = frozenset({
    "TP", "SL", "GAP_TP", "GAP_SL",
    "SL_WORST_CASE_M5", "SL_WORST_CASE_NO_M5",
})

#: §B6. `END_OF_DATA` is absent by construction, not by choice: it resolves to
#: STATE_PENDING or STATE_ACTIVE, neither of which is in TERMINAL_STATES, so
#: `Populations.resolved` excludes it upstream and it never reaches the admitted
#: book. See BENCHMARK_FIX_SPEC.md §B3.
CENSORED_REASONS = frozenset({"HTF_KILL", "MAX_HOLDING"})

#: §B4. `effective_rr` is heavy-tailed: as a fill approaches SL the denominator
#: goes to zero. A bare arithmetic mean is never emitted.
TRIM_FRACTION = 0.10


# ---------------------------------------------------------------------------
# §B1 — geometry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Geometry:
    """Fill-relative geometry of a trade whose fill sits between its barriers."""

    direction: str
    fill_price: float
    sl: float
    tp: float
    entry_price: float

    @property
    def effective_rr(self) -> float:
        return abs(self.tp - self.fill_price) / abs(self.fill_price - self.sl)

    @property
    def r_unit(self) -> float:
        return abs(self.fill_price - self.sl)

    @property
    def adverse_slippage_r(self) -> float:
        """Signed, in planned-R units. Positive means the fill was worse."""
        sign = 1.0 if self.direction == "LONG" else -1.0
        return sign * (self.fill_price - self.entry_price) / abs(
            self.entry_price - self.sl)


def geometry(direction: str, fill_price: Optional[float], sl: float, tp: float,
             entry_price: float = 0.0) -> Optional[Geometry]:
    """`Geometry` when the fill sits **strictly between** the barriers, else None.

    LONG requires `sl < fill < tp`; SHORT requires `tp < fill < sl`. The test is
    ordering, never magnitude: `abs()` cannot tell a fill inside the race from
    one that gapped past a barrier, and the second case is precisely `no_race`.
    """
    if direction not in ("LONG", "SHORT"):
        return None
    values = (fill_price, sl, tp, entry_price)
    if any(v is None or not math.isfinite(float(v)) for v in values):
        return None
    fill, sl, tp, entry = (float(v) for v in values)
    inside = sl < fill < tp if direction == "LONG" else tp < fill < sl
    if not inside:
        return None
    if entry == sl:
        # `adverse_slippage_r` would divide by zero. `_levels_sane` forbids it
        # on any admitted row, so reaching here means the caller built a
        # fixture the walker could not have produced.
        return None
    return Geometry(direction, fill, sl, tp, entry)


def effective_rr(trade: Any) -> float:
    """|tp - fill| / |fill - sl|. Raises where the fill is not interior."""
    geo = _geometry_of(trade)
    if geo is None:
        raise FillNullError(
            f"effective_rr is undefined: the fill is not strictly between the "
            f"barriers (direction={_attr(trade, 'direction')!r}, "
            f"fill={_attr(trade, 'fill_price')!r}, sl={_attr(trade, 'sl')!r}, "
            f"tp={_attr(trade, 'tp')!r}). This row is `no_race` (§B3).")
    return geo.effective_rr


def adverse_slippage_r(trade: Any) -> float:
    geo = _geometry_of(trade)
    if geo is None:
        raise FillNullError(
            "adverse_slippage_r is undefined on a non-interior fill (§B3)")
    return geo.adverse_slippage_r


# ---------------------------------------------------------------------------
# §B2 — the two nulls
# ---------------------------------------------------------------------------

def fill_null(trade: Any) -> float:
    """`1 / (1 + effective_rr)`. Raises wherever `geometry()` is None.

    Never returns nan, never returns inf, never lets a `ZeroDivisionError`
    escape: an out-of-range null that reached a mean would be invisible.
    """
    return 1.0 / (1.0 + effective_rr(trade))


def null_bound(trade: Any) -> float:
    """`1 / (1 + structural_rr)` — the §B5 fallback, total on the admitted book.

    Used **only** for reinstated `no_race` rows, where `fill_null` is undefined.
    §B6's censoring bounds reinstate rows that do have interior geometry and use
    `fill_null` instead.
    """
    rr = _attr(trade, "structural_rr")
    if rr is None or not math.isfinite(float(rr)) or float(rr) <= 0.0:
        raise FillNullError(
            f"null_bound requires structural_rr > 0, got {rr!r}. `_levels_sane` "
            f"(walker.py:212-220) guarantees this for every admitted row, so "
            f"this row did not come from the admitted book.")
    return 1.0 / (1.0 + float(rr))


# ---------------------------------------------------------------------------
# §B3 — populations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Populations:
    """The §B3 partition of one cell's admitted trades."""

    admitted: tuple[Any, ...]
    no_race: tuple[Any, ...]
    barrier_eligible: tuple[Any, ...]
    barrier_resolved: tuple[Any, ...]
    censored: tuple[Any, ...]

    def check(self) -> None:
        """The §B3 identities, plus vocabulary closure.

        Closure matters more than the identities: a misread or newly added exit
        reason drops rows from *both* sides of a partition, so the identities
        still pass while the population silently shrinks. That failure mode is
        not hypothetical — it occurred once during drafting of this spec.
        """
        # Closure is checked *first* and raises rather than asserting: it is
        # both the more informative failure and the cause of the identity
        # failure below, and `assert` disappears under `python -O`.
        seen = {_attr(t, "exit_reason") for t in self.admitted}
        unknown = seen - (BARRIER_REASONS | CENSORED_REASONS)
        if unknown:
            raise FillNullError(
                f"admitted rows carry exit reasons outside the §B3 vocabulary: "
                f"{sorted(map(str, unknown))}. Every admitted row must be "
                f"classifiable as a barrier resolution or a censored exit; an "
                f"unclassified reason would be dropped from both populations.")
        if len(self.barrier_resolved) + len(self.censored) != len(
                self.barrier_eligible):
            raise FillNullError(
                f"§B3 identity broken: barrier_resolved "
                f"({len(self.barrier_resolved)}) ⊎ censored "
                f"({len(self.censored)}) != barrier_eligible "
                f"({len(self.barrier_eligible)})")
        if len(self.barrier_eligible) + len(self.no_race) != len(self.admitted):
            raise FillNullError(
                f"§B3 identity broken: barrier_eligible "
                f"({len(self.barrier_eligible)}) + no_race "
                f"({len(self.no_race)}) != admitted ({len(self.admitted)})")


def partition(trades: Iterable[Any]) -> Populations:
    admitted = tuple(trades)
    no_race, eligible = [], []
    for trade in admitted:
        (eligible if _geometry_of(trade) is not None else no_race).append(trade)
    resolved = tuple(t for t in eligible
                     if _attr(t, "exit_reason") in BARRIER_REASONS)
    censored = tuple(t for t in eligible
                     if _attr(t, "exit_reason") in CENSORED_REASONS)
    return Populations(admitted, tuple(no_race), tuple(eligible),
                       resolved, censored)


# ---------------------------------------------------------------------------
# §B4/§B5/§B6 — the estimator and its bounds
# ---------------------------------------------------------------------------

def _is_win(trade: Any) -> bool:
    realized = _attr(trade, "realized_r")
    return realized is not None and float(realized) > 0.0


def _delta(pairs: Sequence[tuple[bool, float]]) -> Optional[float]:
    """Observed win rate minus mean null, over (won, null) pairs."""
    if not pairs:
        return None
    wins = sum(1 for won, _ in pairs if won)
    nulls = sum(null for _, null in pairs)
    return wins / len(pairs) - nulls / len(pairs)


@dataclass(frozen=True)
class Bounds:
    """One §B5 or §B6 sensitivity bound family. Never a primary output."""

    lower: Optional[float]
    upper: Optional[float]
    reinstated_n: int


def _bounds(primary: Sequence[tuple[bool, float]],
            reinstated: Sequence[float]) -> Bounds:
    """Bracket the reinstated rows' unknown outcomes by count.

    Count-based, not MFE/MAE-based: on `no_race` rows `r_unit` collapses toward
    zero, which inflates `mfe_r`/`mae_r` to +1403.44 / -815.51 on the official
    run. Those bound nothing.
    """
    if not reinstated:
        return Bounds(None, None, 0)
    lower = _delta(list(primary) + [(False, n) for n in reinstated])
    upper = _delta(list(primary) + [(True, n) for n in reinstated])
    return Bounds(lower, upper, len(reinstated))


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _trimmed_mean(values: Sequence[float],
                  fraction: float = TRIM_FRACTION) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    cut = int(len(ordered) * fraction)
    kept = ordered[cut:len(ordered) - cut] or ordered
    return sum(kept) / len(kept)


@dataclass(frozen=True)
class CellResult:
    """§D3 row for one cell — null-comparison channel (a) only."""

    cell: str
    n_barrier_resolved: int
    delta_primary: Optional[float]
    no_race_bounds: Bounds
    censoring_bounds: Bounds
    median_effective_rr: Optional[float]
    trimmed_mean_effective_rr: Optional[float]

    def to_row(self) -> dict[str, Any]:
        """§C5: every diagnostic column carries the `_bound_` infix, so a
        primary and a bound are never confusable by name alone."""
        return {
            "cell": self.cell,
            "n_barrier_resolved": self.n_barrier_resolved,
            "delta_primary": self.delta_primary,
            "delta_bound_lower_no_race": self.no_race_bounds.lower,
            "delta_bound_upper_no_race": self.no_race_bounds.upper,
            "delta_bound_lower_censored": self.censoring_bounds.lower,
            "delta_bound_upper_censored": self.censoring_bounds.upper,
            "median_effective_rr": self.median_effective_rr,
            "trimmed_mean_effective_rr": self.trimmed_mean_effective_rr,
        }


def cell_result(cell: str, trades: Iterable[Any]) -> CellResult:
    """The §D3 row for one cell. Bounds are computed independently (§C5)."""
    pops = partition(trades)
    pops.check()

    primary = [(_is_win(t), fill_null(t)) for t in pops.barrier_resolved]
    rrs = [effective_rr(t) for t in pops.barrier_resolved]

    # §B5: reinstated `no_race` rows are priced with `null_bound`, because
    # `fill_null` is undefined on them. §B6: reinstated censored rows *do* have
    # interior geometry, so they keep `fill_null`. D0-S5 pins this routing.
    return CellResult(
        cell=cell,
        n_barrier_resolved=len(pops.barrier_resolved),
        delta_primary=_delta(primary),
        no_race_bounds=_bounds(primary, [null_bound(t) for t in pops.no_race]),
        censoring_bounds=_bounds(primary,
                                 [fill_null(t) for t in pops.censored]),
        median_effective_rr=_median(rrs),
        trimmed_mean_effective_rr=_trimmed_mean(rrs),
    )


# ---------------------------------------------------------------------------
# §B7 / §D3b — as-traded channel
# ---------------------------------------------------------------------------

def as_traded_row(cell: str, trades: Iterable[Any]) -> dict[str, Any]:
    """§D3b. R-space only — no Δ, no null, no z-score may appear here (§B7).

    Population is the admitted book minus `no_race`, i.e. `barrier_eligible`:
    every censored exit is retained, which is what makes this "as traded"
    rather than "as raced". Trades must arrive in C-9.8 order; the equity path
    is read positionally and re-sorting silently changes drawdown and streaks.
    """
    pops = partition(trades)
    pops.check()
    rows = pops.barrier_eligible
    rs = [float(_attr(t, "realized_r")) for t in rows]
    if not rs:
        return {"cell": cell, "n_as_traded": 0, "mean_r": None,
                "median_r": None, "win_rate": None, "total_r": None,
                "max_drawdown_r": None, "max_consecutive_losses": 0}

    equity, peak, drawdown = 0.0, 0.0, 0.0
    streak, worst_streak = 0, 0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        streak = streak + 1 if r <= 0 else 0
        worst_streak = max(worst_streak, streak)

    return {
        "cell": cell,
        "n_as_traded": len(rs),
        "mean_r": sum(rs) / len(rs),
        "median_r": _median(rs),
        "win_rate": sum(1 for r in rs if r > 0) / len(rs),
        "total_r": sum(rs),
        "max_drawdown_r": drawdown,
        "max_consecutive_losses": worst_streak,
    }


# ---------------------------------------------------------------------------
# §C2 / D0-S8 — the descoped shadow surface
# ---------------------------------------------------------------------------

def shadow_cells(*_args: Any, **_kwargs: Any) -> None:
    """Descoped (§C2). Raises rather than returning an empty result.

    `labelled_trades()` filters `shadow_kind is not None` out upstream via
    `Populations.scored`, so any shadow filter here returns `[]` — which would
    be written into a D3 table as "no effect" and be indistinguishable from a
    genuine null. Two artifacts are missing: a shadow-capable loader and a
    committed volume matcher. See BENCHMARK_FIX_SPEC.md §H.
    """
    raise NotImplementedError(
        "shadow re-derivation is out of scope for the benchmark fix (§C2). "
        "The studies layer cannot load shadow records, and no committed volume "
        "matcher exists. See BENCHMARK_FIX_SPEC.md §H.")


# ---------------------------------------------------------------------------
# study entry point
# ---------------------------------------------------------------------------

def _cell_of(trade: Any) -> Optional[str]:
    mode, direction = _attr(trade, "entry_mode"), _attr(trade, "direction")
    if mode is None or direction not in ("LONG", "SHORT"):
        return None
    return f"{mode}×{direction}"


def run(trades: list[LabelledTrade], metrics: dict) -> Study:
    """Emit the §D3 and §D3b tables. Opt-in: not a member of `STUDIES`."""
    cells: dict[str, list[Any]] = {}
    for trade in trades:
        cell = _cell_of(trade.record)
        if cell is not None:
            cells.setdefault(cell, []).append(trade.record)

    d3 = [cell_result(cell, cells[cell]).to_row() for cell in sorted(cells)]
    d3b = [as_traded_row(cell, cells[cell]) for cell in sorted(cells)]
    payload = {
        "source": "derived",
        "study": "fill_null_analysis",
        "spec": "BENCHMARK_FIX_SPEC.md v4",
        "question": "does the mode comparison survive a null priced from the "
                    "fill rather than the planned entry?",
        "labelling": "benchmark-consistent, not edge estimates",
        "method": {
            "primary": "Δ = win_rate(barrier_resolved) − mean(fill_null)",
            "estimand": "conditional on a completed two-barrier race under "
                        "system rules",
            "bounds": "§B5 no_race (null_bound) and §B6 censoring (fill_null) "
                      "are independent diagnostics, never primary, never "
                      "combined",
        },
        "null_comparison": d3,
        "as_traded": d3b,
    }
    return Study("fill_null_analysis", "Fill-based null", payload,
                 _markdown(payload))


def _markdown(payload: dict[str, Any]) -> str:
    out = ["# Fill-based null\n\n", f"> {payload['question']}\n\n",
           f"> All figures are **{payload['labelling']}**.\n\n",
           "## Null comparison (§D3)\n\n",
           "| Cell | n | Δ primary | no_race bound | censoring bound | "
           "median eff. RR | trimmed mean eff. RR |\n",
           "|---|---:|---:|---|---|---:|---:|\n"]
    for row in payload["null_comparison"]:
        out.append(
            f"| `{row['cell']}` | {row['n_barrier_resolved']} | "
            f"{_num(row['delta_primary'])} | "
            f"{_band(row['delta_bound_lower_no_race'], row['delta_bound_upper_no_race'])} | "
            f"{_band(row['delta_bound_lower_censored'], row['delta_bound_upper_censored'])} | "
            f"{_num(row['median_effective_rr'])} | "
            f"{_num(row['trimmed_mean_effective_rr'])} |\n")

    out.append("\n## As traded (§D3b)\n\n")
    out.append("| Cell | n | mean R | median R | win rate | total R | "
               "max DD R | max losses |\n")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for row in payload["as_traded"]:
        out.append(
            f"| `{row['cell']}` | {row['n_as_traded']} | "
            f"{_num(row['mean_r'])} | {_num(row['median_r'])} | "
            f"{_num(row['win_rate'])} | {_num(row['total_r'], 2)} | "
            f"{_num(row['max_drawdown_r'], 2)} | "
            f"{row['max_consecutive_losses']} |\n")
    return "".join(out)


def _num(value: Any, places: int = 4) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _band(lower: Optional[float], upper: Optional[float]) -> str:
    """An empty reinstated population prints `—`, never the primary value: a
    degenerate column must not be readable as a computed one (§D3)."""
    if lower is None or upper is None:
        return "—"
    return f"[{lower:+.4f}, {upper:+.4f}]"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _attr(trade: Any, name: str) -> Any:
    """Read through `LabelledTrade.__getattr__` or straight off a record."""
    return getattr(trade, name, None)


def _geometry_of(trade: Any) -> Optional[Geometry]:
    return geometry(_attr(trade, "direction"), _attr(trade, "fill_price"),
                    _attr(trade, "sl"), _attr(trade, "tp"),
                    _attr(trade, "entry_price"))
