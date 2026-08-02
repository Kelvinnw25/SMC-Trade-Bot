"""The Baseline Report — assembly and emission (Task 4).

REPLAY_CONTRACT.md §12, C-16.4 (`metrics.json`, `report.md`, `segments.csv`),
C-16.6 (one schema, two sources), C-5.7.2 (comparison preconditions).

## Reproducibility

Running twice over identical outcome records produces **byte-identical** output.
Three things make that true and each is deliberate:

1. **No wall clock.** `generated_at` is an *input*, not a reading. Omit it and the
   report has no non-reproducible key at all. The milestone brief asks the report to
   carry a date; the data's own `window` — first and last instantiation boundary,
   derived from the records — is that date, and it is deterministic. A supplied
   `generated_at` is echoed for provenance and is then the only key a determinism
   comparison must exclude, exactly as C-16.4 already treats the run manifest.
2. **Sorted everything.** Keys, segments, dimensions, distributions.
3. **A seeded bootstrap.** §12 fixes the method, the resample count and the seed;
   a fresh generator per interval means a segment's CI never depends on how many
   segments preceded it.

## Three artifacts, three audiences

- `metrics.json` — the machine-readable baseline. This is the file a future
  experiment diffs against, so it carries the full metric catalogue alongside the
  numbers: a baseline whose definitions are not stored with it cannot be trusted a
  year later.
- `report.md` — the human read. Summary tables, the data-health block, and the
  cautions §12 makes mandatory.
- `segments.csv` — one row per (dimension, segment), for anything that wants a
  spreadsheet or a dataframe.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from backtest.data.repository import canonical_json
from backtest.metrics.bootstrap import BOOTSTRAP_SEED, MIN_SEGMENT_N
from backtest.metrics.core import MetricSet, compute_metrics
from backtest.metrics.definitions import METRIC_ENGINE_VERSION, catalogue
from backtest.metrics.segments import DIMENSIONS, Dimension, Segmentation, segment_all
from backtest.outcome.record import OutcomeRecord

__all__ = [
    "METRICS_FILENAME",
    "REPORT_FILENAME",
    "SEGMENTS_FILENAME",
    "BaselineReport",
    "ReportInputs",
    "build_report",
]

METRICS_FILENAME = "metrics.json"
REPORT_FILENAME = "report.md"
SEGMENTS_FILENAME = "segments.csv"

#: The headline numbers `report.md` leads with and a comparison diffs first.
HEADLINE = (
    "total_trades", "completed_trades", "scored_trades", "pending_trades",
    "fill_rate", "win_rate", "loss_rate", "expectancy", "expectancy_per_signal",
    "median_r", "profit_factor", "payoff_ratio", "avg_win", "avg_loss",
    "largest_win", "largest_loss", "gross_profit", "gross_loss",
    "max_drawdown_r", "max_consecutive_losses", "avg_holding_candles",
    "median_holding_candles", "not_filled_rate", "invalidated_rate",
    "expired_rate", "structural_vs_realized_gap",
)


@dataclass(frozen=True)
class ReportInputs:
    """Everything the report needs that is *not* an outcome record.

    Deliberately explicit. `burn_in_h1` and `universe_mode` are the clearest case:
    C-5.7.2 makes them preconditions for comparing two runs, and no
    `OutcomeRecord` carries either — so they must be declared, and a baseline that
    cannot state them is a baseline that cannot be safely compared. See the M10
    findings.
    """

    run_id: Optional[str] = None
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    burn_in_h1: Optional[int] = None
    universe_mode: Optional[str] = None
    top_coins_limit: Optional[int] = None
    generated_at: Optional[str] = None
    label_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "burn_in_h1": self.burn_in_h1,
            "universe_mode": self.universe_mode,
            "top_coins_limit": self.top_coins_limit,
            "label_fields": list(self.label_fields),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class BaselineReport:
    """One complete evaluation of one outcome population."""

    global_metrics: MetricSet
    shadow_metrics: dict[str, MetricSet]
    segmentation: Segmentation
    lineage: dict[str, Any]
    health: dict[str, Any]
    inputs: ReportInputs
    dataset: dict[str, Any] = field(default_factory=dict)
    bootstrap_seed: int = BOOTSTRAP_SEED
    min_segment_n: int = MIN_SEGMENT_N

    # -- identity ----------------------------------------------------------

    @property
    def metrics_hash(self) -> str:
        """`sha256(canonical_json(the numbers))[:16]`.

        Over the metric *values and counts only* — never over the lineage or the
        catalogue. So a report regenerated by a newer engine that computes the same
        numbers keeps the same hash, and a genuine numeric change is always visible
        as a new one.
        """
        payload = {
            "global": {
                "values": self.global_metrics.values,
                "counts": self.global_metrics.counts,
            },
            "segments": {
                name: {
                    key: metrics.values
                    for key, metrics in sorted(entry.segments.items())
                }
                for name, entry in sorted(self.segmentation.dimensions.items())
            },
        }
        return hashlib.sha256(
            canonical_json(payload).encode("utf-8")).hexdigest()[:16]

    # -- emission ----------------------------------------------------------

    def to_dict(self, *, include_series: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "baseline_report",
            "metrics_engine_version": METRIC_ENGINE_VERSION,
            "metrics_hash": self.metrics_hash,
            "bootstrap_seed": self.bootstrap_seed,
            "min_segment_n": self.min_segment_n,
            "inputs": self.inputs.to_dict(),
            "lineage": self.lineage,
            "dataset": self.dataset,
            "data_health": self.health,
            "global": self.global_metrics.to_dict(include_series=include_series),
            "shadows": {
                kind: metrics.to_dict()
                for kind, metrics in sorted(self.shadow_metrics.items())
            },
            "segmentation": self.segmentation.to_dict(),
            "catalogue": catalogue(),
        }
        if self.inputs.generated_at is not None:
            # The only non-reproducible key, by construction. A determinism check
            # removes exactly this one — the same discipline C-16.4 applies to the
            # run manifest.
            payload["generated_at"] = self.inputs.generated_at
        return payload

    def to_json(self, *, include_series: bool = True) -> str:
        return json.dumps(
            self.to_dict(include_series=include_series),
            indent=2, sort_keys=True, allow_nan=False) + "\n"

    def to_markdown(self) -> str:
        return _markdown(self)

    def to_segments_csv(self) -> str:
        return _segments_csv(self)

    def write(self, out_dir: Path | str, *, overwrite: bool = False
              ) -> dict[str, Path]:
        """Emit all three artifacts.

        Refuses to overwrite unless asked: a baseline is a permanent comparison
        point (Task 8), and silently replacing one is how a frozen reference stops
        meaning anything.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = {
            "metrics": out / METRICS_FILENAME,
            "report": out / REPORT_FILENAME,
            "segments": out / SEGMENTS_FILENAME,
        }
        existing = [path for path in paths.values() if path.exists()]
        if existing and not overwrite:
            raise FileExistsError(
                f"{out} already holds {', '.join(p.name for p in existing)}. "
                f"Pass overwrite=True to replace them.")
        paths["metrics"].write_text(self.to_json(), encoding="utf-8")
        paths["report"].write_text(self.to_markdown(), encoding="utf-8")
        paths["segments"].write_text(self.to_segments_csv(), encoding="utf-8")
        return paths


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_report(
    records: Iterable[OutcomeRecord],
    *,
    labels: Optional[dict] = None,
    health: Optional[dict[str, Any]] = None,
    dataset: Optional[dict[str, Any]] = None,
    inputs: Optional[ReportInputs] = None,
    dimensions: Optional[Sequence[Dimension]] = None,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    min_segment_n: int = MIN_SEGMENT_N,
    segment_intervals: bool = True,
    n_resamples: Optional[int] = None,
) -> BaselineReport:
    """Aggregate, segment, and assemble — the one entry point.

    `records` is the whole population including shadows and burn-in; the engine
    applies C-2.2 and C-5.7.1 itself rather than trusting the caller to have
    filtered, so a caller cannot accidentally promote a shadow into the headline.
    """
    records = tuple(records)
    inputs = inputs or ReportInputs()

    global_metrics = compute_metrics(
        records, bootstrap_seed=bootstrap_seed, min_segment_n=min_segment_n,
        n_resamples=n_resamples)

    # Shadows are reported per class and never merged (C-2.2). Each class answers a
    # different question, so pooling them would answer none.
    shadow_metrics: dict[str, MetricSet] = {}
    for kind in sorted({r.shadow_kind for r in records if r.shadow_kind}):
        members = tuple(r for r in records if r.shadow_kind == kind)
        # Re-labelled as non-shadow so `Populations` admits them: within its own
        # class a shadow is the population, and burn-in still applies.
        shadow_metrics[str(kind)] = compute_metrics(
            [_unshadow(r) for r in members], bootstrap_seed=bootstrap_seed,
            min_segment_n=min_segment_n, n_resamples=n_resamples)

    segmentation = segment_all(
        records, labels=labels, dimensions=dimensions,
        with_intervals=segment_intervals, bootstrap_seed=bootstrap_seed,
        min_segment_n=min_segment_n, n_resamples=n_resamples)

    return BaselineReport(
        global_metrics=global_metrics,
        shadow_metrics=shadow_metrics,
        segmentation=segmentation,
        lineage=_lineage(records, inputs),
        health=dict(health or {}),
        inputs=inputs,
        dataset=dict(dataset or {}),
        bootstrap_seed=bootstrap_seed,
        min_segment_n=min_segment_n,
    )


