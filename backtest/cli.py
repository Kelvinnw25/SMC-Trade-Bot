"""`backtest` command line — the research pipeline as one interface.

REPLAY_CONTRACT.md §10 ("cli.py — entry points"), §11. Milestone M11.

    python -m backtest corpus  build   --root DATA --start 2023-01-01 --end 2025-06-30
    python -m backtest corpus  verify  --root DATA
    python -m backtest reference snapshot --root DATA
    python -m backtest reference funding  --root DATA --symbols BTCUSDT --start ... --end ...
    python -m backtest replay          --corpus DATA --out RUNS --start ... --end ...
    python -m backtest outcome         --corpus DATA --run RUNS/runs/<run_id>
    python -m backtest audit           --run RUNS/runs/<run_id>
    python -m backtest analytics       --run RUNS/runs/<run_id>
    python -m backtest metrics         --run RUNS/runs/<run_id>
    python -m backtest freeze          --run ... --out baselines/v2.0 --label ...
    python -m backtest splits          --start ... --end ... --train 4000 --test 2000
    python -m backtest holdout declare --root RESEARCH --start ... --end ... ...
    python -m backtest holdout status  --root RESEARCH

## Why a CLI is part of the methodology

Every stage was already callable from Python, and that is exactly the problem: a
result produced by a hand-written script is reproducible only if the script
survives. These subcommands take the same arguments in the same order every time
and print the identities (`corpus_id`, `data_snapshot_id`, `run_id`,
`metrics_hash`) that make a result quotable.

The pipeline stages are **separate commands, not one button**. Replaying is hours
of compute, resolving outcomes is minutes, and metrics is seconds; fusing them
would mean re-running the expensive stage to change a report. Each stage reads what
the previous one wrote, from disk, exactly as an independent researcher would.

## What it will not do

There is no `--force`, no `--fix`, and no flag that relaxes an integrity check or
a holdout guard. A command that can be talked out of its checks does not have them.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

__all__ = ["build_parser", "main"]

H1 = 3600


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------

def _boundary(text: str) -> int:
    """An H1 scan boundary, as an epoch second or an ISO-8601 UTC instant."""
    text = text.strip()
    if text.isdigit():
        value = int(text)
    else:
        cleaned = text.replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(cleaned)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{text!r} is neither an epoch second nor an ISO-8601 instant "
                f"(e.g. 2025-03-11T12:00:00Z)") from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        value = int(moment.timestamp())
    if value % H1:
        raise argparse.ArgumentTypeError(
            f"{text} is not on an hour boundary; a scan boundary that is not on "
            f"an hour does not exist (C-5.1)")
    return value


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not an ISO date (YYYY-MM-DD)") from None


def _workers(text: str) -> int:
    """Worker count, bounded by what IMPLEMENTATION_PLAN sanctions.

    The ceiling is empirical: `data.binance.vision` publishes no request limit,
    and above ~6 workers community reports put SSL/EOF failures in range. Refusing
    here rather than clamping silently means an operator who asks for 32 finds out
    why they cannot have it.
    """
    from backtest.acquire.downloader import RECOMMENDED_MAX_WORKERS

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"--workers must be >= 1, got {value}")
    if value > RECOMMENDED_MAX_WORKERS:
        raise argparse.ArgumentTypeError(
            f"--workers {value} exceeds the sanctioned maximum of "
            f"{RECOMMENDED_MAX_WORKERS} for the bulk archive host")
    return value


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Operational observability
# ---------------------------------------------------------------------------
#
# Progress goes to stderr so that redirecting stdout into a results file keeps
# the identities clean; the operational log goes beside the artifacts, in its own
# file, so that deleting it is obviously safe. Neither is ever read back by the
# pipeline. See `backtest/observe.py` for the four-tier separation.

def _observability(
    args: argparse.Namespace, root: Path, job: str,
    *, unit: str = "items", rate_name: str = "/s",
):
    """Build `(Progress, OperationalLog)` for a long-running job."""
    from backtest.observe import OperationalLog, Progress

    log = OperationalLog(Path(root) / _OPS_LOG_DIRNAME / f"{job}.jsonl")
    progress = Progress(
        phase=job,
        unit=unit,
        rate_name=rate_name,
        sink=None if getattr(args, "quiet", False) else _say,
        log=log,
        interval=getattr(args, "heartbeat", None) or 60.0,
    )
    return progress, log


_OPS_LOG_DIRNAME = "logs"


def _add_observability_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--heartbeat", type=float, default=60.0,
        help="seconds between progress heartbeats (default 60); progress is "
             "transient and never affects results")
    parser.add_argument(
        "--quiet", action="store_true",
        help="suppress the human progress stream; the operational log is still "
             "written, because forensics after a failure are not optional")


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------

def _corpus_build(args: argparse.Namespace) -> int:
    from backtest.acquire.transport import RequestsTransport
    from backtest.corpus import CorpusSpec, build_corpus

    spec = CorpusSpec(
        root=Path(args.root), start=args.start, end=args.end,
        symbols=tuple(args.symbols) if args.symbols else None,
        timeframes=tuple(args.timeframes),
        include_archive=not args.listed_only,
    )
    from backtest.observe import JobRecorder

    progress, log = _observability(args, args.root, "corpus-build",
                                   unit="files")
    config = {"root": str(args.root), "start": str(args.start),
              "end": str(args.end), "timeframes": list(args.timeframes),
              "symbols": list(args.symbols) if args.symbols else None,
              "include_archive": not args.listed_only,
              "strict": not args.allow_incomplete,
              "workers": args.workers}
    try:
        with JobRecorder(log, "corpus-build", config, progress):
            report = build_corpus(
                spec, transport=RequestsTransport(), progress=_say,
                strict=not args.allow_incomplete, observer=progress,
                workers=args.workers)
    finally:
        progress.close()
    _emit(report.to_dict())
    return 0


def _corpus_verify(args: argparse.Namespace) -> int:
    from backtest.corpus import verify_corpus

    report = verify_corpus(args.root, deep=not args.shallow)
    _emit(report.to_dict())
    return 0 if report.ok else 1


# ---------------------------------------------------------------------------
# reference tier (M12) — capture only; nothing consumes it
# ---------------------------------------------------------------------------

def _reference_snapshot(args: argparse.Namespace) -> int:
    from backtest.acquire.transport import RequestsTransport
    from backtest.reference import capture_instruments

    observation = capture_instruments(args.root, transport=RequestsTransport())
    _emit(observation.to_dict())
    return 0


def _reference_funding(args: argparse.Namespace) -> int:
    from backtest.acquire.transport import RequestsTransport
    from backtest.reference import acquire_funding, build_reference

    acquired, present, missing = acquire_funding(
        args.root, transport=RequestsTransport(), symbols=args.symbols,
        start=args.start, end=args.end, progress=_say)
    report = build_reference(args.root)
    payload = dict(report.to_dict())
    payload.update({"acquired": acquired, "already_present": present,
                    "unavailable_periods": list(missing)})
    _emit(payload)
    return 0


def _reference_verify(args: argparse.Namespace) -> int:
    from backtest.reference import verify_reference

    ok, problems = verify_reference(args.root, deep=not args.shallow)
    _emit({"ok": ok, "problems": list(problems)})
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# coverage (Stage 1 evidence)
# ---------------------------------------------------------------------------

def _coverage(args: argparse.Namespace) -> int:
    from backtest.coverage import CoverageThresholds, analyze_coverage
    from backtest.data.repository import KlineRepository
    from backtest.observe import JobRecorder
    from backtest.universe import resolve

    out = Path(args.out) if args.out else Path(args.corpus)
    progress, log = _observability(args, out, "coverage", unit="symbols")
    thresholds = CoverageThresholds(
        min_rankable_symbols=args.min_rankable,
        min_union_completeness=args.min_completeness,
        max_gap_hours=args.max_gap_hours,
        max_gap_ratio=args.max_gap_ratio,
    )
    # Resolve the universe before anything is measured. `resolve` raises with the
    # offending names if any instrument cannot be classified, so the failure
    # arrives in seconds with a worklist rather than after a full reconstruction.
    repository = KlineRepository(Path(args.corpus) / "canonical")
    resolution = resolve(repository.symbols(args.timeframe),
                         corpus_root=args.corpus,
                         table_path=args.universe_table)
    config = {"corpus": str(args.corpus), "timeframe": args.timeframe,
              "candidates": list(args.candidates or ()),
              "top_k": args.top, "wide_k": args.wide,
              "universe_stride": args.stride,
              "thresholds": thresholds.to_dict(),
              "universe": resolution.to_dict()}
    with JobRecorder(log, "coverage", config, progress):
        report = analyze_coverage(
            args.corpus, resolution=resolution, timeframe=args.timeframe,
            candidate_starts=tuple(args.candidates or ()),
            end_ms=(args.end * 1000 if args.end else None),
            thresholds=thresholds, top_k=args.top, wide_k=args.wide,
            shakedown_per_dimension=args.shakedown_per_dimension,
            shakedown_controls=args.shakedown_controls,
            universe_stride=args.stride, observer=progress)
        path = report.write(out, overwrite=args.overwrite)

    payload = report.to_dict()
    # The command prints a summary, never a decision. The verdicts are per
    # candidate and the operator reads `coverage.md` to choose.
    _emit({"written_to": str(path),
           "data_snapshot_id": report.data_snapshot_id,
           "corpus_id": report.corpus_id,
           "universe_id": report.universe_id,
           "symbols": payload["inventory"]["symbols"],
           "crypto_perpetual": payload["inventory"]["crypto_perpetual"],
           "excluded_by_universe_rule":
               payload["inventory"]["excluded_by_universe_rule"],
           "classified_from_table": payload["inventory"]["from_table"],
           "candidates": {c["start"]: c["verdict"]
                          for c in payload["window_candidates"]},
           "union_top_k": (report.universe.to_dict()["union_top_k_size"]
                           if report.universe else None),
           "union_wide": (report.universe.to_dict()["union_wide_size"]
                          if report.universe else None),
           "promotion_rate": (report.universe.promotion_rate
                              if report.universe else None),
           "shakedown_candidates": len(report.shakedown)})
    return 0


# ---------------------------------------------------------------------------
# replay -> outcome -> metrics
# ---------------------------------------------------------------------------

def _replay(args: argparse.Namespace) -> int:
    from backtest.corpus import open_corpus
    from backtest.replay import ReplayConfig, ReplayDriver

    from backtest.observe import JobRecorder, preserve_failed_run
    from backtest.universe import resolve

    repository = open_corpus(args.corpus)
    # M13: resolve before the run is configured, so an unclassifiable instrument
    # stops the run in seconds with a worklist rather than after hours of cycles.
    resolution = resolve(repository.symbols("1h"), corpus_root=args.corpus,
                         table_path=args.universe_table)
    config = ReplayConfig(
        repository_root=Path(args.corpus) / "canonical",
        output_root=Path(args.out),
        start=args.start, end=args.end,
        burn_in_h1=args.burn_in,
        top_coins_limit=args.top,
        universe_mode=args.universe_mode,
        static_universe=tuple(args.static) if args.static else None,
        eligible=resolution.eligible_symbols,
        universe_id=resolution.universe_id,
        # C-8.4. Absent from `core()`, so enabling it does not change `run_id`
        # or the output directory — which is exactly why a cached-vs-uncached
        # comparison must point the two arms at different `--out` roots.
        caching_analyzer=args.caching_analyzer,
    )
    progress, log = _observability(args, args.out, "replay",
                                   unit="cycles", rate_name=" replay-h/s")

    started = False

    def report(index: int, total: int, boundary: int) -> None:
        # The driver's third argument is the scan boundary, not a row count
        # (driver.py: `self._progress(index, len(boundaries), boundary)`).
        # Rendering it as a UTC instant answers "where in history are we?",
        # which is what an operator checking in at hour 40 actually wants.
        #
        # `index` is 0-based (`enumerate(boundaries)`), so the completed count
        # is `index + 1`; using it raw would leave a 100-cycle run reporting 99%
        # at the finish line.
        #
        # Cycle index is the work unit because the boundaries are enumerated
        # before the first cycle runs, which is what makes the percentage
        # honest. One cycle is exactly one H1, so the rate is simulated hours
        # per wall second. Nothing R-denominated reaches this callback, and
        # `assert_operational` would reject it if it did.
        nonlocal started
        if not started:
            started = True
            progress.set_phase("replay/cycles", total=total)
        progress.advance(
            max(0, (index + 1) - progress.completed),
            detail=datetime.fromtimestamp(
                boundary, timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))

    driver = ReplayDriver(config, repository=repository,
                          overwrite=args.overwrite, progress=report)
    if args.validate_only:
        _emit(driver.validate().to_dict())
        return 0

    # A crashed run's partial sinks, per-cycle health file and abort record are
    # the only evidence the failure ever produced, and `overwrite` deletes them
    # at exactly the moment an operator restarts to try again. Archive first.
    if args.overwrite:
        archived = preserve_failed_run(driver.run_dir)
        if archived is not None:
            _say(f"preserved previous bundle -> {archived}")
            progress.note("preserved_previous_run", archived=str(archived))

    try:
        with JobRecorder(log, "replay", _replay_config(driver, config), progress):
            result = driver.run()
    finally:
        progress.close()
    _emit(result.to_dict())
    return 0


def _replay_config(driver, config) -> dict:
    """What a forensic reader needs to reconstruct a failed run exactly."""
    from backtest.lineage import optimizations

    return {"run_id": driver.run_id, "run_dir": str(driver.run_dir),
            "data_snapshot_id": driver.data_snapshot_id,
            "universe_id": config.universe_id,
            "universe_size": len(config.eligible or ()),
            "start": config.start, "end": config.end,
            "burn_in_h1": config.burn_in_h1,
            "top_coins_limit": config.top_coins_limit,
            "universe_mode": config.universe_mode,
            "static_universe": list(config.static_universe or ()),
            # `run_id` does not distinguish the two arms — neither flag is in
            # `core()` — so a crashed run's record must name them itself or the
            # forensic reader cannot tell which configuration produced it.
            "optimizations": optimizations(
                caching_analyzer=config.caching_analyzer)}


def _outcome(args: argparse.Namespace) -> int:
    from backtest.corpus import open_corpus
    from backtest.outcome import OutcomeConfig, OutcomeEngine, build_trade_intents

    repository = open_corpus(args.corpus)
    intents = build_trade_intents(args.run)
    engine = OutcomeEngine.for_run(
        repository, args.run,
        config=OutcomeConfig(max_holding_h1=args.max_holding))
    report = engine.write(intents.intents, args.run, overwrite=args.overwrite)
    _emit({"trades": len(report), "states": report.states,
           "lineage": engine.lineage.to_dict(),
           "counters": report.counters.to_dict()})
    return 0


def _metrics(args: argparse.Namespace) -> int:
    from backtest.dataset import LABEL_FIELDS, LabeledDataset
    from backtest.lineage import read_run_lineage
    from backtest.metrics import ReportInputs, build_report

    dataset = LabeledDataset.from_run(args.run)
    lineage = read_run_lineage(args.run)
    report = build_report(
        dataset.records, labels=dataset.labels, health=dataset.health.to_dict(),
        dataset=dataset.to_dict(),
        inputs=ReportInputs(
            run_id=(dataset.records[0].run_id if dataset.records else ""),
            window_start=lineage.window_start, window_end=lineage.window_end,
            label_fields=LABEL_FIELDS))
    out = Path(args.out) if args.out else Path(args.run)
    report.write(out, overwrite=args.overwrite)
    _emit({"metrics_hash": report.metrics_hash,
           "trades": report.global_metrics.counts.get("total_trades"),
           "join_rate": dataset.join_rate,
           "written_to": str(out)})
    return 0


def _analytics(args: argparse.Namespace) -> int:
    """Assemble Phase 1 research analytics over a finished, resolved run.

    Reads `metrics.json`, `outcomes.jsonl` and `signal_ledger.jsonl`; writes only
    into its own output directory. It never runs replay, outcome or metrics —
    §11 keeps those separate, and a command that silently spends hours of compute
    is a command nobody can predict.
    """
    from backtest.analytics import ANALYTICS_DIRNAME, AnalyticsError, analyse_run

    try:
        report = analyse_run(args.run)
    except AnalyticsError as exc:
        raise SystemExit(str(exc)) from exc
    out = Path(args.out) if args.out else Path(args.run) / ANALYTICS_DIRNAME
    written = report.write(out)
    identity = report.identity
    _emit({"run_id": identity.get("run_id"),
           "metrics_hash": identity.get("metrics_hash"),
           "strategy_version": identity.get("strategy_version"),
           "written_to": {name: str(path) for name, path in written.items()}})
    _say(f"analytics written to {out}")
    return 0


def _research(args: argparse.Namespace) -> int:
    """Phase 3 strategy studies over a finished, resolved run.

    A separate verb from `analytics` because the cost is different: the studies
    join the whole signal ledger and run several hundred bootstrap comparisons,
    where `analytics` reads `metrics.json` and finishes in seconds. Same
    reasoning §11 uses to keep replay, outcome and metrics apart.
    """
    from backtest.analytics import ANALYTICS_DIRNAME, AnalyticsError
    from backtest.analytics.core import _identity, _read_optional_json
    from backtest.analytics.studies import (
        RESEARCH_DIRNAME, run_studies, write_studies,
    )
    from backtest.metrics import METRICS_FILENAME

    run_dir = Path(args.run)
    try:
        studies = run_studies(run_dir)
    except AnalyticsError as exc:
        raise SystemExit(str(exc)) from exc

    metrics = _read_optional_json(run_dir / METRICS_FILENAME) or {}
    identity = _identity(metrics, _read_optional_json(run_dir / "manifest.json"),
                         run_dir)
    out = (Path(args.out) if args.out
           else run_dir / ANALYTICS_DIRNAME / RESEARCH_DIRNAME)
    written = write_studies(studies, out, identity=identity)
    _emit({"run_id": identity.get("run_id"),
           "metrics_hash": identity.get("metrics_hash"),
           "studies": sorted(studies),
           "written_to": str(out), "files": len(written)})
    _say(f"{len(studies)} studies written to {out}")
    return 0


def _audit(args: argparse.Namespace) -> int:
    """Verify a finished run bundle against itself and against the contract.

    Reads only; writes nothing into the run directory unless `--out` names a
    destination. Exit 1 on any error finding — warnings do not fail an audit.
    """
    from backtest.audit import audit_run

    report = audit_run(args.run)
    payload = report.to_dict()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        payload["written_to"] = str(out)
    _emit(payload)
    _say(f"{payload['result']}: {payload['errors']} error(s), "
         f"{payload['warnings']} warning(s) over {payload['checks_run']} "
         f"check group(s)")
    return 0 if report.ok else 1


def _freeze(args: argparse.Namespace) -> int:
    from backtest.dataset import LABEL_FIELDS, LabeledDataset
    from backtest.lineage import read_run_lineage
    from backtest.metrics import ReportInputs, build_report, freeze

    dataset = LabeledDataset.from_run(args.run)
    lineage = read_run_lineage(args.run)
    report = build_report(
        dataset.records, labels=dataset.labels, health=dataset.health.to_dict(),
        dataset=dataset.to_dict(),
        inputs=ReportInputs(
            run_id=(dataset.records[0].run_id if dataset.records else ""),
            window_start=lineage.window_start, window_end=lineage.window_end,
            label_fields=LABEL_FIELDS))
    baseline = freeze(report, args.out, label=args.label)
    _emit(baseline.to_dict() if hasattr(baseline, "to_dict")
          else {"baseline_id": baseline.baseline_id})
    return 0


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

def _splits(args: argparse.Namespace) -> int:
    from backtest.research import WalkForwardSpec, Window, walk_forward
    from backtest.research import holdout as holdout_module

    exclude = []
    declared = holdout_module.find(args.research) if args.research else None
    if declared is not None:
        exclude.append(declared.window)

    plan = walk_forward(
        Window(start=args.start, end=args.end),
        WalkForwardSpec(train_h1=args.train, validate_h1=args.validate,
                        test_h1=args.test, step_h1=args.step,
                        burn_in_h1=args.burn_in, embargo_h1=args.embargo,
                        anchored=args.anchored),
        exclude=exclude)
    if declared is not None:
        holdout_module.guard_splits(declared, plan)
    _emit(plan.to_dict())
    return 0


def _holdout_declare(args: argparse.Namespace) -> int:
    from backtest.corpus import load_corpus
    from backtest.research import Window
    from backtest.research import holdout as holdout_module

    snapshot = args.snapshot
    if snapshot is None:
        if not args.corpus:
            raise SystemExit(
                "--snapshot or --corpus is required: a holdout is bound to the "
                "dataset it was declared on (C-6.10)")
        snapshot = load_corpus(args.corpus)["data_snapshot_id"]

    declared = holdout_module.declare(
        args.root, Window(start=args.start, end=args.end,
                          burn_in_h1=args.burn_in),
        data_snapshot_id=snapshot, label=args.label, reason=args.reason)
    _emit(declared.to_dict())
    return 0


def _holdout_status(args: argparse.Namespace) -> int:
    from backtest.research import holdout as holdout_module

    declared = holdout_module.find(args.root)
    if declared is None:
        _emit({"declared": False,
               "note": "no holdout declared; §11 step 8 requires one before the "
                       "first experiment"})
        return 1
    uses = [use.to_dict() for use in holdout_module.holdout_uses(args.root)
            if use.holdout_id == declared.holdout_id]
    _emit({"declared": True, "holdout": declared.to_dict(),
           "uses": len(uses), "log": uses,
           "caution": holdout_module.usage_caution(args.root, declared)})
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backtest", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    # -- corpus ----------------------------------------------------------
    corpus = sub.add_parser("corpus", help="build and verify the historical corpus")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)

    build = corpus_sub.add_parser(
        "build", help="acquire and build; safe to re-run and to interrupt")
    build.add_argument("--root", required=True, type=Path)
    build.add_argument("--start", required=True, type=_date)
    build.add_argument("--end", required=True, type=_date)
    build.add_argument(
        "--symbols", nargs="*", default=None,
        help="pin the universe (declares selection bias); default is the full "
             "inventory, which is what C-6.9 requires")
    build.add_argument("--timeframes", nargs="*", default=["1h", "1d", "5m"])
    build.add_argument(
        "--listed-only", action="store_true",
        help="skip delisted symbols; declares survivorship bias in the manifest")
    build.add_argument(
        "--allow-incomplete", action="store_true",
        help="report acquisition failures instead of failing the build")
    build.add_argument(
        "--workers", type=_workers, default=1, metavar="N",
        help="parallel download workers (default 1 = sequential, the "
             "pre-parallel code path exactly). Ordering, artifacts and corpus "
             "identity are unaffected by this value")
    _add_observability_flags(build)
    build.set_defaults(func=_corpus_build)

    verify = corpus_sub.add_parser("verify", help="re-verify without rebuilding")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--shallow", action="store_true",
                        help="compare identities only; skip re-hashing the bytes")
    verify.set_defaults(func=_corpus_verify)

    # -- reference (M12) -------------------------------------------------
    reference = sub.add_parser(
        "reference", help="capture instrument metadata and funding history")
    reference_sub = reference.add_subparsers(dest="reference_command",
                                             required=True)

    snapshot = reference_sub.add_parser(
        "snapshot",
        help="capture exchangeInfo now; the only acquisition that cannot be "
             "deferred, because no historical archive of it exists")
    snapshot.add_argument("--root", required=True, type=Path)
    snapshot.set_defaults(func=_reference_snapshot)

    funding = reference_sub.add_parser(
        "funding", help="acquire and build funding-rate history")
    funding.add_argument("--root", required=True, type=Path)
    funding.add_argument("--symbols", nargs="+", required=True)
    funding.add_argument("--start", required=True, type=_date)
    funding.add_argument("--end", required=True, type=_date)
    funding.set_defaults(func=_reference_funding)

    reference_verify = reference_sub.add_parser(
        "verify", help="re-verify the reference tier")
    reference_verify.add_argument("--root", required=True, type=Path)
    reference_verify.add_argument("--shallow", action="store_true")
    reference_verify.set_defaults(func=_reference_verify)

    # -- coverage --------------------------------------------------------
    coverage = sub.add_parser(
        "coverage",
        help="Stage 1 evidence: what the corpus holds, and how each candidate "
             "replay window scores against thresholds declared in advance. "
             "Chooses nothing.")
    coverage.add_argument("--corpus", required=True, type=Path)
    coverage.add_argument("--out", type=Path, default=None)
    coverage.add_argument("--timeframe", default="1h")
    coverage.add_argument(
        "--candidates", nargs="*", type=_boundary, default=None,
        help="candidate replay start boundaries to score; supplying none scores "
             "no window, because enumerating them here would be this command "
             "proposing answers")
    coverage.add_argument("--end", type=_boundary, default=None)
    coverage.add_argument("--top", type=int, default=30)
    coverage.add_argument(
        "--wide", type=int, default=50,
        help="the comparison universe size; the report quantifies what the "
             "wider band adds and how often it feeds the top-K")
    coverage.add_argument(
        "--stride", type=int, default=1,
        help="reconstruct the universe every Nth hour; a sampled union is a "
             "lower bound and the report says so")
    coverage.add_argument("--min-rankable", type=int, default=30,
                          dest="min_rankable")
    coverage.add_argument("--min-completeness", type=float, default=0.995,
                          dest="min_completeness")
    coverage.add_argument("--max-gap-hours", type=int, default=24,
                          dest="max_gap_hours")
    coverage.add_argument("--max-gap-ratio", type=float, default=0.01,
                          dest="max_gap_ratio")
    coverage.add_argument("--shakedown-per-dimension", type=int, default=3,
                          dest="shakedown_per_dimension")
    coverage.add_argument("--shakedown-controls", type=int, default=3,
                          dest="shakedown_controls")
    coverage.add_argument(
        "--universe-table", type=Path, default=None, dest="universe_table",
        help="classification table supplying the exchangeInfo metadata of "
             "instruments delisted before snapshot capture began; defaults to "
             "reference/universe_table.json")
    coverage.add_argument("--overwrite", action="store_true")
    _add_observability_flags(coverage)
    coverage.set_defaults(func=_coverage)

    # -- replay ----------------------------------------------------------
    replay = sub.add_parser("replay", help="replay a window over the corpus")
    replay.add_argument("--corpus", required=True, type=Path)
    replay.add_argument("--out", required=True, type=Path)
    replay.add_argument("--start", required=True, type=_boundary)
    replay.add_argument("--end", required=True, type=_boundary)
    replay.add_argument("--burn-in", type=int, default=336, dest="burn_in")
    replay.add_argument("--top", type=int, default=30)
    replay.add_argument("--universe-mode", default="volume",
                        choices=("volume", "static"), dest="universe_mode")
    replay.add_argument("--static", nargs="*", default=None)
    replay.add_argument("--overwrite", action="store_true")
    replay.add_argument(
        "--universe-table", type=Path, default=None, dest="universe_table",
        help="classification table supplying the exchangeInfo metadata of "
             "instruments delisted before snapshot capture began; defaults to "
             "reference/universe_table.json")
    replay.add_argument(
        "--caching", action="store_true", dest="caching_analyzer",
        help="memoize the analysis pipeline (C-8.4), removing the duplicate "
             "evaluation in the orchestrator. Off by default: the flag changes "
             "wall time only, and a run must opt into it explicitly so the "
             "manifest's provenance block records a deliberate choice")
    replay.add_argument("--validate-only", action="store_true",
                        dest="validate_only")
    _add_observability_flags(replay)
    replay.set_defaults(func=_replay)

    # -- outcome ---------------------------------------------------------
    outcome = sub.add_parser("outcome", help="resolve trades for a finished run")
    outcome.add_argument("--corpus", required=True, type=Path)
    outcome.add_argument("--run", required=True, type=Path)
    outcome.add_argument("--max-holding", type=int, default=168,
                         dest="max_holding")
    outcome.add_argument("--overwrite", action="store_true")
    outcome.set_defaults(func=_outcome)

    # -- metrics ---------------------------------------------------------
    metrics = sub.add_parser("metrics", help="aggregate a run into a report")
    metrics.add_argument("--run", required=True, type=Path)
    metrics.add_argument("--out", type=Path, default=None)
    metrics.add_argument(
        "--overwrite", action="store_true",
        help="replace an existing report; a re-derivation replaces its "
             "predecessor deliberately, never silently")
    metrics.set_defaults(func=_metrics)

    # -- analytics ---------------------------------------------------------
    analytics = sub.add_parser(
        "analytics", help="Phase 1 research analytics over a finished run")
    analytics.add_argument("--run", required=True, type=Path,
                           help="a run directory: <out>/runs/<run_id>")
    analytics.add_argument(
        "--out", type=Path, default=None,
        help="output directory; defaults to <run>/analytics/ so the audited "
             "bundle's own files are never touched")
    analytics.set_defaults(func=_analytics)

    # -- research ----------------------------------------------------------
    research = sub.add_parser(
        "research", help="Phase 3 strategy studies over a finished run")
    research.add_argument("--run", required=True, type=Path,
                          help="a run directory: <out>/runs/<run_id>")
    research.add_argument(
        "--out", type=Path, default=None,
        help="output directory; defaults to <run>/analytics/research/")
    research.set_defaults(func=_research)

    # -- audit -----------------------------------------------------------
    audit = sub.add_parser(
        "audit", help="verify a finished run bundle's integrity")
    audit.add_argument("--run", required=True, type=Path,
                       help="a run directory: <out>/runs/<run_id>")
    audit.add_argument("--out", type=Path, default=None,
                       help="write the report here as JSON; without it the "
                            "report goes to stdout only and the run directory "
                            "is not touched")
    audit.set_defaults(func=_audit)

    frozen = sub.add_parser("freeze", help="freeze a run's report as a baseline")
    frozen.add_argument("--run", required=True, type=Path)
    frozen.add_argument("--out", required=True, type=Path)
    frozen.add_argument("--label", required=True)
    frozen.set_defaults(func=_freeze)

    # -- research --------------------------------------------------------
    splits = sub.add_parser("splits", help="plan walk-forward windows")
    splits.add_argument("--start", required=True, type=_boundary)
    splits.add_argument("--end", required=True, type=_boundary)
    splits.add_argument("--train", required=True, type=int)
    splits.add_argument("--test", required=True, type=int)
    splits.add_argument("--validate", type=int, default=0)
    splits.add_argument("--step", type=int, default=None)
    splits.add_argument("--burn-in", type=int, default=336, dest="burn_in")
    splits.add_argument("--embargo", type=int, default=168)
    splits.add_argument("--anchored", action="store_true")
    splits.add_argument("--research", type=Path, default=None,
                        help="research root; its declared holdout is excluded "
                             "and then re-checked")
    splits.set_defaults(func=_splits)

    holdout = sub.add_parser("holdout", help="the permanent holdout")
    holdout_sub = holdout.add_subparsers(dest="holdout_command", required=True)

    declare = holdout_sub.add_parser(
        "declare", help="declare it once, before the first experiment")
    declare.add_argument("--root", required=True, type=Path)
    declare.add_argument("--start", required=True, type=_boundary)
    declare.add_argument("--end", required=True, type=_boundary)
    declare.add_argument("--burn-in", type=int, default=336, dest="burn_in")
    declare.add_argument("--label", required=True)
    declare.add_argument("--reason", default="")
    declare.add_argument("--snapshot", default=None)
    declare.add_argument("--corpus", type=Path, default=None)
    declare.set_defaults(func=_holdout_declare)

    status = holdout_sub.add_parser(
        "status", help="what is declared, and how often it has been looked at")
    status.add_argument("--root", required=True, type=Path)
    status.set_defaults(func=_holdout_status)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":                      # pragma: no cover
    raise SystemExit(main())
