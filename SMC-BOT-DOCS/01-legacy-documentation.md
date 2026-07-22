# SMC Signal Radar — Technical Reference Document

Dokumen ini menyusun pemahaman sistem dari kode aktual (BLUEPRINT.md, MODULE_INTENT.md, dan seluruh source: `main.py`, `src/strategy.py`, `src/api_handler.py`, `src/telegram_notifier.py`, `src/config.py`, `src/executor.py`). **Status verifikasi (2026-07-22):** seluruh modul dan fungsi telah dibaca langsung dan lengkap dari source; tidak ada lagi bagian yang bersandar pada asumsi. Penanda `[TERVERIFIKASI 2026-07-22]` menandai bagian yang sebelumnya bertanda `[tidak terverifikasi]` dan kini dikonfirmasi dari kode.

---

## 1. Overall Purpose

Bot ini adalah **signal generator SMC (Smart Money Concepts)**, bukan execution engine. Identitas intinya (BLUEPRINT §1): ia memindai pasar crypto, mengidentifikasi setup struktural berdasarkan hierarki higher-timeframe, dan menghasilkan hipotesis terarah ("berdasarkan struktur, arah X kemungkinan valid di area Y dengan invalidation di Z"). Keputusan eksekusi final diserahkan ke operator manusia. Executor otomatis ada tetapi **dormant**.

Sistem beroperasi sebagai **radar loop kontinu**: setiap ~60 detik ia mengambil top-30 pair by volume, menjalankan tiap pair melalui pipeline analisis struktural closed-candle, memberi skor, dan mempertahankan memori terbatas dari sinyal aktif (maksimal N slot). Sinyal yang cukup kuat (VALID/A+) di-broadcast via Telegram sebagai opportunity feed; sinyal WATCH tetap dashboard-only.

Filosofi yang memandu (BLUEPRINT §1a): continuation-first, HTF dominance, execution-quality over frequency, structural-coherence over capture, dan signals-are-hypotheses. Bot mengutamakan menghindari sinyal buruk daripada memaksimalkan jumlah sinyal.

---

## 2. Complete Runtime Pipeline

Dari program start sampai satu scan cycle selesai:

1. **Startup** — `run()` mencetak banner, menginstansiasi `BinanceHandler` (api) dan `SMCStrategy` (strategy), lalu memuat memori sinyal via `load_memory()` dari `active_pois.json`.

2. **Masuk while-loop utama** (berulang selamanya):

3. **Fetch universe** — `api.get_top_volume_coins(limit=TOP_COINS_LIMIT)` mengembalikan top-30 pair by volume. Jika kosong, sleep 30s, continue.

4. **Loop per-coin** (untuk tiap symbol di top-30):
   - Sleep 0.20s (rate limiting), fetch D1 (`fetch_data_with_atr`, limit 120), sleep 0.20s, fetch H1 (limit 500).
   - Guard: skip jika data None/empty, atau setelah `iloc[:-1]` (buang forming candle) length D1 < 10 atau H1 < 50.
   - Hitung `h1_last_ts` (timestamp closed candle terakhir) dan `h1_last_ts` epoch via `_to_epoch_ts()`.

5. **Stage 1 — HTF Context** — `strategy.build_signal_context(df_d1, df_h1)` mengembalikan `context` (result, debug, h1_setup, htf_map, regime). Skip jika None.

6. **Inject live price** — `context["market_price"] = df_h1_raw["close"].iloc[-1]` (forming candle close, satu-satunya penggunaan live candle).

7. **Kill switch check** — jika symbol ada di memori: `is_poi_invalidated(active_signals[symbol], context)`. Jika dead → cetak `[☠️ KILL]`, kirim `send_kill_alert` (jika sebelumnya ter-alert), `del` dari memori, `continue`.

8. **Stage 2 — M5 enrichment** — jika `result["score"] >= 8`: fetch M5 (limit 200), guard timestamp alignment (`m5.floor("h") <= h1.floor("h")`), lalu `result = enrich_with_m5(context, df_m5)`.

9. **Atomic log block** — susun log lines (SCAN/HTF/CONF/RAW), print.

10. **Pre-memory invalidation** — bangun `temp_signal`, panggil `is_poi_invalidated`. Jika dead → `[⚠️ STALE]`, `continue`.

