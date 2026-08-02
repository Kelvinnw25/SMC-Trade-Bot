"""The Historical Corpus — one resumable operation over the two data tiers.

REPLAY_CONTRACT.md §6 (C-6.1, C-6.9, C-6.10, C-6.11), Milestone M11.

## What was already here, and what this adds

The corpus *machinery* predates this module and is not re-implemented:

| Concern | Owner (unchanged) |
|---|---|
| which symbols exist, delisted included | `acquire.discovery` |
| what must be fetched, given what is stored | `acquire.planner` |
| attempt order, retries, payload verification | `acquire.downloader` |
| immutable bytes, sidecars, SHA-256, duplicates | `acquire.storage` |
| bytes → rows, invariants, gaps, `data_snapshot_id` | `canonical` |
| serving exact stored rows to replay | `data.repository` |

What did **not** exist was a way to run them as one thing. Building a corpus meant
calling five collaborators in the right order with consistent arguments, and
getting that sequence right twice — once for the first build and once for every
incremental update — was left to whoever typed the commands. A research corpus
whose construction is a remembered procedure is not reproducible, whatever its
components guarantee individually.

This module is that procedure, written down:

    discover -> plan -> download -> build canonical -> verify -> manifest

## Resumability is structural, not a feature

Nothing here tracks progress, because nothing needs to. The raw tier is write-once
with the sidecar as commit marker, so a committed artifact is either whole or
absent; `planner.plan_downloads` asks the store what it already holds and plans
only the rest. An interrupted build is therefore resumed by **running the identical
command again** — there is no resume flag, no checkpoint file, and no state that
can disagree with the disk. The same property makes an incremental update (extend
`end`, re-run) and a retry after a network failure the same operation.

The canonical tier is rebuilt in full each time. It is a pure function of the raw
bytes, so rebuilding is cheap insurance rather than waste, and a partial build
writes no manifest — the repository reads only what a manifest lists, so a crashed
build is not a corrupt corpus.

## Identity

`corpus_id` hashes the spec, the canonical `data_snapshot_id`, and every raw
artifact's `(path, sha256)`. It deliberately excludes `downloaded_at`: two people
downloading the same history on different days must reach the same corpus, and a
timestamp in the identity would make that impossible. Acquisition time remains
recorded per artifact in the raw sidecars, where it is evidence rather than
identity.

## What this module does not do

No parsing, no repair, no window logic, no ranking, and no strategy. It never
writes into the canonical tier itself and never touches a run directory.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Sequence

from backtest.acquire.discovery import DiscoveryResult, SymbolInfo, discover_symbols
from backtest.acquire.downloader import (
    STATUS_ABSENT, STATUS_EMPTY, STATUS_FAILED, DownloadReport, Downloader,
    ItemOutcome,
)
from backtest.acquire.planner import DownloadPlan, TIMEFRAMES, plan_downloads
from backtest.acquire.ratelimit import default_rate_limiter
from backtest.acquire.storage import AcquisitionError, RawStore
from backtest.acquire.transport import HttpTransport
from backtest.canonical.builder import BUILDER_VERSION, BuildReport, CanonicalBuilder
from backtest.data.repository import (
    KlineRepository, canonical_json, compute_snapshot_id,
)

if TYPE_CHECKING:                                           # pragma: no cover
    # Observation only. Imported for the annotation alone so that acquisition
    # never depends on the reporting layer at runtime: a corpus must be
    # buildable by a caller that has no interest in progress.
    from backtest.observe import Progress

__all__ = [
    "CORPUS_MANIFEST_NAME",
    "CORPUS_SCHEMA_VERSION",
    "CorpusError",
    "CorpusIncomplete",
    "CorpusReport",
    "CorpusSpec",
    "build_corpus",
    "load_corpus",
    "open_corpus",
    "verify_corpus",
]

#: Bumped when the corpus manifest's own field set changes incompatibly.
CORPUS_SCHEMA_VERSION = 1

#: The corpus manifest. Named `corpus.json` rather than `manifest.json` because a
#: corpus root also contains the raw manifest and the canonical manifest, and three
#: files with one name in three directories is how the wrong one gets read.
CORPUS_MANIFEST_NAME = "corpus.json"

#: Where each tier lives under the corpus root. C-6.10 requires them separate; this
#: fixes *where*, so a corpus is identified by one path rather than by two.
RAW_DIRNAME = "raw"
CANONICAL_DIRNAME = "canonical"


class CorpusError(Exception):
    """The corpus cannot be built, read, or trusted."""


class CorpusIncomplete(CorpusError):
    """Acquisition did not cover what the spec asked for."""


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorpusSpec:
    """What a corpus is meant to contain. The input side of `corpus_id`.

    `symbols = None` means **the full inventory** — every USDT-M perpetual
    discovery finds, delisted ones included. That is what C-6.9 requires of a
    corpus intended to reconstruct the universe: restricting acquisition to the
    symbols that rank *today* bakes survivorship and look-ahead into every result
    permanently, and no later analysis can remove it.

    A pinned `symbols` tuple is legitimate for a controlled experiment, and is
    recorded as `universe_scope: "pinned"` in the manifest so that every run over
    it inherits a visible declaration rather than a silent bias. Replay over such
    a corpus MUST use `universe_mode="static"` (C-6.9).
    """

    root: Path
    start: date
    end: date
    symbols: Optional[tuple[str, ...]] = None
    timeframes: tuple[str, ...] = TIMEFRAMES
    include_archive: bool = True
    monthly_lag_days: int = 5

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise CorpusError(
                f"end {self.end.isoformat()} precedes start "
                f"{self.start.isoformat()}")
        unknown = [tf for tf in self.timeframes if tf not in TIMEFRAMES]
        if unknown:
            raise CorpusError(
                f"unknown timeframe(s) {unknown}; expected {TIMEFRAMES}")
        if not self.timeframes:
            raise CorpusError("a corpus with no timeframe cannot serve replay")
        if self.symbols is not None and not self.symbols:
            raise CorpusError(
                "symbols=() would acquire nothing; pass None for the full "
                "inventory (C-6.9) or name the symbols to pin")

    @property
    def raw_root(self) -> Path:
        return Path(self.root) / RAW_DIRNAME

    @property
    def canonical_root(self) -> Path:
        return Path(self.root) / CANONICAL_DIRNAME

    @property
    def universe_scope(self) -> str:
        """`"inventory"` (C-6.9 conforming) or `"pinned"` (declared bias)."""
        return "inventory" if self.symbols is None else "pinned"

    def to_dict(self) -> dict[str, Any]:
        """The identity view. Deliberately excludes `root`: where a corpus sits on
        disk is not part of what it contains, and two copies of the same history
        must not have different identities."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": list(self.symbols) if self.symbols else None,
            "timeframes": list(self.timeframes),
            "include_archive": self.include_archive,
            "monthly_lag_days": self.monthly_lag_days,
            "universe_scope": self.universe_scope,
        }

    @property
    def spec_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_dict()).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorpusReport:
    """What one `build_corpus` call did. Counts and identity, never rows."""

    spec: CorpusSpec
    corpus_id: str
    data_snapshot_id: str
    inventory: tuple[str, ...]
    plan: DownloadPlan
    download: Optional[DownloadReport]
    build: BuildReport
    artifact_count: int
    manifest_path: Path

    @property
    def acquired(self) -> int:
        return 0 if self.download is None else self.download.downloaded

    @property
    def already_present(self) -> int:
        """Artifacts the plan skipped because the store already held them —
        the measure of how much a resumed run did not repeat."""
        return self.plan.already_covered

    @property
    def absent(self) -> int:
        """Periods with no published archive. Evidence, not failure (C-6.9:
        a survivorship-free inventory spans symbols that did not all exist for
        the whole window, so most planned periods legitimately have no data)."""
        return 0 if self.download is None else self.download.absent

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "data_snapshot_id": self.data_snapshot_id,
            "spec": self.spec.to_dict(),
            "inventory_size": len(self.inventory),
            "planned": len(self.plan),
            "already_present": self.already_present,
            "acquired": self.acquired,
            "artifact_count": self.artifact_count,
            "datasets": len(self.build.datasets),
            "rows": self.build.total_rows,
            "symbols_built": len(self.build.symbols),
            # The acquisition breakdown an operator needs without opening a log.
            "acquisition": (None if self.download is None
                            else self.download.summary()),
        }


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _item_reporter(
    observer: Optional["Progress"],
) -> Optional[Callable[[ItemOutcome, int, int], None]]:
    """Adapt one acquired item into a `Progress.advance`.

    A 404 is `empty`, not a failure — it is Binance correctly reporting that a
    symbol was not listed in that period, and it is committed as evidence. It is
    counted separately from a genuine failure so the operator can tell "this
    symbol did not exist yet" from "we could not get this".
    """
    if observer is None:
        return None

    def report(outcome: ItemOutcome, index: int, total: int) -> None:
        observer.advance(
            1,
            detail=f"{outcome.symbol} {outcome.timeframe} {outcome.period}",
            retries=int(outcome.retries),
            # `absent` and `empty` are both "nothing was acquired, and that is
            # the correct answer"; the heartbeat groups them because the
            # operational question is only "is anything wrong?". The precise
            # breakdown is in the final report, which is what a decision reads.
            skipped=1 if outcome.status in (STATUS_EMPTY, STATUS_ABSENT) else 0,
            failed=1 if outcome.status == STATUS_FAILED else 0,
        )
        if outcome.status == STATUS_FAILED:
            observer.note("acquisition_failed", symbol=outcome.symbol,
                          timeframe=outcome.timeframe, period=outcome.period,
                          detail=outcome.detail, attempts=outcome.attempts)

    return report


