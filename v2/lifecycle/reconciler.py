"""lifecycle.reconciler — the stateful core, as a pure decision function.

Architecture ref: 02-software-architecture.md §3 (`lifecycle.reconciler`: "Given
a fresh Signal and the current memory, decide: kill? stale? insert/replace/cap?
decay-increment? evict? which alerts? Produces LifecycleDecision. This is the
disciplined replacement for V1's memory-engine branches."), §5 step 4d/4e, §7
(the state machine), §8 (LifecycleDecision contract).

This module is a faithful, line-cited transcription of V1's per-symbol memory
engine — ``main.py`` lines 316-764 — restructured so it *decides* (returns a
declarative LifecycleDecision) and mutates nothing. The store applies the
decision; the orchestrator routes the alerts and logs. Every branch, ordering,
and quirk of V1 is preserved; nothing is "improved".

## What this consumes, and why the extra kill input

V1's ``run()`` reads the fresh cycle's ``context`` directly. The reconciler is
handed the analysis *outputs* instead: the ``Signal`` plus the fresh cycle's
``HTFContext``. The HTF is required because V1's KILL and STALE checks
(``is_poi_invalidated``) read the fresh HTF biases *regardless of the decision
tier* — including on a candidate-less SKIP cycle (veto / no_direction), where the
``Signal`` alone does not carry biases. The orchestrator supplies ``context.htf``
(``None`` on an invalid-input cycle, exactly as V1's ``htf_map`` was empty there).

The kill/stale ATR is derived from the Signal itself: V1 read
``h1_setup.get("atr_last", 0.0)``, which is the real ATR only on a candidate
path and is ``0.0`` for veto / no_direction / invalid (no ``h1_setup``, or the
veto's hard-coded ``atr_last: 0.0``). So ``candidate.atr_last if candidate else
0.0`` reproduces it exactly — no external ATR input is needed.

## The flat "result view"

V1's memory engine read a flat ``result`` dict. The V2 ``Signal`` keeps entry /
risk data on its embedded candidate, and — crucially — V1 only populated
``result``'s entry_price / sl / tp via ``result.update`` on the *scored* path
(``entry_ready`` or ``WATCH_ONLY``). The not-ready exit returns before that
update, so those keys are ABSENT there. :func:`_result_view` reproduces that
exactly: on a non-scored Signal the flat levels are ``None`` (V1's
``result.get("sl")`` -> None on the STALE path; ``result.get("sl", 0.0)`` -> 0.0
in the memory engine), so the STALE ``float(None)`` -> False quirk is preserved.

Purity: no I/O, no filesystem, no globals, no wall-clock reads (the injected
``Clock`` is the only time source), no numpy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from v2.domain import (
    ActiveSignal,
    Alert,
    EvictMutation,
    HTFContext,
    IncrementMissMutation,
    InsertMutation,
    KilledThesisRecord,
    LifecycleDecision,
    LogEvent,
    Mutation,
    PartialRefreshMutation,
    ReplaceMutation,
    Signal,
)
from v2.infra.clock import Clock
from v2.lifecycle.config import LifecycleConfig
from v2.lifecycle.memory import MemoryView
from v2.lifecycle.rules import (
    alert_rank,
    alert_tier,
    is_poi_invalidated,
    is_same_thesis,
    levels_valid,
)

__all__ = ["Reconciler"]

_ACTIONABLE = ("WATCH", "VALID", "A+")


@dataclass(frozen=True)
class _ResultView:
    """The flat view of a Signal as V1's memory engine read its ``result``."""

    decision: str
    direction: str          # "NONE" when absent (V1 result.get("direction","NONE"))
    entry_mode: str         # V1 main.py:379 formula (RETRACEMENT default, upper)
    regime: str
    reason: Optional[str]
    score: int
    volume_spike: bool
    # Present (float) only on the scored path; None otherwise (keys absent in V1).
    entry_price: Optional[float]
    sl: Optional[float]
    tp: Optional[float]
    entry_source: Optional[str]
    tp_source: Optional[str]

    # Memory-engine floats: V1 ``float(result.get(k, 0.0))`` — absent -> 0.0.
    @property
    def entry_price_f(self) -> float:
        return float(self.entry_price) if self.entry_price is not None else 0.0

    @property
    def sl_f(self) -> float:
        return float(self.sl) if self.sl is not None else 0.0

    @property
    def tp_f(self) -> float:
        return float(self.tp) if self.tp is not None else 0.0

    @property
    def entry_source_str(self) -> str:
        # V1 signal_payload: str(result.get("entry_source", "")).
        return str(self.entry_source) if self.entry_source is not None else ""


