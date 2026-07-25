"""app.rendering — pure presentation of V1's terminal output.

Architecture ref: 02-software-architecture.md §3 (`infra.notifier` "…emit
terminal logs" — the presentation half, wired by the orchestrator), §10
Principle 9 (delivery executes; it decides nothing). Every function here is a
PURE mapping from an already-computed domain object (or a lifecycle `LogEvent`)
to the exact text V1 printed. No trading logic, no state, no I/O — the caller
owns the sink.

Fidelity: the format strings are byte-for-byte reproductions of V1's `print`
statements (`main.py` dashboard / scan-log block / lifecycle logs, and
`_log_decay`), each cited inline. The values come from the analysis outputs and
the lifecycle decision — nothing is recomputed here.
"""
from __future__ import annotations

from typing import List, Mapping, Optional

from v2.domain import ActiveSignal, HTFContext, LogEvent, Signal

__all__ = [
    "render_dashboard",
    "render_scan",
    "render_log_event",
    "decision_label",
]

_BAR = "=" * 110


# ==========================================================================
# Dashboard — V1 print_dashboard (main.py:209-247), verbatim format.
# ==========================================================================

def _display_label(direction_decision: str, entry_mode: str) -> str:
    """V1 ``_display_label`` (main.py:214-225): WATCH splits by mode."""
    decision = str(direction_decision).upper()
    mode = str(entry_mode).upper()
    if decision == "WATCH":
        if mode == "CONTINUATION":
            return "WATCH-CONT"
        if mode == "RETRACEMENT":
            return "WATCH-RETR"
        if mode == "WATCH_ONLY":
            return "WATCH-COMP"
    return decision


def render_dashboard(active: Mapping[str, ActiveSignal]) -> List[str]:
    """The RADAR SUMMARY table (main.py:209-247), sorted by score desc."""
    lines: List[str] = ["\n" + _BAR, "📋 RADAR SUMMARY", _BAR]
    if not active:
        lines.append("   [∅] No active setups.")
        lines.append(_BAR)
        return lines

    ordered = sorted(active.items(), key=lambda kv: kv[1].score, reverse=True)
    for symbol, data in ordered:
        label = _display_label(data.decision, data.entry_mode)
        lines.append(
            f"   ➤ {symbol:<14}| "
            f"{data.direction:<5} | "
            f"{label:<10} | "
            f"Score: {data.score:<4} | "
            f"Entry: {data.entry_price:.4f} | "
            f"SL: {data.sl:.4f} | "
            f"TP: {data.tp:.4f} | "
            f"Miss: {data.miss_count}"
        )
    lines.append(_BAR)
    return lines


# ==========================================================================
# Scan log block — V1 main.py:382-439 (SCAN / HTF / CONF / RAW).
# ==========================================================================

def decision_label(decision: str, entry_mode: str) -> str:
    """V1's ``decision_label`` (main.py:382-392) used in the SCAN line."""
    d = str(decision).upper()
    mode = str(entry_mode).upper()
    if d == "WATCH":
        if mode == "CONTINUATION":
            return "WATCH-CONT"
        if mode == "RETRACEMENT":
            return "WATCH-RETR"
        if mode == "WATCH_ONLY":
            return "WATCH-COMP"
        return "WATCH"
    return d


def _scored(signal: Optional[Signal]) -> bool:
    """Whether V1 would have set ``debug['score_raw']`` — i.e. score_signal ran
    (the candidate is entry-ready or WATCH_ONLY). Mirrors the reconciler's
    'scored' gate; here it decides whether the RAW line is emitted."""
    if signal is None or signal.candidate is None:
        return False
    entry = signal.candidate.entry
    return entry.entry_ready or entry.entry_mode == "WATCH_ONLY"