def build_corpus(
    spec: CorpusSpec,
    *,
    transport: HttpTransport,
    inventory: Optional[Sequence[SymbolInfo]] = None,
    verify_integrity: bool = True,
    strict: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    observer: Optional["Progress"] = None,
    workers: int = 1,
) -> CorpusReport:
    """Acquire, build and verify a corpus. Safe to re-run; safe to interrupt.

    Re-running is the resume path *and* the incremental-update path: the planner
    consults the store, so only what is missing is fetched. Nothing already
    committed is re-downloaded, re-hashed on the wire, or rewritten.

    `strict` fails the build when any planned artifact could not be acquired.
    That is the right default for a research corpus — a silently short dataset
    produces results nobody can interpret later — but it is separable, because a
    long acquisition that hit one dead symbol should be inspectable rather than
    lost. Binance simply having no data for a period (a 404 before listing) is
    **not** a failure and never reaches this flag; the downloader classifies it as
    `empty` (see `DownloadReport.ok`).

    `inventory` injects the symbol list, skipping discovery. It exists so tests
    and offline rebuilds are hermetic, not so callers can narrow the universe —
    narrowing is `spec.symbols`, which is declared in the manifest.

    `observer` is an optional `backtest.observe.Progress`. Acquisition is the
    phase that runs for hours with nothing to say — the planner enumerates every
    file before the first byte moves, which is exactly what makes a percentage
    honest here — so it is driven per item. It observes only: no branch in this
    function reads it, and a build with and without one produces byte-identical
    output.
    """
    say = progress or (lambda _message: None)

    def phase(name: str, total: Optional[int] = None) -> None:
        if observer is not None:
            observer.set_phase(name, total=total)
    root = Path(spec.root)
    root.mkdir(parents=True, exist_ok=True)
    store = RawStore(spec.raw_root)

    # -- 1. inventory --------------------------------------------------------
    phase("corpus/discover")
    if inventory is None:
        say("discovering symbols")
        discovery: DiscoveryResult = discover_symbols(
            transport, include_archive=spec.include_archive)
        symbols = list(discovery.symbols)
        # M12: commit the payloads the discovery was derived from. `exchangeInfo`
        # is the only source of instrument metadata — status, onboard date, tick
        # size, delisting — and Binance serves the *current* state only. Fetching
        # it and discarding it, which is what happened before this line existed,
        # destroyed a day of irrecoverable history on every corpus build.
        # `DiscoveryResult` carries the raw bytes for exactly this purpose.
        Downloader(transport, store).commit_discovery(discovery)
    else:
        symbols = list(inventory)
    if spec.symbols is not None:
        wanted = set(spec.symbols)
        found = {info.symbol for info in symbols}
        unknown = sorted(wanted - found)
        if unknown:
            raise CorpusError(
                f"pinned symbol(s) {unknown} are not in the inventory; a corpus "
                f"MUST NOT invent a symbol the exchange never listed")
        symbols = [info for info in symbols if info.symbol in wanted]
    inventory_names = tuple(sorted(info.symbol for info in symbols))
    say(f"inventory: {len(inventory_names)} symbol(s)")

    # -- 2. plan -------------------------------------------------------------
    phase("corpus/plan")
    plan = plan_downloads(
        store, symbols, timeframes=spec.timeframes,
        start=spec.start, end=spec.end,
        monthly_lag_days=spec.monthly_lag_days,
        # Only this caller knows whether the bulk-archive listing was read, and
        # therefore whether `in_archive=False` means "the archive has nothing"
        # or "nobody looked". The planner will not exclude a never-traded
        # instrument on unread evidence.
        archive_evidence=spec.include_archive)
    say(f"plan: {len(plan)} to acquire, {plan.already_covered} already present")

    # -- 3. acquire ----------------------------------------------------------
    download: Optional[DownloadReport] = None
    if len(plan):
        phase("corpus/download", len(plan))
        if observer is not None:
            # `already_covered` is reported before the first fetch because on a
            # resumed acquisition it is the difference between "nothing is
            # happening" and "40,000 committed files were correctly skipped".
            observer.note("resume", already_covered=plan.already_covered,
                          planned=len(plan))
        # Pacing is installed here, at the production entry point, rather
        # than defaulted inside `Downloader`: `fapi.binance.com` allows
        # 4 req/s and even one worker at ~214 ms exceeds 3, so the bound
        # must hold at every worker count. It delays requests; it cannot
        # change what is fetched or how it is classified.
        downloader = Downloader(
            transport, store, workers=workers,
            rate_limiter=default_rate_limiter())
        download = downloader.run(plan, progress=_item_reporter(observer))
        say(f"acquired {download.downloaded}, absent {download.absent}, "
            f"empty {download.empty}, retries {download.retries}, "
            f"failed {len(download.failures)}")
        if strict and not download.ok:
            download.raise_for_failures()

    # -- 4. canonical tier ---------------------------------------------------
    phase("corpus/canonical")
    say("building canonical datasets")
    builder = CanonicalBuilder(store, verify_integrity=verify_integrity)
    build = builder.build(
        spec.canonical_root,
        symbols=spec.symbols,
        timeframes=spec.timeframes,
        inventory=inventory_names,
    )

    # -- 5. identity and manifest -------------------------------------------
    phase("corpus/manifest")
    store.write_manifest()
    records = store.records()
    repository = KlineRepository(spec.canonical_root)
    snapshot_id = repository.data_snapshot_id
    corpus_id = _corpus_id(spec, snapshot_id, records)
    manifest_path = _write_manifest(
        spec, corpus_id=corpus_id, snapshot_id=snapshot_id,
        inventory=inventory_names, build=build, records=records)
    say(f"corpus {corpus_id} · snapshot {snapshot_id} · "
        f"{build.total_rows} rows in {len(build.datasets)} dataset(s)")

    return CorpusReport(
        spec=spec, corpus_id=corpus_id, data_snapshot_id=snapshot_id,
        inventory=inventory_names, plan=plan, download=download, build=build,
        artifact_count=len(records), manifest_path=manifest_path,
    )


