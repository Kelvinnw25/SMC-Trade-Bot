# BLUEPRINT.md — SMC Signal Radar Bot

**Version:** Final (Post-Refinement Cycle)
**Status:** Locked structure, locked module intents
**Purpose:** Single source of truth untuk architecture, behavior, dan calibration philosophy

---

## 1. Philosophy & Non-Goals

### What This Bot Is

Radar SMC continuation-first untuk crypto H1 futures market. Output: directional **signal hypotheses** dengan structural context (entry zone + invalidation level + target + confidence score), surfaced ke trader manual via dashboard dan optional Telegram notification.

Bot adalah **signal generator**, bukan execution engine. Trader manual yang membuat final execution decision.

### NOT Goals

- Auto-execution / autonomous order placement
- ML / statistical / regression-based signals
- High-frequency / scalping / sub-H1 timeframe execution
- Forex-style session-trading patterns (London open sweep, Asia range)
- Maximizing signal count atau coverage breadth
- Replacing trader judgment

### 1a. Core Trading Philosophy

Lima tenets yang setiap refinement harus consistent dengan:

**Continuation-first.** Eksekusi searah momentum struktural yang sedang aktif. Fade-trend setup di environment trending = anti-pattern. Counter-trend hanya valid di range-bound regime dengan structural support.

**HTF dominance.** Higher timeframe bias adalah anchor, bukan one signal among many. Weekly trump D1, D1 trump H4, H4 trump LTF noise. Conflict resolution selalu prioritize HTF.

**Execution quality > signal frequency.** Bot lebih baik diam (high SKIP rate) daripada surface setup yang structurally ambiguous. Operator focus adalah resource scarce — radar yang noisy = radar yang di-ignore.

**Structural coherence > aggressive opportunity capture.** Setiap setup harus internally consistent — HTF, regime, location, entry mode harus tell the same story. Setup yang "lolos" karena threshold tapi struktural kontradiktif harus di-skip, bukan di-surface.

**Signals are directional hypotheses, not guaranteed executions.** Output bot adalah "berdasarkan struktur, arah X kemungkinan valid di area Y dengan invalidation di Z". Bukan "BUY NOW". Operator yang membuat eksekusi decision, bukan bot.

**Consistency test untuk refinement:** Apakah change ini konsisten dengan kelima tenets? Kalau salah satu violated, refinement di-reject.

---

## 2. Pipeline Flow (Stage-by-Stage)

Architecture-level execution flow per cycle. Module-agnostic, focused on **what happens**, not **which function**.

```
Data Acquisition
  ↓
HTF Context Resolution     (weekly/D1/H4 bias + location)
  ↓
Direction Selection        (weighted scoring + anchor logic)
  ↓
Regime Classification      (expansion/rotational/accumulation/etc.)
  ↓
Conflict Veto              (weekly vs regime contradiction check)
  ↓
Entry Mode Assignment      (CONTINUATION / RETRACEMENT / WATCH_ONLY / NONE)
  ↓
Structural Identification  (OB / FVG / displacement / sweep)
  ↓
Entry Calculation          (mode-aware: micro-pullback vs OB-mitigation)
  ↓
Scoring & Decision         (mode-aware thresholds → SKIP/WATCH/VALID/A+)
  ↓
Runtime Invalidation       (live price vs SL check, pre-memory)
  ↓
Memory & Output            (slot management, dashboard, persistence)
```

### Drop Points

Setup dapat di-eliminated di tahap-tahap berikut:

- **HTF Context Resolution** — insufficient data
- **Direction Selection** — no clear consensus, `no_direction`
- **Conflict Veto** — weekly + expansion regime contradiction → fade-momentum protection
- **Entry Mode Assignment** — `NONE` regime (compression weak, neutral, unknown)
- **Structural Identification** — missing required structure untuk mode
- **Entry Calculation** — no valid entry zone (`no_micro_pullback`, `ob_quality_failed`, `rotational_long_needs_discount`)
- **Scoring** — below threshold, `bad_rr`, `weak_watch_only_context`
- **Runtime Invalidation** — live price past SL beyond ATR buffer
- **Memory** — slot full + insufficient score (capped)

