"""Shared sample-object builders for the domain test suites.

Values are arbitrary but V1-plausible; only shapes/types matter here.
Not collected by unittest discovery (filename does not match test*.py).
"""
from __future__ import annotations

import pandas as pd

from v2.domain import (
    ActiveSignal,
    Alert,
    EntryPlan,
    EvictMutation,
    FairValueGap,
    HTFContext,
    IncrementMissMutation,
    InsertMutation,
    KilledThesisRecord,
    LifecycleDecision,
    LiquiditySweep,
    LogEvent,
    MarketSnapshot,
    OBAuditRecord,
    OrderBlock,
    PartialRefreshMutation,
    Regime,
    ReplaceMutation,
    RiskPlan,
    Signal,
    SignalCandidate,
    Swing,
)


def make_ohlcv_frame(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-07-01", periods=n, freq="h"),
        "open": [1.0 + i * 0.01 for i in range(n)],
        "high": [1.1 + i * 0.01 for i in range(n)],
        "low": [0.9 + i * 0.01 for i in range(n)],
        "close": [1.05 + i * 0.01 for i in range(n)],
        "v": [100.0] * n,
        "atr": [0.05] * n,
    })


def make_market_snapshot(**overrides) -> MarketSnapshot:
    kwargs = dict(
        symbol="BTC/USDT",
        d1=make_ohlcv_frame(),
        h1=make_ohlcv_frame(),
        h1_last_ts=1781287200.0,
        h1_last_ts_dt=pd.Timestamp("2026-07-01 04:00:00"),
        live_price=1.06,
    )
    kwargs.update(overrides)
    return MarketSnapshot(**kwargs)


def make_htf(**overrides) -> HTFContext:
    kwargs = dict(
        pdh=1.2, pdl=0.8, daily_eq=1.0,
        weekly_bias="BEARISH", d1_bias="BEARISH", h4_bias="BEARISH",
        location="premium", current_price=1.05, momentum_weakening=False,
        bull=0.0, bear=7.5, direction_source="weighted_bear",
        anchor_override=False, veto_active=False,
        pwh=1.3, pwl=0.7, weekly_eq=1.0, direction="SHORT",
    )
    kwargs.update(overrides)
    return HTFContext(**kwargs)


def make_regime(**overrides) -> Regime:
    kwargs = dict(label="expansion_down", entry_mode="CONTINUATION")
    kwargs.update(overrides)
    return Regime(**kwargs)


def make_swing(**overrides) -> Swing:
    kwargs = dict(index=10, price=1.11, kind="high")
    kwargs.update(overrides)
    return Swing(**kwargs)


def make_order_block(**overrides) -> OrderBlock:
    kwargs = dict(
        index=494, rank_from_last=2, top=111.77, bottom=110.64,
        size=1.1299999999999955, fresh_status=True,
        passed_quality_filter=True, is_selected=True,
        size_atr=0.7466478477547881, depth_from_price=0.3006413900251581,
    )
    kwargs.update(overrides)
    return OrderBlock(**kwargs)


def make_fvg(**overrides) -> FairValueGap:
    kwargs = dict(valid=True)
    kwargs.update(overrides)
    return FairValueGap(**kwargs)


def make_sweep(**overrides) -> LiquiditySweep:
    kwargs = dict(
        detected=True, index=498, ref_index=490, ref_price=1.2,
        sweep_depth=0.01, reclaim_strength=0.55,
    )
    kwargs.update(overrides)
    return LiquiditySweep(**kwargs)


def make_entry_plan(**overrides) -> EntryPlan:
    kwargs = dict(
        entry_mode="CONTINUATION", entry_price=1.05, entry_ready=True,
        entry_source="continuation_micro_pullback",
        entry_reason="continuation_micro_pullback",
        displacement_index=497, pullback_index=498,
    )
    kwargs.update(overrides)
    return EntryPlan(**kwargs)


def make_risk_plan(**overrides) -> RiskPlan:
    kwargs = dict(sl=1.08, tp=0.99, tp_source="EQL_cluster", tp_rr=2.0, rr=2.0)
    kwargs.update(overrides)
    return RiskPlan(**kwargs)