11. **Output & Memory Engine** — hitung `levels_valid`. Tiga cabang:
    - **decision ∈ {WATCH,VALID,A+} + levels_valid** → bangun payload, insert/replace/cap ke memori (slot management), dispatch Telegram (new/upgrade).
    - **decision aktionable + NOT levels_valid** → `[⚠️ GUARD]`, increment miss_count (H1-gated), evict jika limit.
    - **else (SKIP)** → increment miss_count (H1-gated), evict jika limit.

12. **Akhir per-coin loop** — exception handler mencetak error + traceback.

13. **Post-loop housekeeping** — `cleanup_expired_signals()` (TTL), `save_memory()`, `print_dashboard()`, cetak "Cycle Complete", sleep 60s.

14. **Kembali ke langkah 3.**

---

## 3. Major Modules / Functions

### `run()` — main.py
- **Responsibility:** orchestrator tunggal; satu-satunya mutator memori; menjalankan loop, memanggil strategy, mengelola lifecycle, dispatch Telegram/logs.
- **Inputs:** tidak ada langsung (menarik dari api + strategy + memori).
- **Outputs:** side-effects — mutasi `active_signals`, tulisan ke `active_pois.json`, log terminal, alert Telegram.
- **Called by:** entry point program.

### `SMCStrategy.build_signal_context(df_d1, df_h1)` — strategy.py
- **Responsibility:** analisis struktural closed-candle inti; menghasilkan HTF map, regime, direction, entry_mode, OB/FVG detection, entry construction, dan memanggil scoring.
- **Inputs:** D1 dan H1 closed dataframes.
- **Outputs:** dict `{htf_map, regime, h1_setup, result, debug}`.
- **Called by:** `run()` (Stage 1).

### `SMCStrategy.score_signal(htf_map, regime, h1_setup, m5_setup)` — strategy.py
- **Responsibility:** menghitung skor numerik + decision tier (SKIP/WATCH/VALID/A+); mode-aware gates; RR floor hard gate.
- **Inputs:** htf_map, regime, h1_setup, optional m5_setup.
- **Outputs:** dict `{score, decision, direction, notes, regime, rr, entry_mode}`.
- **Called by:** `build_signal_context()` dan `enrich_with_m5()`.

### `SMCStrategy.enrich_with_m5(context, df_m5)` — strategy.py
- **Responsibility:** konfirmasi mikro M5 (continuation momentum atau retracement CHoCH); re-score dengan m5_setup.
- **Inputs:** context, M5 closed dataframe.
- **Outputs:** dict result yang di-update (score + m5 fields).
- **Called by:** `run()` (Stage 2, hanya jika score ≥ 8).

### `_calc_continuation_entry()` — nested dalam `build_signal_context()`
- **Responsibility:** temukan displacement + micro-pullback untuk CONTINUATION entry; hitung entry/sl/tp.
- **Inputs:** closure atas h1, direction, atr_last, swings, indices.
- **Outputs:** tuple (entry_price, sl, tp, entry_ready, reason, disp_idx, pullback_idx).
- **Called by:** `build_signal_context()` saat entry_mode == CONTINUATION.

### `SMCStrategy.find_liquidity_target(...)` — strategy.py
- **Responsibility:** pilih TP dari liquidity candidates (EQH/EQL, swing, PDH/PDL) dengan RR-awareness; mode-aware selection; fallback None jika tak lolos RR floor.
- **Inputs:** h1, direction, entry_price, swings, atr, sl, entry_mode.
- **Outputs:** dict `{tp, source, rr, ...}`.
- **Called by:** entry construction (CONTINUATION dan RETRACEMENT).

### `SMCStrategy.identify_swings(df, custom_window, prominence_mult)` — strategy.py
- **Responsibility:** deteksi swing highs/lows dengan window symmetry + prominence filter.
- **Inputs:** dataframe, window, prominence multiplier.
- **Outputs:** tuple (swing_highs, swing_lows) — list of {index, price}.
- **Called by:** `build_signal_context()`, `enrich_with_m5()`.