def _result_view(signal: Signal) -> _ResultView:
    candidate = signal.candidate
    scored = candidate is not None and (
        candidate.entry.entry_ready or candidate.entry.entry_mode == "WATCH_ONLY"
    )

    if scored and candidate is not None:
        entry_price: Optional[float] = float(candidate.entry.entry_price)
        sl: Optional[float] = float(candidate.risk.sl)
        tp: Optional[float] = candidate.risk.tp
        entry_source: Optional[str] = candidate.entry.entry_source
        tp_source: Optional[str] = candidate.risk.tp_source
    else:
        # Not scored: V1 never ran result.update, so these keys were absent.
        entry_price = sl = tp = None
        entry_source = tp_source = None

    # entry_mode per V1 main.py:379:
    #   str(result.get("entry_mode") or h1_setup.get("entry_mode","RETRACEMENT")).upper()
    if candidate is not None:
        raw_mode: Optional[str] = candidate.entry.entry_mode      # scored / not-ready
    elif signal.direction is not None:
        raw_mode = signal.entry_mode                              # veto -> "NONE"
    else:
        raw_mode = None                                           # invalid / no_direction
    entry_mode = str(raw_mode or "RETRACEMENT").upper()

    return _ResultView(
        decision=str(signal.decision),
        direction=signal.direction if signal.direction is not None else "NONE",
        entry_mode=entry_mode,
        regime=str(signal.regime),
        reason=signal.reason,
        score=int(signal.score),
        volume_spike=bool(candidate.volume_spike) if candidate is not None else False,
        entry_price=entry_price,
        sl=sl,
        tp=tp,
        entry_source=entry_source,
        tp_source=tp_source,
    )


def _htf_pairs(htf: Optional[HTFContext], regime: str) -> tuple:
    """The V1 ``debug['htf']`` summary as ordered pairs for an Alert payload
    (main.py:688 passes ``htf=htf_dbg``). Delivery-cosmetic; reconstructed from
    the HTFContext for completeness."""
    if htf is None:
        return ()
    return (
        ("weekly", htf.weekly_bias),
        ("d1", htf.d1_bias),
        ("h4", htf.h4_bias),
        ("loc", htf.location),
        ("regime", regime),
        ("h1_fb", htf.fallback_h1_bias),
        ("src", htf.direction_source),
        ("dir", htf.direction),
        ("bull", round(float(htf.bull), 1)),
        ("bear", round(float(htf.bear), 1)),
        ("anchor_override", htf.anchor_override),
    )