#: Artifact kinds that are **provenance, not content**. A corpus is its klines;
#: discovery payloads record *how the symbol list was learned*, and their paths
#: carry the discovery instant because two discoveries days apart legitimately
#: differ (`Downloader.commit_discovery`). Admitting them to the identity would
#: make two independent builds of identical history produce different `corpus_id`s
#: — measured and confirmed in M12 before this exclusion existed.
_PROVENANCE_KINDS = frozenset({"discovery_json"})


def _corpus_id(spec: CorpusSpec, snapshot_id: str, records: Sequence) -> str:
    """`sha256(spec + snapshot + every content artifact digest)[:16]`.

    Excludes `downloaded_at` and every other acquisition timestamp: two people
    downloading the same history a month apart must arrive at the same corpus, or
    the identity is measuring the download rather than the data. For the same
    reason it excludes `_PROVENANCE_KINDS`, whose very purpose is to differ
    between two acquisitions of the same history.
    """
    payload = {
        "corpus_schema_version": CORPUS_SCHEMA_VERSION,
        "spec": spec.to_dict(),
        "data_snapshot_id": snapshot_id,
        "artifacts": sorted(
            [record.artifact_path, record.sha256] for record in records
            if record.kind not in _PROVENANCE_KINDS),
    }
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def _write_manifest(
    spec: CorpusSpec, *, corpus_id: str, snapshot_id: str,
    inventory: Sequence[str], build: BuildReport, records: Sequence,
) -> Path:
    """The corpus manifest: identity, scope, integrity and declared bias.

    No build timestamp, by the same rule the canonical builder follows — a
    `built_at` field would make every rebuild differ and destroy the one property
    that makes an identity worth having.
    """
    timeframes = sorted({record.timeframe for record in records
                         if record.timeframe})
    payload = {
        "corpus_schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "data_snapshot_id": snapshot_id,
        "builder_version": BUILDER_VERSION,
        "spec": spec.to_dict(),
        "spec_hash": spec.spec_hash,
        "inventory": list(inventory),
        "inventory_size": len(inventory),
        "artifact_count": len(records),
        "acquired_timeframes": timeframes,
        "datasets": len(build.datasets),
        "rows": build.total_rows,
        "symbols_built": list(build.symbols),
        # C-6.9 requires the manifest to declare an incomplete inventory. The
        # builder decides it — it is the component that knows which symbols the
        # inventory named and which produced no dataset — and it is copied here
        # so a reader of the corpus manifest alone still sees the declaration.
        "survivorship_incomplete": build.survivorship_incomplete,
        "missing_symbols": list(build.missing_symbols),
        "declared_biases": _declared_biases(spec, build),
    }
    # `corpus_id` excludes `_PROVENANCE_KINDS`, so it does not determine which
    # candidate set replay will rank. `universe_id` is the narrower identity that
    # does, and it is recorded here so a corpus carries it alongside the two
    # identities it already had. It is `None` when the corpus cannot be resolved
    # yet — never a guess.
    payload["universe_id"] = _resolved_universe_id(spec, build)
    path = Path(spec.root) / CORPUS_MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def _resolved_universe_id(spec: CorpusSpec, build: BuildReport) -> Optional[str]:
    """`universe_id` for the symbols this corpus actually built.

    Returns `None` — never a fabricated value — when the corpus holds an
    instrument the resolver cannot classify. Writing a manifest is not the place
    to fail an acquisition that otherwise succeeded, and a *missing* identity is
    honest, whereas a partial one would silently certify a universe that was
    never determined. Coverage and replay resolve the same symbols and *do*
    fail, which is where the operator is handed the worklist.
    """
    from backtest.universe import UniverseError, resolve
    try:
        return resolve(build.symbols, corpus_root=spec.root).universe_id
    except UniverseError:
        return None


