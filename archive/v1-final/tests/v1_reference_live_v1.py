"""V1LifecycleReference — a faithful transcription of V1's memory engine.

V1's lifecycle logic lives inline in ``main.py``'s ``run()`` loop (lines
316-777), wrapped around network fetches and an infinite loop, so it cannot be
imported and called directly the way the analysis functions could. This class
transcribes that logic *verbatim* onto plain dicts, with the wall clock and the
H1 timestamp injected, so it can be driven deterministically as the equivalence
oracle for the V2 lifecycle layer.

Fidelity anchors (not transcribed — the REAL V1 code is called):
- the KILL / STALE predicate is the actual ``SMCStrategy.is_poi_invalidated``;
- ``_alert_tier`` / ``_alert_rank`` / ``_is_same_thesis`` are byte-copies of the
  three trivial helpers in ``main.py`` (already pinned to V1 by the harness's
  ``test_oracle``);
- the branch orchestration (kill → stale → memory engine → decay → TTL →
  killed-prune) is transcribed line-for-line, each block cited.

Only the diagnostic-log side effects V1 prints (SCAN/HTF/CONF/RAW) and the
OB-audit assembly are omitted — those are orchestrator/delivery concerns, not
lifecycle state. Lifecycle-relevant events (KILL/STALE/GUARD/DECAY/REPLACE/
CAPPED/SAME_THESIS/KILL_COOLDOWN/ALERTED/TTL) and the alert dispatch ARE
recorded.
"""
from __future__ import annotations

from typing import Any, Dict, List

from src.strategy import SMCStrategy
from v2.infra.clock import Clock
from v2.lifecycle.config import LifecycleConfig

from tests.lifecycle.support import CycleSpec, make_v1_context, make_v1_result


# -- V1 main.py:32-50 / 249-253 (byte copies) -------------------------------

def _alert_rank(tier: str) -> int:
    return {"": 0, "VALID": 1, "A+": 2}.get(str(tier).upper(), 0)


def _alert_tier(decision: str) -> str:
    decision = str(decision).upper()
    if decision == "VALID":
        return "VALID"
    if decision == "A+":
        return "A+"
    return ""


def _is_same_thesis(existing: dict, direction: str, entry_mode: str) -> bool:
    return (
        str(existing.get("direction", "")).upper() == direction.upper()
        and str(existing.get("entry_mode", "")).upper() == entry_mode.upper()
    )


