"""The trade builder — captured lifecycle history to `TradeIntent`s (C-9.2).

REPLAY_CONTRACT.md C-2.1 – C-2.3, C-4.6 – C-4.9, C-9.2, C-9.4, §15.

## The one source

C-2.1 (amended in v1.1) is unambiguous: trade instantiation derives from the
captured `LifecycleDecision` stream, **not** from the notifier stream. So this
module reads `lifecycle_events.jsonl` and nothing else decides that a trade
exists. In particular:

- `alerts.jsonl` is never read. It is a cross-check channel (C-15.7); an
  exception in `_render_symbol` skips dispatch *after* the decision was applied,
  and `Alert` carries no symbol-time identity of its own.
- the signal ledger never instantiates a trade. It is read for exactly one
  purpose the contract assigns it: C-2.2 states that levels for the two
  suppressed shadow classes "come from the canonical ledger row of that scan
  (C-9.4), which always exists", because a suppressed decision produces no
  mutation and therefore carries no levels. Nothing else is taken from it, and a
  missing row suppresses the shadow rather than inventing one.
- nothing is inferred. A trade this layer cannot attribute is reported, never
  guessed at.

## What the builder is, mechanically

A **replay of the captured stream**, in `(run_id, cycle_index, seq)` order,
maintaining exactly two pieces of state:

    resident   symbol      -> the thesis currently occupying that memory slot
    live       thesis_key  -> the trade that thesis has not yet ended

`resident` mirrors `SignalStore._active` at thesis granularity, which is what
lets an eviction — whose payload names only the symbol and the reason — be
attributed to the *trade* it ends. `live` enforces C-2.3: at most one
non-terminal trade per thesis.

The builder decides only what a lifecycle event means. Whether a cancellation
actually applies is the walker's decision, because that depends on price: a fill
during `Wk` beats a cancellation produced by the scan at `Wk`'s close (C-4.13),
and the builder cannot know about fills. So it attaches the intervention and
lets §4 arbitrate.

## Why the HTF-flip kill needs no classification

C-4.9 splits kills in two: a **price-breach** kill is "subsumed by the SL exit"
because the kill threshold sits beyond the SL ([rules.py:147](v2/lifecycle/rules.py#L147)),
while an **HTF-flip** kill closes the trade at the close of `C_ref` →
`INVALIDATED_EXIT`. The captured `LogEvent("KILL")` carries no details
([reconciler.py:237](v2/lifecycle/reconciler.py#L237)), so the two limbs are
indistinguishable in the capture — and they do not need to be. Every kill is
passed as `htf_kill_boundary`; if it was a price breach, price had already
travelled beyond the SL before `C_ref` closed, so the walk exits at SL at or
before that candle and never reaches the kill. The classification resolves
itself, on price, in the one place that has price.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from backtest.capture_schema import (
    KIND_LOG,
    KIND_MUTATION,
    LIFECYCLE_EVENTS_FILENAME,
    CapturedEvent,
    read_events,
)
from backtest.outcome.errors import OutcomeError
from backtest.outcome.trade import Cancellation, TradeIntent, thesis_key

__all__ = [
    "BuildReport",
    "LEDGER_FILENAME",
    "MANIFEST_FILENAME",
    "TradeIntentBuilder",
    "TradeSourceError",
    "build_trade_intents",
]

LEDGER_FILENAME = "signal_ledger.jsonl"
MANIFEST_FILENAME = "manifest.json"

H1_SECONDS = 3600

#: C-2.2: a `KILL_COOLDOWN` shadow exists only when the canonical ledger row's
#: tier is one of these — a suppressed WATCH would not have been alerted anyway,
#: so the cooldown cost nothing.
ALERT_TIERS = ("VALID", "A+")

#: The lifecycle events that end a pending trade, and the `cancel_reason` each
#: maps to (C-4.8). `SUPERSEDED` is not here: it is not an event but the *arrival*
#: of a new insertion on an occupied slot (C-4.7).
EVICT_REASON_TO_CANCEL = {
    "kill": "KILL",
    "decay": "DECAY",
    "ttl": "TTL",
}


class TradeSourceError(OutcomeError):
    """The captured stream cannot be trusted to describe the run.

    Raised for a broken capture — events out of order, a duplicated `seq`, two
    run ids in one file — never for an unusual market. A builder that quietly
    tolerated a corrupt stream would produce a dataset whose defects are
    invisible downstream.
    """


@dataclass
class _Slot:
    """One thesis's in-flight trade, while the stream is being walked."""

    intent: TradeIntent
    cancelled: bool = False


