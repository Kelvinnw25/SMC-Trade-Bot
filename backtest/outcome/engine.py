"""The Outcome Engine — turn trades into terminal outcomes, and write them once.

REPLAY_CONTRACT.md C-9.1 – C-9.8.

## What it is

An **offline** stage. Nothing in `v2/` knows it exists (C-9.1), and it performs no
analysis and generates no signals: given a trade and the prices that followed, it
decides how that trade ended.

    replay creates history  ->  the Outcome Engine evaluates it  ->  metrics aggregate
             (M7)                          (here)                      (not built)

Those are three responsibilities and this module is exactly the middle one. There
is no expectancy, no win rate, no drawdown, no report — the metrics engine owns
all of it, and C-9.8's ordering rule is quoted in this docstring rather than
implemented so that the boundary stays visible.

## One-way derivation (C-9.5)

Outcomes are never written into the signal ledger and no runtime module may read
them. The ledger records *decisions*; outcomes are a re-derivable **projection**
that can be regenerated whenever the labeling rules improve. Consequently the
engine writes exactly one new artifact, `outcomes.jsonl`, and touches no replay
artifact — a run directory it has processed is bit-identical afterwards except for
that one new file.

## Determinism

`resolve` is a pure function of `(intent, repository content, config)`. The file
is written in a fixed order — `(boundary, cycle_index, universe_rank, symbol,
thesis_key)` — with sorted JSON keys, so the same inputs always produce
byte-identical output.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from backtest.data.repository import KlineRepository
from backtest.lineage import (
    RunLineage, STRATEGY_VERSION, environment, read_run_lineage,
)
from backtest.outcome.config import OutcomeConfig
from backtest.outcome.errors import OutcomeError
from backtest.outcome.record import OUTCOME_SCHEMA_VERSION, OutcomeRecord
from backtest.outcome.trade import TradeIntent
from backtest.outcome.walker import Resolution, WalkCounters, resolve_walk
from v2.app.ledger import CONTRACT_VERSION
from v2.domain.signal_ledger import LEDGER_SCHEMA_VERSION

__all__ = [
    "OUTCOMES_FILENAME",
    "OutcomeEngine",
    "OutcomeReport",
]

OUTCOMES_FILENAME = "outcomes.jsonl"


@dataclass(frozen=True)
class OutcomeReport:
    """The result of resolving a population of trades.

    Counts only — the data-quality kind the contract requires a run to surface
    (C-4.3 rule 4). No performance statistic appears here or anywhere else in
    this package.
    """

    records: tuple[OutcomeRecord, ...]
    counters: WalkCounters
    config_hash: str
    path: Optional[Path] = None

    def __len__(self) -> int:
        return len(self.records)

    @property
    def states(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for record in self.records:
            tally[record.state] = tally.get(record.state, 0) + 1
        return tally

    def to_dict(self) -> dict:
        return {
            "trades": len(self.records),
            "states": self.states,
            "outcome_config_hash": self.config_hash,
            "counters": self.counters.to_dict(),
            "path": None if self.path is None else str(self.path),
        }


class OutcomeEngine:
    """Resolves `TradeIntent`s against stored prices.

    Reads the kline repository **directly, by time range** — never through
    `HistoricalMarketDataAdapter`, whose 119/499/199 window limits and ATR column
    are analysis concerns with no meaning for a forward price walk (C-9.5).
    """

    def __init__(
        self,
        repository: KlineRepository,
        *,
        config: Optional[OutcomeConfig] = None,
        strategy_version: str = STRATEGY_VERSION,
        lineage: Optional[RunLineage] = None,
    ) -> None:
        self._repo = repository
        self._config = config if config is not None else OutcomeConfig()
        self._strategy_version = strategy_version
        self._env = environment()
        self._config_hash = self._config.config_hash
        self._lineage = lineage if lineage is not None else RunLineage()

    @property
    def config(self) -> OutcomeConfig:
        return self._config

    @property
    def lineage(self) -> RunLineage:
        """The run-level lineage stamped on every record (C-5.7.2, finding N2).

        Undeclared by default. `for_run` reads it from the run manifest, which is
        the only source that cannot disagree with the run it describes.
        """
        return self._lineage

    @classmethod
    def for_run(
        cls,
        repository: KlineRepository,
        run_dir: Path | str,
        *,
        config: Optional[OutcomeConfig] = None,
        strategy_version: str = STRATEGY_VERSION,
    ) -> "OutcomeEngine":
        """An engine whose lineage comes from `run_dir/manifest.json` (C-16.4).

        The path a caller should take: typing `burn_in_h1` at a command line and
        typing it differently the next time is precisely the failure mode C-5.7.2
        guards against, and a manifest cannot be typed differently.
        """
        return cls(repository, config=config, strategy_version=strategy_version,
                   lineage=read_run_lineage(run_dir))

    @property
    def config_hash(self) -> str:
        return self._config_hash

    # -- resolving ---------------------------------------------------------

    def resolve(self, intent: TradeIntent, *,
                counters: Optional[WalkCounters] = None) -> OutcomeRecord:
        """One trade, one record. Pure with respect to the repository."""
        tally = counters if counters is not None else WalkCounters()
        resolution = resolve_walk(
            intent, repository=self._repo, config=self._config, counters=tally)
        return self._record(intent, resolution)

    def resolve_all(self, intents: Iterable[TradeIntent]) -> OutcomeReport:
        counters = WalkCounters()
        records = [self.resolve(intent, counters=counters)
                   for intent in intents]
        return OutcomeReport(
            records=tuple(_ordered(records)),
            counters=counters,
            config_hash=self._config_hash,
        )

    # -- writing (Task 7) --------------------------------------------------

    def write(
        self,
        intents: Iterable[TradeIntent],
        run_dir: Path | str,
        *,
        overwrite: bool = False,
    ) -> OutcomeReport:
        """Resolve `intents` and write `outcomes.jsonl` into `run_dir`.

        The only file written. Replay artifacts in the same directory are never
        read for mutation and never touched: replay is immutable, and outcomes are
        a projection alongside it (C-9.5).

        An existing `outcomes.jsonl` is refused unless `overwrite=True` — a second
        derivation over the same run should replace its predecessor deliberately,
        not append to it or silently shadow it.
        """
        directory = Path(run_dir)
        if not directory.is_dir():
            raise OutcomeError(f"run directory does not exist: {directory}")
        path = directory / OUTCOMES_FILENAME
        if path.exists() and not overwrite:
            raise OutcomeError(
                f"{path} already exists. Outcomes are a re-derivable projection: "
                f"pass overwrite=True to replace it.")

        report = self.resolve_all(intents)
        payload = "".join(
            json.dumps(record.to_dict(), sort_keys=True) + "\n"
            for record in report.records)
        path.write_text(payload, encoding="utf-8")
        return OutcomeReport(
            records=report.records, counters=report.counters,
            config_hash=report.config_hash, path=path)

    # -- record assembly ---------------------------------------------------

    def _record(self, intent: TradeIntent,
                resolution: Resolution) -> OutcomeRecord:
        return OutcomeRecord(
            run_id=intent.run_id,
            source=intent.source,
            thesis_key=intent.thesis_key,
            symbol=intent.symbol,
            direction=intent.direction,
            entry_mode=intent.entry_mode,
            boundary=intent.boundary,
            h1_last_ts=intent.h1_last_ts,
            cycle_index=intent.cycle_index,
            universe_rank=intent.universe_rank,
            alert_tier=intent.alert_tier,
            shadow_kind=intent.shadow_kind,
            in_burn_in=intent.in_burn_in,
            entry_price=float(intent.entry_price),
            sl=float(intent.sl),
            tp=float(intent.tp),
            structural_rr=resolution.structural_rr,
            tp_source=intent.tp_source,
            entry_source=intent.entry_source,
            score=intent.score,
            filled=resolution.filled,
            fill_kind=resolution.fill_kind,
            fill_T=resolution.fill_T,
            fill_price=resolution.fill_price,
            candles_to_fill=resolution.candles_to_fill,
            state=resolution.state,
            cancel_reason=resolution.cancel_reason,
            exit_reason=resolution.exit_reason,
            exit_T=resolution.exit_T,
            exit_price=resolution.exit_price,
            realized_r=resolution.realized_r,
            realized_r_net=resolution.realized_r_net,
            holding_candles=resolution.holding_candles,
            mae_r=resolution.mae_r,
            mfe_r=resolution.mfe_r,
            excursion_degraded=resolution.excursion_degraded,
            contract_version=CONTRACT_VERSION,
            strategy_version=self._strategy_version,
            ledger_schema_version=LEDGER_SCHEMA_VERSION,
            outcome_schema_version=OUTCOME_SCHEMA_VERSION,
            outcome_config_hash=self._config_hash,
            data_snapshot_id=self._repo.data_snapshot_id,
            env=dict(self._env),
            burn_in_h1=self._lineage.burn_in_h1,
            universe_mode=self._lineage.universe_mode,
            signal_id=intent.signal_id,
        )


def _ordered(records: Sequence[OutcomeRecord]) -> list[OutcomeRecord]:
    """A fixed, total order so the file is byte-stable.

    Instantiation order, which is what a reader expects and what makes two runs
    comparable line by line. Note this is **not** C-9.8's equity ordering —
    `(exit_T, cycle_index, universe_rank, symbol)` — which is the metrics
    engine's concern, applied when it walks the drawdown curve.
    """
    return sorted(records, key=lambda record: (
        record.boundary,
        -1 if record.cycle_index is None else record.cycle_index,
        -1 if record.universe_rank is None else record.universe_rank,
        record.symbol,
        record.thesis_key,
    ))
