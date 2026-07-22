# MODULE_INTENT.md — SMC Signal Radar Bot

**Version:** Final (Post-Refinement Cycle)
**Status:** All 5 modules locked
**Companion to:** `BLUEPRINT.md`
**Purpose:** Per-module behavioral contracts. Defines **what each module should do**, bukan **how it implements**. Future refactor harus pass test: "Apakah implementation baru masih honor intent ini?"

---

## How to Use This Document

For each module: Strategic purpose → Boundary → Method intents → Invariants.

**For refinement:** Implementation changes OK kalau intent preserved. Intent changes require explicit revision + Change Log entry.

**For onboarding:** Read in order strategy.py → main.py → api_handler.py → executor.py → config.py. Read alongside BLUEPRINT.md.

**For drift detection:** Kalau code behavior tidak match intent statement → either code drifted (fix code) atau intent evolved (update document).

---

## Module 1: `strategy.py` — `SMCStrategy` class

### Module-Level Strategic Purpose

Core SMC interpretation engine. Mengubah raw OHLCV data menjadi structured trading hypotheses dengan format: direction + entry zone + invalidation level + target + confidence score. Module ini **stateless terhadap memory bot** (tidak tahu signal yang sedang aktif) dan **stateless terhadap eksekusi** (tidak tahu apakah trade pernah taken). Single concern: **structural interpretation per cycle**.

### Module Boundary

**Owns:** HTF context resolution, regime classification, directional bias selection, entry calculation (mode-aware), scoring & decision tiering, structural identification (OB/FVG/sweep/displacement), liquidity targeting, kill switch logic.

**Does NOT own:** Memory state, signal lifecycle, operator output, slot management, persistence, TTL enforcement, API calls.

### Method Intent Lock

#### `get_session_mode()`
- **Strategic purpose:** Filter operational window berdasarkan asumsi market quality di waktu tertentu.
- **Market condition targeted:** Crypto 24/7 dengan asumsi liquidity tinggi di NY hours, choppy/manipulatif di off-hours + weekend.
- **Expected behavior:** Return `SNIPER` di prime window (mid-week, 07:00-22:00 UTC), `STRICT` di weekend dan off-hours.
- **Expected output:** String `"SNIPER"` atau `"STRICT"`.

#### `_cached_bias()`
- **Strategic purpose:** Performance optimization untuk bias calculation lintas multiple TF.
- **Market condition targeted:** N/A (utility).
- **Expected behavior:** Cache by TF name + last timestamp. Evict oldest kalau penuh.
- **Expected output:** Bias string (`"BULLISH"` / `"BEARISH"` / `"CHOPPY"`).

#### `_is_market_choppy()`
- **Strategic purpose:** Detect environment di mana SMC structural concept tidak reliable.
- **Market condition targeted:** Market dengan high wick-to-body ratio, small body, atau extreme volatility relatif ke price.
- **Expected behavior:** Return True kalau salah satu dari tiga kondisi terdeteksi. Bot menggunakan output sebagai penalty scoring, bukan hard skip.
- **Expected output:** Boolean.

#### `_momentum_weakening()`
- **Strategic purpose:** Detect divergence antara momentum aktif vs momentum baru. Early warning bahwa continuation tidak smooth.
- **Market condition targeted:** Trend established tapi mulai kehilangan steam.
- **Expected behavior:** Compare 5-candle recent vs 5-candle previous. Flag kalau trend magnitude atau range turun substansial.
- **Expected output:** Boolean.

#### `_detect_sweep()`
- **Strategic purpose:** Identify liquidity grab event — SMC signature yang menandakan institutional accumulation/distribution.
- **Market condition targeted:** Post-consolidation atau pre-reversal di mana liquidity di-target sebelum directional move.
- **Expected behavior:** Pattern: candle wick beyond reference swing + reclaim close + minimum depth (ATR-relative) + minimum reclaim body strength.
- **Expected output:** Tuple `(detected: bool, metadata: Optional[Dict])`.

#### `_get_previous_day_high_low()`
- **Strategic purpose:** Identify Daily liquidity reference levels (PDH/PDL) untuk intraday targeting.
- **Market condition targeted:** Any intraday context.
- **Expected behavior:** Aggregate H1 data ke daily candles, return previous day extremes.
- **Expected output:** Dict dengan `pdh`, `pdl`, `day`, atau None.

#### `_cluster_liquidity_levels()`
- **Strategic purpose:** Identify EQH/EQL clusters — primary institutional targets.
- **Market condition targeted:** Markets dengan multiple swing rejections di level yang sama.
- **Expected behavior:** Cluster swing levels yang berdekatan (ATR-relative tolerance).
- **Expected output:** List of cluster dicts dengan level, count, indices.

#### `find_liquidity_target()`
- **Strategic purpose:** TP selection berbasis institutional logic, bukan arbitrary RR multiple.
- **Market condition targeted:** Any directional setup dengan entry + SL.
- **Expected behavior:** Hierarchy: EQH/EQL clusters > PDH/PDL > single swing. Apply RR floor (1.2). Mode-aware sorting: CONTINUATION prefers distance (nearest), RETRACEMENT prefers strength (best liquidity).
- **Expected output:** Dict dengan `tp`, `source`, `rr`, `reason`. TP = None kalau no acceptable target.

#### `identify_swings()`
- **Strategic purpose:** Base structural element identification untuk semua SMC concepts.
- **Market condition targeted:** Any market dengan oscillation structure.
- **Expected behavior:** Window-based pivot identification dengan optional ATR-relative prominence filter.
- **Expected output:** Tuple `(swing_highs, swing_lows)`.