def _declared_biases(spec: CorpusSpec, build: BuildReport) -> list[str]:
    """What a run over this corpus inherits before it computes anything.

    Written into the corpus rather than left to the analyst, so a bias travels
    with the data instead of with the person who remembers it.
    """
    biases: list[str] = []
    if spec.symbols is not None:
        biases.append(
            "universe_scope=pinned: acquisition was restricted to "
            f"{len(spec.symbols)} named symbol(s), so the universe cannot be "
            "reconstructed and results carry selection bias. Replay MUST use "
            "universe_mode='static' (C-6.9).")
    if not spec.include_archive:
        biases.append(
            "include_archive=false: delisted symbols were never acquired, so "
            "the corpus carries survivorship bias by construction (C-6.9).")
    if "1h" not in spec.timeframes:
        biases.append(
            "no 1h data: neither the universe ranking nor a signal scan can be "
            "reconstructed from this corpus (C-6.9).")
    if "5m" not in spec.timeframes:
        biases.append(
            "no 5m data: outcome resolution falls back to H1 worst-case on every "
            "candle and every record is excursion_degraded (C-4.3).")
    if "1d" not in spec.timeframes:
        biases.append("no 1d data: the HTF frame is unavailable (C-6.2).")
    if build.survivorship_incomplete:
        biases.append(
            f"survivorship_incomplete: {len(build.missing_symbols)} inventory "
            f"symbol(s) produced no dataset, so the reconstructed universe omits "
            f"symbols that existed at the time (C-6.9).")
    return biases