def make_candidate(**overrides) -> SignalCandidate:
    kwargs = dict(
        htf=make_htf(), regime=make_regime(), entry=make_entry_plan(),
        risk=make_risk_plan(), sweep=make_sweep(), fvg=make_fvg(),
        swing_highs=(make_swing(),), swing_lows=(make_swing(kind="low"),),
        order_blocks=(make_order_block(),),
        sweep_valid=True, fvg_valid=True, displacement_valid=True,
        fresh_ob=True, volume_spike=True, target_clear=True,
        session_active=True, htf_context_clean=True, atr_last=0.05,
    )
    kwargs.update(overrides)
    return SignalCandidate(**kwargs)


def make_signal(**overrides) -> Signal:
    kwargs = dict(
        symbol="BTC/USDT", score=12, decision="VALID",
        entry_mode="CONTINUATION", direction="SHORT",
        notes=("weekly_aligned", "displacement"), regime="expansion_down",
        candidate=make_candidate(),
    )
    kwargs.update(overrides)
    return Signal(**kwargs)


def make_active_signal(**overrides) -> ActiveSignal:
    kwargs = dict(
        direction="SHORT", score=12, decision="VALID",
        entry_price=67.03, sl=67.66679878018542, tp=65.9522662600618,
        regime="expansion_down", timestamp=1781301522.5820653,
        entry_mode="CONTINUATION", miss_count=1, status="WATCH",
        alert_sent_tier="VALID", alert_sent_ts=1781298098.9254858,
        last_miss_h1_ts=1781298000.0, last_ob_audit_h1_ts=0.0,
        entry_source="continuation_micro_pullback",
        signal_id="SOL/USDT|CONTINUATION|1781294400.0",
    )
    kwargs.update(overrides)
    return ActiveSignal(**kwargs)


def make_killed_thesis(**overrides) -> KilledThesisRecord:
    kwargs = dict(thesis_key="SOL/USDT|SHORT|CONTINUATION", killed_ts=1781298000.0)
    kwargs.update(overrides)
    return KilledThesisRecord(**kwargs)


def make_ob_audit(**overrides) -> OBAuditRecord:
    kwargs = dict(
        symbol="INTC/USDT", h1_last_ts=1780545600.0, direction="SHORT",
        entry_mode="RETRACEMENT", decision="WATCH", score=9,
        regime="accumulation", location="", market_price=111.18,
        entry_price=110.64, sl=112.82940170105435, tp=107.5180097668173,
        rr=1.4259558817731997, entry_source="retracement_ob",
        tp_source="EQL_cluster", volume_spike=False,
        window_bounds=None, ob_meta=(111.77, 110.64),
        ob_candidates=(make_order_block(),),
        fresh_ob=True, fvg_valid=False, current_price=None,
        entry_equals_ob_bottom=True,
    )
    kwargs.update(overrides)
    return OBAuditRecord(**kwargs)


def make_log_event(**overrides) -> LogEvent:
    kwargs = dict(event_type="SCAN", symbol="BTC/USDT",
                  details=(("score", 12), ("decision", "VALID")))
    kwargs.update(overrides)
    return LogEvent(**kwargs)


def make_alert(**overrides) -> Alert:
    kwargs = dict(
        kind="new", symbol="BTC/USDT", direction="SHORT",
        entry_price=1.05, sl=1.08, tp=0.99, dedup_key="VALID",
        decision="VALID", score=12.0, regime="expansion_down",
        entry_mode="CONTINUATION", entry_source="continuation_micro_pullback",
        tp_source="EQL_cluster", volume_spike=True,
        htf=(("weekly", "BEARISH"), ("d1", "BEARISH")),
    )
    kwargs.update(overrides)
    return Alert(**kwargs)


def make_lifecycle_decision(**overrides) -> LifecycleDecision:
    kwargs = dict(
        mutations=(
            InsertMutation(symbol="BTC/USDT", record=make_active_signal()),
            ReplaceMutation(target_symbol="ETH/USDT", symbol="BTC/USDT",
                            record=make_active_signal()),
            PartialRefreshMutation(symbol="BTC/USDT", score=13.0,
                                   decision="VALID", entry_price=1.05, sl=1.08,
                                   tp=0.99, regime="expansion_down",
                                   timestamp=1781301522.0),
            IncrementMissMutation(symbol="BTC/USDT", h1_last_ts=1781298000.0),
            EvictMutation(symbol="BTC/USDT", reason="decay"),
        ),
        alerts=(make_alert(),),
        log_events=(make_log_event(),),
        killed_theses=(make_killed_thesis(),),
    )
    kwargs.update(overrides)
    return LifecycleDecision(**kwargs)
