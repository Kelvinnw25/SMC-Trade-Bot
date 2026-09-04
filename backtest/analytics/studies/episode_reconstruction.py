"""Episode Reconstruction Reader v1 — a raw episode ledger over one captured run.

Opt-in and unregistered: absent from `studies.STUDIES`, absent from the CLI. It
is invoked explicitly, never as part of the standing report.

## What this is

A pure replay of `lifecycle_events.jsonl` (C-15.3) that reconstructs, for every
memory-resident thesis, the span between the mutation that created it and the
mutation (or end of stream) that ended it — an "episode" — plus the raw counts
observed while it was open. It answers no question about *performance*: it
only answers "what did memory do, and when".

## Episode identity, verified before this module was written

`signal_id` (`symbol|entry_mode|h1_last_ts`, minted once per Insert/Replace,
[reconciler.py:356](v2/lifecycle/reconciler.py#L356)) is the episode key —
**not** `thesis_key` (`symbol|direction|entry_mode`), which is coarser and is
deliberately reused across sequential episodes (a same-thesis tier upgrade
mints a fresh `signal_id` and starts a new episode; see the state-transition
table this module was specified against). Verified on the official bundle:
76,576 distinct non-CAPPED `signal_id`, 0 duplicates; `CAPPED` rows (41,625)
carry no `signal_id` and have no episode.

## The state machine, exactly

One resident thesis per symbol (mirrors `SignalStore._active`,
[memory.py](v2/lifecycle/memory.py)):

    InsertMutation / ReplaceMutation  -> start an episode (wholesale write);
                                          if the symbol already had an open
                                          episode with a DIFFERENT signal_id,
                                          that episode ends SUPERSEDED first
                                          (covers both a different thesis and
                                          a same-thesis higher-tier overwrite
                                          — the capture cannot and need not
                                          distinguish the two)
    ReplaceMutation.target_symbol      -> additionally ends that symbol's
                                          episode REPLACED
    PartialRefreshMutation             -> same episode continues; signal_id,
                                          miss_count and last_miss_h1_ts are
                                          untouched by the store
                                          (store.py:101-115, Decision #010)
    IncrementMissMutation              -> same episode continues; +1 to the
                                          episode's miss counter
    EvictMutation(reason)              -> ends the episode KILL / DECAY / TTL
    end of stream, still resident      -> ends the episode
                                          STILL_RESIDENT_AT_END

`STALE` is a pre-memory check that never touches `resident`
(reconciler.py:264-279) and produces zero mutations; it is invisible here by
construction, not by special-casing. `CAPPED` and `KILL_COOLDOWN` likewise
produce zero mutations (the reconciler returns before any `mutations.append`)
and so never open an episode.

## What this does not do

No metric, no expectancy, no win rate, no ranking, no significance claim, no
recommendation, no strategy conclusion. `outcome_joined` is a raw join fact —
"does an `OutcomeRecord` exist for this `signal_id`" — not a performance
figure. Every episode this module reconstructs is emitted, including one with
no outcome (an entry-mode the Outcome Engine does not model, e.g.
`WATCH_ONLY`) — nothing is silently dropped.

## Reuse

`backtest.capture_schema.CapturedEvent` parses the wire format (the same class
`backtest.outcome.builder` reads). `backtest.dataset.read_outcomes` parses
`outcomes.jsonl` for the join. Nothing here recomputes what either already
does; nothing here is imported by, or imports, `backtest.outcome.builder` —
the two are independent readers of the same capture, cross-checked only by
the reconciliation counts each can independently produce.

## Purity and scale

Reader-only. No import of `backtest.replay`, no `v2` or `src` reference, no
RNG, no clock beyond the emission stamp, no write outside the caller's output
directory. The 1.16M-line `lifecycle_events.jsonl` is streamed one line at a
time; only the reconstructed episodes (bounded by the insertion count, not the
event count) are held in memory.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from backtest.capture_schema import (
    CapturedEvent,
    KIND_LOG,
    KIND_MUTATION,
    LIFECYCLE_EVENTS_FILENAME,
    MUTATION_TYPES,
)

__all__ = [
    "DIRNAME",
    "EVIDENCE_CLASS",
    "SCOPE_STATEMENT",
    "Episode",
    "ReconstructionError",
    "ReconstructionResult",
    "analyse",
    "join_outcomes",
    "reconstruct",
    "render",
    "specification_hash",
    "write_episode_reconstruction",
]

DIRNAME = "episode_reconstruction"
ENGINE_VERSION = 1

#: The only evidence class this module can emit.
EVIDENCE_CLASS = "DESCRIPTIVE"

SCOPE_STATEMENT = (
    "Episode reconstruction only; no metrics, inference, ranking, "
    "recommendation, or strategy decision."
)

#: C-4.8's cancel-reason mapping, restated in episode-end vocabulary. Frozen:
#: any new eviction reason must be a deliberate specification change.
_EVICT_REASON_TO_END = {"kill": "KILL", "decay": "DECAY", "ttl": "TTL"}

#: Every end reason this module can emit, for the specification hash and for
#: validation. `REPLACED` / `SUPERSEDED` are inferred, not captured events.
END_REASONS = (
    "KILL", "DECAY", "TTL", "REPLACED", "SUPERSEDED", "STILL_RESIDENT_AT_END",
)


class ReconstructionError(Exception):
    """The captured stream cannot be trusted, or a frozen invariant broke.

    Raised for a broken capture or a bookkeeping impossibility — never for an
    ordinary lifecycle event. A reconstruction that tolerated either would
    produce an episode ledger whose defects are invisible downstream.
    """


# ---------------------------------------------------------------------------
# Episode records
# ---------------------------------------------------------------------------

@dataclass
class _OpenEpisode:
    """One resident thesis's bookkeeping while the stream is being walked."""

    signal_id: str
    symbol: str
    direction: str
    entry_mode: str
    thesis_key: str
    start_boundary: int
    start_h1_last_ts: float
    tier_at_start: str
    tier_at_end: str
    ever_alert_bearing: bool
    increment_miss_count: int = 0
    partial_refresh_count: int = 0
    first_miss_boundary: Optional[int] = None
    last_miss_boundary: Optional[int] = None
    first_refresh_boundary: Optional[int] = None
    last_refresh_boundary: Optional[int] = None


