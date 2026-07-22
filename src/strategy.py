import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

class SMCStrategy:
    def __init__(self, swing_window=2): 
        self.swing_window = swing_window
        self._bias_cache = {}

    # ==========================================
    # 🕒 CACHE & SESSION ENGINE
    # ==========================================
    def get_session_mode(self):
        now = datetime.utcnow()
        if now.weekday() >= 5: return "STRICT" 
        hour = now.hour
        if hour >= 22 or hour < 7: return "STRICT" 
        return "SNIPER" 

    def _cached_bias(self, df: pd.DataFrame, tf_name: str) -> str:
        if df is None or df.empty: return "CHOPPY"
        x = df.copy().reset_index(drop=True)
        if "timestamp" not in x.columns: return self._derive_simple_bias_from_df(x)
        x = x.dropna(subset=["timestamp", "high", "low", "close"])
        if x.empty: return "CHOPPY"
        last_ts = str(x["timestamp"].iloc[-1])
        cache_key = f"{tf_name}_{last_ts}"
        if cache_key in self._bias_cache: return self._bias_cache[cache_key]
        bias = self._derive_simple_bias_from_df(x)
        self._bias_cache[cache_key] = bias
        if len(self._bias_cache) > 128:
            oldest_key = next(iter(self._bias_cache))
            del self._bias_cache[oldest_key]
        return bias

    # ==========================================
    # 🛡️ 5 NEW QUALITY FILTERS (INSTITUTIONAL PATCH)
    # ==========================================
    @staticmethod
    def _is_market_choppy(df: pd.DataFrame, lookback: int = 20, wick_body_threshold: float = 1.8) -> bool:
        if df is None or df.empty or len(df) < lookback: return True
        x = df.copy().reset_index(drop=True)
        w = x.tail(lookback)
        body = (w["close"] - w["open"]).abs()
        upper_wick = w["high"] - w[["open", "close"]].max(axis=1)
        lower_wick = w[["open", "close"]].min(axis=1) - w["low"]
        avg_body = float(body.mean())
        avg_wick = float((upper_wick + lower_wick).mean())
        avg_range = float((w["high"] - w["low"]).mean())
        if avg_range <= 0: return True
        wick_body_ratio = avg_wick / max(avg_body, 1e-9)
        body_ratio = avg_body / avg_range
        atr_ratio = float(w["atr"].dropna().iloc[-1]) / max(float(w["close"].iloc[-1]), 1e-9) if "atr" in w.columns and not w["atr"].dropna().empty else None
        if wick_body_ratio > wick_body_threshold: return True
        if body_ratio < 0.28: return True
        if atr_ratio is not None and atr_ratio > 0.08: return True
        return False

    @staticmethod
    def _momentum_weakening(df: pd.DataFrame, lookback: int = 5) -> bool:
        if df is None or df.empty or len(df) < lookback * 2: return False
        x = df.copy().reset_index(drop=True)
        recent = x.tail(lookback)
        prev = x.iloc[-lookback * 2 : -lookback]
        recent_trend = abs(float(recent["close"].iloc[-1]) - float(recent["close"].iloc[0]))
        prev_trend = abs(float(prev["close"].iloc[-1]) - float(prev["close"].iloc[0]))
        recent_range = float(recent["high"].max() - recent["low"].min())
        prev_range = float(prev["high"].max() - prev["low"].min())
        trend_weak = recent_trend < (prev_trend * 0.6)
        range_weak = recent_range < (prev_range * 0.7)
        return trend_weak or range_weak

    @staticmethod
    def _detect_sweep(
        direction: str,
        x: pd.DataFrame,
        swing_highs: List[Dict[str, Any]],
        swing_lows: List[Dict[str, Any]],
        lookback: int = 12,
        atr_depth_mult: float = 0.12,
        reclaim_ratio: float = 0.40,
        max_swing_age: int = 36
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:

        if x is None or x.empty or len(x) < 5:
            return False, None

        last_idx = len(x) - 1
        start_idx = max(0, last_idx - lookback)

        def _atr_at(i: int) -> float:
            if "atr" not in x.columns:
                return 0.0

            val = x["atr"].iloc[i]

            if pd.isna(val) or not np.isfinite(val):
                return 0.0

            return float(val)

        # =========================================================
        # LONG SWEEP
        # =========================================================

        if direction == "LONG":

            valid_lows = [
                sl for sl in swing_lows
                if (
                    int(sl["index"]) < last_idx
                    and (last_idx - int(sl["index"])) <= max_swing_age
                )
            ]

            if not valid_lows:
                return False, None

            # Use freshest valid liquidity low
            ref_swing = valid_lows[-1]

            ref_idx = int(ref_swing["index"])
            ref_price = float(ref_swing["price"])

            for i in range(last_idx, start_idx - 1, -1):

                candle_low = float(x["low"].iloc[i])
                candle_high = float(x["high"].iloc[i])
                candle_close = float(x["close"].iloc[i])

                candle_range = max(abs(candle_high - candle_low), 1e-9)

                atr_now = _atr_at(i)

                # Depth below liquidity
                sweep_depth = ref_price - candle_low

                # Close reclaimed back above liquidity
                reclaim_buffer = atr_now * 0.03

                reclaimed = candle_close > (ref_price + reclaim_buffer)

                # Body reclaim strength
                reclaim_strength = (
                    (candle_close - candle_low) / candle_range
                )

                # Minimum depth validation
                depth_valid = (
                    sweep_depth >= (atr_now * atr_depth_mult)
                )

                if (
                    candle_low < ref_price
                    and reclaimed
                    and depth_valid
                    and reclaim_strength >= reclaim_ratio
                ):

                    return True, {
                        "index": i,
                        "ref_index": ref_idx,
                        "ref_price": ref_price,
                        "sweep_depth": sweep_depth,
                        "reclaim_strength": reclaim_strength
                    }

        # =========================================================
        # SHORT SWEEP
        # =========================================================

        elif direction == "SHORT":

            valid_highs = [
                sh for sh in swing_highs
                if (
                    int(sh["index"]) < last_idx
                    and (last_idx - int(sh["index"])) <= max_swing_age
                )
            ]

            if not valid_highs:
                return False, None

            # Use freshest valid liquidity high
            ref_swing = valid_highs[-1]

            ref_idx = int(ref_swing["index"])
            ref_price = float(ref_swing["price"])

            for i in range(last_idx, start_idx - 1, -1):

                candle_high = float(x["high"].iloc[i])
                candle_low = float(x["low"].iloc[i])
                candle_close = float(x["close"].iloc[i])

                candle_range = max(abs(candle_high - candle_low), 1e-9)

                atr_now = _atr_at(i)

                # Depth above liquidity
                sweep_depth = candle_high - ref_price

                # Close reclaimed back below liquidity
                reclaim_buffer = atr_now * 0.03

                reclaimed = candle_close < (ref_price - reclaim_buffer)

                # Body reclaim strength
                reclaim_strength = (
                    (candle_high - candle_close) / candle_range
                )

                # Minimum depth validation
                depth_valid = (
                    sweep_depth >= (atr_now * atr_depth_mult)
                )

                if (
                    candle_high > ref_price
                    and reclaimed
                    and depth_valid
                    and reclaim_strength >= reclaim_ratio
                ):

                    return True, {
                        "index": i,
                        "ref_index": ref_idx,
                        "ref_price": ref_price,
                        "sweep_depth": sweep_depth,
                        "reclaim_strength": reclaim_strength
                    }

        return False, None

    # ==========================================
    # 📐 LIQUIDITY & DEALING RANGE ENGINE
    # ==========================================
    @staticmethod
    def _get_previous_day_high_low(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if df is None or df.empty or len(df) < 30: return None
        work = df.loc[:, ["timestamp", "high", "low"]].copy()
        work["timestamp"] = pd.to_datetime(work["timestamp"], utc=False, errors="coerce")
        work = work.dropna(subset=["timestamp", "high", "low"])
        if work.empty: return None
        work["day"] = work["timestamp"].dt.floor("d")
        daily = work.groupby("day", sort=True, observed=True).agg(day_high=("high", "max"), day_low=("low", "min")).reset_index()
        if len(daily) < 2: return None
        prev_day = daily.iloc[-2]
        return {"day": prev_day["day"], "pdh": float(prev_day["day_high"]), "pdl": float(prev_day["day_low"])}

    @staticmethod
    def _cluster_liquidity_levels(levels: List[Dict[str, Any]], tolerance: float) -> List[Dict[str, Any]]:
        if not levels: return []
        ordered = sorted(levels, key=lambda x: float(x["price"]))
        clusters: List[List[Dict[str, Any]]] = [[ordered[0]]]
        for item in ordered[1:]:
            last_cluster = clusters[-1]
            last_price = float(last_cluster[-1]["price"])
            cur_price = float(item["price"])
            if abs(cur_price - last_price) <= tolerance:
                last_cluster.append(item)
            else:
                clusters.append([item])
        result = []
        for cluster in clusters:
            prices = [float(x["price"]) for x in cluster]
            indices = [int(x["index"]) for x in cluster]
            result.append({
                "level": sum(prices) / len(prices),
                "count": len(cluster),
                "first_index": min(indices),
                "last_index": max(indices),
                "prices": prices,
            })
        return result

    def find_liquidity_target(
        self,
        df_h1: pd.DataFrame,
        direction: str,
        entry_price: float,
        swing_highs: List[Dict[str, Any]],
        swing_lows: List[Dict[str, Any]],
        atr: float,
        sl: Optional[float] = None,
        min_rr: float = 1.2,
        entry_mode: str = "RETRACEMENT"
    ) -> Dict[str, Any]:
        """
        RR-aware liquidity target selector.

        Mode behavior:
        - CONTINUATION: prefer nearest valid liquidity target with acceptable RR.
        - RETRACEMENT : keep classic liquidity-strength-first selection.

        Prioritas umum:
        1) EQH / EQL cluster
        2) swing high / swing low
        3) PDH / PDL

        If no target satisfies RR floor, return None so caller can fallback.
        """
        work = df_h1.copy().reset_index(drop=True).tail(180).reset_index(drop=True)
        direction = str(direction).upper().strip()
        entry_mode = str(entry_mode).upper().strip()
        entry = float(entry_price)

        if not np.isfinite(entry) or entry <= 0:
            return {"tp": None, "source": None, "rr": None, "reason": "invalid_entry"}

        if entry_mode not in {"CONTINUATION", "RETRACEMENT"}:
            entry_mode = "RETRACEMENT"

        atr = float(atr) if atr is not None and np.isfinite(float(atr)) else 0.0

        daily_levels = self._get_previous_day_high_low(work)
        pdh = float(daily_levels["pdh"]) if daily_levels and daily_levels.get("pdh") is not None else None
        pdl = float(daily_levels["pdl"]) if daily_levels and daily_levels.get("pdl") is not None else None

        tolerance = max(atr * 0.15, entry * 0.0015)
        candidates: List[Dict[str, Any]] = []

        def add_candidate(level: float, source: str, score: float):
            if level is None:
                return
            level = float(level)
            if not np.isfinite(level):
                return
            if direction == "LONG" and level <= entry:
                return
            if direction == "SHORT" and level >= entry:
                return
            candidates.append({
                "level": level,
                "source": source,
                "score": float(score),
                "distance": abs(level - entry),
            })

        # -----------------------------
        # Collect liquidity candidates
        # -----------------------------
        if direction == "LONG":
            for c in self._cluster_liquidity_levels(swing_highs, tolerance):
                level = float(c["level"])
                if level > entry:
                    add_candidate(level, "EQH_cluster", 3.0 + (1.0 if c["count"] >= 2 else 0.0))

            for sh in swing_highs:
                level = float(sh["price"])
                if level > entry:
                    add_candidate(level, "SWING_HIGH", 2.0)

            if pdh is not None and pdh > entry:
                add_candidate(pdh, "PDH", 3.5)

        elif direction == "SHORT":
            for c in self._cluster_liquidity_levels(swing_lows, tolerance):
                level = float(c["level"])
                if level < entry:
                    add_candidate(level, "EQL_cluster", 3.0 + (1.0 if c["count"] >= 2 else 0.0))

            for slw in swing_lows:
                level = float(slw["price"])
                if level < entry:
                    add_candidate(level, "SWING_LOW", 2.0)

            if pdl is not None and pdl < entry:
                add_candidate(pdl, "PDL", 3.5)

        else:
            return {"tp": None, "source": None, "rr": None, "reason": "invalid_direction"}

        if not candidates:
            return {"tp": None, "source": None, "rr": None, "reason": "no_candidates"}

        # -----------------------------
        # Estimate stop for RR scoring
        # -----------------------------
        def _proxy_stop() -> Optional[float]:
            stop_buffer = max(atr * 0.35, entry * 0.0015)

            if direction == "LONG":
                refs = [float(x["price"]) for x in swing_lows if float(x["price"]) < entry and np.isfinite(float(x["price"]))]
                if pdl is not None and pdl < entry:
                    refs.append(float(pdl))
                if refs:
                    base = max(refs)
                    return base - stop_buffer
                return entry - max(atr * 1.2, entry * 0.003)

            refs = [float(x["price"]) for x in swing_highs if float(x["price"]) > entry and np.isfinite(float(x["price"]))]
            if pdh is not None and pdh > entry:
                refs.append(float(pdh))
            if refs:
                base = min(refs)
                return base + stop_buffer
            return entry + max(atr * 1.2, entry * 0.003)

        if sl is not None and np.isfinite(float(sl)) and float(sl) > 0:
            stop = float(sl)
        else:
            stop = _proxy_stop()

        if not np.isfinite(stop) or stop <= 0:
            return {"tp": None, "source": None, "rr": None, "reason": "invalid_stop"}

        risk = abs(entry - stop)
        if not np.isfinite(risk) or risk <= 0:
            return {"tp": None, "source": None, "rr": None, "reason": "invalid_risk"}

        # -----------------------------
        # Score candidates with RR
        # -----------------------------
        evaluated = []
        for c in candidates:
            if direction == "LONG":
                tp_level = c["level"] - (atr * 0.10)
                if tp_level <= entry:
                    tp_level = c["level"]
            else:
                tp_level = c["level"] + (atr * 0.10)
                if tp_level >= entry:
                    tp_level = c["level"]

            if not np.isfinite(tp_level) or tp_level <= 0:
                continue

            reward = abs(tp_level - entry)
            if not np.isfinite(reward) or reward <= 0:
                continue

            rr = reward / risk
            if not np.isfinite(rr):
                continue

            # Small mode-aware bias:
            # continuation prefers closer liquidity; retracement keeps liquidity-strength bias.
            mode_score = float(c["score"])
            if entry_mode == "CONTINUATION":
                mode_score = mode_score - 0.25  # tiny normalization so distance dominates more cleanly

            evaluated.append({
                "tp": float(tp_level),
                "source": c["source"],
                "score": mode_score,
                "distance": float(c["distance"]),
                "rr": float(rr),
            })

        if not evaluated:
            return {
                "tp": None,
                "source": None,
                "rr": None,
                "reason": "no_valid_rr_candidates",
                "stop_used": float(stop),
                "entry_mode": entry_mode,
            }

        valid = [x for x in evaluated if x["rr"] >= float(min_rr)]

        if valid:
            if entry_mode == "CONTINUATION":
                # For continuation, prefer the nearest clean liquidity that still passes RR floor.
                best = sorted(
                    valid,
                    key=lambda x: (
                        x["distance"],
                        -x["score"],
                        -x["rr"],
                        x["tp"] if direction == "LONG" else -x["tp"]
                    )
                )[0]
            else:
                # Retracement stays liquidity-strength first.
                best = sorted(
                    valid,
                    key=lambda x: (
                        -x["score"],
                        x["distance"],
                        -x["rr"],
                        x["tp"] if direction == "LONG" else -x["tp"]
                    )
                )[0]

            return {
                "tp": float(best["tp"]),
                "source": best["source"],
                "rr": round(float(best["rr"]), 2),
                "stop_used": float(stop),
                "reason": "rr_pass",
                "entry_mode": entry_mode,
            }

        best_fail = sorted(
            evaluated,
            key=lambda x: (
                x["distance"] if entry_mode == "CONTINUATION" else -x["score"],
                -x["score"] if entry_mode == "CONTINUATION" else x["distance"],
                -x["rr"],
                x["tp"] if direction == "LONG" else -x["tp"]
            )
        )[0]

        return {
            "tp": None,
            "source": best_fail["source"],
            "rr": round(float(best_fail["rr"]), 2),
            "stop_used": float(stop),
            "reason": "rr_below_floor",
            "entry_mode": entry_mode,
        }

    def identify_swings(self, df, custom_window=None, prominence_mult=0.0):
        window = custom_window if custom_window is not None else self.swing_window
        swing_highs, swing_lows = [], []
        if 'atr' in df.columns and not df['atr'].dropna().empty:
            atr_mean = float(df['atr'].dropna().mean())
        else:
            atr_mean = 0.0
        if not np.isfinite(atr_mean): atr_mean = 0.0 
        min_prominence = atr_mean * prominence_mult

        for i in range(window, len(df) - window):
            is_high, is_low = True, True
            for j in range(1, window + 1):
                if df['high'].iloc[i] < df['high'].iloc[i-j] or df['high'].iloc[i] < df['high'].iloc[i+j]: is_high = False
                if df['low'].iloc[i] > df['low'].iloc[i-j] or df['low'].iloc[i] > df['low'].iloc[i+j]: is_low = False

            if is_high:
                if prominence_mult > 0:
                    left_lows = df['low'].iloc[i-window:i]
                    right_lows = df['low'].iloc[i+1:i+window+1]
                    prominence = df['high'].iloc[i] - max(left_lows.min(), right_lows.min())
                    if prominence < min_prominence: is_high = False

            if is_low:
                if prominence_mult > 0:
                    left_highs = df['high'].iloc[i-window:i]
                    right_highs = df['high'].iloc[i+1:i+window+1]
                    prominence = min(left_highs.max(), right_highs.max()) - df['low'].iloc[i]
                    if prominence < min_prominence: is_low = False

            if is_high:
                if not swing_highs or not (swing_highs[-1]['price'] == df['high'].iloc[i] and swing_highs[-1]['index'] == i - 1):
                    swing_highs.append({'index': i, 'price': df['high'].iloc[i]})
            if is_low:
                if not swing_lows or not (swing_lows[-1]['price'] == df['low'].iloc[i] and swing_lows[-1]['index'] == i - 1):
                    swing_lows.append({'index': i, 'price': df['low'].iloc[i]})
                
        return swing_highs, swing_lows

    # =========================================================
    # 🗺️ TOP-DOWN ANALYSIS (HTF MAP)
    # =========================================================
    def build_htf_map(self, df_d1: pd.DataFrame, df_h1: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if df_d1 is None or df_h1 is None or df_d1.empty or df_h1.empty: return None

        d1 = df_d1.copy().reset_index(drop=True)
        h1 = df_h1.copy().reset_index(drop=True)
        if len(d1) < 5 or len(h1) < 20: return None

        d1["timestamp"] = pd.to_datetime(d1["timestamp"], errors="coerce")
        h1["timestamp"] = pd.to_datetime(h1["timestamp"], errors="coerce")
        d1 = d1.dropna(subset=["timestamp", "high", "low", "close"])
        h1 = h1.dropna(subset=["timestamp", "high", "low", "close"])

        d1["date"] = d1["timestamp"].dt.date
        d1["year"] = d1["timestamp"].dt.isocalendar().year.astype(int)
        d1["week"] = d1["timestamp"].dt.isocalendar().week.astype(int)

        daily = (d1.groupby("date", sort=True).agg(day_high=("high", "max"), day_low=("low", "min")).reset_index())
        if len(daily) < 2: return None
        pdh, pdl = float(daily.iloc[-2]["day_high"]), float(daily.iloc[-2]["day_low"])
        daily_eq = (pdh + pdl) / 2.0

        weekly = (d1.groupby(["year", "week"], sort=True).agg(week_high=("high", "max"), week_low=("low", "min")).reset_index())
        pwh, pwl, weekly_eq = None, None, None
        weekly_bias = "CHOPPY"
        if len(weekly) >= 2:
            pwh, pwl = float(weekly.iloc[-2]["week_high"]), float(weekly.iloc[-2]["week_low"])
            weekly_eq = (pwh + pwl) / 2.0
            current_price = float(d1.iloc[-1]["close"])
            if current_price > weekly_eq: weekly_bias = "BULLISH"
            elif current_price < weekly_eq: weekly_bias = "BEARISH"

        current_price = float(d1.iloc[-1]["close"])
        d1_bias = self._cached_bias(d1, "D1")

        h1_rs = h1.set_index("timestamp").sort_index()
        h4 = (h1_rs.resample("4h").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last")).dropna().reset_index())
        if len(h4) >= 2: h4 = h4.iloc[:-1].copy()
        h4_bias = self._cached_bias(h4, "H4") if len(h4) >= 5 else "CHOPPY"

        location = "equilibrium"
        if weekly_eq is not None:
            if current_price < daily_eq and current_price < weekly_eq: location = "discount"
            elif current_price > daily_eq and current_price > weekly_eq: location = "premium"

        return {
            "pdh": pdh, "pdl": pdl, "pwh": pwh, "pwl": pwl,
            "daily_eq": daily_eq, "weekly_eq": weekly_eq,
            "h4_bias": h4_bias, "d1_bias": d1_bias,
            "location": location, "current_price": current_price,
            "weekly_bias": weekly_bias,
            "momentum_weakening": self._momentum_weakening(d1)
        }

    def _derive_simple_bias_from_df(self, df: pd.DataFrame) -> str:
        if df is None or df.empty or len(df) < 20:
            return "CHOPPY"

        x = df.copy().reset_index(drop=True)
        x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
        x["ema200"] = x["close"].ewm(span=200, adjust=False).mean()

        sh, sl = self.identify_swings(x, custom_window=3, prominence_mult=0.18)

        current_close = float(x["close"].iloc[-1])
        ema50_now = float(x["ema50"].iloc[-1])
        ema200_now = float(x["ema200"].iloc[-1])
        ema50_prev = float(x["ema50"].iloc[-5]) if len(x) >= 5 else ema50_now

        bull = 0.0
        bear = 0.0

        # 1) Structure swing
        if len(sh) >= 2 and len(sl) >= 2:
            sh1, sh2 = float(sh[-1]["price"]), float(sh[-2]["price"])
            sl1, sl2 = float(sl[-1]["price"]), float(sl[-2]["price"])

            if sh1 > sh2 and sl1 > sl2:
                bull += 2.0
            elif sh1 < sh2 and sl1 < sl2:
                bear += 2.0

        # 2) Price vs EMA50
        if current_close > ema50_now:
            bull += 1.5
        elif current_close < ema50_now:
            bear += 1.5

        # 3) Price vs EMA200
        if current_close > ema200_now:
            bull += 2.0
        elif current_close < ema200_now:
            bear += 2.0

        # 4) EMA50 slope
        if ema50_now > ema50_prev:
            bull += 1.0
        elif ema50_now < ema50_prev:
            bear += 1.0

        if bull >= 4.0 and bull > bear:
            return "BULLISH"
        if bear >= 4.0 and bear > bull:
            return "BEARISH"
        return "CHOPPY"

    # =========================================================
    # 👁️ REGIME ENGINE
    # =========================================================
    def classify_regime(self, df: pd.DataFrame) -> str:
        if df is None or df.empty or len(df) < 30:
            return "unknown"

        x = df.copy().reset_index(drop=True)

        if "atr" not in x.columns or x["atr"].dropna().empty:
            return "unknown"

        atr_now_raw = x["atr"].iloc[-1]
        if pd.isna(atr_now_raw):
            return "unknown"

        atr_now = float(atr_now_raw)
        if atr_now <= 0:
            return "unknown"

        window = min(20, len(x))
        recent = x.tail(window)

        atr_avg_raw = recent["atr"].dropna().mean()
        if pd.isna(atr_avg_raw):
            return "unknown"

        atr_avg = float(atr_avg_raw)
        if atr_avg <= 0:
            return "unknown"

        recent_high = float(recent["high"].max())
        recent_low = float(recent["low"].min())
        recent_range = max(recent_high - recent_low, 1e-9)

        range_ratio = recent_range / atr_avg

        candle_range = (x["high"] - x["low"]).replace(0, np.nan)
        body = (x["close"] - x["open"]).abs()
        body_ratio_series = (body / candle_range).tail(window).dropna()
        body_ratio = float(body_ratio_series.mean()) if not body_ratio_series.empty else 0.0

        rr_low = recent_low
        rr_high = recent_high
        rr = max(rr_high - rr_low, 1e-9)
        close_pos = (float(x["close"].iloc[-1]) - rr_low) / rr

        last_close = float(x["close"].iloc[-1])
        close_5 = float(x["close"].iloc[-5]) if len(x) >= 5 else last_close
        close_10 = float(x["close"].iloc[-10]) if len(x) >= 10 else close_5

        drift_5 = (last_close - close_5) / max(close_5, 1e-9)
        drift_10 = (last_close - close_10) / max(close_10, 1e-9)

        compression_votes = 0
        if range_ratio < 3.0:
            compression_votes += 1
        if atr_now <= atr_avg * 0.95:
            compression_votes += 1
        if body_ratio < 0.42:
            compression_votes += 1

        expansion_votes = 0
        if range_ratio > 4.0:
            expansion_votes += 1
        if atr_now >= atr_avg * 1.03:
            expansion_votes += 1
        if body_ratio > 0.46:
            expansion_votes += 1

        strong_drift = abs(drift_5) > 0.008 or abs(drift_10) > 0.012

        # Compression-like structure
        if compression_votes >= 2 and not strong_drift:
            if close_pos < 0.30:
                return "accumulation"
            if close_pos > 0.70:
                return "distribution"
            return "compression"

        # Expansion-like structure
        if expansion_votes >= 2:
            if drift_5 > 0.003 or drift_10 > 0.005:
                return "expansion_up"
            if drift_5 < -0.003 or drift_10 < -0.005:
                return "expansion_down"
            return "rotational"

        # Middle ground: not dead, not explosive
        if range_ratio >= 2.6 and (abs(drift_5) > 0.004 or abs(drift_10) > 0.006):
            return "rotational"

        return "neutral"

    # =========================================================
    # 🏅 SCORE ENGINE
    # =========================================================
    def score_signal(
        self,
        htf_map: Dict[str, Any],
        regime: str,
        h1_setup: Dict[str, Any],
        m5_setup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:

        if not htf_map or not h1_setup:
            return {
                "score": 0,
                "decision": "SKIP",
                "reason": "missing_input"
            }

        score = 0
        notes = []

        direction = str(h1_setup.get("direction", "")).upper().strip()
        entry_mode = str(h1_setup.get("entry_mode", "RETRACEMENT")).upper().strip()

        if direction not in {"LONG", "SHORT"}:
            return {
                "score": 0,
                "decision": "SKIP",
                "reason": "missing_direction"
            }

        if entry_mode not in {"CONTINUATION", "RETRACEMENT", "WATCH_ONLY"}:
            entry_mode = "RETRACEMENT"

        displacement_present = bool(h1_setup.get("displacement_valid", False))
        volume_spike = bool(h1_setup.get("volume_spike", False))
        sweep_present = bool(h1_setup.get("sweep_valid", False))
        fvg_present = bool(h1_setup.get("fvg_valid", False))
        ob_present = bool(h1_setup.get("fresh_ob", False))

        # =========================================================
        # MODE-AWARE GATES
        # =========================================================

        if entry_mode == "CONTINUATION":
            if str(h1_setup.get("entry_source", "")) != "continuation_micro_pullback":
                return {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": "no_continuation_displacement",
                    "entry_mode": entry_mode
                }

            if not volume_spike:
                score -= 2
                notes.append("weak_volume")

        elif entry_mode == "WATCH_ONLY":
            # observation-only regime:
            # never actionable, but still allowed to surface as radar info
            if not (
                htf_map.get("weekly_bias") in {"BULLISH", "BEARISH"}
                and htf_map.get("d1_bias") in {"BULLISH", "BEARISH"}
            ):
                return {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": "weak_watch_only_context",
                    "entry_mode": entry_mode
                }

        # =========================================================
        # HTF ALIGNMENT
        # =========================================================

        if direction == "LONG":
            if htf_map.get("weekly_bias") == "BULLISH":
                score += 2
                notes.append("weekly_aligned")

            if htf_map.get("d1_bias") == "BULLISH":
                score += 2
                notes.append("d1_aligned")

            if htf_map.get("h4_bias") == "BULLISH":
                score += 1
                notes.append("h4_aligned")

            if htf_map.get("location") == "discount":
                score += 2
                notes.append("discount")

        elif direction == "SHORT":
            if htf_map.get("weekly_bias") == "BEARISH":
                score += 2
                notes.append("weekly_aligned")

            if htf_map.get("d1_bias") == "BEARISH":
                score += 2
                notes.append("d1_aligned")

            if htf_map.get("h4_bias") == "BEARISH":
                score += 1
                notes.append("h4_aligned")

            if htf_map.get("location") == "premium":
                score += 2
                notes.append("premium")

        # =========================================================
        # CORE CONFIRMATIONS
        # =========================================================

        if entry_mode == "CONTINUATION":
            if sweep_present:
                score += 1
                notes.append("sweep")

            if fvg_present:
                score += 1
                notes.append("fvg")

            if str(h1_setup.get("entry_source", "")) == "continuation_micro_pullback":
                score += 4
                notes.append("displacement")

            if ob_present:
                score += 1
                notes.append("fresh_ob")

        elif entry_mode == "RETRACEMENT":
            # Retracement is liquidity-reaction first.
            # Sweep and OB matter more than displacement.
            if sweep_present:
                score += 2
                notes.append("sweep")
            else:
                score -= 1
                notes.append("weak_sweep")

            if fvg_present:
                score += 1
                notes.append("fvg")

            if displacement_present:
                score += 1
                notes.append("displacement")

            if ob_present:
                score += 2
                notes.append("fresh_ob")
            else:
                score -= 1
                notes.append("no_ob")

        else:  # WATCH_ONLY
            if sweep_present:
                score += 1
                notes.append("sweep")

            if fvg_present:
                score += 1
                notes.append("fvg")

            if displacement_present:
                score += 1
                notes.append("displacement")

            if ob_present:
                score += 1
                notes.append("fresh_ob")

        # =========================================================
        # STRUCTURE QUALITY
        # =========================================================

        if h1_setup.get("target_clear", False):
            score += 1
            notes.append("target_clear")

        if h1_setup.get("session_active", False):
            score += 1
            notes.append("session_active")

        if h1_setup.get("htf_context_clean", False):
            score += 1
            notes.append("htf_context_clean")

        # =========================================================
        # REGIME ENGINE
        # =========================================================

        if regime in {"expansion_up", "expansion_down"}:
            score += 1
            notes.append("expansion")

        elif regime in {"accumulation", "distribution"}:
            score += 1
            notes.append("squeeze")

        elif regime in {"neutral", "compression", "unknown"}:
            score -= 2
            notes.append("neutral_penalty")

        # =========================================================
        # RR ENGINE
        # =========================================================

        entry = h1_setup.get("entry_price")
        sl = h1_setup.get("sl")
        tp = h1_setup.get("tp")
        entry_source = str(h1_setup.get("entry_source", "") or "")
        tp_source = str(h1_setup.get("tp_source", "") or "")
        tp_rr = h1_setup.get("tp_rr", None)
        
        rr = None

        if entry and sl and tp:
            try:
                risk = abs(float(entry) - float(sl))
                reward = abs(float(tp) - float(entry))

                if np.isfinite(risk) and np.isfinite(reward) and risk > 0:
                    rr = reward / risk

                    if rr < 1.2:
                        return {
                            "score": 0,
                            "decision": "SKIP",
                            "reason": "bad_rr",
                            "rr": round(rr, 2),
                            "entry_mode": entry_mode
                        }

                    if rr >= 3.0:
                        score += 2
                        notes.append(f"rr_{rr:.1f}")
                    elif rr >= 2.0:
                        score += 1
                        notes.append(f"rr_{rr:.1f}")
                    elif rr >= 1.5:
                        notes.append(f"rr_{rr:.1f}")

            except Exception:
                pass

        # =========================================================
        # MOMENTUM QUALITY
        # =========================================================

        if htf_map.get("momentum_weakening", False):
            score -= 1
            notes.append("momentum_weakening")

        # =========================================================
        # M5 ENRICHMENT
        # =========================================================

        if m5_setup and m5_setup.get("choch_confirmed", False):
            if entry_mode == "CONTINUATION":
                score += 1
                notes.append("m5_continuation_confirm")
            elif entry_mode == "RETRACEMENT":
                score += 2
                notes.append("m5_choch")
            else:
                score += 1
                notes.append("m5_watch_confirm")

        # =========================================================
        # FINAL DECISION
        # =========================================================

        if entry_mode == "CONTINUATION":
            if score >= 14:
                decision = "A+"
            elif score >= 11:
                decision = "VALID"
            elif score >= 8:
                decision = "WATCH"
            else:
                decision = "SKIP"

        elif entry_mode == "RETRACEMENT":
            if score >= 15 and displacement_present:
                decision = "A+"
            elif score >= 15 and not displacement_present:
                decision = "VALID"
            elif score >= 12:
                decision = "VALID"
            elif score >= 9:
                decision = "WATCH"
            else:
                decision = "SKIP"

        else:  # WATCH_ONLY
            if score >= 12:
                decision = "WATCH"
            else:
                decision = "SKIP"

        return {
            "score": int(score),
            "decision": decision,
            "direction": direction,
            "notes": notes,
            "regime": regime,
            "rr": round(rr, 2) if rr is not None else None,
            "entry_mode": entry_mode,
            "volume_spike": volume_spike,
            "entry_source": entry_source,
            "tp_source": tp_source,
            "tp_rr": tp_rr,
        }

    # =========================================================
    # 🧠 STAGE 1: BUILD SIGNAL CONTEXT (HTF & H1)
    # =========================================================
    def build_signal_context(self, df_d1: pd.DataFrame, df_h1: pd.DataFrame) -> Optional[Dict[str, Any]]:
        def _safe_df(df: Optional[pd.DataFrame], min_len: int = 5) -> Optional[pd.DataFrame]:
            if df is None or df.empty or len(df) < min_len:
                return None
            x = df.copy().reset_index(drop=True)
            x = x.dropna(subset=[c for c in ["timestamp", "open", "high", "low", "close"] if c in x.columns])
            if len(x) < min_len:
                return None
            return x

        def _is_displacement_candle(candle: pd.Series, direction: str, atr_last: float) -> bool:
            body = abs(float(candle["close"]) - float(candle["open"]))
            candle_range = float(candle["high"]) - float(candle["low"])
            br = body / candle_range if candle_range > 0 else 0.0

            if direction == "LONG":
                return (
                    float(candle["close"]) > float(candle["open"])
                    and body >= (atr_last * 0.60)
                    and br >= 0.50
                )
            else:
                return (
                    float(candle["close"]) < float(candle["open"])
                    and body >= (atr_last * 0.60)
                    and br >= 0.50
                )

        d1 = _safe_df(df_d1, 5)
        h1 = _safe_df(df_h1, 20)

        debug = {
            "htf": {},
            "conf": {},
            "score_raw": None,
        }

        if d1 is None or h1 is None:
            return {
                "result": {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": "invalid_input"
                },
                "debug": debug,
            }

        market_choppy = self._is_market_choppy(h1)

        htf_map = self.build_htf_map(d1, h1)
        if htf_map is None:
            return {
                "result": {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": "invalid_htf_map"
                },
                "debug": debug,
            }

        regime = self.classify_regime(h1)
        regime_str = str(regime).lower().strip()

        wb = htf_map["weekly_bias"]
        db = htf_map["d1_bias"]
        hb = htf_map["h4_bias"]
        loc = htf_map["location"]

        # =========================================================
        # WEIGHTED DIRECTION SELECTION
        # =========================================================
        bull = 0.0
        bear = 0.0
        fallback_h1_bias = None
        direction_source = "none"

        # Weekly should be dominant
        if wb == "BULLISH":
            bull += 4.0
        elif wb == "BEARISH":
            bear += 4.0

        # Daily still matters, but less than weekly
        if db == "BULLISH":
            bull += 2.0
        elif db == "BEARISH":
            bear += 2.0

        # H4 is supportive, not decisive
        if hb == "BULLISH":
            bull += 1.0
        elif hb == "BEARISH":
            bear += 1.0

        # Location must be context-aware, not absolute
        if loc == "discount":
            if wb == "BULLISH":
                bull += 1.5
            elif wb == "BEARISH":
                bull += 0.5
        elif loc == "premium":
            if wb == "BEARISH":
                bear += 1.5
            elif wb == "BULLISH":
                bear += 0.5

        if max(bull, bear) < 4.0:
            fallback_h1_bias = self._derive_simple_bias_from_df(h1)
            if fallback_h1_bias == "BULLISH":
                bull += 1.0
                direction_source = "fallback_h1"
            elif fallback_h1_bias == "BEARISH":
                bear += 1.0
                direction_source = "fallback_h1"

        direction = None

        # Rotational needs stronger consensus, but weekly can still anchor it
        if regime_str == "rotational":
            min_score_for_direction = 4.0
            min_gap = 1.0
        else:
            min_score_for_direction = 4.0
            min_gap = 1.0

        if bull >= min_score_for_direction and bull >= bear + min_gap:
            direction = "LONG"
            if direction_source == "none":
                direction_source = "weighted_bull"
        elif bear >= min_score_for_direction and bear >= bull + min_gap:
            direction = "SHORT"
            if direction_source == "none":
                direction_source = "weighted_bear"

        # Weekly anchor override for rotational conflict
        anchor_override = False

        if direction is None and regime_str == "rotational":
            if wb == "BULLISH" and bull > bear:
                direction = "LONG"
                direction_source = "weekly_anchor_rotational"
                anchor_override = True

            elif wb == "BEARISH" and bear > bull:
                direction = "SHORT"
                direction_source = "weekly_anchor_rotational"
                anchor_override = True

        debug["htf"] = {
            "weekly": wb,
            "d1": db,
            "h4": hb,
            "loc": loc,
            "regime": regime,
            "h1_fb": fallback_h1_bias,
            "src": direction_source,
            "dir": direction,
            "bull": round(bull, 1),
            "bear": round(bear, 1),
            "anchor_override": anchor_override,
        }

        if direction is None:
            return {
                "result": {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": "no_direction"
                },
                "debug": debug,
            }

        # =========================================================
        # HARD VETO: EXPANSION vs DIRECTION CONFLICT
        # =========================================================

        veto_short = (
            direction == "SHORT"
            and wb == "BULLISH"
            and regime_str == "expansion_up"
        )

        veto_long = (
            direction == "LONG"
            and wb == "BEARISH"
            and regime_str == "expansion_down"
        )

        if veto_short or veto_long:
            veto_reason = "weekly_bullish_expansion_up_short_veto" if veto_short else "weekly_bearish_expansion_down_long_veto"

            debug["veto"] = {
                "active": True,
                "reason": veto_reason,
                "direction": direction,
                "weekly": wb,
                "regime": regime,
            }

            return {
                "htf_map": htf_map,
                "regime": regime,
                "h1_setup": {
                    "direction": direction,
                    "entry_mode": "NONE",
                    "entry_ready": False,
                    "entry_reason": veto_reason,
                    "entry_source": "veto",
                    "sweep_valid": False,
                    "fvg_valid": False,
                    "displacement_valid": False,
                    "fresh_ob": False,
                    "volume_spike": False,
                    "target_clear": False,
                    "session_active": False,
                    "htf_context_clean": False,
                    "entry_price": 0.0,
                    "sl": 0.0,
                    "tp": 0.0,
                    "atr_last": 0.0,
                },
                "result": {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": veto_reason,
                    "direction": direction,
                    "regime": regime,
                    "entry_mode": "NONE",
                },
                "debug": debug,
            }

        # =========================================================
        # REGIME -> EXECUTION PHILOSOPHY
        # =========================================================

        if regime_str.startswith("expansion"):

            entry_mode = "CONTINUATION"

        elif regime_str in {"accumulation", "distribution", "rotational"}:

            entry_mode = "RETRACEMENT"

        elif regime_str == "compression":

            strong_htf_alignment = (
                (
                    direction == "LONG"
                    and wb == "BULLISH"
                    and db == "BULLISH"
                    and loc == "discount"
                )
                or
                (
                    direction == "SHORT"
                    and wb == "BEARISH"
                    and db == "BEARISH"
                    and loc == "premium"
                )
            )

            entry_mode = "WATCH_ONLY" if strong_htf_alignment else "NONE"

        else:

            entry_mode = "NONE"

        atr_last = float(h1["atr"].dropna().iloc[-1]) if "atr" in h1.columns and not h1["atr"].dropna().empty else 0.0
        swing_highs, swing_lows = self.identify_swings(h1, custom_window=2, prominence_mult=0.3)
        last_idx = len(h1) - 1

        sweep_valid, _ = self._detect_sweep(direction, h1, swing_highs, swing_lows)

        fvg_valid, fresh_ob, ob_meta = False, False, None
        min_gap = atr_last * 0.16

        window_start_index = max(1, last_idx - 10)
        window_end_index = last_idx - 1
        window_bounds = {
            "start_index": window_start_index,
            "end_index": window_end_index,
        }

        ob_candidates = []
        ob_index = None
        ob_top = None
        ob_bottom = None
        ob_size = None
        selected_ob_rank = None
        selected_ob_fresh = None

        def _register_ob_candidate(i, candidate_top, candidate_bottom, fresh_status, passed_quality_filter):
            nonlocal ob_meta, ob_index, ob_top, ob_bottom, ob_size, selected_ob_rank, selected_ob_fresh

            candidate_size = abs(candidate_top - candidate_bottom)
            candidate_mid = (candidate_top + candidate_bottom) / 2.0

            candidate = {
                "index": i,
                "rank_from_last": len(ob_candidates) + 1,
                "top": candidate_top,
                "bottom": candidate_bottom,
                "size": candidate_size,
                "size_atr": (candidate_size / atr_last) if atr_last > 0 else None,
                "depth_from_price": (abs(current_price - candidate_mid) / atr_last) if atr_last > 0 else None,
                "fresh_status": fresh_status,
                "passed_quality_filter": passed_quality_filter,
                "is_selected": False,
            }

            ob_candidates.append(candidate)

            if passed_quality_filter and ob_meta is None:
                candidate["is_selected"] = True
                ob_meta = {"top": candidate_top, "bottom": candidate_bottom}
                ob_index = i
                ob_top = candidate_top
                ob_bottom = candidate_bottom
                ob_size = candidate_size
                selected_ob_rank = candidate["rank_from_last"]
                selected_ob_fresh = fresh_status

        current_price = float(h1["close"].iloc[-1])

        if direction == "LONG":
            for i in range(last_idx, max(2, last_idx - 12) - 1, -1):
                if float(h1["low"].iloc[i]) - float(h1["high"].iloc[i - 2]) >= min_gap and float(h1["close"].iloc[i]) > float(h1["open"].iloc[i]):
                    fvg_valid = True
                    break

            for i in range(window_end_index, window_start_index - 1, -1):
                if float(h1["close"].iloc[i]) < float(h1["open"].iloc[i]):
                    candidate_top = float(h1["open"].iloc[i])
                    candidate_bottom = float(h1["low"].iloc[i])

                    fresh_status = not (h1.iloc[i + 1:]["close"] < candidate_bottom).any()

                    passed_quality_filter = False
                    if atr_last > 0:
                        candidate_size = abs(candidate_top - candidate_bottom)
                        if candidate_size >= (atr_last * 0.25):
                            long_ob_not_lost = current_price >= (candidate_bottom - atr_last * 0.20)
                            long_ob_distance_ok = abs(current_price - ((candidate_top + candidate_bottom) / 2.0)) >= (atr_last * 0.15)
                            passed_quality_filter = long_ob_not_lost and long_ob_distance_ok

                    _register_ob_candidate(i, candidate_top, candidate_bottom, fresh_status, passed_quality_filter)

        else:
            for i in range(last_idx, max(2, last_idx - 12) - 1, -1):
                if float(h1["low"].iloc[i - 2]) - float(h1["high"].iloc[i]) >= min_gap and float(h1["close"].iloc[i]) < float(h1["open"].iloc[i]):
                    fvg_valid = True
                    break

            for i in range(window_end_index, window_start_index - 1, -1):
                if float(h1["close"].iloc[i]) > float(h1["open"].iloc[i]):
                    candidate_top = float(h1["high"].iloc[i])
                    candidate_bottom = float(h1["open"].iloc[i])

                    fresh_status = not (h1.iloc[i + 1:]["close"] > candidate_top).any()

                    passed_quality_filter = False
                    if atr_last > 0:
                        candidate_size = abs(candidate_top - candidate_bottom)
                        if candidate_size >= (atr_last * 0.25):
                            short_ob_not_lost = current_price <= (candidate_top + atr_last * 0.20)
                            short_ob_distance_ok = abs(current_price - ((candidate_top + candidate_bottom) / 2.0)) >= (atr_last * 0.15)
                            passed_quality_filter = short_ob_not_lost and short_ob_distance_ok

                    _register_ob_candidate(i, candidate_top, candidate_bottom, fresh_status, passed_quality_filter)

        # Keep selected OB in the original variables used by the rest of the function
        if ob_meta is not None:
            fresh_ob = bool(selected_ob_fresh)
        else:
            fresh_ob = False

        c = h1.iloc[-1]
        body = abs(float(c["close"]) - float(c["open"]))
        candle_range = float(c["high"]) - float(c["low"])
        br = body / candle_range if candle_range > 0 else 0.0

        displacement_valid = _is_displacement_candle(c, direction, atr_last)

        v_sma = h1["v"].rolling(20).mean().iloc[-1] if "v" in h1.columns and len(h1) >= 20 else 0
        volume_spike = float(h1["v"].iloc[-1]) > (v_sma * 1.5) if v_sma > 0 else False

        entry_price, sl, tp = 0.0, 0.0, 0.0
        entry_ready = False
        entry_source = "none"
        entry_reason = "no_entry"

        # =========================================================
        # ENTRY ENGINE
        # =========================================================
        tp_source = None
        tp_rr = None
        if entry_mode == "CONTINUATION":
            cont_lookback = 3
            search_start = max(0, last_idx - cont_lookback)

            def _calc_continuation_entry() -> Tuple[float, float, float, bool, str, Optional[int], Optional[int]]:
                """
                Returns:
                entry_price, sl, tp, entry_ready, reason, displacement_idx, pullback_idx
                """

                disp_idx = None
                disp_close = None

                for i in range(last_idx - 1, search_start - 1, -1):
                    if _is_displacement_candle(h1.iloc[i], direction, atr_last):
                        disp_idx = i
                        disp_close = float(h1["close"].iloc[i])
                        break

                if disp_idx is None:
                    return 0.0, 0.0, 0.0, False, "no_continuation_displacement", None, None

                pullback_end = min(last_idx, disp_idx + cont_lookback)
                pullback_idx = None

                for j in range(disp_idx + 1, pullback_end + 1):
                    pb = h1.iloc[j]
                    pb_high = float(pb["high"])
                    pb_low = float(pb["low"])
                    pb_close = float(pb["close"])

                    if direction == "LONG":
                        micro_retrace = pb_low <= (disp_close + max(atr_last * 0.10, 1e-9))
                        reclaim = pb_close >= disp_close
                        if micro_retrace and reclaim:
                            pullback_idx = j
                            entry_price = pb_close
                            sl = pb_low - (atr_last * 0.30)
                            tp_info = self.find_liquidity_target(
                                h1,
                                direction,
                                entry_price,
                                swing_highs,
                                swing_lows,
                                atr_last,
                                sl=sl,
                                entry_mode=entry_mode
                            )
                            tp = tp_info.get("tp")
                            tp_source = tp_info.get("source")
                            tp_rr = tp_info.get("rr")

                            if not tp:
                                tp = entry_price + (entry_price - sl) * 2.0
                                tp_source = "fallback_2r"
                                tp_rr = 2.0

                            return entry_price, sl, tp, True, "continuation_micro_pullback", disp_idx, pullback_idx
                    else:
                        micro_retrace = pb_high >= (disp_close - max(atr_last * 0.10, 1e-9))
                        reclaim = pb_close <= disp_close
                        if micro_retrace and reclaim:
                            pullback_idx = j
                            entry_price = pb_close
                            sl = pb_high + (atr_last * 0.30)
                            tp_info = self.find_liquidity_target(
                                h1,
                                direction,
                                entry_price,
                                swing_highs,
                                swing_lows,
                                atr_last,
                                sl=sl,
                                entry_mode=entry_mode
                            )
                            tp = tp_info.get("tp")
                            tp_source = tp_info.get("source")
                            tp_rr = tp_info.get("rr")

                            if not tp:
                                tp = entry_price - (sl - entry_price) * 2.0
                                tp_source = "fallback_2r"
                                tp_rr = 2.0

                            return entry_price, sl, tp, True, "continuation_micro_pullback", disp_idx, pullback_idx

                return 0.0, 0.0, 0.0, False, "no_micro_pullback", disp_idx, None

            entry_price, sl, tp, entry_ready, entry_reason, disp_idx, pullback_idx = _calc_continuation_entry()
            entry_source = entry_reason

        elif entry_mode == "RETRACEMENT":

            # =========================================================
            # ROTATIONAL LONG GUARD
            # In HTF conflict, rotational LONGs only make sense in discount.
            # =========================================================
            htf_conflict_long = (
                direction == "LONG"
                and regime_str == "rotational"
                and wb == "BULLISH"
                and db == "BEARISH"
                and hb == "BEARISH"
            )

            if htf_conflict_long and loc != "discount":
                entry_ready = False
                entry_reason = "rotational_long_needs_discount"

            else:
                # =========================================================
                # OB QUALITY FILTER
                # Avoid structurally obsolete retracement entries
                # =========================================================
                ob_valid = False

                if fresh_ob and ob_meta:

                    ob_top = float(ob_meta["top"])
                    ob_bottom = float(ob_meta["bottom"])

                    current_price = float(c["close"])

                    ob_size = abs(ob_top - ob_bottom)
                    ob_mid = (ob_top + ob_bottom) / 2.0

                    # Avoid microscopic / weak OBs
                    if atr_last > 0 and ob_size >= (atr_last * 0.25):

                        if direction == "LONG":

                            # Reject if price already deeply below OB
                            long_ob_not_lost = current_price >= (ob_bottom - atr_last * 0.20)

                            # Reject if OB is too close to current price
                            long_ob_distance_ok = abs(current_price - ob_mid) >= (atr_last * 0.15)

                            ob_valid = long_ob_not_lost and long_ob_distance_ok

                        else:

                            # Reject if price already deeply above OB
                            short_ob_not_lost = current_price <= (ob_top + atr_last * 0.20)

                            # Reject if OB is too close to current price
                            short_ob_distance_ok = abs(current_price - ob_mid) >= (atr_last * 0.15)

                            ob_valid = short_ob_not_lost and short_ob_distance_ok

                if ob_valid:

                    entry_ready = True
                    entry_source = "retracement_ob"

                    sl_buffer_mult = 0.7

                    if direction == "LONG":
                        entry_price = ob_top
                        sl = ob_bottom - (atr_last * sl_buffer_mult)
                    else:
                        entry_price = ob_bottom
                        sl = ob_top + (atr_last * sl_buffer_mult)

                    tp_info = self.find_liquidity_target(
                        h1,
                        direction,
                        entry_price,
                        swing_highs,
                        swing_lows,
                        atr_last,
                        sl=sl,
                        entry_mode=entry_mode
                    )

                    tp = tp_info.get("tp")
                    tp_source = tp_info.get("source")
                    tp_rr = tp_info.get("rr")

                    if not tp:
                        tp = (
                            entry_price + (entry_price - sl) * 2.0
                            if direction == "LONG"
                            else entry_price - (sl - entry_price) * 2.0
                        )
                        tp_source = "fallback_2r"
                        tp_rr = 2.0

                else:
                    entry_ready = False
                    entry_reason = "ob_quality_failed"

        elif entry_mode == "WATCH_ONLY":
            entry_ready = False
            entry_reason = "watch_only_regime"

        else:
            entry_ready = False
            entry_reason = "non_actionable_regime"

        target_clear = (
            (tp is not None and tp > 0 and abs(tp - float(c["close"])) >= (atr_last * 0.75))
            if atr_last > 0
            else True
        )

        h1_setup = {
            "direction": direction,
            "entry_mode": entry_mode,
            "entry_ready": entry_ready,
            "entry_source": entry_source,
            "entry_reason": entry_reason,
            "sweep_valid": sweep_valid,
            "fvg_valid": fvg_valid,
            "displacement_valid": displacement_valid,
            "fresh_ob": fresh_ob,
            "volume_spike": volume_spike,
            "target_clear": target_clear,
            "session_active": self.get_session_mode() == "SNIPER",
            "htf_context_clean": (
                (wb != "BEARISH" and db != "BEARISH" and hb != "BEARISH" and loc in {"discount", "equilibrium"})
                if direction == "LONG"
                else (wb != "BULLISH" and db != "BULLISH" and hb != "BULLISH" and loc in {"premium", "equilibrium"})
            ),
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "atr_last": atr_last,
            "tp_source": tp_source,
            "tp_rr": tp_rr,
        }

        debug["conf"] = {
            "sweep": sweep_valid,
            "fvg": fvg_valid,
            "disp": displacement_valid,
            "vol": volume_spike,
            "ob": fresh_ob,
            "target": target_clear,
            "mode": entry_mode,
            "ready": entry_ready,
            "guard": entry_reason,
        }

        if not entry_ready and entry_mode != "WATCH_ONLY":
            return {
                "htf_map": htf_map,
                "regime": regime,
                "h1_setup": h1_setup,
                "result": {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": entry_reason,
                    "direction": direction,
                    "regime": regime,
                    "entry_mode": entry_mode
                },
                "debug": debug,
            }

        result = self.score_signal(htf_map, regime, h1_setup, m5_setup=None)

        debug["score_raw"] = {
            "type": str(type(result).__name__),
            "score": result.get("score") if isinstance(result, dict) else None,
            "decision": result.get("decision") if isinstance(result, dict) else None,
            "entry_mode": result.get("entry_mode") if isinstance(result, dict) else None,
        }

        if result is None:
            return {
                "htf_map": htf_map,
                "regime": regime,
                "h1_setup": h1_setup,
                "result": {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": "score_signal_none"
                },
                "debug": debug,
            }

        if not isinstance(result, dict):
            return {
                "htf_map": htf_map,
                "regime": regime,
                "h1_setup": h1_setup,
                "result": {
                    "score": 0,
                    "decision": "SKIP",
                    "reason": "score_signal_invalid_type"
                },
                "debug": debug,
            }

        result = dict(result)

        if market_choppy:
            result["score"] -= 2
            result.setdefault("notes", []).append("choppy_penalty")

        result.update({
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "entry_mode": entry_mode,
            "entry_source": entry_source,
            "entry_ready": entry_ready,
            "tp_source": tp_source,
            "tp_rr": tp_rr,
        })

        debug["ob_audit"] = {
            "ob_meta": ob_meta,
            "ob_candidates": ob_candidates,
            "fvg_valid": fvg_valid,
            "fresh_ob": fresh_ob,
            "ob_index": ob_index,
            "ob_top": ob_top,
            "ob_bottom": ob_bottom,
            "ob_size_atr": ob_size / atr_last if atr_last > 0 and ob_size is not None else None,
            "entry_equals_ob_bottom": (
                abs(entry_price - ob_bottom) < 1e-12 if direction == "SHORT" else abs(entry_price - ob_top) < 1e-12
            ) if entry_ready and ob_top is not None and ob_bottom is not None else None,
        }

        return {
            "htf_map": htf_map,
            "regime": regime,
            "h1_setup": h1_setup,
            "result": result,
            "debug": debug,
        }

    # =========================================================
    # 🎯 STAGE 2: M5 ENRICHMENT
    # =========================================================
    def enrich_with_m5(self, context: Dict[str, Any], df_m5: pd.DataFrame) -> Dict[str, Any]:
        base_result = context.get("result")

        if not isinstance(base_result, dict):
            return {
                "score": 0,
                "decision": "SKIP",
                "reason": "invalid_base_result"
            }

        if df_m5 is None or df_m5.empty:
            return dict(base_result)

        m5 = df_m5.copy().reset_index(drop=True)
        m5 = m5.dropna(subset=[c for c in ["timestamp", "open", "high", "low", "close"] if c in m5.columns])

        if len(m5) < 6:
            return dict(base_result)

        h1_setup = context.get("h1_setup", {})
        direction = str(h1_setup.get("direction", "")).upper().strip()
        entry_mode = str(h1_setup.get("entry_mode", "RETRACEMENT")).upper().strip()

        if direction not in {"LONG", "SHORT"}:
            return dict(base_result)

        m5_atr = float(m5["atr"].dropna().iloc[-1]) if "atr" in m5.columns and not m5["atr"].dropna().empty else 0.0

        cw, prom = (1, 0.15) if self.get_session_mode() == "SNIPER" else (2, 0.25)
        sh_m5, sl_m5 = self.identify_swings(m5, custom_window=cw, prominence_mult=prom)

        last = m5.iloc[-1]
        prev = m5.iloc[-2]
        prev2 = m5.iloc[-3]

        last_body = abs(float(last["close"]) - float(last["open"]))
        last_range = float(last["high"]) - float(last["low"])
        last_br = last_body / last_range if last_range > 0 else 0.0

        m5_confirmed = False
        m5_reason = "no_m5_confirmation"

        # =========================================================
        # CONTINUATION MODE
        # - M5 only confirms micro continuation momentum
        # - not classic reversal CHoCH logic
        # =========================================================
        if entry_mode == "CONTINUATION":

            if direction == "LONG":
                ref_level = float(sh_m5[-1]["price"]) if sh_m5 else None

                m5_confirmed = (
                    ref_level is not None
                    and float(last["close"]) > ref_level
                    and float(last["close"]) > float(last["open"])
                    and last_body >= (m5_atr * 0.35)
                    and last_br >= 0.50
                    and float(last["close"]) >= float(prev["close"])
                )

            else:  # SHORT
                ref_level = float(sl_m5[-1]["price"]) if sl_m5 else None

                m5_confirmed = (
                    ref_level is not None
                    and float(last["close"]) < ref_level
                    and float(last["close"]) < float(last["open"])
                    and last_body >= (m5_atr * 0.35)
                    and last_br >= 0.50
                    and float(last["close"]) <= float(prev["close"])
                )

            m5_reason = "continuation_micro_confirm" if m5_confirmed else "continuation_no_confirm"

        # =========================================================
        # RETRACEMENT MODE
        # - keep classic M5 CHoCH / continuation-away-from-OB logic
        # =========================================================
        else:
            breakout = m5.iloc[-2]
            confirm = m5.iloc[-1]

            b_m5 = abs(float(breakout["close"]) - float(breakout["open"]))
            rng_m5 = float(breakout["high"]) - float(breakout["low"])
            br_m5 = b_m5 / rng_m5 if rng_m5 > 0 else 0.0

            if direction == "LONG" and sh_m5:
                breakout_valid = (
                    float(breakout["close"]) > float(sh_m5[-1]["price"])
                    and float(breakout["close"]) > float(breakout["open"])
                    and b_m5 >= (m5_atr * 0.5)
                    and br_m5 >= 0.55
                )

                follow_through = (
                    float(confirm["close"]) >= (float(breakout["high"]) + float(breakout["low"])) / 2.0
                    and float(confirm["low"]) >= float(prev["low"])
                )

                m5_confirmed = breakout_valid and follow_through
                m5_reason = "retracement_long_confirm" if m5_confirmed else "retracement_long_fail"

            elif direction == "SHORT" and sl_m5:
                breakout_valid = (
                    float(breakout["close"]) < float(sl_m5[-1]["price"])
                    and float(breakout["close"]) < float(breakout["open"])
                    and b_m5 >= (m5_atr * 0.5)
                    and br_m5 >= 0.55
                )

                follow_through = (
                    float(confirm["close"]) <= (float(breakout["high"]) + float(breakout["low"])) / 2.0
                    and float(confirm["high"]) <= float(prev["high"])
                )

                m5_confirmed = breakout_valid and follow_through
                m5_reason = "retracement_short_confirm" if m5_confirmed else "retracement_short_fail"

        m5_setup = {
            "choch_confirmed": m5_confirmed,
            "m5_reason": m5_reason,
            "entry_mode": entry_mode
        }

        result = self.score_signal(
            context["htf_map"],
            context["regime"],
            context["h1_setup"],
            m5_setup
        )

        if not isinstance(result, dict):
            result = dict(base_result)
        else:
            result = dict(result)

        if "choppy_penalty" in base_result.get("notes", []):
            result["score"] -= 2
            result.setdefault("notes", []).append("choppy_penalty")

        result.update({
            "entry_price": h1_setup.get("entry_price", 0.0),
            "sl": h1_setup.get("sl", 0.0),
            "tp": h1_setup.get("tp", 0.0),
            "m5_choch": m5_confirmed,
            "m5_reason": m5_reason,
            "entry_mode": entry_mode
        })

        return result

    # =========================================================
    # ☠️ EMERGENCY KILL SWITCH
    # =========================================================
    def is_poi_invalidated(self, saved_signal: dict, current_context: dict) -> bool:
        """
        Emergency kill switch for stale / invalidated signals.
        Kill instantly if HTF bias flips against the signal OR Price violently breaks through SL zone.
        This bypasses miss_count buffer entirely.
        """
        try:
            if not saved_signal or not current_context:
                return False

            result = current_context.get("result", {})
            htf_map = current_context.get("htf_map", {})
            direction = str(saved_signal.get("direction", "")).upper()
            
            current_price = float(current_context.get("market_price", 0.0))
            if not np.isfinite(current_price) or current_price <= 0:
                current_price = float(result.get("entry_price", 0.0))

            sl = float(saved_signal.get("sl", 0.0))
            
            d1_bias = str(htf_map.get("d1_bias", "CHOPPY")).upper()
            h4_bias = str(htf_map.get("h4_bias", "CHOPPY")).upper()
            
            atr_buffer = 0.0
            h1_setup = current_context.get("h1_setup", {})
            atr_last = float(h1_setup.get("atr_last", 0.0))

            if np.isfinite(atr_last) and atr_last > 0:
                atr_buffer = atr_last * 0.30

            if direction == "LONG":
                # Hard HTF bearish flip
                if d1_bias == "BEARISH" and h4_bias == "BEARISH":
                    return True
                # Price nuked below SL
                if current_price < (sl - atr_buffer):
                    return True

            elif direction == "SHORT":
                # Hard HTF bullish flip
                if d1_bias == "BULLISH" and h4_bias == "BULLISH":
                    return True
                # Price nuked above SL
                if current_price > (sl + atr_buffer):
                    return True

            return False
        except Exception:

            return False