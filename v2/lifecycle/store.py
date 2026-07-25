"""lifecycle.store — the single owner of mutable signal memory.

Architecture ref: 02-software-architecture.md §3 (`lifecycle.store`: "hold the
in-memory ActiveSignal collection; apply mutations from LifecycleDecision
atomically; enforce slot limits. The single owner of ``active_signals``."),
§6 (single writer per state; also the logical owner of ``killed_thesis``), §10
Principle 2 (single writer).

The store holds two in-memory dicts — the active signals and the killed-thesis
cooldown store — and is the ONLY module that mutates them. Every mutation flows
through :meth:`apply` (a LifecycleDecision from the reconciler) or the two
end-of-cycle maintenance operations (TTL sweep, killed prune). It performs NO
I/O: loading and saving the dicts is the orchestrator's job via
``infra.persistence`` (Phase 5). This module never touches the filesystem, the
network, or the clock beyond the injected ``Clock`` passed to maintenance.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping, Optional

from v2.domain import (
    ActiveSignal,
    EvictMutation,
    IncrementMissMutation,
    InsertMutation,
    KilledThesisRecord,
    LifecycleDecision,
    LogEvent,
    Mutation,
    PartialRefreshMutation,
    ReplaceMutation,
)
from v2.infra.clock import Clock
from v2.lifecycle.config import LifecycleConfig
from v2.lifecycle.memory import MemoryView
from v2.lifecycle.rules import ttl_for

__all__ = ["SignalStore"]


class SignalStore:
    """Holds and mutates the active-signal + killed-thesis memory.

    Seeded from plain dicts (which the orchestrator loads through
    ``infra.persistence``); it copies them so the caller's dicts are never
    aliased. All state changes go through :meth:`apply`.
    """

    def __init__(
        self,
        *,
        active: Optional[Mapping[str, ActiveSignal]] = None,
        killed_theses: Optional[Mapping[str, KilledThesisRecord]] = None,
        config: LifecycleConfig = LifecycleConfig(),
    ) -> None:
        self._active: dict[str, ActiveSignal] = dict(active or {})
        self._killed: dict[str, KilledThesisRecord] = dict(killed_theses or {})
        self._config = config

    # -- read -------------------------------------------------------------

    def view(self) -> MemoryView:
        """A read-only snapshot for the reconciler (defensive copies)."""
        return MemoryView(self._active, self._killed)

    @property
    def active(self) -> Mapping[str, ActiveSignal]:
        """A copy of the active-signal memory (never the live dict)."""
        return dict(self._active)

    @property
    def killed_theses(self) -> Mapping[str, KilledThesisRecord]:
        """A copy of the killed-thesis store (never the live dict)."""
        return dict(self._killed)

    # -- write (the single writer) ---------------------------------------

    def apply(self, decision: LifecycleDecision) -> None:
        """Apply a LifecycleDecision's mutations and killed-thesis records.

        Mutations are applied in order (V1 performed the same steps inline in
        that order); killed-thesis records are stored last, matching V1's KILL
        branch which writes ``killed_thesis[key]`` alongside the eviction.
        Alerts and log events are *not* consumed here — the orchestrator routes
        those to delivery (Arch §9: decisions separated from execution).
        """
        for mutation in decision.mutations:
            self._apply_mutation(mutation)
        for record in decision.killed_theses:
            self._killed[record.thesis_key] = record

    def _apply_mutation(self, mutation: Mutation) -> None:
        if isinstance(mutation, InsertMutation):
            # V1: replace-in-place (symbol present) OR slot-available insert.
            self._active[mutation.symbol] = mutation.record
        elif isinstance(mutation, ReplaceMutation):
            # V1 [🔄 REPLACE]: drop the weakest, insert the stronger.
            self._active.pop(mutation.target_symbol, None)
            self._active[mutation.symbol] = mutation.record
        elif isinstance(mutation, PartialRefreshMutation):
            # V1 [🟰 SAME THESIS]: in-place field update; miss_count and
            # last_miss_h1_ts are deliberately left unchanged (Decision #010).
            current = self._active.get(mutation.symbol)
            if current is not None:
                self._active[mutation.symbol] = dataclasses.replace(
                    current,
                    score=mutation.score,
                    decision=mutation.decision,
                    entry_price=mutation.entry_price,
                    sl=mutation.sl,
                    tp=mutation.tp,
                    regime=mutation.regime,
                    timestamp=mutation.timestamp,
                )
        elif isinstance(mutation, IncrementMissMutation):
            # V1 H1-gated decay: miss_count += 1, last_miss_h1_ts = h1_last_ts.
            current = self._active.get(mutation.symbol)
            if current is not None:
                self._active[mutation.symbol] = dataclasses.replace(
                    current,
                    miss_count=int(current.miss_count) + 1,
                    last_miss_h1_ts=mutation.h1_last_ts,
                )
        elif isinstance(mutation, EvictMutation):
            self._active.pop(mutation.symbol, None)
        else:  # pragma: no cover - exhaustive over the Mutation union
            raise TypeError(f"unknown mutation type: {type(mutation).__name__}")

    # -- end-of-cycle maintenance ----------------------------------------

    def ttl_sweep(self, clock: Clock) -> LifecycleDecision:
        """The TTL eviction decision (V1 ``cleanup_expired_signals``, main.py:
        169-207) — PURE: it reads the store and the clock and returns the
        eviction mutations + TTL logs, mutating nothing. The caller applies it
        via :meth:`apply` (Arch §5 step 5: "run TTL cleanup producing eviction
        decisions, applies them")."""
        now = clock.timestamp()
        mutations: list[Mutation] = []
        logs: list[LogEvent] = []
        for symbol, signal in self._active.items():
            ttl = ttl_for(signal.status, signal.miss_count, self._config)
            age_seconds = now - float(signal.timestamp)
            if age_seconds > ttl:
                logs.append(
                    LogEvent(
                        "TTL",
                        symbol,
                        (("age_seconds", age_seconds), ("ttl_seconds", ttl)),
                    )
                )
                mutations.append(EvictMutation(symbol, "ttl"))
        return LifecycleDecision(tuple(mutations), (), tuple(logs), ())

    def prune_killed_theses(self, clock: Clock) -> None:
        """Drop killed-thesis entries older than the cooldown window (V1 main.py
        :773-777, pruned silently each cycle — no log). The store owns this
        write; the reconciler only reads the store for the cooldown gate."""
        now = clock.timestamp()
        cutoff = self._config.kill_cooldown_seconds
        self._killed = {
            key: record
            for key, record in self._killed.items()
            if now - float(record.killed_ts) < cutoff
        }
