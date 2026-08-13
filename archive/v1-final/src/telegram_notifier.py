from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional
from urllib import parse, request, error

from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


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