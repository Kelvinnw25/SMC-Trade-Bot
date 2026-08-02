"""Fakes and builders for the orchestrator tests.

Every collaborator the orchestrator coordinates is faked here so the tests touch
no network, filesystem, or Telegram. The fakes record their calls so a test can
assert the orchestrator invoked them in the right way (wiring, order, arguments).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from v2.domain import (
    ActiveSignal,
    KilledThesisRecord,
    LifecycleDecision,
    OrderBlock,
    Signal,
)
from v2.infra.clock import FakeClock
from v2.infra.market_data import SymbolVolume

from tests.lifecycle.support import CycleSpec, make_signal


# -- clocks -----------------------------------------------------------------

def sniper_clock() -> FakeClock:
    """A weekday 12:00 UTC (Mon 2026-01-05) → get_session_mode SNIPER."""
    return FakeClock(datetime(2026, 1, 5, 12, tzinfo=timezone.utc))


def strict_hour_clock() -> FakeClock:
    """A weekday 23:00 UTC (hour >= 22) → STRICT."""
    return FakeClock(datetime(2026, 1, 5, 23, tzinfo=timezone.utc))


def weekend_clock() -> FakeClock:
    """Saturday 12:00 UTC → STRICT (weekday >= 5)."""
    return FakeClock(datetime(2026, 1, 3, 12, tzinfo=timezone.utc))


# -- market data ------------------------------------------------------------

def make_snapshot(
    symbol: str,
    *,
    d1_rows: int = 12,
    h1_rows: int = 60,
    live_price: float = 100.0,
    h1_last_ts: float = 5000.0,
    m5: Optional[pd.DataFrame] = None,
):
    from v2.domain import MarketSnapshot

    d1 = pd.DataFrame({"timestamp": range(d1_rows), "close": [100.0] * d1_rows})
    h1 = pd.DataFrame({"timestamp": range(h1_rows), "close": [100.0] * h1_rows})
    return MarketSnapshot(
        symbol=symbol,
        d1=d1,
        h1=h1,
        h1_last_ts=h1_last_ts,
        h1_last_ts_dt=pd.Timestamp("2026-01-05T00:00:00"),
        live_price=live_price,
        m5=m5,
    )


class FakeMarketData:
    def __init__(
        self,
        symbols: List[str],
        snapshots: Dict[str, Any],
        *,
        universe_error: Optional[Exception] = None,
    ) -> None:
        self._symbols = symbols
        self._snapshots = snapshots
        self.universe_error = universe_error
        self.universe_calls = 0
        self.snapshot_calls: List[tuple] = []

    def get_top_volume_symbols(self, limit: int):
        self.universe_calls += 1
        if self.universe_error is not None:
            raise self.universe_error
        return tuple(SymbolVolume(s, 100.0) for s in self._symbols[:limit])

    def get_market_snapshot(self, symbol: str, *, include_m5: bool = False):
        self.snapshot_calls.append((symbol, include_m5))
        value = self._snapshots.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


# -- analyzer ---------------------------------------------------------------

class _FakeContext:
    """Duck-typed stand-in for the pre-M5 AnalysisContext the orchestrator reads
    (only ``.htf`` and ``.signal`` are used)."""

    def __init__(self, htf, signal) -> None:
        self.htf = htf
        self.signal = signal


class _FakePipeline:
    def __init__(self, analyzer: "FakeAnalyzer") -> None:
        self._analyzer = analyzer
        self.runs: List[str] = []

    def run(self, context):
        symbol = context.snapshot.symbol
        self.runs.append(symbol)
        return _FakeContext(
            self._analyzer.htf_for(symbol), self._analyzer.pre_m5_for(symbol)
        )


class FakeAnalyzer:
    def __init__(
        self,
        signals: Dict[str, Any],
        *,
        htf_by_symbol: Optional[Dict[str, Any]] = None,
        pre_m5_by_symbol: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._signals = signals
        self._htf = htf_by_symbol or {}
        self._pre_m5 = pre_m5_by_symbol or {}
        self.pipeline = _FakePipeline(self)
        self.calls: List[tuple] = []

    def analyze(self, snapshot, *, prior: Optional[Signal] = None, session_active: bool = False):
        self.calls.append((snapshot.symbol, session_active))
        value = self._signals.get(snapshot.symbol)
        if isinstance(value, Exception):
            raise value
        return value

    def htf_for(self, symbol):
        """The fresh HTF the pipeline would expose. Explicit override, else the
        signal's candidate HTF (what the real context carries when scored)."""
        if symbol in self._htf:
            return self._htf[symbol]
        signal = self._signals.get(symbol)
        candidate = getattr(signal, "candidate", None)
        return candidate.htf if candidate is not None else None

    def pre_m5_for(self, symbol):
        """The pre-M5 context signal (defaults to the final signal — identical
        when no M5 enrichment applies)."""
        return self._pre_m5.get(symbol, self._signals.get(symbol))


