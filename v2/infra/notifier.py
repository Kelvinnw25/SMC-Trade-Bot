"""infra.notifier — the anti-corruption layer over V1's notification backend.

Architecture ref: 02-software-architecture.md §3 (`infra.notifier`: "format and
send outbound messages … Inputs: Alert objects … Must NOT know: scoring,
memory, decision logic, or *whether* an alert should fire. It sends what it's
told"), §8 (Alert contract), Principle 9 (decisions separated from execution).

This adapter is the ONLY V2 path to the notification backend; no other V2 module
imports Telegram libraries or V1's notifier. Consumers emit an immutable domain
`Alert`; the adapter routes it — by `Alert.kind` — to the matching V1 sender in
`src/telegram_notifier.py`, reusing V1's exact message formatting. The public
seam is backend-agnostic (`Notifier.send(alert)` with no Telegram terminology),
so a future Discord/Slack/email backend implements the same Protocol without any
consumer changing.

Scope: this wraps the V1 *notification mechanism* (the three Telegram alerts).
Dashboard rendering and terminal LogEvent delivery — which §3 also groups under
the notifier — are separate presentation concerns handled during delivery wiring
in a later phase, not here.

Backend capability: V1's backend is text-only (Telegram `sendMessage`); it has
no image/chart support, so neither does this adapter — adding it would be new
behavior.

Error handling: V1's senders swallow failures and return a bool the caller
ignored. Here a delivery failure (sender returns falsy) or a transport exception
is translated into a deterministic `NotifierError`. This changes error
*surfacing* only, not message format; whether to swallow it to preserve V1's
best-effort delivery is the orchestrator's decision in a later phase.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from src.telegram_notifier import (
    send_kill_alert,
    send_signal_alert,
    send_upgrade_alert,
)
from v2.domain import Alert

__all__ = ["Notifier", "TelegramNotifier", "NotifierError"]


class NotifierError(Exception):
    """A notification could not be delivered. Raised in place of leaking a
    transport error or a silent falsy return, so consumers depend on a stable
    adapter-level failure type rather than the backend's internals."""


@runtime_checkable
class Notifier(Protocol):
    """The backend-agnostic notification contract consumers depend on.

    One method: dispatch a domain `Alert`. Implementations decide nothing about
    *whether* to send — they deliver what they are given. `TelegramNotifier`
    satisfies this today; a future backend only needs to provide `send`.
    """

    def send(self, alert: Alert) -> None:
        """Deliver `alert`, or raise `NotifierError` on failure."""
        ...


class TelegramNotifier:
    """Delivers alerts by routing to V1's `telegram_notifier` senders.

    The three V1 sender functions are injected (defaulting to the real ones), so
    tests supply fakes and never contact Telegram, and production wiring needs no
    knowledge of `src/`.
    """

    def __init__(
        self,
        *,
        signal_sender: Callable[..., bool] = send_signal_alert,
        upgrade_sender: Callable[..., bool] = send_upgrade_alert,
        kill_sender: Callable[..., bool] = send_kill_alert,
    ) -> None:
        self._signal_sender = signal_sender
        self._upgrade_sender = upgrade_sender
        self._kill_sender = kill_sender

    def send(self, alert: Alert) -> None:
        """Route `alert` to the matching V1 sender by kind; raise
        `NotifierError` on an unknown kind or a delivery failure."""
        if alert.kind == "new":
            delivered = self._call(self._signal_sender, self._signal_kwargs(alert))
        elif alert.kind == "upgrade":
            delivered = self._call(self._upgrade_sender, self._upgrade_kwargs(alert))
        elif alert.kind == "kill":
            delivered = self._call(self._kill_sender, self._kill_kwargs(alert))
        else:
            raise NotifierError(f"unknown alert kind: {alert.kind!r}")

        if not delivered:
            raise NotifierError(
                f"failed delivering {alert.kind} alert for {alert.symbol}"
            )

    @staticmethod
    def _call(sender: Callable[..., bool], kwargs: dict[str, Any]) -> bool:
        """Invoke a V1 sender, translating any raised transport error."""
        try:
            return bool(sender(**kwargs))
        except Exception as exc:
            raise NotifierError(f"notifier transport error: {exc}") from exc

    @staticmethod
    def _signal_kwargs(alert: Alert) -> dict[str, Any]:
        """Map a `new` Alert to `send_signal_alert`'s keyword arguments."""
        return {
            "symbol": alert.symbol,
            "decision": alert.decision,
            "direction": alert.direction,
            "score": alert.score,
            "entry_price": alert.entry_price,
            "sl": alert.sl,
            "tp": alert.tp,
            "regime": alert.regime,
            "entry_mode": alert.entry_mode,
            "htf": dict(alert.htf),
            "entry_source": alert.entry_source,
            "tp_source": alert.tp_source,
            "volume_spike": alert.volume_spike,
        }

    @staticmethod
    def _upgrade_kwargs(alert: Alert) -> dict[str, Any]:
        """Map an `upgrade` Alert to `send_upgrade_alert`'s keyword arguments."""
        return {
            "symbol": alert.symbol,
            "direction": alert.direction,
            "old_decision": alert.old_decision,
            "new_decision": alert.new_decision,
            "old_score": alert.old_score,
            "new_score": alert.new_score,
            "entry_price": alert.entry_price,
            "sl": alert.sl,
            "tp": alert.tp,
            "regime": alert.regime,
            "entry_mode": alert.entry_mode,
            "entry_source": alert.entry_source,
            "tp_source": alert.tp_source,
            "volume_spike": alert.volume_spike,
        }

    @staticmethod
    def _kill_kwargs(alert: Alert) -> dict[str, Any]:
        """Map a `kill` Alert to `send_kill_alert`'s keyword arguments."""
        return {
            "symbol": alert.symbol,
            "direction": alert.direction,
            "entry_price": alert.entry_price,
            "sl": alert.sl,
            "tp": alert.tp,
            "reason": alert.reason,
        }