def _unshadow(record: OutcomeRecord) -> OutcomeRecord:
    import dataclasses
    return dataclasses.replace(record, shadow_kind=None)


def _lineage(records: Sequence[OutcomeRecord],
             inputs: ReportInputs) -> dict[str, Any]:
    """Lineage read from the records themselves.

    Every value is a *set* rather than a scalar, because a population that mixes
    two snapshot ids or two strategy versions must say so (C-16.6.2/3) instead of
    reporting the first one it saw. `mixed_*` flags make that unmissable.
    """
    def distinct(name: str) -> list[str]:
        return sorted({str(getattr(r, name)) for r in records
                       if getattr(r, name) is not None})

    boundaries = [r.boundary for r in records]
    envs = sorted({canonical_json(r.env) for r in records if r.env})
    lineage: dict[str, Any] = {
        "run_ids": distinct("run_id"),
        "sources": distinct("source"),
        "strategy_versions": distinct("strategy_version"),
        "contract_versions": distinct("contract_version"),
        "outcome_config_hashes": distinct("outcome_config_hash"),
        "data_snapshot_ids": distinct("data_snapshot_id"),
        "ledger_schema_versions": sorted(
            {int(r.ledger_schema_version) for r in records}),
        "outcome_schema_versions": sorted(
            {int(r.outcome_schema_version) for r in records}),
        "environments": [json.loads(env) for env in envs],
        "window": {
            "first_boundary": min(boundaries) if boundaries else None,
            "last_boundary": max(boundaries) if boundaries else None,
            "declared_start": inputs.window_start,
            "declared_end": inputs.window_end,
        },
        "burn_in_h1": _declared("burn_in_h1", records, inputs.burn_in_h1),
        "universe_mode": _declared(
            "universe_mode", records, inputs.universe_mode),
        "top_coins_limit": inputs.top_coins_limit,
    }
    for name in ("run_ids", "sources", "strategy_versions", "contract_versions",
                 "outcome_config_hashes", "data_snapshot_ids"):
        lineage[f"mixed_{name[:-1] if name.endswith('s') else name}"] = (
            len(lineage[name]) > 1)
    for name in ("burn_in_h1", "universe_mode"):
        lineage[f"mixed_{name}"] = len(_distinct(name, records)) > 1
    return lineage


