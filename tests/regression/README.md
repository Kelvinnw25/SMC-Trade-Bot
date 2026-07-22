# Golden Oracle / Regression Harness

Verification tooling that proves each migration milestone stays **behaviorally
equivalent to V1**. It is not a trading feature and not part of runtime — nothing
under `v2/` or `src/` imports it. It exists only to compare V1 and V2 outputs on
identical, deterministic market inputs (Roadmap §5 "comparison harness", §6
"golden dataset").

The migration allows V2 to have a completely different architecture, but **not**
different observable behavior. This harness is how that rule is enforced,
milestone by milestone.

---

## What is compared

Only **externally observable** results — never internal implementation. One
`ObservableBehavior` record captures, for one input:

| Group | Fields |
|---|---|
| Market snapshot | `symbol`, `h1_last_ts`, `live_price`, `d1_rows`, `h1_rows`, `m5_rows`, `atr_last` |
| Trend | `weekly_bias`, `d1_bias`, `h4_bias`, `location`, `regime`, `direction`, `direction_source`, `bull`, `bear`, `anchor_override`, `veto_reason` |
| Structure (BOS/CHOCH) | `displacement_valid` (H1 break), `m5_choch` (M5 change-of-character) |
| Liquidity | `sweep_valid` |
| Order Blocks | `fresh_ob`, `ob_selected_index`, `ob_selected_top`, `ob_selected_bottom`, `ob_candidate_count` |
| Fair Value Gaps | `fvg_valid` |
| Score | `score`, `notes`, `reason` |
| Entry | `entry_mode`, `entry_ready`, `entry_source`, `entry_price` |
| Take Profit | `tp`, `tp_source`, `tp_rr` |
| Stop Loss | `sl` |
| Kill Thesis | `kill_triggered` (evaluated only when a prior signal is supplied) |
| Upgrade Thesis | `decision`, `alert_tier`, `alert_rank` |
| Persistence payload | `persistence_payload` (the record V1 would persist for a fresh signal) |
| Notification payload | `notification` (the alert V1 would send) |

Two systems are **equivalent for an input iff they produce an equal
`ObservableBehavior`.**

### Scope boundary (what the *harness* captures vs. what later phases replay)

The oracle captures the **analysis-level** observables plus the single-cycle,
state-independent *projection* of the persistence/notification payloads V1 would
emit for a fresh signal. It deliberately does **not**:

- stamp wall-clock fields (`timestamp`, `alert_sent_ts`) — those are validated
  later with an injected clock, not here;
- replay **path-dependent lifecycle** sequencing (miss_count progression, alert
  dedup / upgrade routing, TTL, replace, the OB-audit dedup set). The alert
  *tier* that drives that routing is captured; Phase 4 reuses this same
  comparison machinery to replay the ordered sequence.

This keeps the harness strictly a *verification tool* — it runs V1's existing
analysis, it does not re-implement analysis, lifecycle, or the orchestrator.

---

## How snapshots are stored

An `InputSnapshot` is a frozen capture of exactly what one analysis run consumes:
the closed-candle D1/H1(/M5) frames, the forming-candle `live_price`, and the H1
reference timestamp. It serializes to plain, human-diffable **JSON** (see
`fixtures/synthetic_short.json`):

```json
{
  "format": 1,
  "symbol": "SYNTH/USDT",
  "source": "synthetic",
  "live_price": 174.85,
  "h1_last_ts": 1770692400.0,
  "columns": ["timestamp", "open", "high", "low", "close", "v", "atr"],
  "d1_rows": [ { "timestamp": "2026-01-01T00:00:00", "open": ..., "atr": null }, ... ],
  "h1_rows": [ ... ],
  "m5_rows": null
}
```

Fidelity guarantees:
- V1's exact column set/order is preserved.
- ATR is `null` (→ `NaN`) for the first `ATR_PERIOD` rows, exactly like real V1
  data; the round-trip is lossless.