### State Propagation

Context object accumulates information across stages — downstream stages punya akses ke semua keputusan upstream. Setup yang ter-eliminate tetap returns context dengan clear `reason`, untuk traceability dan operator transparency.

### Log Emission Points

Setiap operator-visible state transition memiliki log line:
- `[SCAN]` — per-symbol scan output (always)
- `[⚠️ STALE]` — pre-memory invalidation
- `[☠️ KILL]` — post-memory kill switch
- `[🔄 REPLACE]` — slot replacement
- `[🚫 CAPPED]` — slot rejection
- `[⚠️ GUARD]` — invalid levels block
- `[⏰ TTL EXPIRED]` — time-based cleanup

---

## 3. Signal Lifecycle

State machine untuk perjalanan satu signal lintas cycle. Berbeda dari Pipeline (per-cycle flow), Lifecycle fokus ke **temporal evolution**.

### States

| State | Meaning |
|-------|---------|
| `discovered` | Passed structural pipeline, decision ∈ {WATCH, VALID, A+}, levels valid |
| `pre_memory_stale` | Invalidated by live price sebelum masuk memory |
| `active` | In memory, surfaced di dashboard |
| `killed` | Runtime invalidation (SL breach atau HTF flip) |
| `expired` | TTL exceeded (untapped/tapped/manual_action tier) |
| `replaced` | Bumped out by stronger signal saat slot full |
| `capped` | Rejected dari entry karena slot full + score insufficient |

### Transitions

```
discovered → active           (normal path)
discovered → pre_memory_stale (live price past SL at compute)
discovered → capped/replaced  (slot management)
active → killed               (runtime structural invalidation)
active → expired              (TTL hit, status-dependent)
active → active               (re-confirmed, payload refreshed)
```

### Eviction Hierarchy

Three mechanisms untuk signal removal, dengan distinct purposes:

1. **Kill switch (immediate)** — Structural invalidation. HTF flip atau SL breach beyond buffer. Bypasses miss_count.
2. **Slot management (score-based)** — Replace-weakest saat slot full + better candidate. Deterministic.
3. **TTL expiry (time-based)** — Safety net untuk signals yang lolos dari mechanisms di atas. Tiered: untapped (longest), tapped (medium), manual_action (medium-long).

### Miss Count Semantics

Confidence decay counter (current default: 3 consecutive misses → eviction).
- Increment ketika signal exists tapi cycle tidak refresh (decision = SKIP atau levels invalid)
- Reset to 0 on successful refresh (implicit via payload replacement)
- Distinct dari TTL: miss_count = confidence decay, TTL = age cap

---

## 4. Decision Hierarchy & Hard Gates

Sequential gates yang setup harus pass untuk reach memory. Listed dengan **what each gate protects against**.

### Gate List

**Direction gate** — Weighted HTF scoring dengan minimum threshold + gap requirement. Rotational regime menggunakan weekly anchor override untuk resolve HTF conflict.
*Protects against:* Ambiguous direction selection di environment dengan weak consensus.

**Veto gate** — Weekly bias + expansion regime conflict = direction None.
*Protects against:* Fade-the-active-momentum trades (weekly bullish + expansion_up + SHORT = veto).

**Entry mode gate** — Regime → mode mapping. Compression dengan weak HTF align = NONE.
*Protects against:* Inappropriate execution philosophy untuk market structure.

**OB quality gate (RETRACEMENT)** — Size minimum (ATR-relative), not lost (price still respects OB), distance OK (not too close, not breached).
*Protects against:* Structurally obsolete OB selection di trending market.

**Rotational LONG guard** — HTF conflict (weekly BULL + D1/H4 BEAR) di rotational LONG requires discount-only location.
*Protects against:* Counter-momentum LONG entry di area struktural mahal.

**Pre-memory gate** — Live market price vs SL with ATR buffer.
*Protects against:* Surface signal yang sudah dead pada compute time.