### `SMCStrategy.is_poi_invalidated(signal, context)` — strategy.py **[TERVERIFIKASI dari kode, 2026-07-22]**
- **Responsibility:** emergency kill switch. Sinyal dianggap mati jika SALAH SATU: (a) HTF hard flip — D1 bias DAN H4 bias keduanya berlawanan arah sinyal (LONG dibunuh oleh BEARISH+BEARISH, SHORT oleh BULLISH+BULLISH); atau (b) live price menembus SL melampaui buffer 0.30×ATR (`atr_last` dari h1_setup). `market_price` fallback ke `result.entry_price` jika tidak finite. Error apa pun → return False (konservatif, tidak membunuh saat tidak pasti).
- **Inputs:** signal dict (direction, sl), context (market_price, htf_map, h1_setup, result).
- **Outputs:** bool.
- **Called by:** `run()` (kill switch + pre-memory).

### `load_memory()` / `save_memory()` — main.py
- **Responsibility:** deserialize/serialize `active_pois.json`; validasi required_fields; coerce tipe; optional fields dengan default.
- **Inputs:** file (load) / memory dict (save).
- **Outputs:** validated dict (load) / file tulisan atomik via temp+replace (save).
- **Called by:** `run()` (startup, akhir cycle).

### `cleanup_expired_signals(active_signals)` — main.py
- **Responsibility:** TTL eviction wall-clock; tier TTL via miss_count tap-proxy (manual/tapped/untapped).
- **Inputs:** active_signals dict.
- **Outputs:** pruned dict; log `[⏰ TTL EXPIRED]`.
- **Called by:** `run()` (akhir cycle).

### Helper functions & constants — main.py **[TERVERIFIKASI dari kode, 2026-07-22]**
- `_alert_tier()`, `_alert_rank()` — mapping decision → alert tier ("" / VALID / A+) dan rank (0/1/2) untuk dedup Telegram.
- `_log_decay()` — emit `[⏳ DECAY]` (from_decision, mode, reason, miss_count) sebelum miss_count eviction.
- `_to_epoch_ts()` — `pd.to_datetime(value).timestamp()` → float; 0.0 untuk None/NaT/error.
- `print_dashboard()` — render memori sorted by score desc; label mode-aware (WATCH-CONT/WATCH-RETR/WATCH-COMP).
- `_is_same_thesis()` — bandingkan direction + entry_mode (case-insensitive) existing vs kandidat.
- `load_killed_thesis()` / `save_killed_thesis()` — deserialize/serialize `killed_thesis.json` (flat dict `{thesis_key: killed_ts}`); save atomik via temp+replace.
- `append_ob_audit_record()` — append satu JSON line ke `retracement_ob_audit.jsonl`. Catatan: fungsi ini terdefinisi DUA KALI di main.py dengan body identik (definisi kedua yang berlaku; tanpa dampak perilaku).
- **Konstanta di main.py (bukan config.py):** `STATE_FILE="active_pois.json"`, `MISS_COUNT_LIMIT=3`, `KILLED_THESIS_FILE="killed_thesis.json"`, `KILL_COOLDOWN_SECONDS=3600`, `OB_AUDIT_FILE="retracement_ob_audit.jsonl"`.

### `BinanceHandler` — api_handler.py **[TERVERIFIKASI dari kode, 2026-07-22]**
- **Responsibility:** data layer non-interpretif; fetch OHLCV + ATR, top-volume universe.
- **Methods:** `get_top_volume_coins(limit)`, `fetch_data_with_atr(symbol, timeframe, limit)`, `get_active_positions()` (dormant, tidak dipanggil `run()`). Sub-class `LockedBinance` me-rewrite URL private call ke demo-fapi.
- **Called by:** `run()`.

### `telegram_notifier.py` — **[TERVERIFIKASI dari kode, 2026-07-22]**
- **Responsibility:** I/O + formatter Telegram murni; no memory/scoring/decision.
- **Functions:** `send_signal_alert`, `send_upgrade_alert`, `send_kill_alert`, `send_telegram_message`, `_post_message`.
- **Called by:** `run()`.

### `executor.py` — **DORMANT [TERVERIFIKASI 2026-07-22: tidak di-import maupun dipanggil dari main.py]**
- **Responsibility:** eksekusi order (LIMIT at entry). Tidak aktif.

---