class V1LifecycleReference:
    """Runs V1's memory engine on plain dicts, deterministically."""

    def __init__(
        self,
        clock: Clock,
        *,
        config: LifecycleConfig = LifecycleConfig(),
        strategy: SMCStrategy | None = None,
    ) -> None:
        self.clock = clock
        self.config = config
        self.strategy = strategy if strategy is not None else SMCStrategy()
        self.active: Dict[str, Dict[str, Any]] = {}
        self.killed: Dict[str, float] = {}
        # per-cycle event capture
        self.events: List[tuple] = []
        self.alerts: List[Dict[str, Any]] = []

    # -- recording --------------------------------------------------------

    def _event(self, event_type: str, symbol: str, **fields: Any) -> None:
        self.events.append((event_type, symbol, tuple(sorted(fields.items()))))

    def _alert(self, kind: str, symbol: str, **fields: Any) -> None:
        record = {"kind": kind, "symbol": symbol}
        record.update(fields)
        self.alerts.append(record)

    def reset_capture(self) -> None:
        self.events = []
        self.alerts = []

    # -- one symbol-cycle (main.py:316-764) ------------------------------

    def process(self, spec: CycleSpec) -> None:
        now = self.clock.timestamp()
        cfg = self.config
        MISS_COUNT_LIMIT = cfg.miss_count_limit
        KILL_COOLDOWN_SECONDS = cfg.kill_cooldown_seconds
        MAX_OPEN_POSITIONS = cfg.max_open_positions

        symbol = spec.symbol
        active_signals = self.active
        killed_thesis = self.killed

        result = make_v1_result(spec)
        context = make_v1_context(spec, result)
        h1_last_ts = spec.h1_last_ts

        # ===== KILL SWITCH (316-347) =====
        if symbol in active_signals:
            signal_dead = self.strategy.is_poi_invalidated(active_signals[symbol], context)
            if signal_dead:
                previous = active_signals[symbol]
                self._event("KILL", symbol)
                prev_tier = str(previous.get("alert_sent_tier", "")).upper().strip()
                if _alert_rank(prev_tier) > 0:
                    self._alert(
                        "kill",
                        symbol,
                        direction=previous.get("direction", "NONE"),
                        entry_price=previous.get("entry_price", 0.0),
                        sl=previous.get("sl", 0.0),
                        tp=previous.get("tp", 0.0),
                        reason="Emergency invalidation triggered",
                    )
                    self._event("ALERTED", symbol, kind="KILL")
                thesis_key = (
                    f"{symbol}|{previous.get('direction','')}|{previous.get('entry_mode','')}"
                )
                killed_thesis[thesis_key] = now
                del active_signals[symbol]
                return

        # ===== derived log vars (379-380) =====
        reason = result.get("reason", "-")
        h1_setup = context.get("h1_setup", {})
        entry_mode = str(
            result.get("entry_mode") or h1_setup.get("entry_mode", "RETRACEMENT")
        ).upper()
        decision = str(result.get("decision", "SKIP")).upper()

        # ===== PRE-MEMORY STALE CHECK (442-458) =====
        temp_signal = {
            "direction": result.get("direction"),
            "entry_price": result.get("entry_price"),
            "sl": result.get("sl"),
            "tp": result.get("tp"),
        }
        if self.strategy.is_poi_invalidated(temp_signal, context):
            self._event("STALE", symbol)
            return

        # ===== MEMORY ENGINE (463-764) =====
        score = result.get("score", 0)
        decision = result.get("decision", "SKIP")
        direction = result.get("direction", "NONE")
        entry_price = float(result.get("entry_price", 0.0))
        sl = float(result.get("sl", 0.0))
        tp = float(result.get("tp", 0.0))

        import math

        levels_valid = (
            math.isfinite(entry_price)
            and math.isfinite(sl)
            and math.isfinite(tp)
            and entry_price > 0.0
            and sl > 0.0
            and tp > 0.0
            and entry_price != sl
            and entry_price != tp
        )

        if decision in ["WATCH", "VALID", "A+"] and levels_valid:
            signal_payload = {
                "direction": direction,
                "score": score,
                "decision": decision,
                "entry_price": entry_price,
                "sl": sl,
                "tp": tp,
                "regime": str(result.get("regime", "unknown")),
                "timestamp": now,
                "miss_count": 0,
                "status": "WATCH",
                "alert_sent_tier": "",
                "alert_sent_ts": 0.0,
                "last_miss_h1_ts": 0.0,
                "last_ob_audit_h1_ts": 0.0,
                "entry_mode": entry_mode,
                "entry_source": str(result.get("entry_source", "")),
            }

            previous_signal = active_signals.get(symbol)
            signal_id = f"{symbol}|{entry_mode}|{h1_last_ts}"

            previous_alert_tier = ""
            previous_alert_ts = 0.0
            if previous_signal:
                previous_alert_tier = str(
                    previous_signal.get("alert_sent_tier", "")
                ).upper().strip()
                previous_alert_ts = float(previous_signal.get("alert_sent_ts", 0.0))

            current_alert_tier = _alert_tier(decision)

            thesis_key = f"{symbol}|{direction}|{entry_mode}"
            killed_ts = float(killed_thesis.get(thesis_key, 0.0))
            if killed_ts > 0:
                cooldown_age = now - killed_ts
                if cooldown_age < KILL_COOLDOWN_SECONDS:
                    self._event("KILL_COOLDOWN", symbol, entry_mode=entry_mode, direction=direction)
                    return

            existing = active_signals.get(symbol)
            if existing:
                same_thesis = _is_same_thesis(existing, direction, entry_mode)
                existing_tier = str(existing.get("alert_sent_tier", "")).upper().strip()
                if same_thesis and _alert_rank(current_alert_tier) <= _alert_rank(existing_tier):
                    self._event(
                        "SAME_THESIS", symbol,
                        entry_mode=entry_mode, direction=direction,
                        existing=existing_tier, current=current_alert_tier,
                    )
                    active_signals[symbol]["score"] = max(
                        score, active_signals[symbol].get("score", score)
                    )
                    if _alert_rank(current_alert_tier) > _alert_rank(existing_tier):
                        active_signals[symbol]["decision"] = decision
                    active_signals[symbol]["entry_price"] = entry_price
                    active_signals[symbol]["sl"] = sl
                    active_signals[symbol]["tp"] = tp
                    active_signals[symbol]["regime"] = str(result.get("regime", "unknown"))
                    active_signals[symbol]["timestamp"] = now
                    return

            signal_payload["signal_id"] = signal_id
            signal_payload["alert_sent_tier"] = previous_alert_tier
            signal_payload["alert_sent_ts"] = previous_alert_ts
            signal_payload["last_miss_h1_ts"] = h1_last_ts
            signal_payload["last_ob_audit_h1_ts"] = (
                float(previous_signal.get("last_ob_audit_h1_ts", 0.0))
                if previous_signal
                else 0.0
            )

            if symbol in active_signals:
                active_signals[symbol] = signal_payload
            elif len(active_signals) < MAX_OPEN_POSITIONS:
                active_signals[symbol] = signal_payload
            else:
                weakest_symbol = min(
                    active_signals, key=lambda s: active_signals[s]["score"]
                )
                weakest_score = active_signals[weakest_symbol]["score"]
                if score > weakest_score:
                    self._event(
                        "REPLACE", symbol,
                        weakest_symbol=weakest_symbol,
                        weakest_score=weakest_score, score=score,
                    )
                    del active_signals[weakest_symbol]
                    active_signals[symbol] = signal_payload
                else:
                    self._event(
                        "CAPPED", symbol, score=score, weakest_score=weakest_score
                    )

            # TELEGRAM ALERT DISPATCH (675-723)
            if symbol in active_signals and current_alert_tier:
                if previous_alert_tier == "":
                    self._alert(
                        "new", symbol,
                        direction=direction, entry_price=entry_price, sl=sl, tp=tp,
                        dedup_key=current_alert_tier,
                    )
                    active_signals[symbol]["alert_sent_tier"] = current_alert_tier
                    active_signals[symbol]["alert_sent_ts"] = now
                    self._event("ALERTED", symbol, tier=current_alert_tier)
                elif _alert_rank(current_alert_tier) > _alert_rank(previous_alert_tier):
                    self._alert(
                        "upgrade", symbol,
                        direction=direction, entry_price=entry_price, sl=sl, tp=tp,
                        dedup_key=current_alert_tier,
                        old_decision=previous_alert_tier,
                        new_decision=current_alert_tier,
                    )
                    active_signals[symbol]["alert_sent_tier"] = current_alert_tier
                    active_signals[symbol]["alert_sent_ts"] = now
                    self._event(
                        "ALERTED", symbol,
                        old=previous_alert_tier, new=current_alert_tier,
                    )

        elif decision in ["WATCH", "VALID", "A+"] and not levels_valid:
            self._event(
                "GUARD", symbol,
                decision=decision, entry_price=entry_price, sl=sl, tp=tp,
            )
            if symbol in active_signals:
                prev_last_miss = float(active_signals[symbol].get("last_miss_h1_ts", 0.0))
                if h1_last_ts > prev_last_miss:
                    active_signals[symbol]["miss_count"] += 1
                    active_signals[symbol]["last_miss_h1_ts"] = h1_last_ts
                    if active_signals[symbol]["miss_count"] >= MISS_COUNT_LIMIT:
                        self._decay_event(symbol, entry_mode, "invalid_levels")
                        del active_signals[symbol]
        else:
            if symbol in active_signals:
                prev_last_miss = float(active_signals[symbol].get("last_miss_h1_ts", 0.0))
                if h1_last_ts > prev_last_miss:
                    active_signals[symbol]["miss_count"] += 1
                    active_signals[symbol]["last_miss_h1_ts"] = h1_last_ts
                    if active_signals[symbol]["miss_count"] >= MISS_COUNT_LIMIT:
                        self._decay_event(symbol, entry_mode, str(reason))
                        del active_signals[symbol]

    def _decay_event(self, symbol: str, entry_mode: str, reason: str) -> None:
        signal = self.active.get(symbol, {})
        self._event(
            "DECAY", symbol,
            from_decision=str(signal.get("decision", "-")).upper().strip(),
            mode=str(entry_mode).upper().strip(),
            reason=reason,
            miss_count=int(signal.get("miss_count", 0)),
        )

    # -- end-of-cycle maintenance (770-777) ------------------------------

    def ttl_cleanup(self) -> None:
        """V1 cleanup_expired_signals (169-207) with injected now."""
        now = self.clock.timestamp()
        cfg = self.config
        to_delete = []
        for symbol, data in self.active.items():
            created_at = float(data.get("timestamp", now))
            age_seconds = now - created_at
            miss_count = int(data.get("miss_count", 0))
            status = str(data.get("status", "")).upper().strip()
            if status == "MANUAL_ACTION":
                ttl = cfg.manual_action_ttl_seconds
            elif miss_count > 0:
                ttl = cfg.tapped_ttl_seconds
            else:
                ttl = cfg.untapped_ttl_seconds
            if age_seconds > ttl:
                to_delete.append(symbol)
                self._event("TTL", symbol, age_seconds=age_seconds, ttl_seconds=ttl)
        for symbol in to_delete:
            del self.active[symbol]

    def prune_killed(self) -> None:
        now = self.clock.timestamp()
        cutoff = self.config.kill_cooldown_seconds
        self.killed = {
            k: ts for k, ts in self.killed.items() if now - float(ts) < cutoff
        }