@dataclass(frozen=True)
class Episode:
    """One reconstructed episode. Raw facts only — see the module docstring."""

    signal_id: str
    symbol: str
    direction: str
    entry_mode: str
    thesis_key: str
    start_boundary: int
    start_h1_last_ts: float
    end_boundary: int
    end_reason: str
    increment_miss_count: int
    partial_refresh_count: int
    first_miss_boundary: Optional[int]
    last_miss_boundary: Optional[int]
    first_refresh_boundary: Optional[int]
    last_refresh_boundary: Optional[int]
    tier_at_start: str
    tier_at_end: str
    ever_alert_bearing: bool
    outcome_joined: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


_EPISODE_FIELDS = tuple(f.name for f in dataclasses.fields(Episode))


def _finalize(open_episode: _OpenEpisode, end_boundary: int,
              end_reason: str) -> Episode:
    return Episode(
        signal_id=open_episode.signal_id,
        symbol=open_episode.symbol,
        direction=open_episode.direction,
        entry_mode=open_episode.entry_mode,
        thesis_key=open_episode.thesis_key,
        start_boundary=open_episode.start_boundary,
        start_h1_last_ts=open_episode.start_h1_last_ts,
        end_boundary=int(end_boundary),
        end_reason=end_reason,
        increment_miss_count=open_episode.increment_miss_count,
        partial_refresh_count=open_episode.partial_refresh_count,
        first_miss_boundary=open_episode.first_miss_boundary,
        last_miss_boundary=open_episode.last_miss_boundary,
        first_refresh_boundary=open_episode.first_refresh_boundary,
        last_refresh_boundary=open_episode.last_refresh_boundary,
        tier_at_start=open_episode.tier_at_start,
        tier_at_end=open_episode.tier_at_end,
        ever_alert_bearing=open_episode.ever_alert_bearing,
    )


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