# ---------------------------------------------------------------------------
# Reading and verifying
# ---------------------------------------------------------------------------

def load_corpus(root: Path | str) -> dict[str, Any]:
    """The corpus manifest as written."""
    path = Path(root) / CORPUS_MANIFEST_NAME
    if not path.exists():
        raise CorpusError(
            f"no {CORPUS_MANIFEST_NAME} at {root}. A directory of klines without "
            f"a manifest is not a corpus: nothing states what it was meant to "
            f"contain, so nothing can say whether it is complete.")
    return json.loads(path.read_text(encoding="utf-8"))


def open_corpus(root: Path | str, *,
                expect_snapshot: Optional[str] = None) -> KlineRepository:
    """The corpus as replay consumes it (C-6.8) — the permanent source.

    Opening through here rather than by pointing `KlineRepository` at a directory
    means the repository's `data_snapshot_id` is checked against the manifest that
    declares it. A canonical tier rebuilt from different raw bytes, or a directory
    swapped underneath a config, is caught at open time rather than by a puzzling
    result three stages downstream.
    """
    manifest = load_corpus(root)
    repository = KlineRepository(Path(root) / CANONICAL_DIRNAME)
    declared = manifest.get("data_snapshot_id")
    if declared and repository.data_snapshot_id != declared:
        raise CorpusError(
            f"{root}: the canonical tier is snapshot "
            f"{repository.data_snapshot_id} but {CORPUS_MANIFEST_NAME} declares "
            f"{declared}. Rebuild the corpus rather than replaying data whose "
            f"identity is unknown (C-6.10).")
    if expect_snapshot and repository.data_snapshot_id != expect_snapshot:
        raise CorpusError(
            f"{root}: expected snapshot {expect_snapshot}, found "
            f"{repository.data_snapshot_id}. Two runs with different "
            f"data_snapshot_id are never comparable (C-6.10).")
    return repository


