import os
import sys
import json
import time
import pandas as pd
import numpy as np
import traceback
from src.config import (
    TOP_COINS_LIMIT,
    ACTIVE_POIS_FILE,
    MAX_OPEN_POSITIONS,
    POI_UNTAPPED_TTL_SECONDS,
    POI_TAPPED_TTL_SECONDS,
    MANUAL_ACTION_TTL_SECONDS,
)
from datetime import datetime

from src.api_handler import BinanceHandler
from src.strategy import SMCStrategy
from src.config import TOP_COINS_LIMIT

from src.telegram_notifier import (
    send_signal_alert,
    send_upgrade_alert,
    send_kill_alert,
)

STATE_FILE = "active_pois.json"
MISS_COUNT_LIMIT = 3
KILLED_THESIS_FILE = "killed_thesis.json"
KILL_COOLDOWN_SECONDS = 60 * 60  # 1 closed H1 candle
def _alert_rank(tier: str) -> int:
    ranks = {
        "": 0,
        "VALID": 1,
        "A+": 2,
    }
    return ranks.get(str(tier).upper(), 0)


def _alert_tier(decision: str) -> str:
    decision = str(decision).upper()

    if decision == "VALID":
        return "VALID"

    if decision == "A+":
        return "A+"

    return ""

def _log_decay(symbol: str, active_signals: dict, entry_mode: str, reason: str) -> None:
    signal = active_signals.get(symbol, {})
    from_decision = str(signal.get("decision", "-")).upper().strip()
    miss_count = int(signal.get("miss_count", 0))

    print(
        f"   [⏳ DECAY] {symbol:<14} | "
        f"from={from_decision} | "
        f"mode={str(entry_mode).upper().strip()} | "
        f"reason={reason} | "
        f"miss_count={miss_count}"
    )

def load_memory():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            raw = json.load(f)

        validated = {}
        required_fields = ["score", "decision", "entry_price", "sl", "tp", "direction"]

        for symbol, data in raw.items():
            try:
                if not all(k in data for k in required_fields):
                    continue

                validated[symbol] = {
                    "direction": str(data["direction"]),
                    "score": float(data["score"]),
                    "decision": str(data["decision"]),
                    "entry_price": float(data["entry_price"]),
                    "sl": float(data["sl"]),
                    "tp": float(data["tp"]),
                    "regime": str(data.get("regime", "unknown")),
                    "timestamp": float(data.get("timestamp", time.time())),
                    "miss_count": int(data.get("miss_count", 0)),
                    "last_miss_h1_ts": float(data.get("last_miss_h1_ts", 0.0)),
                    "status": str(data.get("status", "WATCH")),
                    "last_ob_audit_h1_ts": float(data.get("last_ob_audit_h1_ts", 0.0)),
                    "entry_mode": str(data.get("entry_mode", "RETRACEMENT")),

                    # Telegram metadata
                    "alert_sent_tier": str(
                        data.get("alert_sent_tier", data.get("decision", ""))
                    ),
                    "alert_sent_ts": float(data.get("alert_sent_ts", 0.0)),
                }
            except Exception:
                continue

        print(f"📂 [SYSTEM] Loaded {len(validated)} active signal(s).")
        return validated

    except Exception as e:
        print(f"⚠️ Failed loading memory: {e}")
        return {}
    