def _distinct(name: str, records: Sequence[OutcomeRecord]) -> list:
    values = {getattr(record, name) for record in records}
    return sorted((value for value in values if value is not None), key=str)


def _declared(name: str, records: Sequence[OutcomeRecord], supplied: Any) -> Any:
    """The run-level lineage value, preferring the records over the caller.

    Since M11 an `OutcomeRecord` carries `burn_in_h1` and `universe_mode` itself
    (finding N2), so the report reads the value that *produced* the data instead of
    the value someone typed. `ReportInputs` remains the fallback for records
    written before schema 2, and a caller who supplies a value contradicting the
    data is an error rather than a silent override — that disagreement is exactly
    the mislabeling C-5.7.2 exists to prevent.

    A population mixing two values yields `None`: it is not one run's lineage, and
    an undeclared key is refused by `freeze()` rather than resolved by picking one.
    The `mixed_*` flag beside it says why.
    """
    values = _distinct(name, records)
    if len(values) > 1:
        return None
    if not values:
        return supplied
    value = values[0]
    if supplied is not None and supplied != value:
        raise ValueError(
            f"{name}: the records say {value!r} but ReportInputs says "
            f"{supplied!r}. The data is authoritative — a report labeled with a "
            f"burn-in or universe policy the run did not use would make two "
            f"incomparable runs look comparable (C-5.7.2).")
    return value


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _fmt(value: Any, *, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.{digits}f}"
    return str(value)