**Score decision gate** — Mode-aware thresholds. CONTINUATION lebih lunak (rarer setup), RETRACEMENT lebih strict (commoner setup), WATCH_ONLY most strict.
*Protects against:* Low-quality setups passing dengan generic threshold.

**Memory slot gate** — MAX_OPEN_POSITIONS hard cap. Replace-weakest atau cap rejection.
*Protects against:* Cognitive overload + portfolio dilution.

### Hard vs Soft Constraints

**Hard** (binary skip kalau violated): Direction gate, veto gate, RR floor, levels_valid, OB quality, rotational LONG guard, pre-memory check.

**Soft** (penalty to score, not outright skip): Choppy market, momentum weakening, weak volume (CONTINUATION), weak sweep (RETRACEMENT).

---

## 5. Calibration Reference

Philosophical reference untuk calibration choices. **Ratios dan relationships matter, exact numbers may drift**.

### HTF Weighting Philosophy

- Weekly **dominant** (~2× weight dari D1)
- D1 **supportive** (~2× weight dari H4)
- H4 **corroborative** (smallest standalone weight)
- Location **context-aware** — amplified when aligned dengan weekly, dampened when opposing
- **Hard constraint:** D1 + H4 + Location combined tidak boleh overpower Weekly sendirian

### Scoring Philosophy

**CONTINUATION mode:**
- Displacement = **hard gate** (no displacement → score 0)
- Sweep, FVG, OB = bonus (not requirements)
- Penalty: weak volume

**RETRACEMENT mode:**
- Sweep + OB = **co-primary** (penalty kalau absent)
- FVG, displacement = bonus
- Penalty: no OB

**WATCH_ONLY mode:**
- All confirmations = bonus (no hard gate)
- Requires strong HTF alignment to qualify untuk mode di first place

### HTF Alignment Bonuses

Weekly > D1 > H4 di magnitude. Location bonus only when aligned dengan direction.

### Decision Thresholds Philosophy

- CONTINUATION thresholds **lebih rendah** dari RETRACEMENT (continuation rare, jangan over-filter)
- A+ requires displacement (hard requirement, not just high score)
- RR floor wajib (~1.2) — sub-floor = reject regardless of score
- WATCH_ONLY thresholds highest (observation must be high-confidence)

### ATR-Based Spatial Constants

- SL buffer untuk crypto: **lebih besar** dari forex baseline (wick noise mitigation)
- OB size minimum: filter microscopic OBs yang lacks structural significance
- Pre-memory invalidation buffer: **lebih kecil** dari kill switch buffer (pre-memory adalah strict gate)
- Displacement candle threshold: body ≥ 0.60× ATR + body_ratio ≥ 0.50

### Update Rule

**Exact numbers can change. Ratios and philosophy must remain.**

Refinement scenarios:
- Adjust Weekly weight 4.0 → 4.5: **acceptable** (ratio preserved)
- Make D1 weight equal to Weekly: **architecture change** (HTF dominance violated)
- Lower RR floor to 1.0: **debatable** (RR floor philosophy preserved tapi quality lowered)
- Remove RR floor entirely: **architecture change** (RR-as-gate philosophy abandoned)

Architecture changes require explicit reasoning di Change Log + intent revision.

---

## 6. Regime → Entry Mode Mapping

Single source of truth untuk regime semantics + execution mode assignment.

| Regime | Entry Mode | Strategic Rationale |
|--------|-----------|---------------------|
| `expansion_up` | CONTINUATION | Active upward momentum — ride trend dengan micro-pullback entry |
| `expansion_down` | CONTINUATION | Active downward momentum — ride trend dengan micro-pullback entry |
| `accumulation` | RETRACEMENT | Range with bullish bias bottom-half — OB-based long entry |
| `distribution` | RETRACEMENT | Range with bearish bias top-half — OB-based short entry |
| `rotational` | RETRACEMENT | Moderate drift tanpa momentum — pullback entry valid dengan guards |
| `compression` | WATCH_ONLY or NONE | Coiling — entry only kalau HTF strongly aligned, else observe |
| `neutral` | NONE | True ambiguity — non-actionable |
| `unknown` | NONE | Insufficient data — non-actionable |

