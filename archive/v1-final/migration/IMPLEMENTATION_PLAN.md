# Implementation Plan — Performance Roadmap

**Status:** frozen execution checklist. Architecture review closed 2026-08-02.
Execution audit applied 2026-08-02 (findings F1–F8).
**Scope:** ten steps, six merges, four execution gates.
**Rule:** one independent merge at a time. No step begins until the previous step
has passed its acceptance tests.

Test command for every step:

```
venv/Scripts/python.exe -m unittest discover -s tests -t .
```

The `-t .` is required. `pytest` is not installed and is not introduced.

---

## Terminology — three distinct replays (F6)

These are **not** interchangeable. Each runs on a different corpus, at a different
point in the sequence, and answers a different question.

| Term | When | Corpus | Question it answers |
|---|---|---|---|
| **Validation replay** | During steps 4 and 5, **before** code freeze | `data/corpus-pilot` (5 symbols, 1h+1d+5m, 2024-01-01 → 2025-03-31) | *Does this optimization produce byte-identical output?* |
| **Shakedown A** | Step 7, **after** code freeze | Stage 2 corpus, short window | *Does the frozen code crash?* |
| **Shakedown B** | Step 8, after Shakedown A passes | Stage 2 corpus, longer window + pathology symbols | *Does it survive at scale on hostile data?* |

Nowhere in this plan does a pre-freeze step depend on Shakedown A or B.

---

## Validation corpora — verified inventory (F3)

Every corpus referenced below was checked against its `corpus.json` on 2026-08-02.

| Corpus | `corpus_id` | Timeframes | Coverage | Usable for |
|---|---|---|---|---|
| `data/stage1` | `26c3aa504e43a84b` | **`["1h"]` only** | 797 datasets, 13,987,235 rows, 2020-01-01 → 2026-07-05 | Frame-level differentials (T5.1a, T5.1b-i/ii). **Cannot run a replay** — no D1, no M5. |
| `data/corpus-pilot` | `7dfe03dc31142dc1` | `["1h","1d","5m"]` | BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT · 2024-01-01 → 2025-03-31 | Every validation replay (T4.3, T5.2) and derived-frame differentials (T5.1b-iii). |
| Synthetic fixture | n/a | constructed | see step 4 §5 | T4.1 only. No existing corpus exhibits the required condition. |

**Declared validation-replay window: `2024-06-01 → 2025-03-31`, `burn_in_h1 = 0`.**
Not arbitrary. `corpus-pilot` starts 2024-01-01, so `WARMUP_D1_ROWS = 119` is satisfied
from 2024-04-29 onward; starting 2024-06-01 clears it with margin and matches the
configuration already exercised by the profiling harness. A window that violated warmup
would let the D1<10 guard skip symbols, and skipped symbols make byte-identity hold
trivially — which would silently weaken T4.3, T4.6 and T5.2. Both the ON and OFF runs of
every validation replay use this exact window.

**Correction applied:** the previous revision stated that steps 4 and 5 validate
against the Stage 1 corpus. Stage 1 is 1h-only and cannot run a replay. All
replay-based acceptance tests are redirected to `corpus-pilot`.

---

## Governing constraints (apply to every step)