@dataclass
class _State:
    resident: dict[str, _OpenEpisode] = field(default_factory=dict)
    seen_signal_ids: set = field(default_factory=set)
    episodes: list = field(default_factory=list)
    insertions: int = 0
    capped: int = 0
    replacements: int = 0
    supersedes: int = 0
    evictions: dict = field(
        default_factory=lambda: {"kill": 0, "decay": 0, "ttl": 0})
    total_events: int = 0
    last_boundary: Optional[int] = None
    run_id: Optional[str] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class ReconstructionResult:
    """What one walk of the captured stream produced."""

    run_id: str
    source: str
    episodes: tuple
    total_events: int
    insertions: int
    capped: int
    replacements: int
    supersedes: int
    evictions: dict
    active_at_end: int


def _open_insertion(event: CapturedEvent, state: _State,
                     alert_bearing: bool) -> None:
    """`InsertMutation` / `ReplaceMutation`'s common half: open a new episode
    at `event.symbol`, closing any resident episode there first (SUPERSEDED).

    A same-symbol overwrite always mints a fresh `signal_id`
    ([reconciler.py:480-499](v2/lifecycle/reconciler.py#L480)), so a differing
    `signal_id` is the sole and sufficient signal that the slot was
    superseded — covering both a different thesis and a same-thesis
    higher-tier overwrite, which the capture cannot and need not distinguish.
    """
    symbol = event.symbol or ""
    payload = event.mutation_payload or {}
    record = payload.get("record", payload)
    signal_id = record.get("signal_id")
    if not signal_id:
        raise ReconstructionError(
            f"insertion for {symbol!r} at cycle {event.cycle_index} seq "
            f"{event.seq} carries no signal_id; the capture is incomplete")
    direction = str(record.get("direction") or "")
    entry_mode = str(record.get("entry_mode") or "")
    tier = str(record.get("decision") or "")

    previous = state.resident.get(symbol)
    if previous is not None:
        if previous.signal_id == signal_id:
            raise ReconstructionError(
                f"insertion for {symbol!r} at cycle {event.cycle_index} seq "
                f"{event.seq} repeats the resident episode's own signal_id "
                f"{signal_id!r}; at most one open episode per symbol, and an "
                f"overwrite always mints a fresh signal_id")
        state.supersedes += 1
        state.episodes.append(
            _finalize(state.resident.pop(symbol), event.scan_T, "SUPERSEDED"))

    if signal_id in state.seen_signal_ids:
        raise ReconstructionError(
            f"duplicate signal_id {signal_id!r} at cycle {event.cycle_index} "
            f"seq {event.seq}: signal_id must be a globally unique episode "
            f"key")
    state.seen_signal_ids.add(signal_id)
    state.insertions += 1

    state.resident[symbol] = _OpenEpisode(
        signal_id=signal_id,
        symbol=symbol,
        direction=direction,
        entry_mode=entry_mode,
        thesis_key=f"{symbol}|{direction}|{entry_mode}",
        start_boundary=int(event.scan_T),
        start_h1_last_ts=float(event.h1_last_ts),
        tier_at_start=tier,
        tier_at_end=tier,
        ever_alert_bearing=alert_bearing,
    )


def _close_if_open(state: _State, symbol: str, *, end_boundary: int,
                    end_reason: str, event: CapturedEvent) -> None:
    open_episode = state.resident.pop(symbol, None)
    if open_episode is None:
        raise ReconstructionError(
            f"{end_reason} for {symbol!r} at cycle {event.cycle_index} seq "
            f"{event.seq} found no open episode: at most one open episode "
            f"per symbol, and a terminal event must find the one it ends")
    state.episodes.append(_finalize(open_episode, end_boundary, end_reason))


def _apply_evict(event: CapturedEvent, state: _State) -> None:
    symbol = event.symbol or ""
    reason = event.evict_reason or ""
    end_reason = _EVICT_REASON_TO_END.get(reason)
    if end_reason is None:
        raise ReconstructionError(
            f"unknown evict_reason {reason!r} for {symbol!r} at cycle "
            f"{event.cycle_index} seq {event.seq}")
    state.evictions[reason] += 1
    _close_if_open(state, symbol, end_boundary=int(event.scan_T),
                   end_reason=end_reason, event=event)