@dataclass(frozen=True)
class BuildReport:
    """What one build produced. Counts and attribution — no metrics.

    `unattributed_cancellations` is the number of lifecycle cancellations that
    found no live trade to end. It is expected to be large and is not a defect:
    most evictions remove records that never carried an alert-bearing insertion,
    so no trade ever existed. It is surfaced because a *zero* would be the
    surprising number, and because a sudden change in it is the cheapest possible
    signal that the stream's shape has shifted.
    """

    intents: tuple[TradeIntent, ...]
    run_id: str
    source: str
    events: int
    decisions: int
    alert_bearing: int
    shadows: dict[str, int]
    unattributed_cancellations: int
    superseded: int
    ledger_rows_used: int
    missing_ledger_rows: int

    def __len__(self) -> int:
        return len(self.intents)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "trades": len(self.intents),
            "events": self.events,
            "decisions": self.decisions,
            "alert_bearing": self.alert_bearing,
            "shadows": dict(self.shadows),
            "unattributed_cancellations": self.unattributed_cancellations,
            "superseded": self.superseded,
            "ledger_rows_used": self.ledger_rows_used,
            "missing_ledger_rows": self.missing_ledger_rows,
        }


class TradeIntentBuilder:
    """Turns one run's captured lifecycle history into `TradeIntent`s.

    Stateless between builds: `build` creates its own walking state, so the same
    builder over the same run directory always produces the same result.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        burn_in_until: Optional[int] = None,
        include_shadows: bool = True,
    ) -> None:
        """`burn_in_until` is the first boundary at which a trade is no longer in
        burn-in (C-5.7.1). When omitted it is read from the run manifest, which is
        where the driver recorded it; a run with neither is refused rather than
        silently labelling every trade out of burn-in.

        `include_shadows` drops the three research populations of C-2.2. They are
        built by default because they are never mixed into headline metrics
        anyway — `shadow_kind` segregates them — and because the questions they
        answer (what does the slot cap cost, what does the cooldown cost) are the
        reason the classes exist.
        """
        self._run_dir = Path(run_dir)
        self._include_shadows = include_shadows
        self._burn_in_until = (
            int(burn_in_until) if burn_in_until is not None
            else self._burn_in_from_manifest())

    # -- inputs -------------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def burn_in_until(self) -> int:
        return self._burn_in_until

    def _burn_in_from_manifest(self) -> int:
        path = self._run_dir / MANIFEST_FILENAME
        if not path.exists():
            raise TradeSourceError(
                f"no {MANIFEST_FILENAME} in {self._run_dir} and no burn_in_until "
                f"given. Burn-in decides which trades enter headline metrics "
                f"(C-5.7.1), so it is required rather than defaulted: assuming "
                f"zero would silently promote every early trade.")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if "burn_in_until" not in manifest:
            raise TradeSourceError(
                f"{path} carries no `burn_in_until`")
        return int(manifest["burn_in_until"])

    # -- the build ----------------------------------------------------------

    def build(self) -> BuildReport:
        events = read_events(self._run_dir / LIFECYCLE_EVENTS_FILENAME)
        return self.build_from(events)

    def build_from(self, events: Iterable[CapturedEvent]) -> BuildReport:
        """The whole algorithm, over an event sequence rather than a file — so a
        test can script a scenario and the file path stays one concern."""
        events = tuple(events)
        run_id, source = _validate_stream(events)
        ledger = _LedgerIndex(self._run_dir / LEDGER_FILENAME)

        resident: dict[str, tuple[str, str]] = {}
        live: dict[str, _Slot] = {}
        finished: list[TradeIntent] = []

        decisions = 0
        alert_bearing = 0
        shadows: dict[str, int] = {}
        unattributed = 0
        superseded = 0

        for cycle in _group_by_cycle(events):
            for decision in _group_by_decision(cycle):
                if decision.phase_is_symbol:
                    decisions += 1

                # C-4.13 ordering is a *price* concern and belongs to the walker.
                # Within one decision the order is fixed by the lifecycle itself:
                # what leaves memory leaves before what arrives, because the
                # arrival may claim the slot that was just freed.
                for event in decision.evictions:
                    reason = EVICT_REASON_TO_CANCEL.get(event.evict_reason or "")
                    if reason is None:
                        continue
                    symbol = event.symbol or ""
                    thesis = resident.pop(symbol, None)
                    if not _cancel(live, symbol, thesis, event.scan_T, reason):
                        unattributed += 1
                    if reason == "KILL":
                        _mark_kill(live, symbol, thesis, event.scan_T)

                for event in decision.replacements:
                    target = event.target_symbol or ""
                    thesis = resident.pop(target, None)
                    if not _cancel(live, target, thesis, event.scan_T, "REPLACED"):
                        unattributed += 1

                for event in decision.insertions:
                    symbol = event.symbol or ""
                    previous = resident.get(symbol)
                    if previous is not None and _cancel(
                        live, symbol, previous, event.scan_T, "SUPERSEDED"
                    ):
                        superseded += 1

                    record = (event.mutation_payload or {})
                    payload = record.get("record", record)
                    direction = str(payload.get("direction") or "")
                    entry_mode = str(payload.get("entry_mode") or "")
                    if not direction or not entry_mode:
                        raise TradeSourceError(
                            f"insertion at cycle {event.cycle_index} seq "
                            f"{event.seq} for {symbol} carries no "
                            f"direction/entry_mode; the capture is incomplete")
                    resident[symbol] = (direction, entry_mode)

                    alert = decision.alert
                    if alert is None and not self._include_shadows:
                        continue
                    intent = self._from_insertion(
                        event, payload, direction, entry_mode, alert)
                    if intent is None:
                        continue
                    if alert is None:
                        shadows["UNALERTED_INSERT"] = (
                            shadows.get("UNALERTED_INSERT", 0) + 1)
                    else:
                        alert_bearing += 1
                    _place(live, finished, intent)

                if not self._include_shadows or decision.insertions:
                    continue
                shadow = self._suppressed_shadow(decision, ledger)
                if shadow is not None:
                    shadows[shadow.shadow_kind or "?"] = (
                        shadows.get(shadow.shadow_kind or "?", 0) + 1)
                    # A fixed-window shadow has no memory record, so no lifecycle
                    # event can ever cancel it (C-2.2) — it never enters `live`.
                    finished.append(shadow)

        for slot in live.values():
            finished.append(slot.intent)

        return BuildReport(
            intents=tuple(_ordered(finished)),
            run_id=run_id,
            source=source,
            events=len(events),
            decisions=decisions,
            alert_bearing=alert_bearing,
            shadows=shadows,
            unattributed_cancellations=unattributed,
            superseded=superseded,
            ledger_rows_used=ledger.used,
            missing_ledger_rows=ledger.missing,
        )

    # -- instantiation ------------------------------------------------------

    def _from_insertion(
        self,
        event: CapturedEvent,
        payload: dict,
        direction: str,
        entry_mode: str,
        alert: Optional[CapturedEvent],
    ) -> Optional[TradeIntent]:
        """C-2.1: an insertion carrying a `new`/`upgrade` alert instantiates a
        trade; an unalerted one instantiates an `UNALERTED_INSERT` shadow (C-2.2).

        Levels come from the inserted `ActiveSignal`, which **is** the alert's
        frozen copy — the reconciler builds one record and both the memory entry
        and the alert carry its values ([reconciler.py:480-555](v2/lifecycle/reconciler.py#L480)).
        """
        if entry_mode not in ("RETRACEMENT", "CONTINUATION"):
            # WATCH_ONLY / NONE have no fill rule (C-4.1/C-4.2), so no trade can
            # be modelled. Not an error: analysis legitimately emits them.
            return None
        tier = (alert.alert_tier if alert is not None
                else str(payload.get("decision") or ""))
        # `ActiveSignal` carries no `tp_source` — the alert does, and for an
        # alerted trade the alert is the frozen copy C-1.3 refers to. An unalerted
        # insert therefore has none, and the field stays honest rather than being
        # back-filled from a row that was not what the operator saw.
        alert_fields = (alert.alert_payload or {}) if alert is not None else {}
        return TradeIntent(
            run_id=event.run_id,
            source=event.source,
            symbol=event.symbol or "",
            direction=direction,
            entry_mode=entry_mode,
            boundary=int(event.scan_T),
            h1_last_ts=int(event.h1_last_ts),
            cycle_index=int(event.cycle_index),
            universe_rank=event.universe_rank,
            alert_tier=tier or None,
            shadow_kind=None if alert is not None else "UNALERTED_INSERT",
            in_burn_in=int(event.scan_T) < self._burn_in_until,
            signal_id=payload.get("signal_id"),
            entry_price=float(payload.get("entry_price") or 0.0),
            sl=float(payload.get("sl") or 0.0),
            tp=float(payload.get("tp") or 0.0),
            score=_maybe_int(payload.get("score")),
            tp_source=alert_fields.get("tp_source"),
            entry_source=payload.get("entry_source"),
        )

    def _suppressed_shadow(
        self, decision: "_Decision", ledger: "_LedgerIndex"
    ) -> Optional[TradeIntent]:
        """The two fixed-window classes of C-2.2: a decision carrying `CAPPED` or
        `KILL_COOLDOWN` and **no** mutation. The alert was computed; a slot cap or
        a cooldown suppressed the insertion, so no record exists and the levels
        must come from the canonical ledger row of that scan.
        """
        kind = decision.suppression
        if kind is None:
            return None
        event = decision.log_of(kind)
        if event is None:
            return None
        row = ledger.row(event.symbol or "", event.h1_last_ts)
        if row is None:
            return None

        details = event.log_details or {}
        direction = str(details.get("direction") or row.get("direction") or "")
        entry_mode = str(details.get("entry_mode") or row.get("entry_mode") or "")
        if entry_mode not in ("RETRACEMENT", "CONTINUATION"):
            return None
        if direction not in ("LONG", "SHORT"):
            return None
        tier = str(row.get("decision") or "")
        if kind == "KILL_COOLDOWN" and tier not in ALERT_TIERS:
            # C-2.2 gates this class on the canonical row's tier: a suppressed
            # WATCH would never have been alerted, so the cooldown cost nothing.
            return None
        levels = (row.get("entry_price"), row.get("sl"), row.get("tp"))
        if any(value is None for value in levels):
            return None

        return TradeIntent(
            run_id=event.run_id,
            source=event.source,
            symbol=event.symbol or "",
            direction=direction,
            entry_mode=entry_mode,
            boundary=int(event.scan_T),
            h1_last_ts=int(event.h1_last_ts),
            cycle_index=int(event.cycle_index),
            universe_rank=event.universe_rank,
            alert_tier=tier or None,
            shadow_kind=kind,
            in_burn_in=int(event.scan_T) < self._burn_in_until,
            signal_id=row.get("signal_id"),
            entry_price=float(row["entry_price"]),
            sl=float(row["sl"]),
            tp=float(row["tp"]),
            score=_maybe_int(row.get("score")),
            tp_source=row.get("tp_source"),
            entry_source=row.get("entry_source"),
        )


def build_trade_intents(
    run_dir: Path | str,
    *,
    burn_in_until: Optional[int] = None,
    include_shadows: bool = True,
) -> BuildReport:
    """One call from a completed replay run to the Outcome Engine's input."""
    return TradeIntentBuilder(
        run_dir, burn_in_until=burn_in_until, include_shadows=include_shadows
    ).build()


# ---------------------------------------------------------------------------
# Stream grouping
# ---------------------------------------------------------------------------

@dataclass
class _Decision:
    """The lines one `LifecycleDecision` expanded into (C-15.3)."""

    events: list[CapturedEvent] = field(default_factory=list)

    @property
    def phase_is_symbol(self) -> bool:
        return bool(self.events) and self.events[0].phase == "symbol"

    @property
    def insertions(self) -> list[CapturedEvent]:
        return [event for event in self.events
                if event.kind == KIND_MUTATION and event.is_insertion]

    @property
    def replacements(self) -> list[CapturedEvent]:
        return [event for event in self.events
                if event.mutation_type == "ReplaceMutation"]

    @property
    def evictions(self) -> list[CapturedEvent]:
        return [event for event in self.events
                if event.mutation_type == "EvictMutation"]

    @property
    def alert(self) -> Optional[CapturedEvent]:
        """The `new`/`upgrade` alert this decision carries, if any. A `kill` alert
        is not trade-instantiating (C-2.1)."""
        for event in self.events:
            if event.is_alert_bearing:
                return event
        return None

    @property
    def suppression(self) -> Optional[str]:
        for event in self.events:
            if (event.kind == KIND_LOG
                    and event.log_event_type in ("CAPPED", "KILL_COOLDOWN")):
                return event.log_event_type
        return None

    def log_of(self, event_type: str) -> Optional[CapturedEvent]:
        for event in self.events:
            if event.kind == KIND_LOG and event.log_event_type == event_type:
                return event
        return None


def _group_by_cycle(
    events: tuple[CapturedEvent, ...]
) -> Iterable[tuple[CapturedEvent, ...]]:
    current: list[CapturedEvent] = []
    index: Optional[int] = None
    for event in events:
        if index is not None and event.cycle_index != index:
            yield tuple(current)
            current = []
        index = event.cycle_index
        current.append(event)
    if current:
        yield tuple(current)


def _group_by_decision(
    cycle: tuple[CapturedEvent, ...]
) -> Iterable[_Decision]:
    """Consecutive events sharing `(phase, symbol)` are one decision.

    Sound because C-5.6.2 forbids re-ordering or de-duplicating the universe, so
    a symbol is reconciled exactly once per cycle and its lines are contiguous.
    End-of-cycle lines follow every per-symbol line (C-5.6.4) and are grouped
    per symbol the same way — each inferred TTL eviction is its own decision.
    """
    current = _Decision()
    key: Optional[tuple[str, Optional[str]]] = None
    for event in cycle:
        this = (event.phase, event.symbol)
        if key is not None and this != key:
            yield current
            current = _Decision()
        key = this
        current.events.append(event)
    if current.events:
        yield current


def _validate_stream(events: tuple[CapturedEvent, ...]) -> tuple[str, str]:
    """Order, uniqueness and single-run identity — the three ways a captured file
    can lie about the run that produced it."""
    if not events:
        return ("", "")
    run_ids = {event.run_id for event in events}
    if len(run_ids) > 1:
        raise TradeSourceError(
            f"the capture holds {len(run_ids)} run ids ({sorted(run_ids)}). One "
            f"file is one run: `run_id` is the mandatory first grouping key of "
            f"every analysis (C-16.6), and two runs merged into one file cannot "
            f"be separated after the fact.")
    sources = {event.source for event in events}
    if len(sources) > 1:
        raise TradeSourceError(
            f"the capture mixes sources {sorted(sources)}; live and replay rows "
            f"are never comparable without declaring it (C-16.6)")

    seen: set[tuple[int, int]] = set()
    previous: Optional[tuple[int, int]] = None
    for event in events:
        key = (event.cycle_index, event.seq)
        if key in seen:
            raise TradeSourceError(
                f"duplicate event (cycle_index={event.cycle_index}, "
                f"seq={event.seq}). `(run_id, cycle_index, seq)` is the ordering "
                f"key (C-15.3), so a repeat means the file was appended to twice "
                f"and every count derived from it would be wrong.")
        seen.add(key)
        if previous is not None and key < previous:
            raise TradeSourceError(
                f"event {key} follows {previous}: the capture is out of "
                f"`(cycle_index, seq)` order. Sorting it here would hide the "
                f"corruption rather than surface it.")
        previous = key
    return (events[0].run_id, events[0].source)


# ---------------------------------------------------------------------------
# Trade bookkeeping
# ---------------------------------------------------------------------------

def _place(live: dict[str, _Slot], finished: list[TradeIntent],
           intent: TradeIntent) -> None:
    """C-2.3: at most one non-terminal trade per thesis. An existing live trade
    for this thesis has already been superseded by the caller, so it is retired
    here rather than overwritten."""
    key = intent.thesis_key
    existing = live.pop(key, None)
    if existing is not None:
        finished.append(existing.intent)
    live[key] = _Slot(intent=intent)


def _cancel(live: dict[str, _Slot], symbol: str,
            thesis: Optional[tuple[str, str]], boundary: int,
            reason: str) -> bool:
    """Attach a `Cancellation` to the live trade of `thesis`, if there is one.

    Returns whether a trade was found. Most cancellations find none, and that is
    the normal case: an eviction removes a memory record, and most records never
    carried an alert-bearing insertion, so no trade was ever instantiated.
    """
    if thesis is None:
        return False
    key = thesis_key(symbol, thesis[0], thesis[1])
    slot = live.get(key)
    if slot is None or slot.cancelled:
        return False
    slot.intent = _replace_intent(
        slot.intent, cancellation=Cancellation(int(boundary), reason))
    slot.cancelled = True
    return True


def _mark_kill(live: dict[str, _Slot], symbol: str,
               thesis: Optional[tuple[str, str]], boundary: int) -> None:
    """Record the kill boundary as well as the cancellation.

    Both are set on the same trade because the capture cannot say which limb of
    `is_poi_invalidated` fired, and does not need to: if the trade was still
    pending the cancellation applies (C-4.8), and if it had filled, a price-breach
    kill was already resolved as an SL exit earlier in the walk, leaving the HTF
    flip as the only kill that can reach `INVALIDATED_EXIT` (C-4.9).
    """
    if thesis is None:
        return
    slot = live.get(thesis_key(symbol, thesis[0], thesis[1]))
    if slot is None:
        return
    slot.intent = _replace_intent(slot.intent, htf_kill_boundary=int(boundary))


def _replace_intent(intent: TradeIntent, **changes) -> TradeIntent:
    return dataclasses.replace(intent, **changes)


def _ordered(intents: Iterable[TradeIntent]) -> list[TradeIntent]:
    """The instantiation order the Outcome Engine also sorts by, so a build and a
    resolution of that build agree line for line."""
    return sorted(intents, key=lambda intent: (
        intent.boundary,
        -1 if intent.cycle_index is None else intent.cycle_index,
        -1 if intent.universe_rank is None else intent.universe_rank,
        intent.symbol,
        intent.thesis_key,
    ))


# ---------------------------------------------------------------------------
# The canonical ledger row (C-9.4) — levels for suppressed shadows only
# ---------------------------------------------------------------------------

class _LedgerIndex:
    """`(symbol, h1_last_ts)` to the canonical ledger row of that scan.

    At replay cadence the key is unique by construction: one row per symbol per
    cycle, one cycle per `h1_last_ts` (C-9.4). Loaded lazily, because a build
    that instantiates no suppressed shadow never needs the ledger at all.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows: Optional[dict[tuple[str, int], dict]] = None
        self.used = 0
        self.missing = 0

    def row(self, symbol: str, h1_last_ts: float) -> Optional[dict]:
        if self._rows is None:
            self._rows = self._load()
        found = self._rows.get((symbol, int(h1_last_ts)))
        if found is None:
            self.missing += 1
        else:
            self.used += 1
        return found

    def _load(self) -> dict[tuple[str, int], dict]:
        rows: dict[tuple[str, int], dict] = {}
        if not self._path.exists():
            return rows
        with open(self._path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                key = (str(row.get("symbol")), int(float(row.get("h1_last_ts", 0))))
                rows[key] = row
        return rows


def _maybe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