| Constraint | Source | Consequence |
|---|---|---|
| `v2/` production edits are authorized **only** for the M1 lineage set and the C-8.5 performance work | C-16.3.1 | Step 5 is the **only** step that may touch `v2/`. Steps 0-4 are `backtest/`, `tests/` and repo-root files only. **One approved exception (2026-08-03):** finding A1's `STRATEGY_VERSION` 2.1.0 → 2.2.0 bump in [ledger.py](v2/app/ledger.py). Required because `core()` carries no harness version, so without it two runs either side of the A1 fix would share a `run_id` (C-5.7.2). Scope is that constant and its comment — no `v2/` behaviour, signature or field changes. |
| `atr_mean` MUST use `df["atr"].dropna().mean()` (pandas' summation order) | C-8.5 | [structure.py:90-96](v2/analysis/structure.py#L90) is frozen text. Enforced by T5.4. |
| Vectorization permitted only behind a golden-master test proving bit-identical Signals | C-8.5 | Step 5 gate is T5.1a + T5.1b + T5.2. Our bar (bit-identical *swings*) is stricter. |
| Symbol order MUST NOT be re-sorted, shuffled, parallelized or deduplicated | C-5.6.2 | No step parallelizes replay. Prohibition, not a risk assessment. |
| No cache may return a frame built for another `T` | I-8 | Drives the step 4 key design and T4.1. |
| Analysis memoization permitted as a dependency swap, keyed on the full determinant, cleared or keyed per `data_snapshot_id`, covered by a cached-vs-uncached byte-identical test | C-8.4 | Step 4. Key is **stricter** than C-8.4's text — see step 4 §4. |
| Warmup is a **report**, not a gate — enforced per symbol by the D1<10 / H1<50 guards | [driver.py:124-133](backtest/replay/driver.py#L124) | An under-warmed corpus does **not** fail. Gate A must validate warmup explicitly. See F1 below. |
| No resume in Version 1; acceptable **because** Shakedowns A and B establish crash probability first | PHASE0 §6 | **CODE FREEZE after step 5.** No merge between Shakedown A and the official replay. |
| Baseline MUST be a single unsharded run | C-5.7 item 4, R-14 | Step 9 is serial. Sharding is exploration-only. |
| Frozen docs are never edited; amendments are proposed patches awaiting approval | project rule | The C-8.4 amendment (step 4) blocked that merge, not this plan. **Approved and applied 2026-08-06**, together with the C-16.2 patch — see the closed gate items below. |

---

## Documentation obligations for every merge (F8)

Each of steps 0-5 must, as part of the same merge:

1. Add a **`CHANGELOG.md` Chapter B entry** — semantic milestone with engineering
   intent, never a commit dump (per that file's own header rule).
2. Update **`PROJECT_STATE.md`**: the `Last updated` date, `Doc version`, and — for
   step 0 only — the `Environment` line, which currently names versions inline and
   must instead point at `requirements.txt`.
3. Record the optimization's **flag name and default** in `PROJECT_STATE.md`, so the
   provenance block written into the run manifest at step 9 has a documented source.

These are listed per step under §2 and are part of the merge, not follow-up work.

---

# STEP 0 — Environment pinning

### 1. Objective
Make the interpreter and library set reproducible, and make C-8.5's summation-order
mandate mechanically enforced rather than accidentally satisfied.

### 2. Files modified
- `requirements.txt` **(new)**
- `tests/test_environment.py` **(new)**
- `PROJECT_STATE.md` — `Environment` line now references `requirements.txt`
- `CHANGELOG.md` — Chapter B entry

### 3. Why those files only
The enforcement must not be a production edit. `pandas.options.compute.use_bottleneck`
is `True` today, but `bottleneck` is **not installed** — verified against `pip freeze`.
Pinning therefore already guarantees the current summation path, and setting the option
in `v2/` would be an unauthorized production edit under C-16.3.1. The guard belongs in
`tests/`, where it fails loudly and changes no runtime behaviour.

### 4. Contract clauses
C-8.5 (summation order), C-16.2 (`env` capture), C-19 (float determinism is conditioned
on version identity), PHASE0 class-I.

Pin exactly the current set, verified this session:

```
python 3.13.5
numpy==2.2.6   pandas==3.0.3   pandas-ta==0.4.71b0   requests==2.34.2
```

plus the full `pip freeze` output (34 distributions), `==`-pinned.

### 5. Acceptance tests
| ID | Test |
|---|---|
| **T0.1** | Every distribution in `requirements.txt` is installed at exactly the pinned version; the running `platform.python_version()` matches the recorded interpreter. |
| **T0.2** | `import bottleneck` raises `ImportError`, and `numexpr` is absent. Failure message cites C-8.5. |
| **T0.3** | Full suite green. |

### 6. Rollback criteria
Delete both new files and revert the two doc edits. Zero runtime impact — nothing
imports them. Rollback is never warranted; the only reason to revert is a deliberate,
recorded environment migration, which requires re-baselining under C-5.7.2.

### 7. Expected speedup
None.

### 8. Scientific risk
None. This step *reduces* risk: it converts the contract's reproducibility claim from
an assertion into a checked property.

### 9. Effort
**~1 hour.**

---

# STEP 1 — `requests.Session` per worker

### 1. Objective
Eliminate the per-request TLS handshake. Measured: `requests.get` 394.0 ms vs
`Session.get` 214.3 ms — **1.84×**.

### 2. Files modified
- `backtest/acquire/transport.py`
- `tests/backtest/test_acquire.py`
- `CHANGELOG.md`, `PROJECT_STATE.md`

### 3. Why those files only
[transport.py:74-99](backtest/acquire/transport.py#L74) is the acquisition layer's
single network boundary. The `HttpTransport` Protocol forbids the transport from
retrying, so connection reuse is invisible to every caller. Sessions are held in
`threading.local()` from the outset so step 3b needs no second change here.

### 4. Contract clauses
None in REPLAY_CONTRACT — acquisition (M4) is outside the replay contract's production
scope. Governed by the `HttpTransport` Protocol invariants at
[transport.py:46-61](backtest/acquire/transport.py#L46): complete body or raise,
`TransportNotFound` for 404, `TransportError` for everything else, no retrying.

### 5. Acceptance tests
| ID | Test |
|---|---|
| **T1.1** | Two `get()` calls on one transport reuse one `Session` instance. |
| **T1.2** | Classification unchanged: 404 → `TransportNotFound`; 400 containing `"code":-1121` → `TransportNotFound`; any other non-200 → `TransportError`; connection failure → `TransportError`. |
| **T1.3** | Two threads obtain **distinct** `Session` objects (`threading.local` isolation). |
| **T1.4** | Full suite green, existing acquisition tests unmodified. |

### 6. Rollback criteria
Revert one file. Trigger: any change in HTTP status classification, or an increase in
`TransportError` rate against a live probe.

### 7. Expected speedup
**1.84×** on acquisition. 15.4 h → **8.4 h**.

### 8. Scientific risk
**None.** Identical bytes, identical order, identical retry policy.

### 9. Effort
**1-2 hours.**

---

# STEP 2 — `Retry-After` + 418/429 handling

### 1. Objective
Stop retrying rate-limit responses on a fixed backoff. Binance documents that
continuing after a 429 escalates to a 418 auto-ban, and that bans escalate from
2 minutes to 3 days. Six 418s were already observed during Stage 1 at W=1.

### 2. Files modified
- `backtest/acquire/transport.py`
- `backtest/acquire/downloader.py`
- `tests/backtest/test_acquire.py`
- `CHANGELOG.md`, `PROJECT_STATE.md`

### 3. Why those files only
Classification belongs to the transport (it reads the response); policy belongs to
[`_retry`](backtest/acquire/downloader.py#L451), which the module docstring names as
the owner of retry policy. No other module observes either.

### 4. Contract clauses
None frozen. Governed by the Binance API contract: fapi 2,400 weight/min per IP;
`klines` at `limit=1500` costs weight 10 → 4 req/s ceiling; `Retry-After` supplied on
both 418 and 429.

New types, both subclasses of `TransportError` so existing catch sites keep working:
`TransportRateLimited` (429) and `TransportBanned` (418), each carrying `retry_after`.

### 5. Acceptance tests
| ID | Test |
|---|---|
| **T2.1** | 429 with `Retry-After: 30` → sleeper called with 30 (not the 1/2/4 schedule); counted as a retry. |
| **T2.2** | 418 → **zero** retries; propagates immediately. |
| **T2.3** | 418 with `Retry-After` > 300 s → run aborts with the ban duration in the message. |
| **T2.4** | 418 with `Retry-After` ≤ 300 s → one pause, one resume, no further retries. |
| **T2.5** | Generic `TransportError` retry schedule is still exactly 1 s / 2 s / 4 s across 4 attempts. |
| **T2.6** | `TransportNotFound` and `DuplicateArtifact` still never retried. |
| **T2.7** | Full suite green. |

### 6. Rollback criteria
Revert two files — **but only while `workers == 1`.** After step 3b this step is a
safety prerequisite and must not be reverted independently. Recorded as a forward
coupling in the dependency graph.

### 7. Expected speedup
None. This is the safety prerequisite for step 3b.

### 8. Scientific risk
**None.** Changes *when* a request is retried, never what is fetched or how it is
classified.

### 9. Effort
**3-4 hours.**

---

# STEP 2b — `StorageTransient` retry

### 1. Objective
Stop a transient filesystem error from killing an entire acquisition run.
`os.replace` raises `PermissionError` when a scanner or indexer holds the destination
— routine on Windows with Defender active, and its probability scales with file
creation rate.

### 2. Files modified
- `backtest/acquire/storage.py`
- `backtest/acquire/downloader.py`
- `tests/backtest/test_acquire.py`
- `CHANGELOG.md`, `PROJECT_STATE.md`

### 3. Why those files only
The failure originates in [`_atomic_write`](backtest/acquire/storage.py#L439) and must
be classified there. [`_retry`](backtest/acquire/downloader.py#L469) currently catches
only `(TransportError, IntegrityError, _PayloadRejected)`, so `PermissionError` — an
`OSError`, not an `AcquisitionError` — escapes `acquire()`, escapes `run()`, and
terminates the run.

### 4. Contract clauses
None frozen. Governed by the raw-tier commit protocol documented at
[storage.py:13-28](backtest/acquire/storage.py#L13): bytes → fsync → `os.replace`,
then sidecar; the sidecar is the commit marker.

`StorageTransient` must subclass `AcquisitionError` and **must not** subclass
`IntegrityError` — a locked file is not corrupt bytes, and conflating them would let a
genuine checksum failure be reported as a filesystem hiccup.

### 5. Acceptance tests
| ID | Test |
|---|---|
| **T2b.1** | `_atomic_write` raising `PermissionError` once → the item retries and commits successfully. |
| **T2b.2** | Persistent `PermissionError` → `_CandidateFailed`, item status `failed`, **run continues** and reports it. |
| **T2b.3** | `StorageTransient` is not an `IntegrityError` (explicit `assertNotIsInstance`). |
| **T2b.4** | After a failed write, no `.part` file remains (the existing `except BaseException` cleanup still fires). |
| **T2b.5** | A real `IntegrityError` (checksum mismatch) is still classified as such and not as transient. |
| **T2b.6** | Full suite green. |

### 6. Rollback criteria
Revert two files. Trigger: any disk-full or permission-denied condition being silently
retried into a false success — which T2b.2 exists to prevent.

### 7. Expected speedup
None. Prerequisite for step 3b on Windows.

### 8. Scientific risk
**None.** No committed artifact changes; the commit protocol is untouched.

### 9. Effort
**2-3 hours.**

---

# STEP 3a — Deterministic `DownloadReport` (still W=1)

### 1. Objective
Make `DownloadReport.outcomes` ordering independent of completion order **before**
concurrency exists to confound it. This is the split that makes a future bisect
possible.

### 2. Files modified
- `backtest/acquire/downloader.py`
- `tests/backtest/test_acquire.py`
- `CHANGELOG.md`, `PROJECT_STATE.md`

### 3. Why those files only
[downloader.py:248-259](backtest/acquire/downloader.py#L248) is the only place outcome
order is established. `raw_manifest.json` already sorts by `artifact_path`
([storage.py:245](backtest/acquire/storage.py#L245)) and `_corpus_id` already hashes
*sorted* `(path, sha256)`, so neither is affected — this is the sole ordering seam.

### 4. Contract clauses
None frozen. Governed by [`run()`'s own docstring](backtest/acquire/downloader.py#L232):
*"Deterministic ordering: items are attempted exactly in the order the planner emitted
them."* This step preserves that guarantee under a future pool.

Change: pre-size `results: list[Optional[ItemOutcome]] = [None] * total`, assign by
index, assert no `None` survives, then `tuple(results)`.

### 5. Acceptance tests
| ID | Test |
|---|---|
| **T3a.1** | Golden test: `report.to_dict()` on a fixed mock plan is byte-identical to the pre-change output. **Capture the golden before editing** — it cannot be reconstructed afterwards. |
| **T3a.2** | No `None` survives assembly; a deliberately skipped index raises rather than silently producing a short report. |
| **T3a.3** | `progress` still fires exactly once per item with `(outcome, index, total)`, `index` 1-based and ascending. |
| **T3a.4** | Full suite green. |

### 6. Rollback criteria
Revert one file. Trigger: T3a.1 golden mismatch.

### 7. Expected speedup
None.

### 8. Scientific risk
**None.** Behaviour-identical at W=1 by construction.

### 9. Effort
**1-2 hours.**

---

# STEP 3b — Parallel downloader (W=6, token bucket, thread-safe temp files)

### 1. Objective
Overlap network latency. 98.5% of per-item cost is HTTP; CPU is 1.5%.

### 2. Files modified
- `backtest/acquire/downloader.py` (bounded pool)
- `backtest/acquire/storage.py` (thread id in temp name)
- `backtest/acquire/ratelimit.py` **(new — token bucket)**
- `backtest/cli.py` — `--workers` on the **`corpus build`** subparser
  ([cli.py:507](backtest/cli.py#L507)); default **1**. *(F7: there is no `acquire`
  subcommand.)*
- `tests/backtest/test_acquire.py`
- `CHANGELOG.md`, `PROJECT_STATE.md`

### 3. Why those files only
Each worker runs the **entire existing `acquire(item)` unchanged** — no stage is split,
no queue is introduced between download and commit. The per-item pipeline is already
correct and crash-safe; splitting it would create a new failure mode for a 1.5% CPU
saving. The token bucket is a new module because it is host-scoped policy, not
downloader policy.

### 4. Contract clauses
None frozen. Safety argument, each element verified against source:

- **Disjoint paths.** Paths encode `(symbol, timeframe, period, kind)` —
  [planner.py:273](backtest/acquire/planner.py#L273), [:299](backtest/acquire/planner.py#L299),
  [:303](backtest/acquire/planner.py#L303). Monthly/daily/REST namespaces are
  prefix-disjoint. Two items cannot target one path.
- **Atomicity.** `os.replace` is atomic on Windows and POSIX.
- **Commit marker.** Sidecar-or-nothing ([storage.py:213](backtest/acquire/storage.py#L213)).
- **Duplicate defence.** Byte-identical re-commit is a no-op returning the existing
  record; differing bytes raise `DuplicateArtifact`, never retried.
- **`corpus_id`** hashes sorted `(path, sha256)` — structurally order-independent.

Concurrency: **6 workers** on `data.binance.vision`; **≤3 req/s enforced by a shared
token bucket** on `fapi.binance.com` (4 req/s is the hard weight ceiling; 3 leaves 25%
headroom). Worker count is *not* the fapi control — even 2 workers at 214 ms would
exceed the limit.

### 5. Acceptance tests
| ID | Test |
|---|---|
| **T3b.1** | Two runs at W=6 with randomised per-item delays produce byte-identical `to_dict()`, and identical to the W=1 run. |
| **T3b.2** | `DownloadPlan.expected_paths()` contains no duplicates — converts the disjointness invariant into a check. |
| **T3b.3** | Token bucket admits ≤3 requests/s to the fapi host over a simulated 60 s, regardless of worker count. |
| **T3b.4** | A 429 pauses **all** workers, not only the one that received it; in-flight requests do not re-arm the pool during the pause. |
| **T3b.5** | Every `Future`'s `.result()` is retrieved — a raising `progress` callback surfaces as an exception, preserving the "nothing is ever dropped silently" guarantee at [downloader.py:32-38](backtest/acquire/downloader.py#L32). |
| **T3b.6** | Concurrent `_atomic_write` calls on distinct paths produce distinct temp names (pid **and** thread id). |
| **T3b.7** | Full suite green at W=1 **and** at W=6. |

### 6. Rollback criteria
**`--workers 1` restores exact sequential behaviour with no code revert.** This is the
strongest rollback in the plan. Trigger: T3b.1 divergence, sustained 418s, or an
SSL/EOF retry count that is nonzero in the final report.

**Interrupt semantics (S11 bug fix).** Acting on the "sustained 418s" trigger requires
halting the run, so the run must be haltable. Ctrl+C cancels every queued item and
aborts within the longest **in-flight** operation — bounded by
`BAN_PAUSE_LIMIT_SECONDS` (300 s) or the 60 s transport timeout, independent of plan
size. Work already running finishes and commits normally; cancellation never
interrupts a commit. Before the fix the interrupt drained the entire remaining plan
(measured: 900/900 items), which made this rollback criterion unexecutable.

### 7. Expected speedup
~6× on top of step 1. **15.4 h → 1.6-2.0 h** expected; best 1.2 h; worst 4-6 h if the
CDN throttles and W must drop to 2-3.

### 8. Scientific risk
**None to corpus identity** (sorted hashing). **None to report ordering** (step 3a).
The 6× itself is the least-validated number in the plan — an inference, not a
measurement.

### 9. Effort
**1-2 days.**

---

# GATE A — Stage 2 acquisition (execution, not a merge)

### 1. Objective
Acquire the full inventory at **1d** and **5m**, with sufficient lead-in that every
symbol is fully warmed at `T_0 = 2023-01-01`.

### 2. Acquisition ranges — normative (F1)

| Timeframe | Start | End | Why this start |
|---|---|---|---|
| **1d** | **2022-01-01** | 2026-07-05 | `WARMUP_D1_ROWS = 119` ([driver.py:103](backtest/replay/driver.py#L103)) → 119 closed daily candles before `T_0` requires data from **2022-09-04** at the latest. 2022-01-01 is chosen instead because 1d is negligible in size (~1,600 rows/symbol for the whole span) and a four-month margin removes any dependence on this arithmetic being exactly right. |
| **5m** | **2022-12-01** | 2026-07-05 | `WARMUP_M5_ROWS = 199` ([driver.py:104](backtest/replay/driver.py#L104)) → 199 closed 5-minute candles ≈ **16.6 hours** before `T_0`. One full month is taken because bulk archives are monthly and a partial month buys nothing. |
| **1h** | already held | — | Stage 1 covers 2020-01-01 → 2026-07-05. No re-acquisition. |

### 3. Why the lead-in is mandatory, and why it cannot be left to the runtime

Warmup is **not enforced**. [driver.py:124-133](backtest/replay/driver.py#L124) states
it explicitly:

> *"Warmup is enforced **per symbol by the existing guards**, not globally… So this is
> a **report**, not a gate."*

The guards are `D1 < 10` and `H1 < 50` closed rows. With 1d acquired only from
2023-01-01, a symbol has 10 D1 rows by 2023-01-11 and **passes the guard**, while the
adapter serves 10 rows where the configuration calls for 119. `_derive_simple_bias`
computes EMA50 and EMA200 over that frame ([htf.py:69-70](v2/analysis/htf.py#L69)), so
the D1 bias — and therefore direction, veto and kill — would be computed on a
progressively-warming frame for the **first four months** of a 3.5-year replay.

The run would not crash. The manifest would be clean. `verify_corpus` would pass. The
baseline would be silently wrong in its first ~10% and correct thereafter, and nothing
downstream would detect the seam. That is precisely the failure class PHASE0 §6 rejects
resume for.

### 4. Acceptance — explicit, not runtime-implied

| ID | Check |
|---|---|
| **TA.1** | `verify_corpus` reports `ok: true`; `artifacts_verified` matches `artifact_count`. |
| **TA.2** | `failed == 0`. Every `absent` item is justified by the planner's `PENDING_TRADING` + archive-absence rule. |
| **TA.3** | **Warmup pre-flight.** For every eligible symbol whose lifespan overlaps `T_0`: the canonical 1d series holds **≥119 closed rows strictly before 2023-01-01**, and the 5m series holds **≥199 closed rows strictly before 2023-01-01**. Symbols listed after `T_0` are exempt and must be listed by name in the gate's output, not silently skipped. |
| **TA.4** | Acquired timeframes are exactly `{1h, 1d, 5m}`; `spec.start` per timeframe matches §2 above. |
| **TA.5** | New `corpus_id` and `data_snapshot_id` recorded in `PROJECT_STATE.md`. |

**TA.3 is the gate this finding exists to create.** Without it the roadmap relies on a
runtime guard that is documented not to be a gate.

### 5. Rollback
The raw tier is immutable and append-only; a bad run is discarded explicitly via
`RawStore.discard`, never patched. A short lead-in is repaired by re-running with the
corrected range — the already-committed periods resume for free via `store.is_committed`.

### 6. Effort
~1.6-2.0 h wall clock, unattended. TA.3 adds minutes.

Steps 4 and 5 may be developed **in parallel with this gate** — both validate against
`data/corpus-pilot` and `data/stage1`, neither of which this gate modifies.

---

# STEP 4 — `CachingAnalyzer`

### 1. Objective
Remove the duplicate pipeline evaluation. [orchestrator.py:265-268](v2/app/orchestrator.py#L265)
runs the full analysis twice per symbol-scan — once for the observability/kill context,
once inside `analyze`. Contract-measured: ~214 ms → ~107 ms.

### 2. Files modified
- `backtest/replay/caching.py` **(new)**
- `backtest/replay/driver.py` (one line: the `analyzer=` argument at [driver.py:430](backtest/replay/driver.py#L430))
- `backtest/replay/config.py` (`caching_analyzer: bool = False`, **excluded from `core()`**)
- `tests/backtest/test_caching_analyzer.py` **(new)**
- `tests/backtest/fixtures/h1_gap_snapshots.py` **(new — see §5)**
- `CHANGELOG.md`, `PROJECT_STATE.md`

### 3. Why those files only
**No `v2/` edit is permitted here.** C-16.3.1 authorizes only the M1 lineage set and
the C-8.5 work as production edits. C-8.2's dependency table already sanctions
`V2Analyzer()` *"optionally wrapped by `CachingAnalyzer` (C-8.4)"*, so the wrapper is a
dependency swap in `backtest/` and nothing else changes.

The wrapper must expose exactly the two seams the orchestrator uses: a `.pipeline`
property whose `.run(context)` is memoized, and `.analyze(snapshot, *, prior, session_active)`.

### 4. Contract clauses
**C-8.4** (permitted, behaviour-neutral, keyed on the full determinant, cleared or keyed
per `data_snapshot_id`, covered by a cached-vs-uncached byte-identical test),
**C-8.2** (dependency table), **I-8** (*"no cache that can return a frame built for
another `T`"*), **C-16.3.1** (no production edit).

**The key is deliberately stricter than C-8.4's text.** C-8.4 specifies
`(symbol, h1_last_ts, session_active)`. That under-determines the snapshot: during an
H1 gap `h1_last_ts` freezes for hours while D1, M5 and `live_price` advance, and the
corpus holds **6,204 gap hours across 52 symbols**. Two cycles would collide on the key
with genuinely different snapshots and the cache would serve a stale Signal — silently.

Required key, all components available to the wrapper from the snapshot it already
receives:

```
(data_snapshot_id, symbol, h1_last_ts, len(h1), d1_last_ts, len(d1),
 m5_last_ts, len(m5), bit-pattern of live_price, session_active)
```

Two verified design facts:
- `MarketSnapshot` declares `d1`/`h1`/`m5` as `field(compare=False)`
  ([market.py:39-44](v2/domain/market.py#L39)), so the snapshot **cannot** be its own
  key — the determinant must be built explicitly.
- `AnalysisContext` and `Signal` are both `@dataclass(frozen=True)`, so returning a
  cached instance to two callers is safe.

`prior` is documented as intentionally unused; the wrapper must **bypass the cache**
when `prior is not None` rather than ignore it.

**This step is blocked on a proposed C-8.4 amendment** recording the key change, per
the frozen-docs workflow. The amendment must be approved before merge, not before
development.

### 5. The T4.1 synthetic fixture — specification (F4)

No existing corpus exhibits the condition. `corpus-pilot` holds the five most liquid
perpetuals and is effectively gapless; the gapped symbols (ICPUSDT, LUNAUSDT, ZENUSDT)
live in `data/stage1`, which has no M5. The fixture is therefore constructed.

**Shape.** Three consecutive replay cycles for one symbol — `T`, `T+3600`, `T+7200` —
simulating an H1 gap in which no new hourly candle closes.

| Component | Across all three cycles | Why |
|---|---|---|
| `symbol` | **unchanged** | The gap is within one symbol. |
| `h1` frame, `h1_last_ts`, `len(h1)` | **unchanged, byte-identical** | This *is* the gap: no H1 candle closed. |
| `session_active` | **unchanged** | Isolates the variable under test. |
| `data_snapshot_id` | **unchanged** | Isolates the variable under test. |
| `m5` frame | **CHANGES** — gains 12 rows per cycle; `m5_last_ts` advances by 3600 s | M5 archives are published independently of H1; this is the real condition. |
| `live_price` | **CHANGES** — a different float each cycle | The forming-candle close advances regardless of H1 closure. |
| `d1` frame | **CHANGES in variant B** — one additional closed daily row | A gap spanning a UTC midnight advances D1 while H1 is frozen. |

**Variant A** holds `d1` fixed and varies only `m5` + `live_price`.
**Variant B** additionally advances `d1`. Both must be present: A proves the M5 leg,
B proves the D1 leg, and C-8.4's key omits both.

**The M5 data must be chosen so the outcome genuinely differs.** The three cycles' M5
frames must produce **different `m5_choch` / `m5_reason` results** through
`enrich_with_m5`. Otherwise equality could hold by coincidence and the test would pass
against a broken cache. The fixture asserts this precondition explicitly before
asserting the cache behaviour.

**Assertions — equality alone is insufficient:**

1. `CachingAnalyzer` output == `V2Analyzer` output for all three cycles, both variants.
2. The cache registers a **MISS** on cycles 2 and 3. A hit there *is* the defect.
3. **Control:** a key computed as C-8.4 specifies — `(symbol, h1_last_ts,
   session_active)` — is shown to **collide** across all three cycles. This is what
   demonstrates the failure mode is real rather than hypothetical, and it is the reason
   the amendment is being raised.

### 6. Acceptance tests
| ID | Test | Corpus |
|---|---|---|
| **T4.1** | The fixture of §5: equality, MISS on cycles 2-3, and the C-8.4-key collision control. Variants A and B. | synthetic |
| **T4.2** | Cache is keyed or cleared per `data_snapshot_id`; two snapshot ids never share an entry. | synthetic |
| **T4.3** | Cached and uncached **validation replays** produce byte-identical output bundles (ledger, capture, manifest modulo wall-clock-only fields). Required verbatim by C-8.4. | **`data/corpus-pilot`** |
| **T4.4** | `prior is not None` bypasses the cache. | synthetic |
| **T4.5** | No entry is ever shared across symbols. | `data/corpus-pilot` |
| **T4.6** | **Hit-rate assertion:** ≥1 hit per symbol-scan. A cache that never hits passes T4.1-T4.5 trivially; this is the only test that proves it is doing anything. | `data/corpus-pilot` |
| **T4.7** | `caching_analyzer` is absent from `ReplayConfig.core()`. | n/a |
| **T4.8** | Full suite green with the flag ON and OFF. | — |

### 7. Rollback criteria
Set `analyzer=V2Analyzer()` — one line, no revert. Trigger: T4.3 divergence, T4.1 MISS
assertion failing, or any observed hit whose key components are not all present.

### 8. Expected speedup
**~2×** on replay. ~55 h → ~27 h.

### 9. Scientific risk
**This is the one live scientific risk in the plan.** Cache leakage and hidden state
are introduced deliberately. Bounded by the snapshot-complete key, T4.1 (including its
MISS and collision-control assertions), T4.2 and T4.5. Accepted only because C-8.4
sanctions the mechanism and I-8 states the invariant the key must satisfy.

### 10. Effort
**2-3 days**, including the fixture and the amendment.

---

# STEP 5 — Vectorized `identify_swings`

### 1. Objective
Remove the dominant replay cost. Measured: `identify_swings` is 88.3% of profiled
replay time (70-80% unprofiled), driven by 4.53 M pandas scalar lookups at 2.10 µs
against 0.058 µs for numpy — **36×**.

### 2. Files modified
- `v2/analysis/structure.py` — **lines 98-126 only**
- `tests/analysis/test_structure.py`
- `tests/analysis/test_swings_differential.py` **(new — T5.1a and T5.1b)**
- `CHANGELOG.md`, `PROJECT_STATE.md`

### 3. Why those files only
This is the **only** step authorized to touch `v2/`, and C-8.5 authorizes exactly this
function. `analysis.structure` is the single owner of swing detection
([structure.py:15-19](v2/analysis/structure.py#L15)); `analysis.htf` and
`analysis.scoring` import it, so one edit covers all three call sites.

**Frozen — must not be touched:**

| Lines | Content | Reason |
|---|---|---|
| [90-96](v2/analysis/structure.py#L90) | `atr_mean`, `math.isfinite` guard, `min_prominence` | **C-8.5 mandate.** `.mean()` is the function's only accumulation. |
| [128-139](v2/analysis/structure.py#L128) | Consecutive-equal suppression | Sequential and order-dependent; reads back the value it just appended. |
| [144-149](v2/analysis/structure.py#L144) | `to_swings` | Out of scope. |
| Signature, return type | `Tuple[list, list]` of `{"index", "price"}` | Every consumer depends on it. |

### 4. Contract clauses
**C-8.5** (*"permitted only behind a golden-master test proving bit-identical Signals,
and `atr_mean` MUST keep using `df["atr"].dropna().mean()`"*), **C-16.3.1** (this is
the authorized exception), **C-8.3/C-5.6** (call order unchanged).

Three implementation hazards, each with a mandated primitive:

1. **`Series.min()` skips NaN; `np.min()` propagates.** Use `np.nanmin`/`np.nanmax`.
2. **Python `max()` propagates NaN asymmetrically; `np.maximum` does not.**
   `max(a,b)` = *"b if b > a else a"* → use `np.where(b > a, b, a)`, **never**
   `np.maximum`.
3. **`n <= 2*window` must be the first statement.** The shifted-array form produces a
   zero-length right operand and a broadcast error where V1 returns `([], [])`.

Also verified: `to_numpy()` returns a **read-only** array under pandas 3.0
Copy-on-Write — no in-place assignment to extracted arrays. All price columns are
`float64` (`KLINE_DTYPE`), so no accumulation-width divergence exists.

### 5. The differential test — measured budget (F2)

The previous revision specified *"every rolling window the walker presents"* across all
797 datasets. That is 797 × 30,744 ≈ **24.5 M windows**; at the measured cost below it
would run for **~780 hours**. It is replaced by two tests with a declared, deterministic
sample.

**Measured cost of the V1 implementation** (this machine, 2026-08-02) — linear in rows:

```
n =    499 rows →    57.7 ms     (115.6 µs/row)
n =  5,000 rows →   568.6 ms     (113.7 µs/row)
n = 17,550 rows → 2,008.2 ms     (114.4 µs/row)
```

The vectorized implementation is expected to be 50-100× faster, so **V1 dominates the
budget** and every figure below is V1's cost.

#### T5.1a — whole-frame differential, all Stage 1 datasets

Corpus: **`data/stage1`** (1h-only is sufficient — this test needs *frames*, not a
runnable replay).

- Every one of the **797 datasets**, complete series, **13,987,235 rows total**.
- All five parameter sets: (2, 0.30), (3, 0.18), (1, 0.15), (2, 0.25), (2, 0.0).
- Budget: 13,987,235 rows × 114.4 µs × 5 sets ≈ **2.2 hours**.

This is the data-diversity gate: every real price series the project holds, at every
parameter set in production.

#### T5.1b — declared deterministic rolling-window sample

Three components, each with its selection rule written down so the sample is
reproducible and reviewable rather than ad hoc.

| Component | Selection rule | Volume | Budget |
|---|---|---|---|
| **(i) Strided windows** | For each of the 797 Stage 1 datasets, windows of 499 rows at offsets `0, s, 2s, …` where `s = max(1, (n_rows − 499) // 100)` → **exactly 101 windows per dataset**, plus the final window. Deterministic given the dataset. | 81,297 windows × 2 parameter sets, (2, 0.30) and (3, 0.18) — the two that run on H1 frames in replay | **2.6 h** |
| **(ii) Pathology dense segments** | The 15 shakedown symbols already selected in `data/stage1/coverage.md`. For each, **2,000 consecutive windows at stride 1** centred on that symbol's longest gap — or **every available window** where the symbol holds fewer than 2,499 rows, which is the case for the `fewest_rows` members (CAPUSDT, GRAMUSDT, DATAIPUSDT). The count actually used is asserted and reported per symbol, so a short symbol reduces the sample visibly rather than silently. Stride 1 is what exercises gap-adjacent boundary behaviour. | ≤30,000 windows × 2 parameter sets | **≤58 min** |
| **(iii) Derived frames** | From **`data/corpus-pilot`** (the only corpus with 1d and 5m): the H4 resample produced at [htf.py:206-216](v2/analysis/htf.py#L206) — which has **no `atr` column**, exercising the `atr_mean = 0.0` branch — plus the EMA-augmented D1/H1 frames from `_derive_simple_bias` and the 199-row M5 frames from `enrich_with_m5`. Sampled at **stride 10** over the pilot window (10,944 h × 5 symbols → **5,472 cycle-symbols**), each yielding four frame types. | 5,472 × 4 frame types, parameter sets (3, 0.18), (1, 0.15), (2, 0.25) | **~10 min** |

**T5.1b total ≈ 3.7 hours. T5.1a + T5.1b ≈ 6 hours**, one time.

**Why this preserves scientific confidence.** T5.1a covers all 14 M rows of real data at
every parameter set — data diversity is exhaustive, not sampled. T5.1b exists to catch a
*different* bug class: slice offsets, window edges and boundary arithmetic. Those are
structural and do not require dense data sampling; 81,297 distinct (dataset, offset)
pairs is ample, and component (ii) supplies stride-1 density exactly where the boundary
behaviour is hardest. Component (iii) covers the two input classes no raw corpus
contains — the `atr`-less H4 frame and the EMA-augmented frame — which whole-frame
testing on Stage 1 cannot reach.

Six hours is proportionate: it gates a change that protects every future baseline, and
it runs once.

### 6. Acceptance tests
| ID | Test | Corpus |
|---|---|---|
| **T5.1a** | Whole-frame bit-exact differential vs `SMCStrategy.identify_swings` ([src/strategy.py:517](src/strategy.py#L517)) — all 797 datasets, all five parameter sets. Compared by `np.float64(...).tobytes()` plus `type(...)` — never `assertEqual`, which passes on type changes and fails on NaN. | `data/stage1` |
| **T5.1b** | The three declared components of §5, same comparison method. | `data/stage1` (i, ii) · `data/corpus-pilot` (iii) |
| **T5.2** | Byte-identical **validation replay**, vectorized ON vs OFF. Satisfies C-8.5's golden-master requirement. *(Not Shakedown A/B — see Terminology.)* | **`data/corpus-pilot`** |
| **T5.3** | The three hazards explicitly: NaN in a low window; NaN on exactly one side of the `max`; `n <= 2*window`. | synthetic |
| **T5.4** | **Source assertion:** the `atr_mean` line still reads `float(df["atr"].dropna().mean())`. Freezes the C-8.5 mandate against future edits. | n/a |
| **T5.5** | Ties preserved: `prominence == min_prominence` survives (strict `<`); equal neighbours remain swings (strict `<`/`>`). | synthetic |
| **T5.6** | Full suite green. | — |

**Acceptance criterion: one differing swing — one index, one float bit, one list
length, one element type — rejects the change.** No tolerance, no sampling *within* the
declared set.

### 7. Rollback criteria
`SWING_IMPL = "v1" | "vectorized"` module constant, or revert one function. The V1
oracle remains in the tree permanently. Trigger: any T5.1a, T5.1b or T5.2 difference.

### 8. Expected speedup
Removes ~85-90% of remaining replay time. ~27 h → **6-12 h expected**; best ~4 h;
worst ~25 h.

### 9. Scientific risk
**LOW.** The changed region contains only IEEE comparisons (exact), selections (return
an input unmodified) and one subtraction (one IEEE op). No accumulation. The only
accumulation is line 91, which is frozen. Residual risk is implementation error in the
three named hazards, and T5.1b/T5.3 exist to catch exactly those.

### 10. Effort
**3-5 days**, plus ~6 h of unattended differential runtime.

---

# ═══ CODE FREEZE ═══

PHASE0 §6: *"No resume in Version 1… This is acceptable **because** Shakedowns A and B
establish a low crash probability first. The two decisions are coupled."*

**No merge of any kind between Shakedown A and the official replay.** A post-shakedown
change invalidates the evidence the no-resume decision rests on. If a defect is found
after Shakedown A, the shakedowns are re-run from A — they are not patched around.

**Gate items owned by the user — ALL CLOSED 2026-08-06:**
- ✅ `T_n` decision: **2026-06-28T23:00:00Z** (1782687600), chosen because all
  trades resolve — it sits exactly 168 h (`OutcomeConfig.max_holding_h1`) before
  the corpus's last boundary, so there is no `OPEN` tail to censor. 30,600
  boundaries. Recorded in `PHASE0_FREEZE.md` §9.
- ✅ `universe_id` vs C-16.2 `core` — patch **applied**. `static_universe` is named
  in `core` to match the implementation; `universe_id` and the two `optimizations`
  flags are documented as recorded *outside* it, with the shared-`run_id`
  consequence stated and Gate B's stability result as the mitigation.
- ✅ C-8.4 amendment (step 4) — **applied**. The original three-component key is
  marked unsafe (T4.1 proves it collides across an H1 gap); the implemented
  ten-component determinant is now the required key.
- ✅ `PHASE0_FREEZE.md` — version history gains **2.2.0** (the A1 stale-ranking
  fix, final frozen strategy state); new §9 records the selected window with its
  PASS/FAIL evidence, the frozen identities, the optimization provenance block
  (`caching_analyzer = true`, `vectorized_swings = true`) and the shakedown
  evidence the no-resume decision rests on.

---

# GATE B — STEP 6: Coverage re-run

### 1. Objective
Re-measure on the post-Stage-2 corpus. The 2023 PASS was earned on 1h alone; 5m and 1d
are now present.

### 2. Files modified
None. Artifacts only, plus `PROJECT_STATE.md` for the recorded identities.

### 3. Contract clauses
C-6.9 (universe reconstruction), `COVERAGE_SCHEMA_VERSION = 3`.

### 4. Acceptance — `universe_id` is evidence, not an invariant (F5)

The previous revision required *"`universe_id` unchanged"*. That is not a property the
hash provides. `universe_id` is the sha256 of

```
{universe_definition_version, table_version, universe_table_sha256,
 resolved: {symbol: [contract_type, underlying_type, quote_asset, source]}}
```

resolved from the corpus's **own committed discovery snapshots**. Stage 2 commits new
snapshots into a new corpus root, so any instrument listed between the Stage 1 and
Stage 2 discoveries enlarges `resolved` and changes the hash. A changed `universe_id`
is the expected outcome, not a failure.

| ID | Check |
|---|---|
| **TB.1** | 2023-01-01 still **PASSes** with all three timeframes present. |
| **TB.2** | `universe_id` is **recorded** in `PROJECT_STATE.md` alongside the Stage 1 value. |
| **TB.3** | If it differs, a **symbol-level diff** is produced: every added and removed symbol, with its `[contract_type, underlying_type, quote_asset, source]`. |
| **TB.4** | **Acceptable deltas:** *additions only*, each a newly-listed instrument absent from the Stage 1 discovery. **Not acceptable without investigation:** any *removal*; any change to an existing symbol's `contract_type`, `underlying_type` or `quote_asset`; any change of `source` from `snapshot` to `table` or vice versa; any change to `table_version` or `universe_table_sha256`. |
| **TB.5** | The diff is reviewed and signed off by the user before Shakedown A. An unreviewed delta blocks the gate. |
| **TB.6** | `read_snapshots` raised no `SnapshotDisagreement` — a conflict between two snapshots on the same symbol is a hard stop, not a delta. |
| **TB.7** | **Hash-seed determinism:** coverage run twice under differing `PYTHONHASHSEED` yields byte-identical output. Freezes the load-bearing `, s` tie-breaks at [coverage.py:962](backtest/coverage.py#L962) and [:967](backtest/coverage.py#L967). |

### 5. Rollback
N/A — measurement only.

### 6. Effort
~1 hour including review of the TB.3 diff.

---

# GATE C — STEPS 7-8: Shakedown A, then Shakedown B

### 1. Objective
Establish crash probability on the exact frozen code. **Distinct from the validation
replays of steps 4 and 5** (see Terminology): those ran pre-freeze on `corpus-pilot` to
prove byte-identity; these run post-freeze on the Stage 2 corpus to prove survivability.

### 2. Files modified
None.

### 3. Contract clauses
PHASE0 §6 (the coupling), C-5.7 (burn-in), C-15.8 (swallowed errors).

### 4. Acceptance
Zero crashes; zero swallowed errors above the C-15.8 budget; every `universe_error`
`None`; run health clean; wall-clock rate consistent with the step 4+5 projection.
**Shakedown B** must include the 15 pathology symbols already selected in
`data/stage1/coverage.md` (BTCSTUSDT, CAPUSDT, GRAMUSDT, ICPUSDT, LUNAUSDT, ZENUSDT,
ZILUSDT and the liquid controls).

### 5. Rollback
A failure sends the roadmap back to the step that introduced it — the shakedowns are
not patched around.

### 6. Effort
A: hours. B: proportional to the window chosen.

---

# GATE D — STEP 9: Official replay

### 1. Objective
Produce the canonical baseline.

### 2. Files modified
None. `PROJECT_STATE.md` and `CHANGELOG.md` record the baseline on completion.

### 3. Contract clauses
C-5.7 items 2-4 and R-14 — **single unsharded run**, empty lifecycle memory, declared
`burn_in_h1 = 336`. C-16.2/C-16.4 — manifest and bundle.

### 4. Acceptance
Manifest records `env`, `universe_id`, `data_snapshot_id`, and the optimization
provenance block (`{"caching_analyzer": true, "vectorized_swings": true}`) — **excluded
from `core()`**, exactly as `universe_id` is, because the claim being made is that they
do not change results.

### 5. Rollback
None. A crash means restart from zero (PHASE0 §6).

### 6. Effort
6-12 h wall clock.

---

# Dependency graph

```
STEP 0  Environment pinning
   │    gate: T0.1-T0.3
   │
   ├──────────────────────────────────────────────┐
   │                                              │
   ▼                                              │
STEP 1  requests.Session                          │
   │    gate: T1.1-T1.4                           │
   ▼                                              │
STEP 2  Retry-After + 418/429                     │
   │    gate: T2.1-T2.7                           │
   │    ── forward coupling: MUST NOT be          │
   │       reverted independently after 3b ──     │
   ▼                                              │
STEP 2b StorageTransient                          │
   │    gate: T2b.1-T2b.6                         │
   ▼                                              │
STEP 3a Deterministic report (W=1)                │
   │    gate: T3a.1-T3a.4                         │
   ▼                                              │
STEP 3b Parallel downloader (W=6)                 │
   │    gate: T3b.1-T3b.7                         │
   ▼                                              │
GATE A  ═══ Stage 2 acquisition ═══               │
   │    1d from 2022-01-01 · 5m from 2022-12-01   │
   │    gate: TA.1-TA.5 (TA.3 = warmup pre-flight)│
   │                                              │
   │        ┌─────────────────────────────────────┘
   │        │  steps 4 and 5 validate against
   │        │  data/corpus-pilot and data/stage1,
   │        │  neither of which Gate A modifies.
   │        │  May run IN PARALLEL with Gate A.
   │        ▼
   │   STEP 4  CachingAnalyzer
   │      │    gate: T4.1-T4.8
   │      │    BLOCKED ON: C-8.4 amendment approval
   │      ▼
   │   STEP 5  Vectorized identify_swings
   │      │    gate: T5.1a, T5.1b, T5.2-T5.6
   │      │    (after 4 → the 2× halves every
   │      │     validation replay T5.2 runs)
   │      │
   └──────┴──► ═══════ CODE FREEZE ═══════
                  │
                  ▼
              STEP 6  Coverage re-run
                  │    gate: TB.1-TB.7
                  ▼
              STEP 7  Shakedown A
                  │    gate: zero crashes
                  ▼
              STEP 8  Shakedown B
                  │    gate: zero crashes
                  ▼
              STEP 9  Official replay
```

## Hard ordering constraints, with their reasons

| Constraint | Reason |
|---|---|
| **0 before everything** | Pinning after baselines exist pins the wrong environment. |
| **2 and 2b before 3b** | Raising concurrency while 418s retry on a fixed backoff converts a 2-minute throttle into a 3-day ban. `PermissionError` becomes likely rather than rare at W=6. |
| **3a before 3b** | Proves plan-order reassembly at W=1, so a later report divergence is attributable to the pool and not the refactor. |
| **4 before 5** | Testing, not performance: the 2× halves every validation replay T5.2 runs. Steps are independent in code. |
| **5 last of the merges** | Only step touching `v2/`; C-16.3.1. |
| **CODE FREEZE after 5** | PHASE0 §6 — the no-resume decision is coupled to shakedown evidence. |
| **Gate A before 6** | Coverage measures the Stage 2 corpus. |
| **6 before 7** | A corpus that fails coverage must not be shaken down. |
| **7 before 8 before 9** | PHASE0 §6. |

## What may run concurrently

- Steps **4 and 5** may be developed while **Gate A** runs unattended. Step 4 validates
  against `data/corpus-pilot` (T4.3, T4.5, T4.6) and a synthetic fixture (T4.1, T4.2,
  T4.4); step 5 validates against `data/stage1` (T5.1a, T5.1b-i/ii) and
  `data/corpus-pilot` (T5.1b-iii, T5.2). Gate A writes to neither.
- Nothing else. Every other edge above is a hard dependency.

## Cumulative projection

| Stage | Before | After |
|---|---:|---:|
| Stage 2 acquisition | 15.4 h | **1.6-2.0 h** |
| Official replay | ~55 h | **6-12 h** |
| **Total** | **~70 h** | **~8-14 h** |

Worst case (CDN throttling forces W=2-3; vectorization residual is higher than
projected): ~30 h — still better than 2×.

One-time validation cost not in the table: **~6 h** of unattended differential runtime
at step 5 (T5.1a + T5.1b).