- Floats round-trip bit-exactly (Python's `repr`-based JSON float encoding).
- `h1_last_ts` is stored as captured (V1's `_to_epoch_ts` value), not recomputed.

`InputSnapshot.to_market_snapshot()` rebuilds the shared
`v2.domain.MarketSnapshot` — the one contract **both** the V1 oracle and any
future V2 analyzer consume — so both sides are fed byte-identical inputs. Store
one file per symbol/cycle; a directory of them is a dataset.

---

## How to generate snapshots from V1

Run from the repo root, using the project venv.

**Live capture** (one network round-trip through the real V1 stack; manual use
only — never run by the test suite):

```bash
python -m tests.regression.generate live \
    --symbol BTC/USDT \
    --out tests/regression/fixtures/btcusdt.json \
    --m5 --golden
```

`--m5` attaches closed, hour-aligned M5 candles (V1's guards). `--golden` also
writes `btcusdt.golden.json`, the V1 `ObservableBehavior` field map for that
snapshot.

**Synthetic fixture** (deterministic, no network — this is what the tests load):

```bash
python -m tests.regression.generate synthetic \
    --out tests/regression/fixtures/synthetic_short.json
```

`build_synthetic_snapshot()` is pure and importable, so the fixture is fully
reproducible.

To build a multi-symbol golden dataset once V1 is capturing live: loop `live`
over the top-volume universe per cycle, writing one snapshot (and golden) per
symbol into a dated directory. That directory is a replayable dataset for
`load_snapshots(...)`.

---

## How to compare V1 against V2

The comparison engine works on any two analyzers implementing the `Analyzer`
protocol (`analyze(MarketSnapshot, *, prior=None) -> ObservableBehavior`). Today
only `V1Oracle` exists; a `V2Analyzer` arrives with Phase 3.

```python
from tests.regression.snapshot import InputSnapshot
from tests.regression.oracle import V1Oracle
from tests.regression.runner import compare_analyzers
from tests.regression.report import render_report

snap = InputSnapshot.load("tests/regression/fixtures/synthetic_short.json")

# Once V2 exists:  report = compare_analyzers(snap, V1Oracle(), V2Analyzer())
report = compare_analyzers(snap, V1Oracle(), V1Oracle())   # self-check baseline
print(render_report(report))
assert report.ok        # zero divergence == behaviorally equivalent
```

Or replay against a **stored golden** (lets you diff V2 without V1 present, and
catches accidental changes to V1 itself):

```python
from tests.regression.runner import load_golden, compare_against_golden

golden = load_golden("tests/regression/fixtures/synthetic_short.golden.json")
report = compare_against_golden(snap, V1Oracle(), golden)   # or V2Analyzer()
assert report.ok
```

**Comparison policy:** exact equality by default — a rounding difference that
flips a decision tier is a real divergence, not noise (Roadmap §6). A
`float_tolerance` argument exists strictly for diagnosis; it defaults to `0.0` so
the migration bar stays exact. A field present on only one side is always a
failure (a dropped or added observable is a divergence). Every report gives
per-field PASS/FAIL with the exact path, expected value, actual value, and reason.

---

## How future milestones use the harness

- **Phase 3 (Pure Analysis).** Implement `V2Analyzer(Analyzer)` producing an
  `ObservableBehavior` from a `MarketSnapshot`. For every golden snapshot, assert
  `compare_analyzers(snap, V1Oracle(), V2Analyzer()).ok`. Because the report
  localizes divergence to a field, a break shows up as e.g. `regime` or
  `ob_selected_top`, pinpointing the first analysis stage that diverged.

- **Phase 4 (Lifecycle).** Reuse `compare_field_maps` / `ComparisonReport` to
  diff the *ordered sequence* of memory states, evictions, and alerts. Lifecycle
  adds the path-dependent fields this harness intentionally omits (miss_count
  progression, dedup routing, TTL) and drives them with the injected clock.

- **Phase 5/6 (Orchestration, Cutover).** Capture live snapshots each cycle,
  replay both systems, and gate cutover on a divergence-free window (Roadmap §5
  Stage B, §8). The per-cycle report is the divergence log.

New observable fields are added to `ObservableBehavior` (and populated by both
the oracle and the V2 analyzer); the comparison engine picks them up
automatically.

---

## Files

| File | Role |
|---|---|
| `snapshot.py` | `InputSnapshot` — file-backed deterministic input; ↔ `MarketSnapshot`. |
| `observable.py` | `ObservableBehavior` — the comparison record + `as_field_map()`. |
| `oracle.py` | `Analyzer` protocol + `V1Oracle` (runs V1's unmodified analysis). |
| `compare.py` | Field-level PASS/FAIL engine. |
| `report.py` | Text rendering of comparison results. |
| `runner.py` | Comparison workflow + golden capture/replay + `load_snapshots`. |
| `generate.py` | CLI: `live` capture and `synthetic` fixture builder. |
| `fixtures/` | Committed deterministic snapshot + its golden field map. |
| `test_*.py` | Unit tests (no network, no Telegram, temp files only). |

Run the tests: `python -m unittest discover tests` (from the repo root).