## 4. Strategy Pipeline (Ordered, dari Kode Aktual)

Dalam `build_signal_context()`, urutan aktual:

1. **Input validation** — `_safe_df` (dropna, min length) untuk D1 & H1.
2. **Choppiness check** — `_is_market_choppy(h1)` (dipakai nanti sebagai penalty).
3. **HTF map** — `build_htf_map(d1, h1)` → PDH/PDL, PWH/PWL, daily/weekly equilibrium, weekly bias (close vs prev-week eq), D1 bias (cached `_derive_simple_bias_from_df`: swing structure + EMA50/200 + slope), H4 bias (resample H1→4h, drop forming, cached), location (discount/premium/equilibrium vs daily+weekly eq), momentum_weakening. **[terverifikasi 2026-07-22]**
4. **Regime classification** — `classify_regime(h1)`: majority-vote 2-of-3 untuk compression (range_ratio<3.0, atr_now≤0.95×avg, body_ratio<0.42) dan expansion (range_ratio>4.0, atr_now≥1.03×avg, body_ratio>0.46); drift veto; close-position memisahkan accumulation/distribution/compression; drift sign memisahkan expansion_up/down/rotational; fall-through rotational; default neutral/unknown. **[terverifikasi 2026-07-22]**
5. **Weighted direction selection** — weekly (4.0) > D1 (2.0) > H4 (1.0) + location context + fallback H1 bias + rotational anchor override → LONG/SHORT/None.
6. **Hard veto** — expansion-vs-direction conflict (weekly bullish + expansion_up + short, atau sebaliknya).
7. **Regime → execution philosophy** — expansion→CONTINUATION, accumulation/distribution/rotational→RETRACEMENT, compression→WATCH_ONLY/NONE.
8. **ATR + swings** — `identify_swings`.
9. **Sweep detection** — `_detect_sweep`: freshest valid swing (umur ≤36 candle) sebagai referensi; scan mundur ≤12 candle; syarat wick beyond ref + reclaim close (buffer 0.03×ATR) + depth ≥0.12×ATR + reclaim strength ≥0.40. **[terverifikasi 2026-07-22]**
10. **FVG + OB detection** — inline loops (FVG gap check; OB = first green/red candle in 10-candle lookback, first-match break).
11. **Displacement + volume** — `_is_displacement_candle` (last candle), volume spike vs 20-SMA.
12. **Entry engine** — CONTINUATION (`_calc_continuation_entry`) / RETRACEMENT (OB quality filter + entry at OB) / WATCH_ONLY / NONE.
13. **Target clear check** — jarak TP vs current price ≥ 0.75 ATR.
14. **Early exit** — jika not entry_ready dan bukan WATCH_ONLY → SKIP dengan reason.
15. **Scoring** — `score_signal(...)`.
16. **Choppy penalty** — jika choppy, score -= 2.
17. **Result assembly** — update entry/sl/tp/mode/source ke result.

Ringkas: **market data → HTF context → direction → veto → regime→mode → swings/sweep → OB/FVG/displacement → entry construction → scoring → M5 enrichment (di run) → decision → lifecycle (di run)**.

---

## 5. Complete Feature Inventory

