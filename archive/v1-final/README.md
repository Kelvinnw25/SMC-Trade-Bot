# V1 Final Archive

This is the complete, frozen state of **SMC Signal Radar V1** at the moment it
was retired, together with the recorded evidence that V2 reproduces its
behaviour. V2 2.2.0 is the only implementation in production; nothing in the
running system imports anything from this directory.

- **Git tag:** `v1-final` (see `MANIFEST.json` for the exact commit and per-file
  sha256 checksums)
- **V1 source archived:** 3,617 lines across 10 files
- **Recorded V1 answers:** 10,757 across 11 methods

## Why this exists

V1 was not just the previous implementation — it was the **behavioural oracle**.
Every equivalence test in the suite asserted "V2 reproduces V1's observable
output for this input", and executed that assertion by calling V1's real
`SMCStrategy` at test time. Deleting V1 would therefore have deleted the proof.

The proof was preserved instead of discarded. `equivalence-proof/goldens/`
contains V1's recorded return values for every call the test suite makes;
`tests/regression/v1_recorded.py` (in the live tree) replays them through a
drop-in stand-in with V1's exact method surface. The equivalence tests are
unchanged apart from their import line, and all of them still run.

**This directory is load-bearing.** The live test suite reads
`equivalence-proof/goldens/`. Deleting it breaks 146 equivalence tests.

## Layout

| path | contents |
|---|---|
| `src/` | V1's analysis stack: `strategy.py` (2,012 ln), `api_handler.py`, `config.py`, `telegram_notifier.py`, `executor.py` |
| `runtime/main.py` | V1's orchestrator loop (791 ln) |
| `scripts/` | V1 development scripts: `test_api.py`, `test_strategy.py`, `final_test.py`, `debug_api.py` |
| `tests/` | the live-V1 test bindings, superseded by the recorded oracle |
| `docs/` | `BLUEPRINT.md`, `MODULE_INTENT.md` — V1 architecture and per-module contracts |
| `migration/IMPLEMENTATION_PLAN.md` | the completed migration roadmap |
| `equivalence-proof/goldens/` | **live dependency** — V1's recorded answers, one JSONL per method |
| `equivalence-proof/fixtures/` | the committed input snapshot and its golden observable |
| `equivalence-proof/RESULTS.md` | what the proof covered and what it showed at archive time |

### `src/executor.py`

The only order-execution code that has ever existed in this repository. The
project is advisory-only — it emits Telegram signals and places no orders — so
its removal from the live tree is deliberate, not incidental.

### `tests/`

| file | was | superseded by |
|---|---|---|
| `oracle_live_v1.py` | `tests/regression/oracle.py` — `V1Oracle` over the live `SMCStrategy` | `tests/regression/oracle.py` (recorded binding) + `tests/regression/v1_projection.py` (extraction) |
| `v1_reference_live_v1.py` | `tests/lifecycle/v1_reference.py` — V1's lifecycle loop transcribed, calling live V1 for the kill predicate | the same file, now calling the recorded stand-in |
| `test_oracle_at_retirement.py` | snapshot of `tests/regression/test_oracle.py` | still live and passing; kept here as the retirement-time reference |
| `test_lifecycle_equivalence_at_retirement.py` | snapshot of `tests/lifecycle/test_lifecycle_equivalence.py` | still live and passing; kept here as the retirement-time reference |

## Re-recording the goldens

Only possible while `src/strategy.py` still exists. After V1 is deleted, restore
it from the `v1-final` tag first:

```bash
git checkout v1-final -- src/ main.py
rm -f archive/v1-final/equivalence-proof/goldens/*.jsonl
SMC_V1_RECORD=1 SMC_V1_SESSION=SNIPER venv/Scripts/python.exe -m unittest discover -s tests -t .
SMC_V1_RECORD=1 SMC_V1_SESSION=STRICT venv/Scripts/python.exe -m unittest discover -s tests -t .
git checkout HEAD -- src/ main.py   # or `git rm -r --cached` as appropriate
```

Two passes are required because V1 reads the wall clock in exactly one place
(`get_session_mode`, `src/strategy.py:14-19`) and two methods branch on it.
See `tests/regression/v1_recorded.py` for the full contract.

## What the goldens are keyed on

`sha256(method, arguments, instance state[, session])`. Instance state is V1's
`_bias_cache` (`src/strategy.py:9,21-34`), which had to be part of the key
because **`build_signal_context` is not a pure function of its arguments** — the
memo is keyed by `f"{timeframe}_{last_timestamp}"`, so two frames sharing a last
timestamp return the first one's bias for the second. `tests/analysis/test_scoring.py`
had documented this as a latent collision; recording proved it by aborting on
the conflict.

## Restoring V1 in full

```bash
git checkout v1-final -- src/ main.py test_api.py test_strategy.py final_test.py debug_api.py
git checkout v1-final -- tests/ BLUEPRINT.md MODULE_INTENT.md IMPLEMENTATION_PLAN.md
```

`requirements.txt` is unchanged by the retirement: `ccxt` remains pinned but
unused, because that file is the C-19 determinism contract and editing it is a
re-baselining event that would make every prior baseline incomparable.