#### `build_htf_map()`
- **Strategic purpose:** Top-down structural analysis. Multi-TF context yang menjadi anchor untuk H1 decisions.
- **Market condition targeted:** Any market. Mandatory context, bukan optional.
- **Expected behavior:** Compute Weekly bias (price vs prev-week eq), D1 bias (cached EMA + structure), H4 bias (resampled, cached), location (discount/equilibrium/premium), PDH/PDL, momentum_weakening flag.
- **Expected output:** Dict berisi semua HTF facts, atau None kalau insufficient data.

#### `_derive_simple_bias_from_df()`
- **Strategic purpose:** Generic bias detector untuk arbitrary TF.
- **Market condition targeted:** Any TF.
- **Expected behavior:** Multi-factor scoring (structure HH/HL atau LH/LL, EMA50/200 relationship, EMA slope). Minimum score + clear dominance untuk pick directional bias.
- **Expected output:** `"BULLISH"` / `"BEARISH"` / `"CHOPPY"`.

#### `classify_regime()`
- **Strategic purpose:** Determine market structure mode di H1. Regime dictates execution philosophy.
- **Market condition targeted:** All H1 states. Spectrum: expansion → rotational → accumulation/distribution → compression → neutral.
- **Expected behavior:** Majority-vote system (2-of-3) untuk compression dan expansion detection. Drift veto: strong drift overrides compression. Fall-through ke rotational untuk middle-ground. Default neutral.
- **Expected output:** Regime label string: `expansion_up`, `expansion_down`, `accumulation`, `distribution`, `compression`, `rotational`, `neutral`, `unknown`.

#### `score_signal()`
- **Strategic purpose:** Convert structural observations menjadi numeric confidence + operator-actionable decision.
- **Market condition targeted:** Setup yang sudah passed structural pipeline.
- **Expected behavior:** Mode-aware scoring:
  - **CONTINUATION:** displacement = hard gate (zero score kalau absent), bonus untuk sweep/FVG/OB, penalty untuk weak volume
  - **RETRACEMENT:** sweep + OB = co-primary (penalty kalau absent), FVG + displacement = bonus
  - **WATCH_ONLY:** semua confirmation = bonus (no hard gate), require strong HTF context
  
  Apply HTF alignment bonuses (weekly > d1 > h4), location bonus when aligned, regime bonus/penalty, RR engine (hard floor 1.2, escalating bonus above), momentum_weakening penalty, M5 enrichment bonus when present. Translate final score ke decision tier dengan mode-specific thresholds.
- **Expected output:** Dict dengan `score`, `decision` (SKIP/WATCH/VALID/A+), `direction`, `notes` (audit trail), `regime`, `rr`, `entry_mode`.

#### `build_signal_context()`
- **Strategic purpose:** Canonical pipeline runner. Single entry point untuk full structural analysis.
- **Market condition targeted:** Any market state. Universal pipeline.
- **Expected behavior:** Sequential pipeline:
  1. Input validation
  2. Choppiness check
  3. HTF map build
  4. Regime classification
  5. Weighted direction selection (weekly dominant, location context-aware, fallback H1, rotational anchor override)
  6. Hard veto (weekly + expansion regime conflict)
  7. Regime → entry mode mapping
  8. Structural identification (sweep, FVG, OB)
  9. Mode-specific entry calculation:
     - **CONTINUATION:** displacement → micro-pullback search → entry at pullback close
     - **RETRACEMENT:** rotational LONG guard (discount-only kalau HTF conflict) → OB quality filter → entry at OB top/bottom
  10. Score signal
  11. Apply choppy penalty
  12. Return complete context dengan debug breadcrumbs
- **Expected output:** Dict dengan `htf_map`, `regime`, `h1_setup`, `result`, `debug`. Setup yang fail di any stage tetap return context dengan clear `reason`.

#### `enrich_with_m5()`
- **Strategic purpose:** LTF confirmation layer untuk high-quality setups (score ≥ 8). Micro-timing validator, bukan independent signal source.
- **Market condition targeted:** Setup yang passed H1 structural pipeline.
- **Expected behavior:** Mode-aware M5 logic:
  - **CONTINUATION:** confirm micro continuation momentum (M5 swing break + body strength + close trajectory)
  - **RETRACEMENT:** classic CHoCH validation (M5 break of swing + follow-through candle)
  
  Add bonus ke score kalau confirmed. Preserve entry/SL/TP dari H1 setup (M5 hanya validator, bukan recalculator). Re-apply choppy penalty kalau base result had it.
- **Expected output:** Updated result dict dengan `m5_choch` flag dan `m5_reason`.

#### `is_poi_invalidated()`
- **Strategic purpose:** Emergency kill switch. Detect definitive structural invalidation event yang require immediate signal removal.
- **Market condition targeted:** Two scenarios:
  1. HTF bias hard flip (D1 + H4 both flipped opposite to signal direction)
  2. Price violently breached SL beyond ATR buffer tolerance
