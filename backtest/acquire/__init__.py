"""backtest.acquire — the Raw Historical Acquisition Layer (Milestone M4.1).

Acquires Binance USDⓈ-M perpetual history and stores **immutable raw artifacts**.
That is its entire responsibility. It does not build datasets, does not know what a
window is, and never imports `backtest.data`: the raw tier and the canonical tier
are separate by construction (REPLAY_CONTRACT.md C-6.10), and bridging them is
Milestone M4.2's job.

Five collaborators, one responsibility each:

- `transport`  — the single boundary to the network (injectable, so tests are hermetic)
- `discovery`  — which symbols exist, including delisted ones
- `planner`    — what must be acquired, as a pure function of window + storage
- `downloader` — attempt order, retry policy, payload verification
- `storage`    — write-once artifacts, sidecars, integrity, the raw manifest
"""
from backtest.acquire.discovery import (
    DELISTED,
    DiscoveryError,
    DiscoveryResult,
    LISTED,
    SymbolInfo,
    discover_symbols,
)
from backtest.acquire.downloader import (
    DOWNLOADER_VERSION,
    DownloadReport,
    Downloader,
    ItemOutcome,
)
from backtest.acquire.planner import (
    Candidate,
    DownloadPlan,
    PlanError,
    PlanItem,
    TIMEFRAMES,
    plan_downloads,
)
from backtest.acquire.storage import (
    AcquisitionError,
    ArchiveCorrupt,
    ArchiveIncomplete,
    ArtifactNameMismatch,
    ArtifactRecord,
    ChecksumMismatch,
    CoverageGap,
    DuplicateArtifact,
    IntegrityError,
    MANIFEST_NAME,
    RAW_SCHEMA_VERSION,
    RawStore,
    StorageTransient,
    TruncatedArtifact,
    expected_member_for,
    verify_archive,
)
from backtest.acquire.transport import (
    HttpTransport,
    RequestsTransport,
    TransportBanned,
    TransportError,
    TransportNotFound,
    TransportRateLimited,
)

__all__ = [
    "AcquisitionError",
    "ArchiveCorrupt",
    "ArchiveIncomplete",
    "ArtifactNameMismatch",
    "ArtifactRecord",
    "Candidate",
    "ChecksumMismatch",
    "CoverageGap",
    "DELISTED",
    "DOWNLOADER_VERSION",
    "DiscoveryError",
    "DiscoveryResult",
    "DownloadPlan",
    "DownloadReport",
    "Downloader",
    "DuplicateArtifact",
    "HttpTransport",
    "IntegrityError",
    "ItemOutcome",
    "LISTED",
    "MANIFEST_NAME",
    "PlanError",
    "PlanItem",
    "RAW_SCHEMA_VERSION",
    "RawStore",
    "RequestsTransport",
    "StorageTransient",
    "SymbolInfo",
    "TIMEFRAMES",
    "TransportBanned",
    "TransportError",
    "TransportNotFound",
    "TransportRateLimited",
    "TruncatedArtifact",
    "discover_symbols",
    "expected_member_for",
    "plan_downloads",
    "verify_archive",
]