def _apply_partial_refresh(event: CapturedEvent, state: _State) -> None:
    symbol = event.symbol or ""
    open_episode = state.resident.get(symbol)
    if open_episode is None:
        raise ReconstructionError(
            f"PartialRefresh for {symbol!r} at cycle {event.cycle_index} seq "
            f"{event.seq} found no open episode")
    open_episode.partial_refresh_count += 1
    boundary = int(event.scan_T)
    if open_episode.first_refresh_boundary is None:
        open_episode.first_refresh_boundary = boundary
    open_episode.last_refresh_boundary = boundary
    # signal_id, miss_count and last_miss_h1_ts are deliberately absent from
    # this mutation's payload (store.py:101-115, Decision #010) and so stay
    # untouched here by simply never being written.
    tier = (event.mutation_payload or {}).get("decision")
    if tier is not None:
        open_episode.tier_at_end = str(tier)


def _apply_increment_miss(event: CapturedEvent, state: _State) -> None:
    symbol = event.symbol or ""
    open_episode = state.resident.get(symbol)
    if open_episode is None:
        raise ReconstructionError(
            f"IncrementMiss for {symbol!r} at cycle {event.cycle_index} seq "
            f"{event.seq} found no open episode")
    open_episode.increment_miss_count += 1
    boundary = int(event.scan_T)
    if open_episode.first_miss_boundary is None:
        open_episode.first_miss_boundary = boundary
    open_episode.last_miss_boundary = boundary


def _apply_decision(events: tuple, state: _State) -> None:
    """One `(cycle_index, phase, symbol)` decision group — at most one
    reconcile() call's worth of mutations (C-5.6.2)."""
    alert_bearing = any(event.is_alert_bearing for event in events)
    for event in events:
        if event.kind != KIND_MUTATION:
            continue
        mutation_type = event.mutation_type
        if mutation_type not in MUTATION_TYPES:
            raise ReconstructionError(
                f"unknown mutation_type {mutation_type!r} at cycle "
                f"{event.cycle_index} seq {event.seq}")
        if mutation_type == "ReplaceMutation":
            state.replacements += 1
            _close_if_open(
                state, event.target_symbol or "", end_boundary=int(event.scan_T),
                end_reason="REPLACED", event=event)
            _open_insertion(event, state, alert_bearing)
        elif mutation_type == "InsertMutation":
            _open_insertion(event, state, alert_bearing)
        elif mutation_type == "PartialRefreshMutation":
            _apply_partial_refresh(event, state)
        elif mutation_type == "IncrementMissMutation":
            _apply_increment_miss(event, state)
        elif mutation_type == "EvictMutation":
            _apply_evict(event, state)


def reconstruct(events: Iterable[CapturedEvent]) -> ReconstructionResult:
    """Walk a captured event stream and reconstruct every episode.

    Takes an event sequence rather than a file — mirrors
    `TradeIntentBuilder.build_from` — so a test can script a scenario and the
    file path stays a separate, thin concern (`analyse`).
    """
    state = _State()
    prev_order_key: Optional[tuple] = None
    buffer: list = []
    buffer_key: Optional[tuple] = None

    for event in events:
        state.total_events += 1
        state.last_boundary = int(event.scan_T)

        # `(cycle_index, seq)` strictly increasing catches both an
        # out-of-order event and an exact duplicate in one comparison: a
        # duplicate has an equal key, which is not `>` the previous one
        # either. `seq` resets to 0 each cycle, but tuple comparison already
        # resolves that correctly because `cycle_index` differs first.
        order_key = (event.cycle_index, event.seq)
        if prev_order_key is not None and order_key <= prev_order_key:
            raise ReconstructionError(
                f"event {order_key} is not strictly after {prev_order_key}: "
                f"the capture is out of (cycle_index, seq) order or "
                f"duplicated")
        prev_order_key = order_key

        if state.run_id is None:
            state.run_id, state.source = event.run_id, event.source
        elif event.run_id != state.run_id or event.source != state.source:
            raise ReconstructionError(
                f"mixed run_id/source in one stream: "
                f"({state.run_id!r}, {state.source!r}) vs "
                f"({event.run_id!r}, {event.source!r}). One file is one run.")

        if event.kind == KIND_LOG and event.log_event_type == "CAPPED":
            state.capped += 1

        key = (event.cycle_index, event.phase, event.symbol)
        if buffer_key is not None and key != buffer_key:
            _apply_decision(tuple(buffer), state)
            buffer = []
        buffer_key = key
        buffer.append(event)

    if buffer:
        _apply_decision(tuple(buffer), state)

    active_at_end = len(state.resident)
    end_boundary = state.last_boundary if state.last_boundary is not None else 0
    for open_episode in state.resident.values():
        state.episodes.append(
            _finalize(open_episode, end_boundary, "STILL_RESIDENT_AT_END"))

    if state.insertions != len(state.episodes):
        raise ReconstructionError(
            f"accounting mismatch: {state.insertions} insertion(s) but "
            f"{len(state.episodes)} episode(s) reconstructed; every episode "
            f"ends exactly once")

    return ReconstructionResult(
        run_id=state.run_id or "",
        source=state.source or "",
        episodes=tuple(state.episodes),
        total_events=state.total_events,
        insertions=state.insertions,
        capped=state.capped,
        replacements=state.replacements,
        supersedes=state.supersedes,
        evictions=dict(state.evictions),
        active_at_end=active_at_end,
    )