- **Expected behavior:** Compare signal direction dengan current HTF state + current price. Conservative: errors return False (don't kill on uncertainty).
- **Expected output:** Boolean. True = kill immediately, False = signal still valid.

### Module-Level Invariants — strategy.py

1. **Stateless per cycle.** Tidak ada state lintas cycle kecuali `_bias_cache` (performance optimization).
2. **Closed-candle structural analysis.** Semua structural identification menggunakan closed candle. Live forming candle hanya untuk `is_poi_invalidated`.
3. **HTF dominance hierarchy.** Weekly > D1 > H4. Lower TF tidak boleh overpower higher TF.
4. **Mode separation.** CONTINUATION dan RETRACEMENT punya different gates, weights, calculation. No mode-mixing artifacts.
5. **Conservative defaults.** Ambiguous → SKIP. Failed method → safe sentinel, bukan crash.
6. **Audit trail mandatory.** Setiap decision traceable via debug atau notes. No black-box scoring.

---

## Module 2: `main.py`

### Module-Level Strategic Purpose

Operational orchestrator. Bridge antara stateless structural analysis (strategy.py) dan stateful operator-facing output (memory + dashboard). Single concern: **per-cycle execution coordination** — fetch data, invoke strategy, manage signal lifecycle, persist state, emit operator logs.

### Module Boundary

**Owns:** Loop control, memory management, slot allocation, operator output (logs + dashboard), TTL enforcement, persistence, error containment.

**Does NOT own:** Structural analysis, scoring decisions, directional logic, entry calculations — all delegated to strategy.py.

**Trust contract:** main.py trusts strategy.py completely — never second-guesses scoring, never re-interprets HTF context, never overrides decisions.

### Function Intent Lock

#### `load_memory()`
- **Strategic purpose:** Resurrect operator state across bot restarts. Persisted signals adalah commitment yang sudah surfaced ke operator — restart tidak boleh menghapus context yang operator sudah aware.
- **Market condition targeted:** N/A (infrastructure).
- **Expected behavior:** Read persistent storage, validate required fields (score, decision, entry_price, sl, tp, direction), coerce types untuk consistency, silently skip malformed entries. Return empty dict kalau file tidak ada atau corrupted.
- **Expected output:** Dict `{symbol: signal_data}` atau empty dict.
- **Behavioral invariant:** Defensive + idempotent. Partial-validity entries → dropped silently (operator akan re-discover kalau masih valid).

#### `save_memory()`
- **Strategic purpose:** Persist signal state untuk survive crash/restart.
- **Market condition targeted:** N/A (infrastructure).
- **Expected behavior:** Atomic write via temp file + rename. Tidak ada partial-state corruption kalau crash mid-write. Failure logged tapi tidak crash bot.
- **Expected output:** None (side effect: file written).
- **Behavioral invariant:** Best-effort, bukan hard requirement. Bot tetap functional walaupun save fails.

#### `cleanup_expired_signals()`
- **Strategic purpose:** Time-based safety net untuk signals yang lolos dari kill switch dan slot management.
- **Market condition targeted:** Temporal pruning, tidak market-state specific.
- **Expected behavior:** Tiered TTL:
  - `manual_action` status → longest TTL (operator commit deserves patience)
  - `miss_count > 0` (tapped proxy) → medium TTL
  - Untapped (newly discovered, never missed) → shortest TTL
  
  Apply TTL based on age sejak `timestamp` field. Expire by deletion. Log expiry.
- **Expected output:** Modified active_signals dict (in-place + return).
- **Behavioral invariant:** TTL adalah secondary cleanup. Primary cleanups: kill switch (structural) + slot management (score-based).

#### `print_dashboard()`
- **Strategic purpose:** Operator-facing summary. Primary surface untuk trade decisions.
- **Market condition targeted:** N/A (presentation).
- **Expected behavior:** Sort signals by score descending. Format setiap row: symbol, direction, mode-aware decision label (WATCH-CONT/WATCH-RETR/WATCH-COMP), score, entry, SL, TP, miss_count. Empty state message kalau no signals.
- **Expected output:** None (side effect: console output).
- **Behavioral invariant:** Read-only operator surface. No decision logic. Setiap field directly derivable dari payload tanpa re-interpretation.

#### `run()` — Main Orchestration Loop

**Strategic purpose:** Single canonical execution flow yang mengkoordinasikan semua components: data fetch, structural analysis delegation, signal lifecycle management, persistence, operator output.

**Market condition targeted:** Continuous operation across all market conditions. Loop tidak skip based on market state — semua state harus di-scan dan di-evaluate.

**Expected behavior — per-cycle pipeline:**

**Phase 1 — Universe acquisition:**
- Fetch top volume pairs (universe boundary)
- Skip cycle dengan backoff kalau API failure

**Phase 2 — Per-symbol processing:**

Untuk setiap symbol:

1. **Data acquisition** — Fetch D1, H1, M5 OHLCV dengan rate-limit-friendly pacing (sleep 0.20s). Drop forming candles untuk structural analysis.

2. **Sufficiency check** — Skip kalau insufficient data depth (D1 < 10, H1 < 50).

3. **Strategy invocation** — Delegate ke `build_signal_context()`. Authoritative.

4. **Live price binding** — Attach forming H1 candle close ke context as `market_price`. Critical: structural analysis uses closed candle, runtime invalidation uses live candle.

5. **Kill switch (post-memory)** — Untuk signals already in memory, check `is_poi_invalidated`. Kill + log `[☠️ KILL]`.

6. **M5 enrichment (selective)** — Only invoke kalau score ≥ 8. Validate M5 timestamp aligned dengan H1 (no cross-period contamination).

7. **Atomic logging** — Single log block per symbol: SCAN + HTF + CONF + RAW debug breadcrumbs. No interleaving dengan other symbols.

8. **Pre-memory invalidation check** — Untuk new candidates, check `is_poi_invalidated` dengan live market price. Skip + log `[⚠️ STALE]` kalau dead.

9. **Memory engine** — Mode-aware lifecycle:
   - Decision ∈ {WATCH, VALID, A+} + valid levels → insert/update memory
   - Slot management:
     - Already in memory → update payload
     - Slot available (< MAX_OPEN_POSITIONS) → insert
     - Slot full + score > weakest → replace, log `[🔄 REPLACE]`
     - Slot full + score ≤ weakest → reject, log `[🚫 CAPPED]`
   - Invalid levels + valid decision → log `[⚠️ GUARD]`, increment miss_count
   - SKIP → increment miss_count kalau in memory, evict kalau exceed MISS_COUNT_LIMIT

10. **Error containment** — Per-symbol try/except dengan traceback. One bad symbol tidak crash cycle.

**Phase 3 — Cycle cleanup:**
- TTL expiry via `cleanup_expired_signals`
- Persist memory via `save_memory`
- Render dashboard via `print_dashboard`
- Sleep 60 seconds

**Phase 4 — Lifecycle control:**
- `KeyboardInterrupt` → graceful shutdown
- Critical exception → log + 10s pause + retry (loop tidak die)

**Expected output:** None (perpetual side effects: console logs, persistence, dashboard).

### Sub-Invariants — `run()`

1. **Closed-candle structural / live-candle runtime separation.** Hard rule.
2. **Kill switch dual-stage.** Post-memory (existing signals) + pre-memory (new candidates). Same function (`is_poi_invalidated`), different inputs.
3. **Logging atomicity.** All prints untuk satu symbol consecutive, single `print` call dengan `\n`-joined list.
4. **Memory mutation discipline.** Hanya main.py mutate memory. strategy.py read-only terhadap memory state.
5. **Error isolation.** Per-symbol + per-cycle try/except. Critical exceptions: pause + retry, never kill bot.
6. **Slot management invariant.** MAX_OPEN_POSITIONS hard cap. Replace adalah score-based, deterministic.
7. **Miss count semantics.** Increment on miss/invalid, reset on refresh, evict at MISS_COUNT_LIMIT. Distinct dari TTL.

### Embedded Constants

**`STATE_FILE = "active_pois.json"`**
Hardcoded di main.py. Architectural artifact patut diconsolidate dengan `ACTIVE_POIS_FILE` di config.py.

**`MISS_COUNT_LIMIT = 3`**
Confidence decay threshold. Lightweight default, philosophically should be small.

### Module-Level Invariants — main.py

1. **Separation of concerns.** Orchestrator, bukan analyzer.
2. **Stateless per-symbol processing.** Symbols independent.
3. **Operator-facing transparency.** Setiap state transition loggable.
4. **Crash resilience.** Errors contained, never escalate to termination kecuali user shutdown.
5. **Read-only delegation.** strategy.py results trusted completely.
6. **Defensive persistence.** Save/load → graceful degradation.

---

## Module 3: `api_handler.py` — `BinanceHandler` class

### Module-Level Strategic Purpose

External data layer. Single source of truth untuk **all interactions dengan Binance**: market metadata, universe discovery, OHLCV historical data, position state. Module ini isolates bot dari API contract details — strategy.py dan main.py tidak perlu tahu Binance specifics.

### Module Boundary

**Owns:** API connection management, exchange info injection, HTTP request orchestration, raw data → DataFrame transformation, ATR computation di fetch level, position state retrieval.

**Does NOT own:** Trading logic, signal interpretation, strategy decisions, memory persistence. Returns raw market facts, bukan opinions.

**Trust contract:** Caller trusts that data returned represents market state akurat at fetch time. Module tidak embed analytical bias di data — semua field returned as-fetched.

---

### Sub-Class: `LockedBinance`

- **Strategic purpose:** URL rewriter untuk routing private API calls ke **demo endpoint** (testnet). Transparent intercept signed requests, redirect ke demo-fapi.binance.com untuk safe testing.
- **Market condition targeted:** N/A (infrastructure).
- **Expected behavior:** Inherit ccxt.binance untuk public endpoints. Intercept only signed requests. Rewrite URL: `binance.com/...` → `demo-fapi.binance.com/...`. Public requests pass through unchanged.
- **Expected output:** Standard ccxt request object dengan URL rewritten kalau private.
- **Behavioral invariant:** **Demo-only contract.** Hardcoded ke demo endpoint. Production refactor required untuk real-money execution.

---

### Method Intent Lock

#### `__init__()`
- **Strategic purpose:** Initialize API client dengan demo routing + bootstrap exchange info untuk symbol resolution.
- **Market condition targeted:** N/A (constructor).
- **Expected behavior:** Create LockedBinance client dengan futures defaults + rate limit handling. Manually load exchange info (bypass ccxt's auto-load).
- **Expected output:** None (constructor).

#### `_load_exchange_info_manually()`
- **Strategic purpose:** Inject market metadata ke ccxt client tanpa relying pada ccxt's auto-load. Bypass design protects bot dari upstream ccxt parsing bugs.
- **Market condition targeted:** N/A (bootstrap).
- **Expected behavior:**
  - Fetch raw exchange info dari Binance Futures endpoint
  - Filter ke PERPETUAL contracts dengan USDT quote asset (bot universe scope)
  - Extract `tickSize` (price precision) dan `stepSize` (amount precision) dari filters
  - Construct ccxt-compatible market dict per symbol
  - Inject ke `client.markets`, `client.markets_by_id`, `client.symbols`
  - Set `markets_loaded = True`
  - Log success count
- **Expected output:** None (side effect: client.markets populated).
- **Behavioral invariant:**
  - **Universe boundary:** Only USDT-perpetual pairs. Spot, coin-margined, non-USDT excluded.
  - **Failure tolerance:** Kalau gagal, bot tetap functional dengan empty markets dict.

#### `get_top_volume_coins()`
- **Strategic purpose:** Universe acquisition. Determine **scope of analysis** per cycle — bot hanya analyze top-N pairs by 24h volume.
- **Market condition targeted:** All markets. Volume sorting adalah liquidity proxy — high volume = better SMC structural validity (institutional participation).
- **Expected behavior:**
  - Fetch 24hr ticker dari Binance Futures public endpoint
  - Detect API rate-limit response → log + return empty
  - Filter ke USDT pairs, exclude special pairs dengan underscore (delisted/legacy)
  - Sort by `quoteVolume` descending
  - Take top N, convert symbol format (BTCUSDT → BTC/USDT)
- **Expected output:** List of `{'symbol': str, 'volume': float}` atau empty list on failure.
- **Behavioral invariant:**
  - **Dynamic universe.** Top 30 hari ini bisa beda dengan top 30 cycle berikutnya. Tidak track historical membership.
  - **Volume-based filter implicit assumption:** High volume = SMC-valid.
  - **Empty return acceptable:** Caller handle dengan sleep + retry.

#### `fetch_data_with_atr()`
- **Strategic purpose:** Core OHLCV acquisition. Single method untuk semua TF needs (D1, H1, M5) dengan consistent shape + ATR pre-computed.
- **Market condition targeted:** All markets. Method agnostic terhadap regime atau direction.
- **Expected behavior:**
  - Fetch klines dari Binance Futures public endpoint
  - HTTP failure → return None
  - Parse to DataFrame dengan strict column ordering
  - Cast OHLCV + volume to float
  - Convert timestamp dari Unix ms ke pandas datetime
  - Compute ATR dengan configured period via pandas_ta
  - Return DataFrame dengan columns: `[timestamp, open, high, low, close, v, atr]`
- **Expected output:** DataFrame atau None on failure.
- **Behavioral invariant:**
  - **Consistent schema across TFs.** D1, H1, M5 semua return shape yang sama.
  - **ATR mandatory.** Method bernama `fetch_data_with_atr` — ATR adalah part of contract.
  - **Last candle is forming.** Caller responsible untuk drop kalau butuh closed-only.
  - **No interpretation.** Raw OHLCV returned as-is.

#### `get_active_positions()`
- **Strategic purpose:** Position state retrieval. Currently unused di main loop — available untuk future integration kalau bot expand ke execution layer.
- **Market condition targeted:** N/A.
- **Expected behavior:**
  - Fetch position risk data via authenticated endpoint (routes ke demo)
  - Filter ke positions dengan non-zero amount
  - Filter ke USDT pairs
  - Convert symbol format ke standard
- **Expected output:** List of symbol strings atau empty list on failure.
- **Behavioral invariant:**
  - **Dormant capability.** Exists untuk forward compatibility.
  - **Demo-bound.** Returns demo positions karena LockedBinance routes ke demo.

### Module-Level Invariants — api_handler.py

1. **Demo-bound.** Authenticated calls route ke demo-fapi.binance.com.
2. **Hybrid data path.** Public via raw HTTP, authenticated via ccxt.
3. **USDT-perp-only universe.** Hardcoded filter, intentional.
4. **Defensive failure.** Every method returns safe sentinel on failure.
5. **ATR mandatory in OHLCV.** `fetch_data_with_atr` always includes ATR column.
6. **No interpretation layer.** Raw market facts returned. Analysis happens downstream.
7. **Schema consistency.** All OHLCV returns identical shape regardless of TF.
8. **Volume-based universe.** Implicit assumption: high volume = SMC-valid.

---

## Module 4: `executor.py` — `BinanceExecutor` class

### Module-Level Strategic Purpose

**Trade execution layer.** Bridge antara signal hypothesis (output dari strategy.py) dan actual market order placement. Module ini convert structured signal menjadi:
1. Account configuration (margin mode, leverage)
2. Position sizing (risk-based + margin-capped)
3. Limit order placement
4. Operator notification (Telegram + console)

### Module Boundary

**Owns:** Account setup, position sizing math, order placement (limit entry), Telegram alerting, manual-action communication to operator.

**Does NOT own:** Signal generation, structural analysis, signal lifecycle management, memory state. Stateless terhadap signal history.

**Trust contract:** Module trusts caller that signal sudah validated upstream. No re-validation atau sanity check terhadap signal quality.

### Critical Status Note — Currently Dormant

**Module ini TIDAK terintegrasi ke main.py current flow.** Bot scope saat ini adalah **radar-only**. Tidak ada call ke `BinanceExecutor` di mana pun di main loop.

Status: **Dormant capability** — locked sekarang supaya kalau ada decision untuk activate execution mode di future, intent sudah established.

### Method Intent Lock

#### `__init__()`
- **Strategic purpose:** Initialize executor dengan exchange client reference + risk limit ceiling.
- **Market condition targeted:** N/A.
- **Expected behavior:** Store client reference, import margin limit dari config.
- **Expected output:** None.

#### `setup_account()`
- **Strategic purpose:** Pre-trade account configuration. Set margin mode dan dynamically calculate appropriate leverage based on signal's SL distance. Return **confirmed leverage** dari Binance.
- **Market condition targeted:** Per-trade configuration. Different signals butuh different leverage:
  - Tight SL (small % distance) → higher leverage allowed
  - Wide SL (large % distance) → lower leverage required
- **Expected behavior:**
  1. Set margin mode ke ISOLATED (per-position risk isolation)
  2. Compute SL distance as percentage of entry
  3. Apply 35% safety buffer (prevent edge-case liquidation)
  4. Calculate max safe leverage: `1 / safe_sl_percentage`
  5. Clamp leverage ke [1, 125]
  6. **Request** leverage to Binance, **capture actual leverage returned** (Binance may downscale)
  7. Return actual leverage
  8. Failure fallback: return 20x default
- **Expected output:** Integer leverage value (1-125).
- **Behavioral invariant:**
  - **Trust Binance response, not local calculation.** Binance may reject due to tier/symbol limits.
  - **Safety buffer mandatory.** 35% buffer = defensive margin terhadap slippage + wick noise.

#### `calculate_position_size()`
- **Strategic purpose:** Position sizing dengan dual constraint: **fixed risk per trade** AND **maximum margin exposure cap**. Algoritma named "The Margin Presser".
- **Market condition targeted:** All trades. Adaptive terhadap volatility.
- **Expected behavior:**
  1. Calculate ideal notional: `risk_amount / sl_percentage`
  2. Calculate expected margin: `ideal_notional / actual_leverage`
  3. **Margin check:** kalau expected margin > MAX_MARGIN_ALLOWED:
     - Scale down notional: `notional = MAX_MARGIN * actual_leverage`
     - Calculate actual risk akan jadi (akan < target risk amount)
     - Log scale-down event
  4. Apply minimum notional floor ($6.00 — Binance minimum)
  5. Convert notional → quantity → apply symbol precision
- **Expected output:** Tuple `(quantity: str, error_message: str)`.
- **Behavioral invariant:**
  - **Risk-fixed > size-fixed.** Default targets exact risk amount.
  - **Margin cap is HARD limit.** Saat conflict dengan target risk, margin cap wins.
  - **Minimum order floor mandatory** (Binance constraint).

#### `send_telegram_alert()`
- **Strategic purpose:** Out-of-band operator notification. Push alert sehingga operator aware bahwa LIMIT entry placed dan **manual SL/TP action required**.
- **Market condition targeted:** Post-order placement. Critical karena bot **does NOT place SL/TP orders**.
- **Expected behavior:**
  1. Skip silently kalau Telegram credentials not configured
  2. Format HTML message dengan trade details + manual action warning
  3. POST ke Telegram Bot API dengan 5s timeout
  4. Log success/failure
- **Expected output:** None (side effect: Telegram push + console log).
- **Behavioral invariant:**
  - **Manual action emphasis mandatory.** Message MUST clearly communicate that SL/TP butuh manual action.
  - **Best-effort delivery.** Telegram failure tidak fail order placement.

#### `place_order_pair()`
- **Strategic purpose:** Main execution entry point. Misleading name — method places only entry order (limit), bukan actual "pair" of orders. SL/TP manual oleh operator.
- **Market condition targeted:** Per-signal execution.
- **Expected behavior — sequential pipeline:**
  1. **Account setup** — Call `setup_account()`, capture actual leverage
  2. **Size calculation** — Call `calculate_position_size()` dengan captured leverage
  3. **Order placement** — LIMIT order: GTC, side BUY/SELL, price at entry, precision-adjusted quantity
  4. **API call** — Place via `fapiPrivatePostOrder`. Capture exception.
  5. **Operator notification** (dual-channel): Console + Telegram
- **Expected output:** Tuple `(success: bool, message: str)`.
- **Behavioral invariant:**
  - **Entry-only execution.** Method places only entry limit order.
  - **LIMIT, not MARKET.** Bot pricing philosophy: enter exactly at calculated structural level, atau no fill.
  - **GTC time-in-force.** Order hidup sampai filled atau cancelled.
  - **Dual notification mandatory.** Operator harus aware via two channels minimum.

### Module-Level Invariants — executor.py

1. **Semi-automated by design.** Entry auto, protective orders manual.
2. **LIMIT-only entry.** No market orders. Exact-level fills atau no fill.
3. **Risk-first sizing.** Risk amount targeted, margin cap as hard ceiling.
4. **Leverage adaptive.** Per-trade computed, Binance-confirmed value used.
5. **Safety buffer mandatory.** 35% buffer on SL distance.
6. **Dual notification.** Console + Telegram redundant alert.
7. **Stateless executor.** No tracking of placed orders.
8. **Currently dormant.** Module exists tapi tidak invoked di current main loop.

### Integration Gaps (For Future Activation)

Saat lu decide activate executor.py, several integration questions need resolution:

1. **Trigger condition.** Kapan signal trigger execution? A+ only? VALID + A+? Operator manual approval?
2. **Order cancellation.** Kalau signal STALE atau KILL, apakah cancel pending limit order? Module tidak have cancel method.
3. **Order tracking.** Bagaimana bot tahu order filled vs pending vs cancelled? No state sync.
4. **Multi-signal management.** Kalau bot replace di slot management, apakah cancel pending order untuk evicted signal?

**Implication:** Activation requires substantial main.py refactoring untuk handle order lifecycle.

---

## Module 5: `config.py`

### Module-Level Strategic Purpose

**Operational configuration store.** Single source of truth untuk semua constants yang govern bot behavior **outside of structural logic**. Module ini answers: "Berapa banyak yang bot scan? Berapa lama signal tetap valid? Berapa risk per trade? Kemana credentials?"

Config separation philosophy: nilai-nilai yang **operator might want to tune without code changes** masuk sini. Nilai yang **architecturally locked** (HTF weights, scoring thresholds, OB quality filter parameters) tetap di strategy.py.

### Module Boundary

**Owns:** Environment-loaded credentials, operational limits, TTL configuration, technical indicator parameters, file paths.

**Does NOT own:** Strategy logic, scoring weights, HTF dominance ratios, regime classification thresholds, entry calculation parameters.

**Loading discipline:** All values loaded at import time via `dotenv`. Environment overrides defaults. **Read-only after import** — no runtime mutation.

### Constant Intent Lock

#### API Keys & Environment Group

**`API_KEY`, `SECRET_KEY`**
- **Strategic purpose:** Binance authentication credentials.
- **Expected behavior:** Loaded dari env vars `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`. None kalau tidak set.
- **Behavioral invariant:**
  - **Never hardcoded.** Environment-only loading.
  - **Demo-safe.** LockedBinance routes ke demo, leaked keys low-impact.
  - **Optional untuk radar-only scope.**

**`MODE`**
- **Strategic purpose:** Environment indicator (testnet vs production).
- **Expected behavior:** Default `'testnet'`. Currently not consumed di code path.
- **Behavioral invariant:** Currently inactive (forward-compatibility marker).

**`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`**
- **Strategic purpose:** Out-of-band notification credentials.
- **Expected behavior:** Loaded dari env. None → executor skips Telegram alerts gracefully.
- **Behavioral invariant:**
  - **Optional capability.** Bot functional tanpa Telegram setup.
  - **Currently dormant.**

#### Risk Management SOP Group

**`MAX_OPEN_POSITIONS = 10`**
- **Strategic purpose:** Hard cap pada concurrent active signals di radar memory. Cognitive load ceiling untuk operator + portfolio risk ceiling untuk execution future.
- **Market condition targeted:** All conditions.
- **Expected behavior:** main.py enforces via slot management — signal #11 triggers replace-weakest atau cap rejection.
- **Behavioral invariant:**
  - **Operator-cognitive-bound.** 10 = heuristic untuk realistic operator focus capacity.
  - **Score-prioritized saat full.**
  - **Not regime-adaptive.**

**`RISK_PER_TRADE = 100.0`**
- **Strategic purpose:** Fixed dollar risk amount per trade (USDT). Foundation untuk position sizing math.
- **Market condition targeted:** All trades.
- **Expected behavior:** Caller passes ke `calculate_position_size()`. Method targets exact $100 loss kalau SL hit.
- **Behavioral invariant:**
  - **Fixed amount, bukan percentage.** Akun growth tidak auto-scale risk.
  - **Currently dormant** di radar-only flow.
  - **Hard limit philosophy.**

**`MAX_MARGIN_ALLOWED = 200.0`**
- **Strategic purpose:** Hard ceiling pada margin exposure per trade. Defensive constraint terhadap leverage runaway.
- **Market condition targeted:** All trades, terutama wide-SL setups di high-volatility regime.
- **Expected behavior:** executor.py "Margin Presser" auto-scale down notional kalau ideal margin exceeded.
- **Behavioral invariant:**
  - **Margin cap > risk target** saat conflict.
  - **Auto-scale-down logged** untuk operator awareness.
  - **2× of risk amount** default ratio = moderate leverage ceiling.

#### Technical Settings Group

**`TOP_COINS_LIMIT = 30`**
- **Strategic purpose:** Universe scope per scan cycle.
- **Market condition targeted:** All conditions.
- **Expected behavior:** api_handler fetches top N by 24h volume. Bot scans only this universe.
- **Behavioral invariant:**
  - **Quality > quantity universe.** Top 30 captures liquid pairs.
  - **Cycle duration bound.** N=30 dengan 0.20s pacing = ~30s minimum baseline.
  - **Dynamic membership.**

**`ATR_PERIOD = 14`**
- **Strategic purpose:** ATR calculation lookback. Standard period.
- **Market condition targeted:** All.
- **Expected behavior:** api_handler computes ATR dengan period ini.
- **Behavioral invariant:**
  - **Industry-standard.** 14 = convention.
  - **Universal across TFs.**
  - **Foundation untuk spatial constants.** Banyak strategy.py thresholds ATR-relative.

**`ATR_MULTIPLIER = 1.5`**
- **Strategic purpose:** Generic ATR multiplier. Currently **NOT consumed** di strategy.py modern logic.
- **Behavioral invariant:**
  - **Dead constant.** Architectural artifact dari earlier version.
  - **Drift indicator.** Kalau referenced di strategy.py modern code, itu drift dari intent.

#### Memory & Cleanup Settings Group

**`ACTIVE_POIS_FILE = "active_pois.json"`**
- **Strategic purpose:** Persistent storage path.
- **Behavioral invariant:**
  - **Duplicated di main.py** as `STATE_FILE`. Architectural artifact pending consolidation.

**`POI_UNTAPPED_TTL_SECONDS` (default 24h)**
- **Strategic purpose:** TTL untuk signals yang baru discovered dan belum tapped. Longest tier.
- **Market condition targeted:** Newly-surfaced setups menunggu price reach entry.
- **Behavioral invariant:**
  - **Longest tier.** 24h = meaningful window untuk H1 setup.
  - **Environment-overridable.**
  - **Time-based safety net.**

**`POI_TAPPED_TTL_SECONDS` (default 6h)**
- **Strategic purpose:** TTL untuk signals yang sudah tapped (proxied via `miss_count > 0`).
- **Market condition targeted:** Signals where price has engaged structural levels.
- **Behavioral invariant:**
  - **Shorter tier.** Faster decay setelah engagement.
  - **Proxy semantics.** miss_count > 0 = crude tap detection.

**`POI_MAX_DISTANCE_ATR_MULTIPLIER = 3.0`**
- **Strategic purpose:** Distance-based invalidation threshold. **NOT consumed** di main pipeline.
- **Behavioral invariant:**
  - **Dormant capability.** Defined untuk future distance-based pruning.
  - **Forward compatibility.**

**`MANUAL_ACTION_TTL_SECONDS` (default 10h)**
- **Strategic purpose:** TTL untuk signals dengan `status = "MANUAL_ACTION"`.
- **Market condition targeted:** Post-execution waiting period.
- **Behavioral invariant:**
  - **Medium tier.** Longer than tapped (commit deserves patience), shorter than fresh untapped.
  - **Currently unused** di radar-only flow.

### Module-Level Invariants — config.py

1. **Read-only post-import.** No runtime mutation.
2. **Operational tuning here, calibration philosophy in strategy.py.**
3. **Safe defaults.** Bot functional dengan minimal env setup.
4. **Environment-overridable untuk TTL.** Hardcoded untuk architectural limits.
5. **Dormant credentials acceptable.** Bot scope = radar-only.
6. **Single source of truth.** Duplication elsewhere = artifact pending cleanup.

### Config vs Strategy Separation

Values **NOT di config.py** secara intentional (lock di strategy.py):

- HTF weights (4.0/2.0/1.0/1.5) → calibration philosophy
- Score thresholds (CONTINUATION 14/11/8, RETRACEMENT 15/12/9)
- OB quality filter parameters (0.25, 0.15, 0.20 ATR multipliers)
- Choppy market detection parameters

**Rationale:** Calibration philosophy harus survive operator-level tuning. Kalau operator can change via env var, philosophy can drift. Lock di code, change via PR + lock revision.

---

## Locking Status — FINAL

| Module | Status | Notes |
|--------|--------|-------|
| `strategy.py` | ✅ LOCKED | Core analytical engine |
| `main.py` | ✅ LOCKED | Operational orchestrator |
| `api_handler.py` | ✅ LOCKED | External data layer |
| `executor.py` | ✅ LOCKED | Dormant — execution capability |
| `config.py` | ✅ LOCKED | Operational config store |

---

## Cross-Module Architectural Map

```
config.py          (read-only constants, env-loaded)
    ↓ imports
api_handler.py     (external data, demo-routed)
    ↓ provides data
strategy.py        (structural analysis, stateless)
    ↓ produces signals
main.py            (orchestration, stateful memory)
    ↓ outputs to
[Dashboard]        (operator surface)
[Telegram]         (out-of-band, DORMANT)
    ↓ operator decides
executor.py        (semi-auto execution, DORMANT)
```

**Active flow:** config → api_handler → strategy → main → Dashboard.

**Dormant capability:** Telegram + executor.py exist tapi not invoked. Future activation requires main.py integration work.

---

## Architectural Tensions (For Future Cleanup)

### Duplicated File Path
`ACTIVE_POIS_FILE` di config + `STATE_FILE` di main.py = same value, two locations. Single-source consolidation pending.

### Dead Constants
`ATR_MULTIPLIER = 1.5` dan `POI_MAX_DISTANCE_ATR_MULTIPLIER = 3.0` defined tapi tidak consumed. Audit dan removed atau implemented.

### Hardcoded vs Env-Overridable Inconsistency
`MAX_OPEN_POSITIONS` dan `TOP_COINS_LIMIT` hardcoded. Beberapa operator preference patut env-overridable.

### Dormant Capabilities
`RISK_PER_TRADE`, `MAX_MARGIN_ALLOWED`, `MANUAL_ACTION_TTL_SECONDS` defined untuk execution mode tapi belum invoked. Kept untuk forward compatibility.

### Demo Endpoint Hardcoded
`LockedBinance` routes private API ke demo. Production execution requires class refactor.

### ccxt vs Raw HTTP Duality
Module menggunakan dua approaches paralel. Acceptable hybrid, tapi future consolidation possible.

---

## Refinement Discipline

### Test untuk Future Changes

Apakah change ini mengubah:
- **Strategic purpose** of any method?
- **Market condition targeted**?
- **Expected behavior**?
- **Expected output**?

Kalau **tidak** → safe internal refactor, no doc update needed.

Kalau **ya** → intent revision required, lock harus di-update + Change Log entry di BLUEPRINT.md.

### Adding New Methods

Setiap new method requires intent block sebelum implementation:
- Strategic purpose
- Market condition targeted
- Expected behavior
- Expected output
- Behavioral invariant

### Removing Methods

Method removal requires:
- Audit caller references
- Migration note kalau function di-reference elsewhere
- Update lock + Change Log

### Module Boundary Crossings

Kalau lu find yourself wanting:
- strategy.py mutate memory → architecture violation
- main.py do structural analysis → architecture violation
- api_handler.py interpret data → architecture violation
- executor.py make decisions → architecture violation
- config.py contain calibration ratios → architecture violation

These boundary crossings indicate either:
1. Wrong module choice for new functionality
2. Architecture has evolved and document needs update

Resolve via explicit architecture decision, bukan ad-hoc boundary violation.

---

## Quick Reference Cheatsheet

**Where does X belong?**

| Functionality | Module |
|--------------|--------|
| New scoring rule | strategy.py |
| New entry mode | strategy.py |
| New regime type | strategy.py |
| New log format | main.py |
| New API call | api_handler.py |
| New universe filter | api_handler.py |
| New order type | executor.py (dormant) |
| New TTL tier | config.py + main.py |
| New env-overridable constant | config.py |
| New calibration ratio | strategy.py (NOT config.py) |
| New persistence format | main.py |
| New memory field | main.py (load_memory + payload construction) |

**Where does the data flow?**

```
Universe → api_handler → main → strategy → main → dashboard
              ↓                              ↑
              └─── ATR computed at fetch ────┘
```

**State ownership:**

| State | Owner | Mutability |
|-------|-------|------------|
| Active signals | main.py | Read-write via main.py only |
| Bias cache | strategy.py | Internal, read-write via _cached_bias |
| Market metadata | api_handler.py | Set once at bootstrap, then read-only |
| Config constants | config.py | Read-only post-import |
| Pending orders | (none, dormant) | (would be executor.py if activated) |

---

## Document Maintenance

**Update triggers:**
- New method/function added → add intent block
- Method removed → strike-through entry + migration note
- Method behavior changed → revise intent block + Change Log entry di BLUEPRINT.md
- Module boundary changed → revise boundary section + invariants
- Dormant capability activated → update status table + integration notes

**Do NOT update for:**
- Bug fixes yang preserve intent
- Variable renames (kalau intent preserved)
- Internal refactor (kalau intent preserved)
- Performance optimizations (kalau intent preserved)
- Comment changes
- Code style changes

**Goal:** MODULE_INTENT.md adalah **behavioral contract**, bukan implementation reference. Keep it stable, focused on **what** dan **why**, not **how**.

---

**End of MODULE_INTENT.md**

**Companion documents:**
- `BLUEPRINT.md` — Architecture overview, philosophy, lifecycle, calibration reference
- `active_pois.json` — Runtime memory state (operational artifact, not documentation)
- `.env` — Operational credentials (not committed)

**Estimated length:** ~720 lines. Within target untuk detailed behavioral contract document. Trade-off: longer than BLUEPRINT.md karena per-method granularity required untuk lock-level precision.