def render_scan(
    signal: Signal, htf: Optional[HTFContext], raw_signal: Optional[Signal]
) -> List[str]:
    """The per-symbol SCAN block V1 printed after M5 and before the memory
    engine (main.py:394-439). Emits, in order:

    - SCAN  (always) — final score / decision-label / reason;
    - HTF   iff ``htf`` is present (V1's ``debug['htf']`` was set — i.e. a
      direction resolved or vetoed; absent for invalid-input);
    - CONF  iff a candidate exists (V1's ``debug['conf']`` — the scored /
      not-ready path);
    - RAW   iff score_signal ran, from the PRE-M5 signal (V1's ``score_raw`` is
      the pre-M5, pre-choppy score; reconstructed as ``raw_signal.score`` plus
      the choppy penalty it already carries).
    """
    reason = signal.reason if signal.reason is not None else "-"
    label = decision_label(signal.decision, signal.entry_mode)

    lines: List[str] = [
        f"   [SCAN] {signal.symbol:<14}"
        f"| Score: {int(signal.score):<3} "
        f"| {label:<10} "
        f"| Reason: {reason}"
    ]

    if htf is not None:
        lines.append(
            f"      HTF  => "
            f"W:{htf.weekly_bias} "
            f"| D1:{htf.d1_bias} "
            f"| H4:{htf.h4_bias} "
            f"| LOC:{htf.location} "
            f"| REGIME:{signal.regime} "
            f"| H1_FB:{htf.fallback_h1_bias} "
            f"| SRC:{htf.direction_source} "
            f"| DIR:{htf.direction} "
            f"| BULL:{round(float(htf.bull), 1)} "
            f"| BEAR:{round(float(htf.bear), 1)}"
        )

    candidate = signal.candidate
    if candidate is not None:
        lines.append(
            f"      CONF => "
            f"sweep={candidate.sweep_valid} "
            f"| fvg={candidate.fvg_valid} "
            f"| disp={candidate.displacement_valid} "
            f"| vol={candidate.volume_spike} "
            f"| ob={candidate.fresh_ob} "
            f"| target={candidate.target_clear} "
            f"| mode={candidate.entry.entry_mode} "
            f"| ready={candidate.entry.entry_ready}"
        )

    if _scored(raw_signal):
        assert raw_signal is not None
        # V1's score_raw.score is the score_signal return BEFORE the choppy
        # penalty; raw_signal.score already has it applied, so add it back.
        raw_score = int(raw_signal.score) + (
            2 if "choppy_penalty" in raw_signal.notes else 0
        )
        lines.append(
            f"      RAW  => "
            f"type=dict "
            f"| score={raw_score} "
            f"| decision={raw_signal.decision} "
            f"| mode={raw_signal.entry_mode}"
        )

    return lines


# ==========================================================================
# Lifecycle LogEvent -> terminal line.
# The lifecycle already emits LogEvent objects (Phase 4); this only formats
# them, reproducing V1's exact per-event print statements.
# ==========================================================================

def render_log_event(event: LogEvent) -> str:
    """Map one lifecycle ``LogEvent`` to V1's terminal line for that event."""
    symbol = event.symbol if event.symbol is not None else ""
    d = dict(event.details)
    et = event.event_type

    if et == "KILL":
        # main.py:322-323
        return f"   [☠️ KILL] {symbol:<14} | Emergency invalidation triggered."

    if et == "ALERTED":
        # kill (337) / new (697) / upgrade (720-723) — distinguished by details.
        if d.get("kind") == "KILL":
            return f"   [📱 ALERTED] {symbol:<14} | KILL"
        if "tier" in d:
            return f"   [📱 ALERTED] {symbol:<14} | {d['tier']}"
        return f"   [📱 ALERTED] {symbol:<14} | {d.get('from')} -> {d.get('to')}"

    if et == "STALE":
        # main.py:454-457
        return f"   [⚠️ STALE] {symbol:<14} | Setup invalidated by live market price."

    if et == "KILL_COOLDOWN":
        # main.py:581-586
        return (
            f"   [🚫 KILL COOLDOWN] {symbol:<14} | "
            f"{d.get('entry_mode')} | {d.get('remaining_min', 0.0):.0f}m remaining"
        )

    if et == "SAME_THESIS":
        # main.py:612-617
        return (
            f"   [🟰 SAME THESIS] {symbol:<14} | "
            f"{d.get('entry_mode')} | {d.get('direction')} | "
            f"existing={d.get('existing')} current={d.get('current')}"
        )

    if et == "REPLACE":
        # main.py:658-662
        return (
            f"   [🔄 REPLACE] Slot penuh. "
            f"{d.get('weakest_symbol')} (score={d.get('weakest_score')}) diganti "
            f"{symbol} (score={d.get('score')})"
        )

    if et == "CAPPED":
        # main.py:666-670
        return (
            f"   [🚫 CAPPED] {symbol:<14} | "
            f"Score={d.get('score')} tidak cukup kuat "
            f"untuk masuk (weakest={d.get('weakest_score')})"
        )

    if et == "GUARD":
        # main.py:727-731
        return (
            f"   [⚠️ GUARD] {symbol:<14} | "
            f"{d.get('decision')} blocked — invalid levels "
            f"(entry={d.get('entry_price')}, sl={d.get('sl')}, tp={d.get('tp')})"
        )

    if et == "DECAY":
        # main.py:57-62 (_log_decay)
        return (
            f"   [⏳ DECAY] {symbol:<14} | "
            f"from={d.get('from_decision')} | "
            f"mode={d.get('mode')} | "
            f"reason={d.get('reason')} | "
            f"miss_count={d.get('miss_count')}"
        )

    if et == "TTL":
        # main.py:199-202
        age_h = float(d.get("age_seconds", 0.0)) / 3600.0
        ttl_h = float(d.get("ttl_seconds", 0.0)) / 3600.0
        return (
            f"   [⏰ TTL EXPIRED] {symbol:<14} | "
            f"Age: {age_h:.1f}h | TTL: {ttl_h:.1f}h"
        )

    # SCAN is not a lifecycle LogEvent (it is rendered by render_scan); any
    # other type falls through to a stable generic line.
    return f"   [{et}] {symbol}"
