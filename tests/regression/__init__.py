"""Golden Oracle / Regression Harness — V1↔V2 behavioral-equivalence tooling.

This package is **verification-only**. It is not imported by any runtime module
(`v2/` or `src/`); nothing under runtime depends on it. It exists so that every
future migration milestone can prove its output is behaviorally identical to
V1's, on identical, deterministic market inputs (Roadmap §5 "comparison
harness", §6 "golden dataset").

The pieces:

- `snapshot`   — `InputSnapshot`: a deterministic, file-backed capture of the
                 market data one analysis run consumes. Loads/saves as JSON;
                 reconstructs the shared `MarketSnapshot` input contract that
                 both V1 and V2 analysis consume.
- `observable` — `ObservableBehavior`: the normalized, backend-neutral record of
                 every externally observable result (trend, structure, OB, FVG,
                 score, entry/TP/SL, kill/upgrade tier, persistence &
                 notification payloads). The unit of comparison.
- `oracle`     — `Analyzer` protocol + `V1Oracle`: runs V1's *existing* analysis
                 functions on a snapshot and extracts an `ObservableBehavior`.
                 A future `V2Analyzer` implements the same protocol; the harness
                 diffs the two.
- `compare`    — the field-level comparison engine (PASS/FAIL per observable
                 field, with diagnostics).
- `report`     — renders comparison results as readable text.
- `runner`     — the comparison workflow (snapshot → two analyzers → report;
                 and golden capture/replay).
- `generate`   — a CLI to capture snapshots from live V1, and to rebuild the
                 deterministic synthetic fixture used by the tests.

No harness code contacts the network, Telegram, or the real filesystem outside
temporary/committed test assets.
"""