class Reconciler:
    """Reconciles one fresh Signal against memory, emitting a LifecycleDecision.

    The injected ``Clock`` is the only time source — every wall-clock value V1
    stamped with its own clock read (the payload timestamp, alert_sent_ts, and
    the killed-thesis kill_ts) comes from it, so outcomes are deterministic in
    tests.
    """

    def __init__(
        self, clock: Clock, config: LifecycleConfig = LifecycleConfig()
    ) -> None:
        self._clock = clock
        self._config = config

    def reconcile(
        self,
        signal: Signal,
        memory: MemoryView,
        *,
        htf: Optional[HTFContext],
        live_price: float,
        h1_last_ts: float,
    ) -> LifecycleDecision:
        now = self._clock.timestamp()
        symbol = signal.symbol
        rv = _result_view(signal)

        d1_bias = htf.d1_bias if htf is not None else "CHOPPY"
        h4_bias = htf.h4_bias if htf is not None else "CHOPPY"
        # V1 read h1_setup.get("atr_last", 0.0): real only on a candidate path,
        # 0.0 for veto / no_direction / invalid (no h1_setup / hard-coded 0.0).
        atr_last = (
            float(signal.candidate.atr_last) if signal.candidate is not None else 0.0
        )

        mutations: List[Mutation] = []
        alerts: List[Alert] = []
        logs: List[LogEvent] = []
        killed: List[KilledThesisRecord] = []

        # =================================================================
        # KILL SWITCH — main.py:316-347. Uses the SAVED signal's direction/sl
        # against the FRESH cycle's HTF/price. On a kill, V1 `continue`s.
        # =================================================================
        prev = memory.get(symbol)
        if prev is not None and is_poi_invalidated(
            direction=prev.direction,
            sl=prev.sl,
            live_price=live_price,
            fallback_entry_price=rv.entry_price_f,
            d1_bias=d1_bias,
            h4_bias=h4_bias,
            atr_last=atr_last,
        ):
            logs.append(LogEvent("KILL", symbol))
            if alert_rank(prev.alert_sent_tier) > 0:
                alerts.append(
                    Alert(
                        kind="kill",
                        symbol=symbol,
                        direction=prev.direction,
                        entry_price=prev.entry_price,
                        sl=prev.sl,
                        tp=prev.tp,
                        reason="Emergency invalidation triggered",
                    )
                )
                logs.append(LogEvent("ALERTED", symbol, (("kind", "KILL"),)))
            killed.append(
                KilledThesisRecord(
                    KilledThesisRecord.make_key(
                        symbol, prev.direction, prev.entry_mode
                    ),
                    now,
                )
            )
            mutations.append(EvictMutation(symbol, "kill"))
            return LifecycleDecision(
                tuple(mutations), tuple(alerts), tuple(logs), tuple(killed)
            )

        # =================================================================
        # PRE-MEMORY STALE CHECK — main.py:442-458. The fresh setup itself is
        # invalidated by the live price (uses the fresh result's sl, which is
        # None on the not-ready path -> is_poi_invalidated returns False).
        # =================================================================
        if is_poi_invalidated(
            direction=rv.direction,
            sl=rv.sl,
            live_price=live_price,
            fallback_entry_price=rv.entry_price_f,
            d1_bias=d1_bias,
            h4_bias=h4_bias,
            atr_last=atr_last,
        ):
            logs.append(LogEvent("STALE", symbol))
            return LifecycleDecision((), (), tuple(logs), ())

        # =================================================================
        # MEMORY ENGINE — main.py:463-764.
        # =================================================================
        decision = rv.decision
        score = rv.score
        direction = rv.direction
        entry_mode = rv.entry_mode
        levels_ok = levels_valid(rv.entry_price_f, rv.sl_f, rv.tp_f)
        actionable = decision in _ACTIONABLE

        if actionable and levels_ok:
            self._actionable(
                symbol, rv, memory, now, h1_last_ts, htf,
                mutations, alerts, logs, killed,
            )
        elif actionable and not levels_ok:
            # GUARD — main.py:725-748.
            logs.append(
                LogEvent(
                    "GUARD",
                    symbol,
                    (
                        ("decision", decision),
                        ("entry_price", rv.entry_price_f),
                        ("sl", rv.sl_f),
                        ("tp", rv.tp_f),
                    ),
                )
            )
            self._decay_miss(
                symbol, prev, entry_mode, "invalid_levels", h1_last_ts,
                mutations, logs,
            )
        else:
            # SKIP else-branch — main.py:749-764.
            reason = rv.reason if rv.reason is not None else "-"
            self._decay_miss(
                symbol, prev, entry_mode, str(reason), h1_last_ts,
                mutations, logs,
            )

        return LifecycleDecision(
            tuple(mutations), tuple(alerts), tuple(logs), tuple(killed)
        )

    # -- actionable + valid levels (main.py:526-723) ---------------------

    def _actionable(
        self,
        symbol: str,
        rv: _ResultView,
        memory: MemoryView,
        now: float,
        h1_last_ts: float,
        htf: Optional[HTFContext],
        mutations: List[Mutation],
        alerts: List[Alert],
        logs: List[LogEvent],
        killed: List[KilledThesisRecord],
    ) -> None:
        cfg = self._config
        decision = rv.decision
        score = rv.score
        direction = rv.direction
        entry_mode = rv.entry_mode
        entry_price = rv.entry_price_f
        sl = rv.sl_f
        tp = rv.tp_f

        prev = memory.get(symbol)
        previous_alert_tier = (
            str(prev.alert_sent_tier).upper().strip() if prev is not None else ""
        )
        previous_alert_ts = float(prev.alert_sent_ts) if prev is not None else 0.0
        current_alert_tier = alert_tier(decision)
        signal_id = f"{symbol}|{entry_mode}|{h1_last_ts}"

        # --- kill-cooldown gate (main.py:564-588) ---
        thesis_key = f"{symbol}|{direction}|{entry_mode}"
        killed_ts = memory.killed_ts(thesis_key)
        if killed_ts > 0:
            cooldown_age = now - killed_ts
            if cooldown_age < cfg.kill_cooldown_seconds:
                remaining_min = (cfg.kill_cooldown_seconds - cooldown_age) / 60.0
                logs.append(
                    LogEvent(
                        "KILL_COOLDOWN",
                        symbol,
                        (
                            ("entry_mode", entry_mode),
                            ("direction", direction),
                            ("remaining_min", remaining_min),
                        ),
                    )
                )
                return  # V1 `continue` — no insertion, no alert.

        # --- same-thesis partial refresh (main.py:590-637) ---
        existing = prev
        if existing is not None:
            same = is_same_thesis(
                existing.direction, existing.entry_mode, direction, entry_mode
            )
            existing_tier = str(existing.alert_sent_tier).upper().strip()
            if same and alert_rank(current_alert_tier) <= alert_rank(existing_tier):
                logs.append(
                    LogEvent(
                        "SAME_THESIS",
                        symbol,
                        (
                            ("entry_mode", entry_mode),
                            ("direction", direction),
                            ("existing", existing_tier),
                            ("current", current_alert_tier),
                        ),
                    )
                )
                # score = max(new, old); decision NEVER changes in this branch
                # (V1's inner `if rank(current) > rank(existing)` is always
                # False here); miss_count / last_miss_h1_ts unchanged.
                mutations.append(
                    PartialRefreshMutation(
                        symbol=symbol,
                        score=max(score, int(existing.score)),
                        decision=existing.decision,
                        entry_price=entry_price,
                        sl=sl,
                        tp=tp,
                        regime=rv.regime,
                        timestamp=now,
                    )
                )
                return  # V1 `continue`.

        # --- decide the alert first, so the inserted record carries the
        #     post-alert metadata V1 wrote in-place after insertion ---
        alert_fires = False
        alert: Optional[Alert] = None
        alerted_log: Optional[LogEvent] = None
        if current_alert_tier:
            if previous_alert_tier == "":
                alert_fires = True
                alert = Alert(
                    kind="new",
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    sl=sl,
                    tp=tp,
                    dedup_key=current_alert_tier,
                    decision=decision,
                    score=score,
                    regime=rv.regime,
                    entry_mode=entry_mode,
                    entry_source=rv.entry_source_str,
                    tp_source=str(rv.tp_source) if rv.tp_source is not None else "-",
                    volume_spike=rv.volume_spike,
                    htf=_htf_pairs(htf, rv.regime),
                )
                alerted_log = LogEvent(
                    "ALERTED", symbol, (("tier", current_alert_tier),)
                )
            elif alert_rank(current_alert_tier) > alert_rank(previous_alert_tier):
                alert_fires = True
                alert = Alert(
                    kind="upgrade",
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    sl=sl,
                    tp=tp,
                    dedup_key=current_alert_tier,
                    old_decision=previous_alert_tier,
                    new_decision=current_alert_tier,
                    old_score=float(prev.score) if prev is not None else float(score),
                    new_score=score,
                    regime=rv.regime,
                    entry_mode=entry_mode,
                    entry_source=rv.entry_source_str,
                    tp_source=str(rv.tp_source) if rv.tp_source is not None else "-",
                    volume_spike=rv.volume_spike,
                )
                alerted_log = LogEvent(
                    "ALERTED",
                    symbol,
                    (("from", previous_alert_tier), ("to", current_alert_tier)),
                )

        # The alert only actually fires if the signal is inserted (V1's
        # dispatch is gated on `symbol in active_signals`). CAPPED skips it.
        final_tier = previous_alert_tier
        final_ts = previous_alert_ts

        # --- build the payload (main.py:528-647) ---
        # last_ob_audit carried over from the previous record, else 0.0.
        last_ob_audit = (
            float(prev.last_ob_audit_h1_ts) if prev is not None else 0.0
        )

        def _make_record(tier: str, ts: float) -> ActiveSignal:
            return ActiveSignal(
                direction=direction,
                score=score,
                decision=decision,
                entry_price=entry_price,
                sl=sl,
                tp=tp,
                regime=rv.regime,
                timestamp=now,
                miss_count=0,
                status="WATCH",
                alert_sent_tier=tier,
                alert_sent_ts=ts,
                last_miss_h1_ts=h1_last_ts,
                last_ob_audit_h1_ts=last_ob_audit,
                entry_mode=entry_mode,
                entry_source=rv.entry_source_str,
                signal_id=signal_id,
            )

        # --- decide insert / replace / cap (main.py:649-670) ---
        # `replace_target` is set only on the [🔄 REPLACE] path; None means a
        # plain in-place / slot-available insert. CAPPED returns early.
        replace_target: Optional[str] = None
        replace_log: Optional[LogEvent] = None

        if symbol in memory:
            pass  # replace the same-symbol payload (V1 line 649-650)
        elif len(memory) < cfg.max_open_positions:
            pass  # slot available (V1 line 651-652)
        else:
            weakest_symbol = memory.weakest_symbol()
            assert weakest_symbol is not None  # memory is full -> non-empty
            weakest = memory.get(weakest_symbol)
            assert weakest is not None
            weakest_score = weakest.score
            if score > weakest_score:
                replace_target = weakest_symbol
                replace_log = LogEvent(
                    "REPLACE",
                    symbol,
                    (
                        ("weakest_symbol", weakest_symbol),
                        ("weakest_score", weakest_score),
                        ("score", score),
                    ),
                )
            else:
                logs.append(
                    LogEvent(
                        "CAPPED",
                        symbol,
                        (("score", score), ("weakest_score", weakest_score)),
                    )
                )
                return  # V1 does not insert; alert dispatch is skipped.

        # Reaching here means the signal is inserted; the alert (decided above)
        # now actually fires, so the record carries the post-alert metadata V1
        # wrote in-place after insertion (main.py:694-695 / 717-718).
        if alert_fires:
            final_tier = current_alert_tier
            final_ts = now

        record = _make_record(final_tier, final_ts)

        if replace_target is not None:
            if replace_log is not None:
                logs.append(replace_log)
            mutations.append(ReplaceMutation(replace_target, symbol, record))
        else:
            mutations.append(InsertMutation(symbol, record))

        if alert_fires and alert is not None:
            alerts.append(alert)
            if alerted_log is not None:
                logs.append(alerted_log)

    # -- decay / miss (main.py:733-748 GUARD, 749-764 SKIP) --------------

    def _decay_miss(
        self,
        symbol: str,
        prev: Optional[ActiveSignal],
        entry_mode: str,
        reason: str,
        h1_last_ts: float,
        mutations: List[Mutation],
        logs: List[LogEvent],
    ) -> None:
        """The H1-gated miss increment + at-limit decay eviction, shared by the
        GUARD (invalid levels) and SKIP branches — one implementation."""
        if prev is None:
            return
        if float(h1_last_ts) <= float(prev.last_miss_h1_ts):
            return  # H1 gate closed (Decision #006): no new candle -> no miss.

        mutations.append(IncrementMissMutation(symbol, h1_last_ts))
        new_miss = int(prev.miss_count) + 1
        if new_miss >= self._config.miss_count_limit:
            logs.append(_decay_log(symbol, prev, entry_mode, reason, new_miss))
            mutations.append(EvictMutation(symbol, "decay"))


def _decay_log(
    symbol: str,
    prev: ActiveSignal,
    entry_mode: str,
    reason: str,
    miss_count: int,
) -> LogEvent:
    """The DECAY log (V1 ``_log_decay``, main.py:52-63). Its fields come from
    intentionally-mixed sources (Decision #009): ``from_decision`` from the
    *stored* signal, ``mode`` from the *fresh* cycle, ``reason`` from the branch,
    ``miss_count`` the post-increment value."""
    return LogEvent(
        "DECAY",
        symbol,
        (
            ("from_decision", str(prev.decision).upper().strip()),
            ("mode", str(entry_mode).upper().strip()),
            ("reason", reason),
            ("miss_count", miss_count),
        ),
    )