- **HTF Context Engine** (weekly/D1/H4 bias + location)
- **Weighted Direction Selection** (dominance weighting + fallback + rotational anchor override)
- **Regime Detection** (expansion_up/down, accumulation, distribution, rotational, compression, neutral)
- **Regime → Execution Mode Mapping** (CONTINUATION/RETRACEMENT/WATCH_ONLY/NONE)
- **Hard Veto** (expansion-vs-direction conflict)
- **Swing Detection** (window symmetry + prominence filter)
- **Order Block Detection** (single-candle definition, hybrid wick+body geometry, first-match selection, freshness check, quality filter)
- **FVG Detection** (gap-based)
- **Liquidity Sweep Detection**
- **Displacement Detection** (body ratio + ATR threshold)
- **Volume Spike Detection** (vs 20-period SMA)
- **Continuation Entry Engine** (displacement + micro-pullback)
- **Retracement Entry Engine** (OB-based + quality filter + rotational long guard)
- **Liquidity Target Selection** (EQH/EQL cluster, swing, PDH/PDL, RR-aware, mode-aware, 2R fallback)
- **RR Engine** (hard floor 1.2 + escalating bonus)
- **Scoring System** (mode-aware gates + HTF alignment + confirmations + structure quality + regime + RR + momentum + M5)
- **Decision Tiering** (SKIP/WATCH/VALID/A+, mode-specific thresholds)
- **M5 Enrichment** (continuation momentum / retracement CHoCH confirmation)
- **Choppiness Penalty**
- **Momentum Weakening** (penalty)
- **Kill Switch** (emergency invalidation via live price)
- **Pre-memory STALE check**
- **Signal Memory** (slot-limited, max N positions)
- **Slot Management** (score-based replace/cap)
- **Miss-count Decay** (H1-close-gated confidence decay)
- **DECAY logging** (audit trail)
- **TTL Expiry** (wall-clock, tapped/untapped tiering)
- **Session Mode** (`get_session_mode`): STRICT saat weekend (UTC weekday ≥5) atau jam 22:00–07:00 UTC; selain itu SNIPER. Dipakai untuk `session_active` (+1 skor) dan parameter swing M5. **[terverifikasi 2026-07-22]**
- **Telegram Alerts** (new/upgrade/kill, dedup via alert_sent_tier/ts)
- **Dashboard rendering**
- **Persistent memory** (JSON, atomic write)
- **Killed-Thesis Cooldown** — pada KILL, `symbol|direction|entry_mode` dicatat ke `killed_thesis.json`; re-insert diblokir 1 jam (`[🚫 KILL COOLDOWN]`); pruning per cycle. **[TERVERIFIKASI 2026-07-22 — sebelumnya ditandai belum terverifikasi]**
- **Same-Thesis Refresh** — thesis sama + tier tidak naik → partial refresh in-place (score=max, decision tidak pernah turun, entry/sl/tp/regime di-update, timestamp reset ke now) TANPA reset miss_count/last_miss_h1_ts; log `[🟰 SAME THESIS]`; tanpa Telegram. **[terverifikasi 2026-07-22]**
- **OB Audit Trail** — sinyal RETRACEMENT dengan `entry_source=retracement_ob` + levels_valid → append record diagnostik ke `retracement_ob_audit.jsonl`, dedup in-memory per (symbol, h1_ts, mode). **[terverifikasi 2026-07-22]**

---

## 6. Persistent State

### `active_pois.json` (persistent store utama — BUKAN satu-satunya; lihat juga `killed_thesis.json` dan `retracement_ob_audit.jsonl`)
- **Writer:** `run()` via `save_memory()` (akhir tiap cycle, atomik temp+replace).
- **Reader:** `run()` via `load_memory()` (startup; validasi required fields score/decision/entry_price/sl/tp/direction, coercion tipe, default untuk optional; **entry_source dan signal_id TIDAK dibaca kembali** — lossy by design di V1).
- **Purpose:** memori sinyal aktif lintas restart.
- **Per-signal fields (17, terverifikasi dari save path + file aktual):** direction, score, decision, entry_price, sl, tp, regime, timestamp (wall-clock, TTL), miss_count, last_miss_h1_ts (epoch, decay gating), status, alert_sent_tier, alert_sent_ts, last_ob_audit_h1_ts, entry_mode, entry_source, signal_id. Catatan: `last_ob_audit_h1_ts` dipersist dan di-carry-over tapi **tidak pernah di-update** oleh audit path saat ini (vestigial; dedup audit aktual memakai set in-memory `ob_audit_seen`, reset saat restart).

### `killed_thesis.json` (persistent store kedua)
- **Writer/Reader:** `run()` via `save_killed_thesis()` / `load_killed_thesis()`.
- **Purpose:** cooldown re-entry 1h per thesis pasca-KILL. Flat dict `{symbol|direction|entry_mode: killed_ts}`; pruned per cycle.

### `retracement_ob_audit.jsonl` (append-only diagnostic log)
- **Writer:** `run()` via `append_ob_audit_record()`. Tidak pernah dibaca kembali oleh bot (observability only).

