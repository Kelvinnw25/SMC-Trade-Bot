"""Phase 3 studies — strategy research over a finished bundle. M16.

    <run>/analytics/research/
        score_component_analysis.json / .md
        setup_quality_analysis.json   / .md
        direction_analysis.json       / .md
        risk_analysis.json            / .md
        drawdown_analysis.json        / .md
        strategy_research_priority.json / .md

## What a study may and may not do

May: read the bundle, reuse the metrics engine's join, compare populations,
classify by a pre-declared rule, and write into its own output directory.

May not: change strategy code, propose a parameter value, mutate any bundle
artifact, or import `backtest.replay` or a `v2` module. All four are pinned by
test — the same rules the analytics package has followed since Phase 1.

## Why studies are separate from `backtest.analytics.core`

`core` assembles the standing report that runs on every bundle in seconds.
Studies join the full 886,954-row ledger and run several hundred bootstrap
comparisons; putting them behind the same command would make the cheap path
expensive for everyone. They are a separate CLI verb for the same reason §11
keeps replay, outcome and metrics apart: different cost, different cadence.

## Reproducibility

Every study is a pure function of the bundle. The only stochastic step is the
bootstrap, seeded from `BOOTSTRAP_SEED`, so two runs over one bundle produce
byte-identical output apart from `generated_at`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backtest.analytics.studies import (
    direction,
    drawdown,
    priority,
    risk,
    scoring,
    setups,
)
from backtest.analytics.studies.common import LabelledTrade, Study, labelled_trades

__all__ = [
    "RESEARCH_DIRNAME",
    "STUDIES",
    "Study",
    "run_studies",
    "write_studies",
]

RESEARCH_DIRNAME = "research"

#: The five analytical studies, in the order the roadmap declares them. The
#: ranking is assembled afterwards from their payloads, so it is not a member.
STUDIES: tuple[tuple[str, Callable[[list[LabelledTrade], dict], Study]], ...] = (
    ("score_component_analysis", scoring.run),
    ("setup_quality_analysis", setups.run),
    ("direction_analysis", direction.run),
    ("risk_analysis", risk.run),
    ("drawdown_analysis", drawdown.run),
)


def run_studies(run_dir: Path | str) -> dict[str, Study]:
    """Run every study plus the ranking. Reads only; writes nothing."""
    from backtest.analytics.core import AnalyticsError
    from backtest.analytics.research import evaluate_hypotheses
    from backtest.metrics import METRICS_FILENAME

    run_dir = Path(run_dir)
    metrics_path = run_dir / METRICS_FILENAME
    if not metrics_path.exists():
        raise AnalyticsError(
            f"no {METRICS_FILENAME} in {run_dir}. The studies read the metrics "
            f"engine's output rather than recomputing it:\n"
            f"    python -m backtest.cli metrics --run {run_dir}")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))

    trades = labelled_trades(run_dir)
    if not trades:
        raise AnalyticsError(
            f"{run_dir} holds no scored trades; there is nothing to study")

    produced: dict[str, Study] = {}
    for name, runner in STUDIES:
        produced[name] = runner(trades, metrics)

    payload, markdown = priority.build(
        {name: study.payload for name, study in produced.items()},
        evaluate_hypotheses([trade.record for trade in trades]),
        len(trades))
    produced["strategy_research_priority"] = Study(
        "strategy_research_priority", "Research priority", payload, markdown)
    return produced


def write_studies(studies: dict[str, Study], out_dir: Path | str,
                  *, identity: dict[str, Any] | None = None
                  ) -> dict[str, Path]:
    """Emit `<name>.json` and `<name>.md` for each study."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written: dict[str, Path] = {}
    for name, study in sorted(studies.items()):
        payload = dict(study.payload)
        payload["identity"] = dict(identity or {})
        payload["generated_at"] = stamp
        json_path = out / f"{name}.json"
        md_path = out / f"{name}.md"
        json_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        header = _header(study.title, identity or {}, stamp)
        md_path.write_text(header + study.markdown, encoding="utf-8")
        written[f"{name}.json"] = json_path
        written[f"{name}.md"] = md_path
    return written


def _header(title: str, identity: dict[str, Any], stamp: str) -> str:
    """Provenance every study carries, so a page that escapes into a document
    can still be traced back to the bundle that produced it."""
    return (f"<!-- {title} · run_id={identity.get('run_id')} · "
            f"metrics_hash={identity.get('metrics_hash')} · "
            f"strategy_version={identity.get('strategy_version')} · "
            f"generated_at={stamp} -->\n\n")