@dataclass(frozen=True)
class VerifyReport:
    """The outcome of re-checking a corpus against its own manifest."""

    corpus_id: str
    data_snapshot_id: str
    artifacts_verified: int
    datasets: int
    rows: int
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "data_snapshot_id": self.data_snapshot_id,
            "artifacts_verified": self.artifacts_verified,
            "datasets": self.datasets,
            "rows": self.rows,
            "ok": self.ok,
            "problems": list(self.problems),
        }


def verify_corpus(root: Path | str, *, deep: bool = True) -> VerifyReport:
    """Re-verify a corpus without rebuilding it.

    `deep` re-reads and re-hashes every raw artifact — the check that catches bit
    rot, a truncated copy, or an edited file. It costs one full pass over the raw
    tier. With `deep=False` only the identities are compared, which is fast and
    still catches a canonical tier that no longer matches its manifest.

    Problems are collected and returned rather than raised one at a time: an
    operator fixing a corpus wants the whole list, not the first entry.
    """
    root = Path(root)
    manifest = load_corpus(root)
    problems: list[str] = []
    store = RawStore(root / RAW_DIRNAME)

    verified = 0
    records = store.records()
    if deep:
        try:
            records = store.verify_all()
        except AcquisitionError as exc:
            problems.append(f"raw tier: {exc}")
        verified = len(records)

    declared_count = manifest.get("artifact_count")
    if declared_count is not None and len(records) != declared_count:
        problems.append(
            f"artifact count {len(records)} != manifest {declared_count}")

    datasets = rows = 0
    snapshot = ""
    try:
        repository = KlineRepository(root / CANONICAL_DIRNAME)
        snapshot = repository.data_snapshot_id
        if snapshot != manifest.get("data_snapshot_id"):
            problems.append(
                f"canonical snapshot {snapshot} != manifest "
                f"{manifest.get('data_snapshot_id')}")
        datasets = int(manifest.get("datasets", 0))
        rows = int(manifest.get("rows", 0))
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        problems.append(f"canonical tier: {exc}")

    if not problems and records:
        recomputed = _corpus_id(_spec_from_manifest(root, manifest),
                                snapshot, records)
        if recomputed != manifest.get("corpus_id"):
            problems.append(
                f"corpus_id {recomputed} != manifest {manifest.get('corpus_id')}")

    return VerifyReport(
        corpus_id=str(manifest.get("corpus_id", "")),
        data_snapshot_id=snapshot,
        artifacts_verified=verified,
        datasets=datasets,
        rows=rows,
        problems=tuple(problems),
    )


def _spec_from_manifest(root: Path, manifest: dict[str, Any]) -> CorpusSpec:
    spec = manifest["spec"]
    symbols = spec.get("symbols")
    return CorpusSpec(
        root=root,
        start=date.fromisoformat(spec["start"]),
        end=date.fromisoformat(spec["end"]),
        symbols=tuple(symbols) if symbols else None,
        timeframes=tuple(spec["timeframes"]),
        include_archive=bool(spec["include_archive"]),
        monthly_lag_days=int(spec["monthly_lag_days"]),
    )