### Decision Tier per Mode

| Decision | CONTINUATION | RETRACEMENT | WATCH_ONLY |
|----------|--------------|-------------|------------|
| A+ | ✓ (high score + displacement) | ✓ (high score + displacement) | — |
| VALID | ✓ | ✓ | — |
| WATCH | ✓ | ✓ | ✓ (observation only, never executed) |
| SKIP | ✓ | ✓ | ✓ |

---

## 7. Expected Log Patterns

**Important framing:** Metrics di section ini adalah **heuristic health ranges**, bukan hard KPIs. Crypto market regime shifts across weeks/months — bull season vs bear season vs choppy consolidation menghasilkan distribusi yang berbeda secara natural. Ranges adalah baseline reference untuk detect anomaly relatif ke operator's own historical baseline, bukan target absolut.

### Healthy Patterns (Heuristic Baseline)

- SKIP rate **~40-60%** — bot honest tentang ketiadaan setup
- STALE rate **single digit %** per cycle — pre-memory check working
- Direction distribution **roughly proportional** dengan HTF bias distribution di universe
- Score distribution: mayoritas low (0-5), some medium (6-9), rare high (10+)
- Regime diversity: **no single regime > 60%** dari pairs di cycle yang sama

### Anomaly Signals (Investigation Required)

- Semua pair satu arah (100% SHORT atau LONG) → directional engine bias
- Semua pair satu regime (e.g., 90% neutral) → regime classifier broken
- STALE rate spike (1-2 → 10+ per cycle) → OB quality filter regression atau drastic market shift
- `no_direction` rate spike (>30%) → HTF resolution terlalu strict atau weekly bias broken
- Score frequently negative → choppy penalty too aggressive atau scoring drift
- Same pair flip regime setiap cycle → regime classifier noise, butuh persistence

### Adaptive Interpretation

Ranges calibrated untuk mixed-market crypto Q4 2025-Q1 2026 dengan top 30 USDT perp. Kalau kondisi market shift signifikan:

- **Strong directional bull/bear season:** Direction distribution akan skew — structurally expected, bukan anomaly
- **High volatility period:** STALE rate akan naik (race condition lebih wide) — environmental
- **Low volatility / range market:** Regime distribution skew ke compression/accumulation — natural
- **Macro event windows (FOMC, CPI):** All metrics tidak reliable untuk window itu

**Rule of investigation:** Anomaly hanya valid kalau metric **menyimpang dari operator's historical baseline**, bukan blueprint numbers.

### Consistency Checks (More Reliable Across Market Regimes)

Internal coherence checks yang stable lintas market state:

- Same pair, same HTF context, regime **jangan flip per cycle** (noise indicator)
- Same pair, same setup, score **jangan oscillate dramatic** (>5 points cycle-to-cycle)
- Direction once picked **stable kecuali HTF bias change** (directional flip-flop = bias resolution broken)

---

## 8. Known Trade-Offs & Edge Cases

Decisions yang sudah pernah dibahas dan diputuskan. Prevent re-debating.

### Closed-Candle vs Live-Candle Mismatch

Structural analysis menggunakan closed candle (deterministic). Runtime invalidation menggunakan live forming candle (current state). Race condition antara compute time dan live price = inherent gap.

**Mitigation:** OB quality filter (reject obsolete OBs), location guard (rotational LONG discount-only), pre-memory invalidation check, ATR-buffered SL.

### Weekly Bias Can Lag

EMA-based bias detection adalah lagging indicator. Weekly bias bisa stale 1-2 candles dari actual structural shift.

**Mitigation:** Location context-awareness (premium in uptrend = different meaning dari premium in downtrend), regime-aware direction resolution.

### Rotational LONG with HTF Conflict

Weekly BULL + D1/H4 BEAR + rotational regime + LONG direction = counter-momentum trade. STALE prone karena LTF drift masih bearish.