# ---------------------------------------------------------------------------
# Outcome join
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _OutcomeJoin:
    signal_ids: frozenset
    strategy_version: Optional[str]
    outcome_config_hash: Optional[str]
    data_snapshot_id: Optional[str]


def join_outcomes(episodes: Iterable[Episode],
                   signal_ids: Iterable[str]) -> tuple:
    """Stamp `outcome_joined` by exact `signal_id` membership. No fallback
    key: an episode whose `signal_id` is absent from `signal_ids` is emitted
    with `outcome_joined=False`, never dropped."""
    ids = frozenset(signal_ids)
    return tuple(dataclasses.replace(episode, outcome_joined=episode.signal_id in ids)
                 for episode in episodes)


def _load_outcome_join(path: Path) -> _OutcomeJoin:
    from backtest.dataset import read_outcomes

    records = read_outcomes(path)
    ids: set = set()
    for record in records:
        if not record.signal_id:
            continue
        if record.signal_id in ids:
            raise ReconstructionError(
                f"{path.name} carries duplicate signal_id "
                f"{record.signal_id!r}; the outcome join requires 1:1 "
                f"signal_id identity")
        ids.add(record.signal_id)
    first = records[0] if records else None
    return _OutcomeJoin(
        signal_ids=frozenset(ids),
        strategy_version=first.strategy_version if first is not None else None,
        outcome_config_hash=first.outcome_config_hash if first is not None else None,
        data_snapshot_id=first.data_snapshot_id if first is not None else None,
    )


# ---------------------------------------------------------------------------
# Specification hash
# ---------------------------------------------------------------------------