### `active_signals` (in-memory dict, runtime global-ish)
- **Writer:** HANYA `run()` (mutator tunggal — kill, insert, replace, cap, miss-increment, evict, cleanup).
- **Reader:** `run()`, dan dilewatkan ke `is_poi_invalidated`, `cleanup_expired_signals`, `_log_decay`.
- **Purpose:** state hidup dalam satu cycle; disinkronkan ke JSON tiap cycle.

### Config constants **[TERVERIFIKASI 2026-07-22]**
- **config.py:** TOP_COINS_LIMIT, MAX_OPEN_POSITIONS, TTL constants (MANUAL_ACTION/POI_TAPPED/POI_UNTAPPED), ACTIVE_POIS_FILE, ATR_PERIOD; dead constants: ATR_MULTIPLIER, POI_MAX_DISTANCE_ATR_MULTIPLIER.
- **main.py (bukan config.py):** STATE_FILE (duplikat ACTIVE_POIS_FILE), MISS_COUNT_LIMIT, KILLED_THESIS_FILE, KILL_COOLDOWN_SECONDS, OB_AUDIT_FILE.
- **Writer:** none (static config).
- **Reader:** `run()`, `cleanup_expired_signals`.
- **Purpose:** operational tuning (BUKAN calibration ratios, per MODULE_INTENT boundary).

---

## 7. External Dependencies

**External services / APIs:**
- **Binance API** (via `BinanceHandler`) — OHLCV data, top-volume pairs. Mainnet futures perpetual.
- **Telegram Bot API** (via `telegram_notifier`) — alert delivery.

**Python libraries (dari import terlihat):**
- `pandas` — dataframe manipulation, timestamp handling.
- `numpy` — numeric ops, isfinite checks.
- `time` — sleep, wall-clock timestamps.
- `json` — persistence.
- `os` — file ops (atomic replace).
- `sys` — exit.
- `traceback` — error logging.
- **[TERVERIFIKASI 2026-07-22]** HTTP: `requests` (public endpoints) + `ccxt` (authenticated, demo-routed via LockedBinance) di `BinanceHandler`; `pandas_ta` untuk ATR; `python-dotenv` di config; `urllib` (stdlib) di telegram_notifier.

---

## 8. High-Level Dependency Graph

```
                    ┌─────────────┐
                    │   run()     │  (main.py — orchestrator)
                    └──────┬──────┘
          ┌────────────────┼────────────────┬──────────────┐
          ▼                ▼                ▼              ▼
   ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌──────────┐
   │BinanceHandler│  │ SMCStrategy │  │telegram_     │  │ memory   │
   │(api_handler) │  │ (strategy)  │  │ notifier     │  │(JSON I/O)│
   └─────────────┘  └──────┬──────┘  └──────────────┘  └──────────┘
                           │
              ┌────────────┼────────────┬─────────────┐
              ▼            ▼            ▼             ▼
        build_signal   score_signal  enrich_with  find_liquidity
        _context        │             _m5          _target
              │         │
              ├─ identify_swings
              ├─ build_htf_map
              ├─ classify_regime
              ├─ _detect_sweep
              ├─ _calc_continuation_entry (nested)
              └─ is_poi_invalidated (also called direct by run)

   executor.py ── DORMANT, not wired into run()
```

Arah dependensi: `run()` bergantung pada semua; `SMCStrategy` self-contained (stateless, tidak import main); `api_handler`/`telegram_notifier` adalah leaf (tidak bergantung strategy/main); memory I/O adalah fungsi dalam main.

---

## 9. Known Architectural Risks

Identifikasi saja, tanpa solusi.

**Oversized modules:**
- `run()` adalah **fungsi tunggal yang sangat besar** — mengandung orchestration, kill switch, M5 gating, seluruh log block, tiga cabang memory engine, slot management, Telegram dispatch, dan miss-count logic dalam satu while-loop bersarang. Satu fungsi memegang mayoritas logika lifecycle.
- `build_signal_context()` **monolitik** — HTF, direction, veto, regime mapping, swing, sweep, OB, FVG, displacement, entry construction (dua mode + nested function), scoring call, dan assembly semua dalam satu fungsi. `_calc_continuation_entry` bersarang sebagai closure di dalamnya.