**Mitigation:** Discount-only location requirement untuk specific HTF conflict pattern. Equilibrium dan premium di-skip.

### Continuation-First Means Skipping Counter-Trend in Active Trend

Setup yang struktural valid (e.g., short di expansion_up dengan good confirmations) tetap di-veto kalau melawan weekly + regime. Filosofi: bot adalah continuation engine, bukan reversal hunter.

### Binary Pre-Memory Check (No Soft Mode)

Clear actionable signal lebih penting dari opportunity capture. Setup yang ambiguous = skip, bukan surface dengan caveat. Operator focus protection.

### Universe Limited to Top 30 USDT-Perp

Volume-based filter implicit assumption: high volume = SMC-valid. Low-volume pairs structurally noisier. Trade-off: miss opportunity di mid-cap pairs untuk gain signal quality.

### Demo Endpoint Hardcoded

`LockedBinance` routes private API ke demo. Production execution requires class refactor. Acceptable karena scope = radar-only.

### Miss Count as Tap Proxy

Bot tidak track real-time price taps. `miss_count > 0` digunakan sebagai indirect indicator untuk tapped/tested signals. Acceptable simplification untuk lightweight architecture.

### Dormant Modules

`executor.py` dan Telegram integration exist tapi tidak invoked di current flow. Bot scope = radar-only. Future activation requires substantial main.py refactor untuk handle order lifecycle.

---

## 9. Module Intent Reference

Locked behavioral contracts per module. Detail lengkap di `MODULE_INTENT.md`.

### `strategy.py` — `SMCStrategy` class

**Strategic purpose:** Core SMC interpretation engine. Convert raw OHLCV → structured trading hypotheses. Stateless terhadap memory + execution.

**Key invariants:**
- Closed-candle structural analysis (live candle only untuk kill switch)
- HTF dominance hierarchy (Weekly > D1 > H4)
- Mode separation (CONTINUATION vs RETRACEMENT distinct gates + weights)
- Conservative defaults (ambiguous → SKIP)
- Audit trail mandatory (no black-box decisions)

### `main.py`

**Strategic purpose:** Operational orchestrator. Bridge antara stateless analysis dan stateful operator output.

**Key invariants:**
- Read-only delegation ke strategy.py (no second-guessing)
- Memory mutation discipline (only main.py mutates)
- Closed-candle structural / live-candle runtime separation
- Logging atomicity (per-symbol consecutive blocks)
- Error isolation (per-symbol + per-cycle try/except)
- Slot management deterministic (score-based replacement)

### `api_handler.py` — `BinanceHandler` class

**Strategic purpose:** External data layer. Single source of truth untuk Binance interactions.

**Key invariants:**
- Demo-bound (`LockedBinance` routes ke demo-fapi)
- Hybrid data path (raw HTTP untuk public, ccxt untuk authenticated)
- USDT-perp-only universe (hardcoded filter)
- Defensive failure (return None/empty, never raise)
- ATR mandatory di OHLCV fetches
- No interpretation layer (raw market facts only)

### `executor.py` — `BinanceExecutor` class (DORMANT)

**Strategic purpose:** Trade execution layer. Currently not invoked di main loop.

**Key invariants:**
- Semi-automated by design (entry auto, SL/TP manual)
- LIMIT-only entry (no market orders)
- Risk-first sizing dengan margin cap ceiling
- Adaptive leverage (per-trade computed, Binance-confirmed)
- 35% safety buffer mandatory
- Dual notification (console + Telegram)
- Stateless executor (no order tracking)

### `config.py`

**Strategic purpose:** Operational configuration store. Operator-tunable values.

**Key invariants:**
- Read-only post-import
- Operational tuning here, calibration philosophy in strategy.py
- Safe defaults (testnet mode, conservative TTL)
- Env-overridable untuk TTL, hardcoded untuk architectural limits
- Dormant credentials acceptable (radar-only scope)

---

## 10. Architectural Map

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

**Active flow:** config → api_handler → strategy → main → Dashboard

**Dormant capability:** Telegram + executor.py exist tapi belum integrated.