# -- persistence ------------------------------------------------------------

class FakePersistence:
    def __init__(
        self,
        *,
        active: Optional[Dict[str, ActiveSignal]] = None,
        killed: Optional[Dict[str, KilledThesisRecord]] = None,
        load_error: Optional[Exception] = None,
        save_error: Optional[Exception] = None,
        append_error: Optional[Exception] = None,
    ) -> None:
        self._active_in = active or {}
        self._killed_in = killed or {}
        self.load_error = load_error
        self.save_error = save_error
        self.append_error = append_error
        self.saved_active: Optional[Dict[str, Any]] = None
        self.saved_killed: Optional[Dict[str, Any]] = None
        self.ob_audits: List[Any] = []
        self.ledger_records: List[Any] = []
        self.load_calls = 0
        self.save_calls = 0
        self.default_timestamps: List[Any] = []

    def load_active_signals(self, *, default_timestamp: Optional[float] = None):
        self.load_calls += 1
        self.default_timestamps.append(default_timestamp)
        if self.load_error is not None:
            raise self.load_error
        return dict(self._active_in)

    def load_killed_theses(self):
        if self.load_error is not None:
            raise self.load_error
        return dict(self._killed_in)

    def save_active_signals(self, signals):
        self.save_calls += 1
        if self.save_error is not None:
            raise self.save_error
        self.saved_active = dict(signals)

    def save_killed_theses(self, theses):
        if self.save_error is not None:
            raise self.save_error
        self.saved_killed = dict(theses)

    def append_ob_audit(self, record) -> None:
        if self.append_error is not None:
            raise self.append_error
        self.ob_audits.append(record)

    def append_signal_ledger(self, record) -> None:
        if self.append_error is not None:
            raise self.append_error
        self.ledger_records.append(record)


# -- writer sink ------------------------------------------------------------

class CapturingWriter:
    """A terminal sink that records every line, for asserting rendered output."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    def joined(self) -> str:
        return "\n".join(self.lines)


# -- signal builder with a selected OB (for OB-audit routing) ---------------

def retracement_ob_signal(
    symbol: str = "BTC/USDT",
    *,
    direction: str = "LONG",
    live_price: float = 100.0,
    sl: float = 95.0,
    tp: float = 115.0,
    entry_price: float = 100.0,
    h1_last_ts: float = 5000.0,
) -> Signal:
    """A scored RETRACEMENT signal whose entry is a selected OB — eligible for
    the OB audit."""
    signal, _ = make_signal(CycleSpec(
        symbol=symbol, decision="VALID", direction=direction,
        entry_mode="RETRACEMENT", regime="expansion_up", score=12,
        entry_ready=True, entry_price=entry_price, sl=sl, tp=tp,
        entry_source="retracement_ob", tp_source="SWING_HIGH",
        d1_bias="BULLISH", h4_bias="BULLISH", weekly_bias="BULLISH",
        location="discount", atr_last=1.0, volume_spike=True,
        live_price=live_price, h1_last_ts=h1_last_ts,
    ))
    ob = OrderBlock(
        index=5, rank_from_last=3, top=entry_price, bottom=entry_price - 2.0,
        size=2.0, fresh_status=True, passed_quality_filter=True,
        is_selected=True, size_atr=2.0, depth_from_price=0.5,
    )
    cand = dataclasses.replace(
        signal.candidate, order_blocks=(ob,), fresh_ob=True, fvg_valid=True
    )
    return dataclasses.replace(signal, candidate=cand)


# -- notifier ---------------------------------------------------------------

class FakeNotifier:
    def __init__(self, *, error: Optional[Exception] = None) -> None:
        self.error = error
        self.sent: List[Any] = []

    def send(self, alert) -> None:
        self.sent.append(alert)
        if self.error is not None:
            raise self.error


# -- reconciler spy ---------------------------------------------------------

class FakeReconciler:
    """Records reconcile() arguments and returns a canned decision — so a test
    can prove the orchestrator passes the analyzer's Signal (and the derived
    htf / live_price / h1_last_ts) straight through without owning any logic."""

    def __init__(self, decision: Optional[LifecycleDecision] = None) -> None:
        self.decision = decision if decision is not None else LifecycleDecision()
        self.calls: List[Dict[str, Any]] = []

    def reconcile(self, signal, memory, *, htf, live_price, h1_last_ts):
        self.calls.append(
            {
                "signal": signal,
                "memory": memory,
                "htf": htf,
                "live_price": live_price,
                "h1_last_ts": h1_last_ts,
            }
        )
        return self.decision