**Tight coupling:**
- `run()` terkopel erat ke struktur internal `result`/`context`/`h1_setup` dict — mengakses banyak key by string; perubahan bentuk dict di strategy langsung memecah main.
- Slot management + Telegram dispatch + miss-count semuanya inline di `run()` — ketiganya saling terkait lewat `active_signals` dan urutan operasi yang sensitif.
- `find_liquidity_target` dipanggil dari dalam entry construction dengan swings yang dihitung di scope `build_signal_context` — swing scope mengalir lintas beberapa fungsi.

**Duplicated responsibilities:**
- `is_poi_invalidated` dipanggil **dua kali** per cycle (kill switch + pre-memory STALE) dengan input berbeda (stored signal vs temp_signal) — logika invalidasi dijalankan di dua titik dengan semantik sedikit beda.
- Choppy penalty di-apply di **dua tempat** (`build_signal_context` dan `enrich_with_m5`) dengan guard untuk mencegah double-count — tanggung jawab yang sama tersebar.
- Entry construction untuk CONTINUATION dan RETRACEMENT punya pola serupa (find target, fallback 2R) yang diduplikasi di dua cabang.
- OB detection ada di `build_signal_context` (untuk fresh_ob/ob_meta) sementara OB quality filter ada di cabang RETRACEMENT — logika OB tersebar di dua tempat.

**Hidden shared state:**
- `active_signals` dimutasi di banyak titik dalam `run()` dengan urutan yang sensitif; beberapa cabang `del` lalu re-check `if symbol in active_signals` — state berubah di tengah alur.
- `miss_count` melayani **dua mekanisme berbeda** (decay eviction DAN TTL tiering via tap-proxy di `cleanup_expired_signals`) — satu field, dua konsumen dengan semantik berbeda; perubahan di satu memengaruhi yang lain secara tersembunyi.
- `last_miss_h1_ts` reset semantics (ke current H1 pada refresh, bukan 0.0) adalah aturan halus yang tidak terlihat dari struktur data — hanya benar jika alur increment/reset dipatuhi persis.
- `entry_source` string ("continuation_micro_pullback") menjadi **kontrak tersembunyi** yang dipakai lintas fungsi (entry calc menghasilkannya, score_signal gate mengandalkannya, +4 bonus mengandalkannya) — coupling via magic string.

**Long execution chains:**
- Satu scan cycle per coin: fetch D1 → fetch H1 → build_signal_context (belasan sub-step) → kill check → fetch M5 → enrich → log → pre-memory check → memory engine (tiga cabang) → dispatch. Rantai panjang dengan banyak titik `continue`/early-exit yang membuat alur sulit ditelusuri linear.
- `entry_price` mengalir dari `_calc_continuation_entry`/OB-branch → h1_setup → result → score_signal (RR gate) → payload → Telegram → (dormant) executor — satu nilai melewati banyak konsumen dengan kontrak semantik yang, seperti kita bahas, ambigu (structural reference vs executable).

**Regime/state recomputation:**
- Regime diklasifikasi ulang setiap cycle; sinyal di memori bisa berubah mode/regime antar cycle, menciptakan lifecycle noise yang tidak terisolasi.

**Coverage/generation limits sebagai risiko diagnostik:**
- OB first-match + 10-candle lookback window adalah kebijakan implisit yang tersebar dalam loop inline — bukan komponen terisolasi, sehingga sulit di-audit atau diubah tanpa menyentuh `build_signal_context` inti.

---

**Catatan verifikasi (updated 2026-07-22):** Seluruh fungsi yang sebelumnya ditandai tak-terverifikasi (`build_htf_map`, `classify_regime`, `_detect_sweep`, `is_poi_invalidated`, `get_session_mode`, `print_dashboard`, `_log_decay`, `_to_epoch_ts`, isi `BinanceHandler`, `telegram_notifier`, `executor`, `config.py`) telah dibaca langsung dan lengkap dari source. Temuan material: (1) killed-thesis cooldown ADA di kode (sebelumnya unverified); (2) same-thesis partial refresh tidak me-reset miss_count; (3) kill switch juga fire pada HTF flip D1+H4; (4) skema persist aktual 17 field; (5) `last_ob_audit_h1_ts` vestigial. Semua telah direkonsiliasi ke dokumen V2 via Decision #010 dan pembaruan Architecture §2/§6/§7/§8.