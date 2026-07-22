"""The comparison workflow — snapshot(s) → two analyzers → comparison report(s).

This ties the pieces together into the two ways the harness gets used across the
migration:

1. `compare_analyzers` — run two live analyzers (V1 oracle vs a future V2
   analyzer) on the same snapshot and diff their observable outcomes. This is
   the shadow-mode check of Roadmap §5.

2. `compare_against_golden` — run one analyzer against a *stored* golden field
   map captured earlier from V1. This lets a future phase prove V2 equivalence
   without V1 present, and also catches an accidental change to V1 itself.

Golden files are just the JSON of an `ObservableBehavior.as_field_map()`, so they
diff cleanly in review and load without any backend.

Determinism: nothing here reads the clock, network, or Telegram. `load_snapshots`
reads committed/temporary JSON only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from tests.regression.compare import ComparisonReport, compare_field_maps
from tests.regression.observable import ObservableBehavior
from tests.regression.oracle import Analyzer, V1Oracle
from tests.regression.snapshot import InputSnapshot


def _as_market_snapshot(snapshot: Any) -> Any:
    """Accept either an `InputSnapshot` or an already-built `MarketSnapshot`."""
    return snapshot.to_market_snapshot() if isinstance(snapshot, InputSnapshot) else snapshot


def compare_analyzers(
    snapshot: Any,
    expected: Analyzer,
    actual: Analyzer,
    *,
    prior: Optional[dict[str, Any]] = None,
    label: Optional[str] = None,
    float_tolerance: float = 0.0,
) -> ComparisonReport:
    """Diff two analyzers on one snapshot. `expected` is the reference (V1)."""
    market = _as_market_snapshot(snapshot)
    expected_ob = expected.analyze(market, prior=prior)
    actual_ob = actual.analyze(market, prior=prior)
    return compare_field_maps(
        expected_ob.as_field_map(),
        actual_ob.as_field_map(),
        label=label or market.symbol,
        float_tolerance=float_tolerance,
    )


def compare_against_golden(
    snapshot: Any,
    analyzer: Analyzer,
    golden: dict[str, Any],
    *,
    prior: Optional[dict[str, Any]] = None,
    label: Optional[str] = None,
    float_tolerance: float = 0.0,
) -> ComparisonReport:
    """Diff an analyzer's output against a stored golden field map."""
    market = _as_market_snapshot(snapshot)
    actual_ob = analyzer.analyze(market, prior=prior)
    return compare_field_maps(
        golden,
        actual_ob.as_field_map(),
        label=label or market.symbol,
        float_tolerance=float_tolerance,
    )


def capture_golden(
    snapshot: Any,
    *,
    analyzer: Optional[Analyzer] = None,
    prior: Optional[dict[str, Any]] = None,
) -> ObservableBehavior:
    """Capture the reference observable for a snapshot (V1 oracle by default)."""
    market = _as_market_snapshot(snapshot)
    analyzer = analyzer if analyzer is not None else V1Oracle()
    return analyzer.analyze(market, prior=prior)


def save_golden(path: Any, observable: ObservableBehavior) -> None:
    """Persist an observable's field map as a golden JSON file."""
    text = json.dumps(observable.as_field_map(), indent=2, ensure_ascii=False, sort_keys=True)
    Path(path).write_text(text, encoding="utf-8")


def load_golden(path: Any) -> dict[str, Any]:
    """Load a golden field map."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_snapshots(directory: Any) -> list[InputSnapshot]:
    """Load every ``*.json`` snapshot in a directory, sorted by filename."""
    return [InputSnapshot.load(p) for p in sorted(Path(directory).glob("*.json"))]
