"""infra.notifier — native notification backend (V2-standalone).

Architecture ref: 02-software-architecture.md §3 (`infra.notifier`: "format and
send outbound messages … Inputs: Alert objects … Must NOT know: scoring,
memory, decision logic, or *whether* an alert should fire. It sends what it's
told"), §8 (Alert contract), Principle 9 (decisions separated from execution).

This adapter is the ONLY V2 path to the notification backend. Consumers emit an
immutable domain `Alert`; the adapter routes it — by `Alert.kind` — to the
matching Telegram sender, then translates the result into a deterministic
`NotifierError`. The public seam is backend-agnostic (`Notifier.send(alert)` with
no Telegram terminology), so a future Discord/Slack/email backend implements the
same Protocol without any consumer changing.

Standalone note (Step 2 of the V2-standalone work): the Telegram senders and their
formatting helpers previously lived in `src/telegram_notifier.py`. They are moved
here **verbatim** — identical message text, identical formatting, identical
`sendMessage` transport (stdlib `urllib`) — so no notification differs by a single
byte. The only change is where the token/chat-id come from: `v2.infra.config`
instead of `src.config` (both read the same `.env`, so the values are identical).

Backend capability: Telegram `sendMessage` is text-only (no image/chart support),
so neither is this adapter — adding it would be new behavior.

Error handling: the senders return a bool (best-effort, as in V1). A delivery
failure (sender returns falsy) or a transport exception is translated into a
`NotifierError`; whether to swallow it to preserve best-effort delivery is the
orchestrator's decision.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Dict, Optional, Protocol, runtime_checkable
from urllib import error, parse, request

from v2.domain import Alert
from v2.infra.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

__all__ = ["Notifier", "TelegramNotifier", "NotifierError"]


# ==========================================================================
# Telegram senders + formatting — moved verbatim from src/telegram_notifier.py
# (only the config import source changed). Message text is byte-for-byte V1.
# ==========================================================================

TELEGRAM_API_BASE = "https://api.telegram.org"


def _is_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _safe_text(value: Any, default: str = "-") -> str:
    if value is None:
        return default
    if isinstance(value, float):
        if value != value:  # NaN check
            return default
        return f"{value}"
    return str(value)


def _fmt_price(value: Any) -> str:
    try:
        if value is None:
            return "-"
        v = float(value)
        if abs(v) >= 1000:
            return f"{v:,.2f}"
        if abs(v) >= 1:
            return f"{v:.4f}".rstrip("0").rstrip(".")
        return f"{v:.8f}".rstrip("0").rstrip(".")
    except Exception:
        return _safe_text(value)


def _fmt_pct(price: Any, ref: Any) -> str:
    try:
        p = float(price)
        r = float(ref)
        if r == 0:
            return "-"
        pct = abs((p - r) / r) * 100.0
        return f"{pct:.2f}%"
    except Exception:
        return "-"


def _fmt_rr(entry_price: Any, sl: Any, tp: Any) -> str:
    try:
        e = float(entry_price)
        s = float(sl)
        t = float(tp)
        risk = abs(s - e)
        reward = abs(t - e)
        if risk <= 0:
            return "-"
        return f"{reward / risk:.2f}"
    except Exception:
        return "-"


def _fmt_htf(htf: Optional[Dict[str, Any]]) -> str:
    if not htf:
        return ""
    weekly = _safe_text(htf.get("weekly"))
    d1 = _safe_text(htf.get("d1"))
    h4 = _safe_text(htf.get("h4"))
    loc = _safe_text(htf.get("loc"))
    regime = _safe_text(htf.get("regime"))
    direction = _safe_text(htf.get("dir"))
    return (
        f"HTF: W {weekly} | D1 {d1} | H4 {h4}\n"
        f"Loc: {loc} | Regime: {regime} | Dir: {direction}"
    )


def _post_message(text: str) -> bool:
    if not _is_enabled():
        print("⚠️ [TELEGRAM] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }

    data = parse.urlencode(payload).encode("utf-8")
    req = request.Request(url, data=data, method="POST")

    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(raw)
                return bool(parsed.get("ok", False))
            except Exception:
                return resp.status == 200
    except error.HTTPError as e:
        print(f"⚠️ [TELEGRAM] HTTPError: {e.code} {e.reason}")
    except error.URLError as e:
        print(f"⚠️ [TELEGRAM] URLError: {e.reason}")
    except Exception as e:
        print(f"⚠️ [TELEGRAM] Error: {e}")

    return False


def send_telegram_message(text: str) -> bool:
    if not text:
        return False
    return _post_message(text)


def send_signal_alert(
    *,
    symbol: str,
    decision: str,
    direction: str,
    score: float,
    entry_price: Any,
    sl: Any,
    tp: Any,
    regime: str,
    entry_mode: str,
    htf: Optional[Dict[str, Any]] = None,
    entry_source: str = "-",
    tp_source: str = "-",
    volume_spike: Any = None,
) -> bool:
    decision = str(decision).upper().strip()
    direction = str(direction).upper().strip()
    entry_mode = str(entry_mode).upper().strip()
    score_str = f"{float(score):.0f}" if isinstance(score, (int, float)) else _safe_text(score)

    if decision == "A+":
        header = "🟢 A+"
    elif decision == "VALID":
        header = "🔵 VALID"
    else:
        header = f"ℹ️ {decision}"

    mode_label = "CONT" if entry_mode == "CONTINUATION" else "RETR" if entry_mode == "RETRACEMENT" else entry_mode

    rr = _fmt_rr(entry_price, sl, tp)
    entry_s = _fmt_price(entry_price)
    sl_s = _fmt_price(sl)
    tp_s = _fmt_price(tp)

    entry_pct = _fmt_pct(entry_price, sl)
    tp_pct = _fmt_pct(tp, entry_price)
    entry_source = _safe_text(entry_source)
    tp_source = _safe_text(tp_source)
    volume_label = "YES" if bool(volume_spike) else "NO"

    lines = [
        f"{header} {mode_label}",
        f"{symbol} {direction}",
        f"Score: {score_str} | RR: {rr}",
        f"Regime: {regime}",
        f"Entry: {entry_s}",
        f"SL: {sl_s} ({entry_pct})",
        f"TP: {tp_s} ({tp_pct})",
        f"Entry Src: {entry_source}",
        f"TP Src: {tp_source}",
        f"Vol Spike: {volume_label}",
    ]

    htf_text = _fmt_htf(htf)
    if htf_text:
        lines.append(htf_text)

    lines.append("Structural hypothesis only. Operator decides execution.")

    return _post_message("\n".join(lines))


def send_upgrade_alert(
    *,
    symbol: str,
    direction: str,
    old_decision: str,
    new_decision: str,
    old_score: Any,
    new_score: Any,
    entry_price: Any,
    sl: Any,
    tp: Any,
    regime: str,
    entry_mode: str,
    entry_source: str = "-",
    tp_source: str = "-",
    volume_spike: Any = None,
) -> bool:
    direction = str(direction).upper().strip()
    old_decision = str(old_decision).upper().strip()
    new_decision = str(new_decision).upper().strip()
    entry_mode = str(entry_mode).upper().strip()
    mode_label = "CONT" if entry_mode == "CONTINUATION" else "RETR" if entry_mode == "RETRACEMENT" else entry_mode

    entry_source = _safe_text(entry_source)
    tp_source = _safe_text(tp_source)
    volume_label = "YES" if bool(volume_spike) else "NO"

    lines = [
        f"⬆️ UPGRADE {mode_label}",
        f"{symbol} {direction}",
        f"{old_decision} → {new_decision}",
        f"Score: {_safe_text(old_score)} → {_safe_text(new_score)}",
        f"Regime: {regime}",
        f"Entry: {_fmt_price(entry_price)}",
        f"SL: {_fmt_price(sl)}",
        f"TP: {_fmt_price(tp)}",
        f"Entry Src: {entry_source}",
        f"TP Src: {tp_source}",
        f"Vol Spike: {volume_label}",
        "New tier warrants re-evaluation.",
    ]

    return _post_message("\n".join(lines))


def send_kill_alert(
    *,
    symbol: str,
    direction: str,
    entry_price: Any,
    sl: Any,
    tp: Any,
    reason: str,
) -> bool:
    direction = str(direction).upper().strip()

    lines = [
        f"☠️ KILL",
        f"{symbol} {direction}",
        f"Reason: {reason}",
        f"Entry: {_fmt_price(entry_price)}",
        f"SL: {_fmt_price(sl)}",
        f"TP: {_fmt_price(tp)}",
        "Active hypothesis invalidated. Consider canceling any manual orders.",
    ]

    return _post_message("\n".join(lines))


# ==========================================================================
# The adapter (unchanged behavior — only the sender source is now in-module).
# ==========================================================================


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
    """Delivers alerts by routing to the native Telegram senders.

    The three sender functions are injected (defaulting to the native ones in
    this module), so tests supply fakes and never contact Telegram, and
    production wiring needs no knowledge of `src/`.
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
        """Route `alert` to the matching sender by kind; raise
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
        """Invoke a sender, translating any raised transport error."""
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
