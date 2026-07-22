"""Unit tests for v2.infra.notifier — the V1 notification adapter (Phase 2.4).

V1's Telegram senders are replaced by injected recording fakes; no network call
is ever made. Covers routing per alert kind, exact kwargs mapping, htf
translation, failure and exception translation, unknown kind, success, Protocol
compatibility, and dependency isolation.
"""
from __future__ import annotations

import unittest
from typing import Any, Optional

from v2.domain import Alert
from v2.infra.notifier import Notifier, NotifierError, TelegramNotifier


class RecordingSender:
    """A stand-in for a V1 sender: records kwargs, returns a preset result or
    raises a preset exception."""

    def __init__(self, result: bool = True, raises: Optional[Exception] = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result
        self._raises = raises

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result


def build(**senders) -> tuple[TelegramNotifier, RecordingSender, RecordingSender, RecordingSender]:
    signal = senders.get("signal", RecordingSender())
    upgrade = senders.get("upgrade", RecordingSender())
    kill = senders.get("kill", RecordingSender())
    notifier = TelegramNotifier(
        signal_sender=signal, upgrade_sender=upgrade, kill_sender=kill
    )
    return notifier, signal, upgrade, kill


def new_alert(**overrides) -> Alert:
    base = dict(
        kind="new", symbol="BTC/USDT", direction="SHORT",
        entry_price=1.05, sl=1.08, tp=0.99, dedup_key="VALID",
        decision="VALID", score=12.0, regime="expansion_down",
        entry_mode="CONTINUATION", entry_source="continuation_micro_pullback",
        tp_source="EQL_cluster", volume_spike=True,
        htf=(("weekly", "BEARISH"), ("d1", "BEARISH"), ("dir", "SHORT")),
    )
    base.update(overrides)
    return Alert(**base)


def upgrade_alert(**overrides) -> Alert:
    base = dict(
        kind="upgrade", symbol="BTC/USDT", direction="SHORT",
        entry_price=1.05, sl=1.08, tp=0.99, dedup_key="A+",
        old_decision="VALID", new_decision="A+", old_score=12.0, new_score=15.0,
        regime="expansion_down", entry_mode="CONTINUATION",
        entry_source="continuation_micro_pullback", tp_source="EQL_cluster",
        volume_spike=True,
    )
    base.update(overrides)
    return Alert(**base)


def kill_alert(**overrides) -> Alert:
    base = dict(
        kind="kill", symbol="BTC/USDT", direction="SHORT",
        entry_price=1.05, sl=1.08, tp=0.99, reason="Emergency invalidation triggered",
    )
    base.update(overrides)
    return Alert(**base)


class TestRouting(unittest.TestCase):
    def test_new_routes_to_signal_sender_only(self):
        notifier, signal, upgrade, kill = build()
        notifier.send(new_alert())
        self.assertEqual(len(signal.calls), 1)
        self.assertEqual(upgrade.calls, [])
        self.assertEqual(kill.calls, [])

    def test_upgrade_routes_to_upgrade_sender_only(self):
        notifier, signal, upgrade, kill = build()
        notifier.send(upgrade_alert())
        self.assertEqual(len(upgrade.calls), 1)
        self.assertEqual(signal.calls, [])
        self.assertEqual(kill.calls, [])

    def test_kill_routes_to_kill_sender_only(self):
        notifier, signal, upgrade, kill = build()
        notifier.send(kill_alert())
        self.assertEqual(len(kill.calls), 1)
        self.assertEqual(signal.calls, [])
        self.assertEqual(upgrade.calls, [])


class TestKwargsMapping(unittest.TestCase):
    def test_new_alert_kwargs(self):
        notifier, signal, _, _ = build()
        notifier.send(new_alert())
        kw = signal.calls[0]
        self.assertEqual(kw["symbol"], "BTC/USDT")
        self.assertEqual(kw["decision"], "VALID")
        self.assertEqual(kw["direction"], "SHORT")
        self.assertEqual(kw["score"], 12.0)
        self.assertEqual(kw["entry_price"], 1.05)
        self.assertEqual(kw["sl"], 1.08)
        self.assertEqual(kw["tp"], 0.99)
        self.assertEqual(kw["regime"], "expansion_down")
        self.assertEqual(kw["entry_mode"], "CONTINUATION")
        self.assertEqual(kw["entry_source"], "continuation_micro_pullback")
        self.assertEqual(kw["tp_source"], "EQL_cluster")
        self.assertEqual(kw["volume_spike"], True)

    def test_htf_tuple_converted_to_dict(self):
        notifier, signal, _, _ = build()
        notifier.send(new_alert())
        self.assertEqual(
            signal.calls[0]["htf"],
            {"weekly": "BEARISH", "d1": "BEARISH", "dir": "SHORT"},
        )

    def test_upgrade_alert_kwargs(self):
        notifier, _, upgrade, _ = build()
        notifier.send(upgrade_alert())
        kw = upgrade.calls[0]
        self.assertEqual(kw["old_decision"], "VALID")
        self.assertEqual(kw["new_decision"], "A+")
        self.assertEqual(kw["old_score"], 12.0)
        self.assertEqual(kw["new_score"], 15.0)
        self.assertNotIn("htf", kw)  # V1 send_upgrade_alert takes no htf

    def test_kill_alert_kwargs(self):
        notifier, _, _, kill = build()
        notifier.send(kill_alert())
        kw = kill.calls[0]
        self.assertEqual(
            set(kw), {"symbol", "direction", "entry_price", "sl", "tp", "reason"}
        )
        self.assertEqual(kw["reason"], "Emergency invalidation triggered")


class TestErrorTranslation(unittest.TestCase):
    def test_delivery_failure_raises(self):
        notifier, _, _, _ = build(signal=RecordingSender(result=False))
        with self.assertRaises(NotifierError):
            notifier.send(new_alert())

    def test_sender_exception_translated(self):
        notifier, _, _, _ = build(signal=RecordingSender(raises=RuntimeError("boom")))
        with self.assertRaises(NotifierError):
            notifier.send(new_alert())

    def test_unknown_kind_raises(self):
        notifier, signal, upgrade, kill = build()
        with self.assertRaises(NotifierError):
            notifier.send(new_alert(kind="bogus"))
        # Nothing was dispatched.
        self.assertEqual(signal.calls, [])
        self.assertEqual(upgrade.calls, [])
        self.assertEqual(kill.calls, [])


class TestInterfaceBehavior(unittest.TestCase):
    def test_successful_send_returns_none(self):
        notifier, _, _, _ = build()
        self.assertIsNone(notifier.send(new_alert()))

    def test_satisfies_notifier_protocol(self):
        notifier, _, _, _ = build()
        self.assertIsInstance(notifier, Notifier)

    def test_arbitrary_object_is_not_a_notifier(self):
        self.assertNotIsInstance(object(), Notifier)

    def test_dependency_isolation_only_injected_senders_used(self):
        # The adapter touches only the injected fakes — the test never contacts
        # Telegram, proving all backend access flows through the injection seam.
        notifier, signal, upgrade, kill = build()
        notifier.send(new_alert())
        notifier.send(upgrade_alert())
        notifier.send(kill_alert())
        self.assertEqual(len(signal.calls), 1)
        self.assertEqual(len(upgrade.calls), 1)
        self.assertEqual(len(kill.calls), 1)


if __name__ == "__main__":
    unittest.main()