def load_killed_thesis():
    if not os.path.exists(KILLED_THESIS_FILE):
        return {}

    try:
        with open(KILLED_THESIS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_killed_thesis(memory):
    try:
        temp_file = f"{KILLED_THESIS_FILE}.tmp"

        with open(temp_file, "w") as f:
            json.dump(memory, f, indent=4)

        os.replace(temp_file, KILLED_THESIS_FILE)

    except Exception as e:
        print(f"⚠️ Failed saving killed thesis memory: {e}")

OB_AUDIT_FILE = "retracement_ob_audit.jsonl"

def append_ob_audit_record(record: dict) -> None:
    try:
        with open(OB_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ Failed writing OB audit: {e}")

def save_memory(memory):
    try:
        temp_file = f"{STATE_FILE}.tmp"
        with open(temp_file, "w") as f:
            json.dump(memory, f, indent=4)
        os.replace(temp_file, STATE_FILE)
    except Exception as e:
        print(f"⚠️ Failed saving memory: {e}")

def append_ob_audit_record(record):
    try:
        with open(OB_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ Failed writing OB audit: {e}")

def _to_epoch_ts(value) -> float:
    try:
        if value is None:
            return 0.0
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            return 0.0
        return float(ts.timestamp())
    except Exception:
        return 0.0

def cleanup_expired_signals(active_signals: dict) -> dict:
    """
    Expire stale signals using TTL rules.

    Rules:
    - manual_action signals use MANUAL_ACTION_TTL_SECONDS
    - tapped signals use POI_TAPPED_TTL_SECONDS
    - untapped signals use POI_UNTAPPED_TTL_SECONDS

    Since this radar is lightweight and does not track realtime price touches here,
    we use miss_count > 0 as a practical proxy for a tapped/active signal state.
    """
    now = time.time()
    to_delete = []

    for symbol, data in active_signals.items():
        created_at = float(data.get("timestamp", now))
        age_seconds = now - created_at
        miss_count = int(data.get("miss_count", 0))
        status = str(data.get("status", "")).upper().strip()

        if status == "MANUAL_ACTION":
            ttl = MANUAL_ACTION_TTL_SECONDS
        elif miss_count > 0:
            ttl = POI_TAPPED_TTL_SECONDS
        else:
            ttl = POI_UNTAPPED_TTL_SECONDS

        if age_seconds > ttl:
            to_delete.append(symbol)
            print(
                f"   [⏰ TTL EXPIRED] {symbol:<14} | "
                f"Age: {age_seconds/3600:.1f}h | TTL: {ttl/3600:.1f}h"
            )

    for symbol in to_delete:
        del active_signals[symbol]

    return active_signals

def print_dashboard(active_signals):
    print("\n" + "=" * 110)
    print("📋 RADAR SUMMARY")
    print("=" * 110)

    def _display_label(data: dict) -> str:
        decision = str(data.get("decision", "SKIP")).upper()
        entry_mode = str(data.get("entry_mode", "RETRACEMENT")).upper()

        if decision == "WATCH":
            if entry_mode == "CONTINUATION":
                return "WATCH-CONT"
            if entry_mode == "RETRACEMENT":
                return "WATCH-RETR"
            if entry_mode == "WATCH_ONLY":
                return "WATCH-COMP"
        return decision

    if not active_signals:
        print("   [∅] No active setups.")
        print("=" * 110)
        return

    sorted_signals = sorted(active_signals.items(), key=lambda x: x[1]["score"], reverse=True)

    for symbol, data in sorted_signals:
        label = _display_label(data)
        print(
            f"   ➤ {symbol:<14}| "
            f"{data['direction']:<5} | "
            f"{label:<10} | "
            f"Score: {data['score']:<4} | "
            f"Entry: {data['entry_price']:.4f} | "
            f"SL: {data['sl']:.4f} | "
            f"TP: {data['tp']:.4f} | "
            f"Miss: {data['miss_count']}"
        )

    print("=" * 110)

def _is_same_thesis(existing: dict, direction: str, entry_mode: str) -> bool:
    return (
        str(existing.get("direction", "")).upper() == direction.upper()
        and str(existing.get("entry_mode", "")).upper() == entry_mode.upper()
    )

def run():
    print("=" * 110)
    print("🚀 SMC SIGNAL RADAR - V.FINAL")
    print("   HTF Context Engine + M5 Enrichment + Kill Switch")
    print("=" * 110)

    api = BinanceHandler()
    strategy = SMCStrategy()
    active_signals = load_memory()
    killed_thesis = load_killed_thesis()
    ob_audit_seen = set()

    while True:
        try:
            print(f"\n[{time.strftime('%H:%M:%S')}] 🔍 Fetching Top {TOP_COINS_LIMIT} Volume Pairs...")
            top_coins = api.get_top_volume_coins(limit=TOP_COINS_LIMIT)

            if not top_coins:
                print("⚠️ No coins received.")
                time.sleep(30)
                continue

            for coin in top_coins:
                symbol = coin["symbol"]
                try:
                    time.sleep(0.20)
                    df_d1_raw = api.fetch_data_with_atr(symbol=symbol, timeframe="1d", limit=120)
                    time.sleep(0.20)
                    df_h1_raw = api.fetch_data_with_atr(symbol=symbol, timeframe="1h", limit=500)

                    if df_d1_raw is None or df_h1_raw is None or df_d1_raw.empty or df_h1_raw.empty:
                        continue

                    df_d1 = df_d1_raw.iloc[:-1].copy()
                    df_h1 = df_h1_raw.iloc[:-1].copy()

                    if len(df_d1) < 10 or len(df_h1) < 50:
                        continue

                    h1_last_ts_dt = pd.to_datetime(df_h1["timestamp"].iloc[-1])
                    h1_last_ts = _to_epoch_ts(df_h1["timestamp"].iloc[-1])

                    # =====================================================
                    # STAGE 1 → H1 / HTF CONTEXT
                    # =====================================================
                    context = strategy.build_signal_context(df_d1, df_h1)
                    if context is None or "result" not in context:
                        continue

                    # =====================================================
                    # LIVE MARKET PRICE (FORMING H1 CANDLE)
                    # =====================================================
                    context["market_price"] = float(df_h1_raw["close"].iloc[-1])

                    result = context["result"]
                    debug = context.get("debug", {})
                    h1_setup = context.get("h1_setup", {})

                    # =====================================================
                    # EMERGENCY INVALIDATION CHECK (KILL SWITCH)
                    # =====================================================
                    if symbol in active_signals:
                        signal_dead = strategy.is_poi_invalidated(active_signals[symbol], context)
                        if signal_dead:
                            previous = active_signals[symbol]

                            print(
                                f"   [☠️ KILL] {symbol:<14} | Emergency invalidation triggered."
                            )

                            prev_tier = str(previous.get("alert_sent_tier", "")).upper().strip()

                            if _alert_rank(prev_tier) > 0:
                                send_kill_alert(
                                    symbol=symbol,
                                    direction=previous.get("direction", "NONE"),
                                    entry_price=previous.get("entry_price", 0.0),
                                    sl=previous.get("sl", 0.0),
                                    tp=previous.get("tp", 0.0),
                                    reason="Emergency invalidation triggered",
                                )

                                print(f"   [📱 ALERTED] {symbol:<14} | KILL")

                            thesis_key = (
                                f"{symbol}|"
                                f"{previous.get('direction','')}|"
                                f"{previous.get('entry_mode','')}"
                            )

                            killed_thesis[thesis_key] = time.time()
                            del active_signals[symbol]
                            continue

                    # =====================================================
                    # STAGE 2 → M5 ENRICHMENT
                    # =====================================================
                    if result["score"] >= 8:
                        time.sleep(0.20)
                        df_m5_raw = api.fetch_data_with_atr(symbol=symbol, timeframe="5m", limit=200)

                        if df_m5_raw is not None and not df_m5_raw.empty and len(df_m5_raw) >= 20:
                            df_m5 = df_m5_raw.iloc[:-1].copy()
                            if not df_m5.empty:
                                m5_last_ts = pd.to_datetime(df_m5["timestamp"].iloc[-1])
                                if m5_last_ts.floor("h") <= h1_last_ts_dt.floor("h"):
                                    result = strategy.enrich_with_m5(context, df_m5)
                                    context["result"] = result
                                    context.setdefault("debug", {})["final_result"] = {
                                        "score": result.get("score"),
                                        "decision": result.get("decision"),
                                        "entry_mode": result.get("entry_mode"),
                                        "m5_choch": result.get("m5_choch"),
                                        "m5_reason": result.get("m5_reason"),
                                    }

                    # =====================================================
                    # ATOMIC LOG BLOCK PER SYMBOL
                    # =====================================================
                    htf_dbg = debug.get("htf", {})
                    conf_dbg = debug.get("conf", {})
                    score_raw = debug.get("score_raw")

                    reason = result.get("reason", "-")
                    entry_mode = str(result.get("entry_mode") or h1_setup.get("entry_mode", "RETRACEMENT")).upper()
                    decision = str(result.get("decision", "SKIP")).upper()

                    if decision == "WATCH":
                        if entry_mode == "CONTINUATION":
                            decision_label = "WATCH-CONT"
                        elif entry_mode == "RETRACEMENT":
                            decision_label = "WATCH-RETR"
                        elif entry_mode == "WATCH_ONLY":
                            decision_label = "WATCH-COMP"
                        else:
                            decision_label = "WATCH"
                    else:
                        decision_label = decision

                    log_lines = []
                    log_lines.append(
                        f"   [SCAN] {symbol:<14}"
                        f"| Score: {result.get('score', 0):<3} "
                        f"| {decision_label:<10} "
                        f"| Reason: {reason}"
                    )

                    if htf_dbg:
                        log_lines.append(
                            f"      HTF  => "
                            f"W:{htf_dbg.get('weekly')} "
                            f"| D1:{htf_dbg.get('d1')} "
                            f"| H4:{htf_dbg.get('h4')} "
                            f"| LOC:{htf_dbg.get('loc')} "
                            f"| REGIME:{htf_dbg.get('regime')} "
                            f"| H1_FB:{htf_dbg.get('h1_fb')} "
                            f"| SRC:{htf_dbg.get('src')} "
                            f"| DIR:{htf_dbg.get('dir')} "
                            f"| BULL:{htf_dbg.get('bull')} "
                            f"| BEAR:{htf_dbg.get('bear')}"
                        )

                    if conf_dbg:
                        log_lines.append(
                            f"      CONF => "
                            f"sweep={conf_dbg.get('sweep')} "
                            f"| fvg={conf_dbg.get('fvg')} "
                            f"| disp={conf_dbg.get('disp')} "
                            f"| vol={conf_dbg.get('vol')} "
                            f"| ob={conf_dbg.get('ob')} "
                            f"| target={conf_dbg.get('target')} "
                            f"| mode={conf_dbg.get('mode')} "
                            f"| ready={conf_dbg.get('ready')}"
                        )

                    if score_raw and score_raw.get("score") is not None:
                        log_lines.append(
                            f"      RAW  => "
                            f"type={score_raw.get('type')} "
                            f"| score={score_raw.get('score')} "
                            f"| decision={score_raw.get('decision')} "
                            f"| mode={score_raw.get('entry_mode')}"
                        )

                    print("\n".join(log_lines))

                    # =====================================================
                    # PRE-MEMORY INVALIDATION CHECK
                    # =====================================================
                    temp_signal = {
                        "direction": result.get("direction"),
                        "entry_price": result.get("entry_price"),
                        "sl": result.get("sl"),
                        "tp": result.get("tp"),
                    }

                    signal_dead_now = strategy.is_poi_invalidated(temp_signal, context)

                    if signal_dead_now:
                        print(
                            f"   [⚠️ STALE] {symbol:<14} | "
                            f"Setup invalidated by live market price."
                        )
                        continue
                    
                    # =====================================================
                    # OUTPUT & MEMORY ENGINE
                    # =====================================================
                    score = result.get("score", 0)
                    decision = result.get("decision", "SKIP")
                    direction = result.get("direction", "NONE")

                    entry_price = float(result.get("entry_price", 0.0))
                    sl = float(result.get("sl", 0.0))
                    tp = float(result.get("tp", 0.0))

                    levels_valid = (
                        np.isfinite(entry_price) and
                        np.isfinite(sl) and
                        np.isfinite(tp) and
                        entry_price > 0.0 and
                        sl > 0.0 and
                        tp > 0.0 and
                        entry_price != sl and
                        entry_price != tp
                    )

                    ob_audit = debug.get("ob_audit", {})

                    if (
                        entry_mode == "RETRACEMENT"
                        and str(result.get("entry_source", "")).strip() == "retracement_ob"
                        and levels_valid
                    ):
                        audit_key = (symbol, h1_last_ts, entry_mode)

                        if audit_key not in ob_audit_seen:
                            audit_record = {
                                "symbol": symbol,
                                "h1_last_ts": h1_last_ts,
                                "direction": direction,
                                "entry_mode": entry_mode,
                                "decision": decision,
                                "score": score,
                                "regime": str(result.get("regime", "unknown")),
                                "location": str(result.get("location", "")),
                                "market_price": float(context.get("market_price", 0.0)),
                                "entry_price": entry_price,
                                "sl": sl,
                                "tp": tp,
                                "rr": (abs(tp - entry_price) / abs(entry_price - sl)) if abs(entry_price - sl) > 0 else None,
                                "entry_source": str(result.get("entry_source", "-")),
                                "tp_source": str(result.get("tp_source", "-")),
                                "volume_spike": bool(h1_setup.get("volume_spike", False)),
                                "window_bounds": ob_audit.get("window_bounds"),
                                "ob_meta": ob_audit.get("ob_meta"),
                                "ob_candidates": ob_audit.get("ob_candidates", []),
                                "selected_ob_index": ob_audit.get("selected_ob_index"),
                                "selected_ob_rank": ob_audit.get("selected_ob_rank"),
                                "selected_ob_top": ob_audit.get("selected_ob_top"),
                                "selected_ob_bottom": ob_audit.get("selected_ob_bottom"),
                                "selected_ob_size": ob_audit.get("selected_ob_size"),
                                "fresh_ob": ob_audit.get("fresh_ob"),
                                "fvg_valid": ob_audit.get("fvg_valid"),
                                "current_price": ob_audit.get("current_price"),
                                "entry_equals_ob_bottom": ob_audit.get("entry_equals_ob_bottom"),
                            }

                            append_ob_audit_record(audit_record)
                            ob_audit_seen.add(audit_key)

                    if decision in ["WATCH", "VALID", "A+"] and levels_valid:

                        signal_payload = {
                            "direction": direction,
                            "score": score,
                            "decision": decision,
                            "entry_price": entry_price,
                            "sl": sl,
                            "tp": tp,
                            "regime": str(result.get("regime", "unknown")),
                            "timestamp": time.time(),
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

                            previous_alert_ts = float(
                                previous_signal.get("alert_sent_ts", 0.0)
                            )

                        current_alert_tier = _alert_tier(decision)

                        thesis_key = f"{symbol}|{direction}|{entry_mode}"

                        killed_ts = float(
                            killed_thesis.get(thesis_key, 0.0)
                        )

                        if killed_ts > 0:

                            cooldown_age = time.time() - killed_ts

                            if cooldown_age < KILL_COOLDOWN_SECONDS:

                                remaining = (
                                    KILL_COOLDOWN_SECONDS
                                    - cooldown_age
                                ) / 60

                                print(
                                    f"   [🚫 KILL COOLDOWN] "
                                    f"{symbol:<14} | "
                                    f"{entry_mode} | "
                                    f"{remaining:.0f}m remaining"
                                )

                                continue

                        existing = active_signals.get(symbol)

                        same_thesis = False
                        existing_tier = ""

                        if existing:

                            same_thesis = _is_same_thesis(
                                existing,
                                direction,
                                entry_mode
                            )

                            existing_tier = str(
                                existing.get("alert_sent_tier", "")
                            ).upper().strip()

                            if (
                                same_thesis
                                and _alert_rank(current_alert_tier)
                                <= _alert_rank(existing_tier)
                            ):
                                print(
                                    f"   [🟰 SAME THESIS] {symbol:<14} | "
                                    f"{entry_mode} | {direction} | "
                                    f"existing={existing_tier} "
                                    f"current={current_alert_tier}"
                                )

                                # refresh state
                                active_signals[symbol]["score"] = max(
                                    score,
                                    active_signals[symbol].get("score", score)
                                )

                                # never downgrade decision
                                if _alert_rank(current_alert_tier) > _alert_rank(existing_tier):
                                    active_signals[symbol]["decision"] = decision

                                active_signals[symbol]["entry_price"] = entry_price
                                active_signals[symbol]["sl"] = sl
                                active_signals[symbol]["tp"] = tp
                                active_signals[symbol]["regime"] = str(
                                    result.get("regime", "unknown")
                                )
                                active_signals[symbol]["timestamp"] = time.time()

                                continue

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
                            weakest_symbol = min(active_signals, key=lambda s: active_signals[s]["score"])
                            weakest_score = active_signals[weakest_symbol]["score"]

                            if score > weakest_score:
                                print(
                                    f"   [🔄 REPLACE] Slot penuh. "
                                    f"{weakest_symbol} (score={weakest_score}) diganti "
                                    f"{symbol} (score={score})"
                                )
                                del active_signals[weakest_symbol]
                                active_signals[symbol] = signal_payload
                            else:
                                print(
                                    f"   [🚫 CAPPED] {symbol:<14} | "
                                    f"Score={score} tidak cukup kuat "
                                    f"untuk masuk (weakest={weakest_score})"
                                )

                        # =====================================================
                        # TELEGRAM ALERT DISPATCH
                        # =====================================================
                        if symbol in active_signals and current_alert_tier:

                            if previous_alert_tier == "":
                                send_signal_alert(
                                    symbol=symbol,
                                    decision=decision,
                                    direction=direction,
                                    score=score,
                                    entry_price=entry_price,
                                    sl=sl,
                                    tp=tp,
                                    regime=str(result.get("regime", "unknown")),
                                    entry_mode=entry_mode,
                                    htf=htf_dbg,
                                    entry_source=str(result.get("entry_source", h1_setup.get("entry_source", "-"))),
                                    tp_source=str(result.get("tp_source", h1_setup.get("tp_source", "-"))),
                                    volume_spike=bool(h1_setup.get("volume_spike", False)),
                                )

                                active_signals[symbol]["alert_sent_tier"] = current_alert_tier
                                active_signals[symbol]["alert_sent_ts"] = time.time()

                                print(f"   [📱 ALERTED] {symbol:<14} | {current_alert_tier}")

                            elif _alert_rank(current_alert_tier) > _alert_rank(previous_alert_tier):
                                send_upgrade_alert(
                                    symbol=symbol,
                                    direction=direction,
                                    old_decision=previous_alert_tier,
                                    new_decision=current_alert_tier,
                                    old_score=previous_signal.get("score", score) if previous_signal else score,
                                    new_score=score,
                                    entry_price=entry_price,
                                    sl=sl,
                                    tp=tp,
                                    regime=str(result.get("regime", "unknown")),
                                    entry_mode=entry_mode,
                                    entry_source=str(result.get("entry_source", h1_setup.get("entry_source", "-"))),
                                    tp_source=str(result.get("tp_source", h1_setup.get("tp_source", "-"))),
                                    volume_spike=bool(h1_setup.get("volume_spike", False)),
                                )

                                active_signals[symbol]["alert_sent_tier"] = current_alert_tier
                                active_signals[symbol]["alert_sent_ts"] = time.time()

                                print(
                                    f"   [📱 ALERTED] {symbol:<14} | "
                                    f"{previous_alert_tier} -> {current_alert_tier}"
                                )

                    elif decision in ["WATCH", "VALID", "A+"] and not levels_valid:

                        print(
                            f"   [⚠️ GUARD] {symbol:<14} | "
                            f"{decision} blocked — invalid levels "
                            f"(entry={entry_price}, sl={sl}, tp={tp})"
                        )

                        if symbol in active_signals:
                            prev_last_miss = float(active_signals[symbol].get("last_miss_h1_ts", 0.0))

                            if h1_last_ts > prev_last_miss:
                                active_signals[symbol]["miss_count"] += 1
                                active_signals[symbol]["last_miss_h1_ts"] = h1_last_ts

                                if active_signals[symbol]["miss_count"] >= MISS_COUNT_LIMIT:
                                    _log_decay(
                                        symbol=symbol,
                                        active_signals=active_signals,
                                        entry_mode=entry_mode,
                                        reason="invalid_levels",
                                    )
                                    del active_signals[symbol]

                    else:
                        if symbol in active_signals:
                            prev_last_miss = float(active_signals[symbol].get("last_miss_h1_ts", 0.0))

                            if h1_last_ts > prev_last_miss:
                                active_signals[symbol]["miss_count"] += 1
                                active_signals[symbol]["last_miss_h1_ts"] = h1_last_ts

                                if active_signals[symbol]["miss_count"] >= MISS_COUNT_LIMIT:
                                    _log_decay(
                                        symbol=symbol,
                                        active_signals=active_signals,
                                        entry_mode=entry_mode,
                                        reason=str(reason),
                                    )
                                    del active_signals[symbol]

                except Exception as coin_err:
                    print(f"⚠️ [{symbol}] Error: {coin_err}")
                    traceback.print_exc()

            active_signals = cleanup_expired_signals(active_signals)
            now = time.time()

            killed_thesis = {
                k: ts
                for k, ts in killed_thesis.items()
                if now - float(ts) < KILL_COOLDOWN_SECONDS
            }
            save_memory(active_signals)
            save_killed_thesis(killed_thesis)
            print_dashboard(active_signals)
            print(f"\n[{time.strftime('%H:%M:%S')}] ✅ Cycle Complete. Sleeping 60 seconds...")
            time.sleep(60)

        except KeyboardInterrupt:
            print("\n🛑 Shutdown requested.")
            sys.exit()
        except Exception as e:
            print(f"⚠️ Critical Runtime Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()