def _markdown(report: BaselineReport) -> str:
    out = io.StringIO()
    lineage = report.lineage
    metrics = report.global_metrics

    out.write("# Baseline Report\n\n")
    out.write(f"`metrics_hash` **{report.metrics_hash}** · metrics engine v"
              f"{METRIC_ENGINE_VERSION}\n\n")

    # -- identity --------------------------------------------------------
    out.write("## Identity\n\n")
    out.write("| Field | Value |\n|---|---|\n")
    rows = [
        ("run_id(s)", ", ".join(lineage["run_ids"]) or "—"),
        ("source(s)", ", ".join(lineage["sources"]) or "—"),
        ("strategy_version", ", ".join(lineage["strategy_versions"]) or "—"),
        ("contract_version", ", ".join(lineage["contract_versions"]) or "—"),
        ("outcome_config_hash", ", ".join(lineage["outcome_config_hashes"]) or "—"),
        ("data_snapshot_id", ", ".join(lineage["data_snapshot_ids"]) or "—"),
        ("window (first → last boundary)",
         f"{lineage['window']['first_boundary']} → "
         f"{lineage['window']['last_boundary']}"),
        ("burn_in_h1", _fmt(lineage["burn_in_h1"])),
        ("universe_mode", _fmt(lineage["universe_mode"])),
        ("bootstrap_seed", str(report.bootstrap_seed)),
        ("min_segment_n", str(report.min_segment_n)),
    ]
    if report.inputs.generated_at is not None:
        rows.append(("generated_at", report.inputs.generated_at))
    for name, value in rows:
        out.write(f"| {name} | {value} |\n")

    warnings = [
        f"**mixed {name}** — this population spans "
        f"{len(lineage[name + 's'] if name + 's' in lineage else [])} values; "
        f"C-16.6 requires any aggregate that mixes them to declare it"
        for name in ("source", "strategy_version", "contract_version",
                     "data_snapshot_id", "outcome_config_hash")
        if lineage.get(f"mixed_{name}")
    ]
    if warnings:
        out.write("\n> ⚠ " + "\n> ⚠ ".join(warnings) + "\n")

    # -- population ------------------------------------------------------
    out.write("\n## Population\n\n")
    out.write("| Population | n | Note |\n|---|---:|---|\n")
    counts = metrics.counts
    for name, note in (
        ("all_records", "everything supplied"),
        ("total_trades", "the headline population"),
        ("completed_trades", "terminal"),
        ("scored_trades", "carry an R"),
        ("pending_trades", "OPEN at end of data — excluded from expectancy"),
        ("shadow_trades", "research tier (C-2.2), reported separately"),
        ("burn_in_trades", "excluded (C-5.7.1)"),
        ("invalid_levels_trades", "excluded (C-4.14)"),
    ):
        out.write(f"| `{name}` | {counts.get(name, 0)} | {note} |\n")

    # -- headline --------------------------------------------------------
    out.write("\n## Headline metrics\n\n")
    out.write("| Metric | Value | 95% CI | n |\n|---|---:|---|---:|\n")
    for name in HEADLINE:
        value = metrics.values.get(name)
        interval = metrics.intervals.get(name)
        ci = "—"
        if interval is not None and interval.low is not None:
            flag = "" if interval.inferential else " ⚠"
            ci = f"[{interval.low:.4f}, {interval.high:.4f}]{flag}"
        out.write(f"| `{name}` | {_fmt(value)} | {ci} | "
                  f"{counts.get('scored_trades', 0)} |\n")
    if metrics.flags.get("profit_factor_undefined"):
        out.write("\n> `profit_factor` is undefined: no losing trade, so the "
                  "denominator is zero. Reported as `null`, never as infinity.\n")
    if not metrics.inferential:
        out.write(f"\n> ⚠ **Not inferential.** "
                  f"{counts.get('scored_trades', 0)} scored trades is below "
                  f"`min_segment_n` = {report.min_segment_n}. §12: this population "
                  f"may be reported but MUST NOT justify a strategy change.\n")

    # -- distributions ---------------------------------------------------
    out.write("\n## Distributions\n\n")
    for name, values in sorted(metrics.distributions.items()):
        if not values:
            continue
        out.write(f"**{name}** — ")
        out.write(", ".join(f"`{key}` {value}"
                            for key, value in sorted(values.items())))
        out.write("\n\n")

    # -- equity ----------------------------------------------------------
    curve = metrics.equity
    out.write("## Equity path (C-9.8, 1R per trade)\n\n")
    out.write("| Field | Value |\n|---|---:|\n")
    for name, value in (
        ("trades on the curve", curve.trades),
        ("final R", _fmt(curve.final_r)),
        ("max drawdown (R)", _fmt(curve.max_drawdown_r)),
        ("max run-up (R)", _fmt(curve.max_runup_r)),
        ("max consecutive losses", curve.max_consecutive_losses),
        ("max consecutive wins", curve.max_consecutive_wins),
    ):
        out.write(f"| {name} | {value} |\n")
    out.write("\nOrdered by `(exit_T, cycle_index, universe_rank, symbol)`; the "
              "curve is a sum of R, not a compounded return series — no position "
              "sizing exists (A25).\n")

    # -- shadows ---------------------------------------------------------
    if report.shadow_metrics:
        out.write("\n## Shadow populations (C-2.2 — never in the headline)\n\n")
        out.write("| shadow_kind | n | fill_rate | win_rate | expectancy |\n")
        out.write("|---|---:|---:|---:|---:|\n")
        for kind, shadow in sorted(report.shadow_metrics.items()):
            out.write(f"| `{kind}` | {shadow.counts.get('scored_trades', 0)} | "
                      f"{_fmt(shadow.values.get('fill_rate'))} | "
                      f"{_fmt(shadow.values.get('win_rate'))} | "
                      f"{_fmt(shadow.values.get('expectancy'))} |\n")

    # -- data health -----------------------------------------------------
    out.write("\n## Data health (mandatory — §12)\n\n")
    if not report.health:
        out.write("> ⚠ **No data-health block was supplied.** §12 makes it "
                  "mandatory in every report: without it, a run that analysed "
                  "nothing is indistinguishable from a strategy that found "
                  "nothing.\n")
    else:
        out.write("| Field | Value |\n|---|---:|\n")
        for name, value in sorted(report.health.items()):
            out.write(f"| `{name}` | {_fmt(value)} |\n")
        if report.health.get("clean") is False:
            out.write("\n> ⚠ The run is **not clean**: a swallowed-error or "
                      "attribution counter is non-zero (C-15.8). Metrics from it "
                      "describe an incomplete dataset.\n")
    out.write(f"\n`excursion_degraded_share` "
              f"{_fmt(metrics.values.get('excursion_degraded_share'))} · "
              f"`worst_case_share` "
              f"{_fmt(metrics.values.get('worst_case_share'))} — both bound how "
              f"far the path metrics can be trusted.\n")

    # -- segmentation ----------------------------------------------------
    segmentation = report.segmentation
    out.write(f"\n## Segmentation — {len(segmentation.dimensions)} dimensions, "
              f"{segmentation.total_segments} segments\n\n")
    if segmentation.multiple_comparison_caution:
        out.write(f"> ⚠ **Multiple comparisons.** "
                  f"{segmentation.total_segments} segments are reported; with this "
                  f"many, some will look significant by chance. A segment may "
                  f"motivate a hypothesis, never justify a change (§12).\n\n")
    if segmentation.unavailable:
        out.write(f"> Unavailable dimensions (no labels joined): "
                  f"{', '.join(f'`{n}`' for n in segmentation.unavailable)}\n\n")

    for name, entry in sorted(segmentation.dimensions.items()):
        if not entry.segments:
            continue
        note = "" if entry.dimension.partitions else \
            " — multi-valued; segments do not sum to the population"
        out.write(f"### `{name}`{note}\n\n")
        out.write("| Segment | n | fill_rate | win_rate | expectancy | "
                  "profit_factor | inferential |\n")
        out.write("|---|---:|---:|---:|---:|---:|---|\n")
        for key, segment in sorted(entry.segments.items()):
            out.write(
                f"| `{key}` | {segment.counts.get('scored_trades', 0)} | "
                f"{_fmt(segment.values.get('fill_rate'))} | "
                f"{_fmt(segment.values.get('win_rate'))} | "
                f"{_fmt(segment.values.get('expectancy'))} | "
                f"{_fmt(segment.values.get('profit_factor'))} | "
                f"{'yes' if segment.inferential else 'no'} |\n")
        if entry.excluded:
            out.write(f"\n{entry.excluded} record(s) had no value for this "
                      f"dimension and were excluded from it.\n")
        out.write("\n")

    # -- glossary --------------------------------------------------------
    out.write("## Metric definitions\n\n")
    out.write("| Metric | Population | Formula | Excluded | Zero-trade |\n")
    out.write("|---|---|---|---|---|\n")
    for entry in catalogue():
        out.write(f"| `{entry['name']}` | {entry['population']} | "
                  f"{entry['formula']} | {entry['excluded_states']} | "
                  f"`{entry['zero_trades']}` |\n")

    return out.getvalue()


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