def specification_hash() -> str:
    """A digest over every frozen constant. Changing any of them changes the
    hash, which is what makes "the specification did not move" verifiable."""
    payload = {
        "engine_version": ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
        "evict_reason_to_end": dict(_EVICT_REASON_TO_END),
        "tracked_mutation_types": sorted(MUTATION_TYPES),
        "end_reasons": list(END_REASONS),
        "episode_fields": list(_EPISODE_FIELDS),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _iter_captured_events(path: Path) -> Iterator[CapturedEvent]:
    """Streams the file one line at a time. Never materializes the file as a
    list — the 1.16M-line official capture is 658MB on disk."""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield CapturedEvent.from_dict(json.loads(line))


def analyse(run_dir: Path | str) -> dict[str, Any]:
    """Reconstruct every episode of one finished bundle and join outcomes."""
    run_dir = Path(run_dir)
    from backtest.dataset import OUTCOMES_FILENAME

    result = reconstruct(_iter_captured_events(run_dir / LIFECYCLE_EVENTS_FILENAME))
    join = _load_outcome_join(run_dir / OUTCOMES_FILENAME)
    episodes = join_outcomes(result.episodes, join.signal_ids)
    outcomes_joined = sum(1 for episode in episodes if episode.outcome_joined)
    episodes_without_outcomes = len(episodes) - outcomes_joined

    return {
        "run_id": result.run_id,
        "source": result.source,
        "strategy_version": join.strategy_version,
        "outcome_config_hash": join.outcome_config_hash,
        "data_snapshot_id": join.data_snapshot_id,
        "reconstruction_specification_hash": specification_hash(),
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
        "reconciliation": {
            "total_lifecycle_events": result.total_events,
            "insertions": result.insertions,
            "capped": result.capped,
            "evictions": dict(result.evictions),
            "replacements": result.replacements,
            "supersedes": result.supersedes,
            "active_at_end": result.active_at_end,
            "episodes_reconstructed": len(episodes),
            "outcomes_joined": outcomes_joined,
            "episodes_without_outcomes": episodes_without_outcomes,
        },
        "episodes": [episode.to_dict() for episode in episodes],
    }


def render(payload: dict[str, Any]) -> str:
    """A minimal human-readable rendering: identity, reconciliation counts,
    and an end-reason tally. No metric, no ranking, no recommendation."""
    lines: list = []
    lines.append("# Episode Reconstruction")
    lines.append("")
    lines.append(f"- run_id: `{payload.get('run_id', '')}`")
    lines.append(f"- source: `{payload.get('source', '')}`")
    lines.append(f"- strategy_version: `{payload.get('strategy_version')}`")
    lines.append(f"- outcome_config_hash: `{payload.get('outcome_config_hash')}`")
    lines.append(f"- data_snapshot_id: `{payload.get('data_snapshot_id')}`")
    lines.append(
        f"- reconstruction_specification_hash: "
        f"`{payload.get('reconstruction_specification_hash')}`")
    lines.append(f"- evidence_class: `{payload.get('evidence_class')}`")
    lines.append(f"- generated_at: `{payload.get('generated_at', '')}`")
    lines.append("")
    lines.append(f"> {payload.get('scope', '')}")
    lines.append("")
    lines.append("## Reconciliation")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    reconciliation = payload.get("reconciliation", {})
    evictions = reconciliation.get("evictions", {})
    rows = [
        ("total_lifecycle_events", reconciliation.get("total_lifecycle_events")),
        ("insertions", reconciliation.get("insertions")),
        ("capped", reconciliation.get("capped")),
        ("evictions.kill", evictions.get("kill")),
        ("evictions.decay", evictions.get("decay")),
        ("evictions.ttl", evictions.get("ttl")),
        ("replacements", reconciliation.get("replacements")),
        ("supersedes", reconciliation.get("supersedes")),
        ("active_at_end", reconciliation.get("active_at_end")),
        ("episodes_reconstructed", reconciliation.get("episodes_reconstructed")),
        ("outcomes_joined", reconciliation.get("outcomes_joined")),
        ("episodes_without_outcomes",
         reconciliation.get("episodes_without_outcomes")),
    ]
    for name, value in rows:
        lines.append(f"| {name} | {value} |")
    lines.append("")
    lines.append("## End-reason distribution")
    lines.append("")
    counts: dict[str, int] = {}
    for episode in payload.get("episodes", []):
        reason = episode.get("end_reason", "")
        counts[reason] = counts.get(reason, 0) + 1
    lines.append("| end_reason | episodes |")
    lines.append("|---|---|")
    for reason in sorted(counts):
        lines.append(f"| {reason} | {counts[reason]} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_episode_reconstruction(payload: dict[str, Any],
                                  out_dir: Path | str) -> dict[str, Path]:
    """Emit the two artifacts into the caller's directory."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["generated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    written: dict[str, Path] = {}
    json_path = out / "episode_reconstruction.json"
    json_path.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written["episode_reconstruction.json"] = json_path

    md_path = out / "episode_reconstruction.md"
    md_path.write_text(render(body), encoding="utf-8")
    written["episode_reconstruction.md"] = md_path
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Explicit entry point. Deliberately not a CLI verb."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="episode_reconstruction",
        description="Raw episode ledger over a finished replay bundle's "
                    "lifecycle capture.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    payload = analyse(run_dir)
    out_dir = (Path(args.out) if args.out
               else run_dir / "analytics" / "research" / DIRNAME)
    written = write_episode_reconstruction(payload, out_dir)
    for name, path in sorted(written.items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(main())