---

## 11. Refinement Discipline

### For Any Future Change

Test consistency dengan:

1. **Five tenets di Section 1a** — Does change preserve continuation-first, HTF dominance, quality > frequency, coherence > capture, hypothesis semantics?
2. **Module intent contracts** — Does change preserve strategic purpose, market condition targeted, expected behavior, expected output dari affected method?
3. **Ratio relationships di Section 5** — Does change preserve HTF dominance ratios, mode-specific score thresholds, RR floor philosophy?

### Change Classification

| Change Type | Scope | Process |
|-------------|-------|---------|
| Threshold tweak | Exact number, ratio preserved | Update + log di Change Log |
| Calibration ratio change | Affect HTF dominance, scoring philosophy | Architecture review + intent revision |
| New gate / mode | Pipeline additions | Update BLUEPRINT + MODULE_INTENT |
| Module split / merge | Architecture restructure | Full intent re-lock + blueprint update |

### Documentation Discipline

Every refinement should answer:
- What changed (concrete delta)
- Why (reasoning di trading logic terms)
- What invariant preserved
- What expected behavior shift

Append minimal entry ke Change Log.

---

## 12. Change Log

```
v.Final — Initial blueprint lock
  - 9 sections structure frozen
  - All 5 modules intent-locked
  - Architecture state: radar-only, executor + Telegram dormant

Previous calibration milestones (pre-blueprint):
  - Weekly weight increased to dominant (~2× D1)
  - Location made context-aware (weekly-bias-dependent amplification)
  - OB quality filter added (size, distance, freshness checks)
  - Rotational LONG discount-only guard untuk HTF conflict
  - Rotational threshold relaxation + weekly anchor override
  - Pre-memory invalidation check (live price vs SL with buffer)
  - Mode-aware scoring (CONTINUATION displacement hard gate, RETRACEMENT OB-primary)
  - Regime classifier majority-vote (compression/expansion 2-of-3)
  - Hard veto: weekly + expansion regime conflict
```

Format: 1-2 lines per change. Focus on **what changed + why**, not exact numbers.

---

## 13. Quick Reference for Onboarding

For new session (GPT/Claude/operator) joining the project:

**Read in order:**
1. This BLUEPRINT.md (Sections 1, 1a, 2, 3) — 5-minute philosophy + flow grasp
2. `MODULE_INTENT.md` — module-by-module behavioral contracts
3. Latest terminal logs — see actual behavior
4. Codebase — only when implementing specific refinement

**Don't:**
- Don't read codebase first (implementation distracts dari intent)
- Don't propose refactor without checking philosophy consistency
- Don't tweak numbers tanpa checking ratio invariants
- Don't activate dormant modules tanpa explicit architecture decision

**Do:**
- Treat philosophy section as anchor untuk semua keputusan
- Check anomaly relative ke operator baseline, bukan blueprint absolutes
- Preserve module boundaries (strategy = analysis, main = orchestration, api_handler = data, executor = dormant execution, config = operational tuning)
- Reference Change Log untuk historical context

---

## Document Maintenance

**Update triggers:**
- Calibration ratio change → Section 5 + Change Log
- New gate/mode/regime → Section 4, 6 + relevant module intent
- Pipeline stage addition/removal → Section 2 + Module Intent affected
- Trade-off resolution → Section 8
- Module activation (executor, Telegram) → Section 9 status + Section 10

**Do NOT update for:**
- Bug fixes yang preserve intent
- Implementation refactor yang preserve intent
- Variable renames
- Code style changes

**Goal:** BLUEPRINT.md adalah **behavioral contract**, bukan changelog of every commit. Keep it stable, dense, and worth reading.

---

**End of BLUEPRINT.md**

**Estimated length:** ~420 lines. Within target range (200-400 baseline + 20% architecture map + maintenance section overhead).

**Companion documents:**
- `MODULE_INTENT.md` — Detailed per-module behavioral contracts
- `active_pois.json` — Runtime memory state (not documentation, but reference for state schema)
- `.env` — Operational credentials (not committed)