_CSV_COLUMNS = (
    "dimension", "segment", "partitions", "n_total", "n_completed", "n_scored",
    "fill_rate", "win_rate", "loss_rate", "expectancy", "expectancy_per_signal",
    "median_r", "profit_factor", "payoff_ratio", "avg_win", "avg_loss",
    "largest_win", "largest_loss", "max_drawdown_r", "avg_holding_candles",
    "not_filled_rate", "invalidated_rate", "expired_rate",
    "structural_vs_realized_gap", "expectancy_ci_low", "expectancy_ci_high",
    "inferential",
)


def _segments_csv(report: BaselineReport) -> str:
    buffer = io.StringIO(newline="")
    # `\n` explicitly: the default `\r\n` would make byte-identity depend on the
    # platform that generated the file.
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    writer.writerow(_csv_row("__global__", "all", True, report.global_metrics))
    for name, entry in sorted(report.segmentation.dimensions.items()):
        for key, segment in sorted(entry.segments.items()):
            writer.writerow(_csv_row(
                name, key, entry.dimension.partitions, segment))
    return buffer.getvalue()


def _csv_row(dimension: str, segment: str, partitions: bool,
             metrics: MetricSet) -> list[Any]:
    interval = metrics.intervals.get("expectancy")
    values = metrics.values
    counts = metrics.counts
    return [
        dimension, segment, "true" if partitions else "false",
        counts.get("total_trades", 0), counts.get("completed_trades", 0),
        counts.get("scored_trades", 0),
        _csv(values.get("fill_rate")), _csv(values.get("win_rate")),
        _csv(values.get("loss_rate")), _csv(values.get("expectancy")),
        _csv(values.get("expectancy_per_signal")), _csv(values.get("median_r")),
        _csv(values.get("profit_factor")), _csv(values.get("payoff_ratio")),
        _csv(values.get("avg_win")), _csv(values.get("avg_loss")),
        _csv(values.get("largest_win")), _csv(values.get("largest_loss")),
        _csv(values.get("max_drawdown_r")),
        _csv(values.get("avg_holding_candles")),
        _csv(values.get("not_filled_rate")), _csv(values.get("invalidated_rate")),
        _csv(values.get("expired_rate")),
        _csv(values.get("structural_vs_realized_gap")),
        _csv(None if interval is None else interval.low),
        _csv(None if interval is None else interval.high),
        "true" if metrics.inferential else "false",
    ]


def _csv(value: Optional[float]) -> str:
    """Empty string for `None` — a spreadsheet reads it as blank, not as zero.
    Fixed precision so two runs produce identical bytes."""
    if value is None:
        return ""
    return f"{float(value):.10g}"
