"""Symbol discovery — which USDⓈ-M perpetuals exist, and which ever existed.

Two sources, because one is not enough:

1. **`exchangeInfo`** lists what is *currently* listed, with authoritative
   metadata (`contractType`, `quoteAsset`, `status`, `onboardDate`).
2. **The bulk-archive listing** enumerates every symbol directory that has ever
   held data — including symbols that have since been delisted and therefore no
   longer appear in `exchangeInfo` at all.

Using only (1) would silently bake survivorship bias into every dataset built
downstream (finding A58; REPLAY_CONTRACT.md C-6.9 requires the candidate set to be
"all perpetuals in the inventory, **not** the symbols that happen to rank today").
So a symbol present only in the archive is discovered and marked `DELISTED`
rather than dropped.

Filtering mirrors the live adapter exactly (`v2/infra/market_data.py`, C-6.9): the
name ends with `USDT` and contains no `_`. The `_` rule is what removes dated
delivery contracts (`BTCUSDT_240628`), which share the `um` archive with
perpetuals; the `USDT` suffix removes BUSD- and coin-margined pairs.

This module knows nothing about strategy, ranking, signals, or replay. It answers
one question — *which symbols are there* — and records the evidence it answered
from.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Optional
from xml.etree import ElementTree

from backtest.acquire.storage import AcquisitionError
from backtest.acquire.transport import HttpTransport, TransportError

__all__ = [
    "ARCHIVE_LISTING_URL",
    "DiscoveryError",
    "DiscoveryResult",
    "EXCHANGE_INFO_URL",
    "SymbolInfo",
    "discover_symbols",
    "parse_archive_listing",
    "parse_exchange_info",
]

EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

#: The S3 bucket behind data.binance.vision, listed with a delimiter so each
#: `CommonPrefix` is one symbol directory.
ARCHIVE_LISTING_URL = (
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    "?delimiter=/&prefix=data/futures/um/daily/klines/"
)

_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

#: A perpetual USDT pair: ends with USDT, no underscore (C-6.9).
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")

LISTED = "TRADING"
DELISTED = "DELISTED"


class DiscoveryError(AcquisitionError):
    """A discovery source could not be read or understood."""


@dataclass(frozen=True)
class SymbolInfo:
    """One discovered symbol and where the knowledge came from.

    `listing_state` is `TRADING` when `exchangeInfo` currently lists the symbol,
    `DELISTED` when only the archive knows it, and otherwise whatever status
    `exchangeInfo` reported (`SETTLING`, `PENDING_TRADING`, ...) — reported
    verbatim rather than collapsed, because acquisition must not decide what a
    non-trading status means.

    `onboard_date_ms` bounds how far back data can exist and is `None` for a
    symbol known only from the archive.
    """

    symbol: str
    listing_state: str
    in_exchange_info: bool
    in_archive: bool
    contract_type: Optional[str] = None
    quote_asset: Optional[str] = None
    onboard_date_ms: Optional[int] = None
    delivery_date_ms: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    """The discovered symbols plus the raw payloads they were derived from.

    The payloads are returned so the caller can commit them as evidence: a
    discovery that cannot be re-derived from stored bytes is not reproducible.
    """

    symbols: tuple[SymbolInfo, ...]
    exchange_info_bytes: bytes
    archive_listing_bytes: Optional[bytes]

    def names(self) -> tuple[str, ...]:
        return tuple(info.symbol for info in self.symbols)


# ---------------------------------------------------------------------------
# Parsing (pure — no I/O, so every branch is testable without a network)
# ---------------------------------------------------------------------------

def _is_perpetual_usdt_name(symbol: str) -> bool:
    return bool(_SYMBOL_RE.match(symbol)) and "_" not in symbol


def parse_exchange_info(payload: bytes) -> dict[str, SymbolInfo]:
    """USDⓈ-M perpetual USDT symbols from an `exchangeInfo` response."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"exchangeInfo is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
        raise DiscoveryError("exchangeInfo has no 'symbols' array")

    found: dict[str, SymbolInfo] = {}
    for entry in data["symbols"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("symbol", ""))
        if not _is_perpetual_usdt_name(name):
            continue
        if entry.get("contractType") != "PERPETUAL":
            continue
        if entry.get("quoteAsset") != "USDT":
            continue
        found[name] = SymbolInfo(
            symbol=name,
            listing_state=str(entry.get("status", "")) or LISTED,
            in_exchange_info=True,
            in_archive=False,
            contract_type="PERPETUAL",
            quote_asset="USDT",
            onboard_date_ms=_opt_int(entry.get("onboardDate")),
            delivery_date_ms=_opt_int(entry.get("deliveryDate")),
        )
    return found


def parse_archive_listing(payload: bytes) -> tuple[tuple[str, ...], Optional[str]]:
    """Symbol names and the next paging marker from one S3 listing page."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise DiscoveryError(f"archive listing is not XML: {exc}") from exc

    names: list[str] = []
    for prefix in root.findall("s3:CommonPrefixes/s3:Prefix", _S3_NS):
        text = (prefix.text or "").rstrip("/")
        name = text.rsplit("/", 1)[-1]
        if _is_perpetual_usdt_name(name):
            names.append(name)

    truncated = (root.findtext("s3:IsTruncated", default="false", namespaces=_S3_NS)
                 or "false").strip().lower() == "true"
    marker = root.findtext("s3:NextMarker", namespaces=_S3_NS)
    if truncated and not marker and names:
        # S3 omits NextMarker when a delimiter is used; the last prefix is the
        # marker to continue from.
        last = root.findall("s3:CommonPrefixes/s3:Prefix", _S3_NS)[-1].text or ""
        marker = last
    return tuple(names), (marker if truncated else None)


def _opt_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_symbols(
    transport: HttpTransport,
    *,
    include_archive: bool = True,
    max_listing_pages: int = 100,
) -> DiscoveryResult:
    """Every USDⓈ-M perpetual USDT symbol, currently listed or delisted.

    Deterministic: the result is sorted by symbol name, so identical exchange
    responses always produce an identical result, and the ordering does not
    depend on dict iteration, network arrival order, or S3 pagination.

    `include_archive=False` restricts discovery to currently-listed symbols. It
    exists for a caller that knowingly accepts survivorship bias (and must declare
    it downstream, C-6.9); it is not the default.
    """
    try:
        exchange_bytes = transport.get(EXCHANGE_INFO_URL)
    except TransportError as exc:
        raise DiscoveryError(f"could not read exchangeInfo: {exc}") from exc
    listed = parse_exchange_info(exchange_bytes)

    archive_names: tuple[str, ...] = ()
    listing_bytes: Optional[bytes] = None
    if include_archive:
        archive_names, listing_bytes = _list_archive_symbols(
            transport, max_pages=max_listing_pages
        )

    merged: dict[str, SymbolInfo] = {}
    for name, info in listed.items():
        merged[name] = info
    for name in archive_names:
        existing = merged.get(name)
        if existing is None:
            merged[name] = SymbolInfo(
                symbol=name,
                listing_state=DELISTED,
                in_exchange_info=False,
                in_archive=True,
            )
        else:
            merged[name] = SymbolInfo(
                symbol=existing.symbol,
                listing_state=existing.listing_state,
                in_exchange_info=True,
                in_archive=True,
                contract_type=existing.contract_type,
                quote_asset=existing.quote_asset,
                onboard_date_ms=existing.onboard_date_ms,
                delivery_date_ms=existing.delivery_date_ms,
            )

    symbols = tuple(merged[name] for name in sorted(merged))
    return DiscoveryResult(
        symbols=symbols,
        exchange_info_bytes=exchange_bytes,
        archive_listing_bytes=listing_bytes,
    )


def _list_archive_symbols(
    transport: HttpTransport, *, max_pages: int
) -> tuple[tuple[str, ...], bytes]:
    """Walk the S3 listing, following markers, and return the union of names.

    The concatenated pages are returned as the evidence payload: discovery must be
    re-derivable from stored bytes, and one page is not the whole answer.
    """
    names: list[str] = []
    pages: list[bytes] = []
    marker: Optional[str] = None
    for _ in range(max_pages):
        url = ARCHIVE_LISTING_URL
        if marker:
            url = f"{url}&marker={marker}"
        try:
            payload = transport.get(url)
        except TransportError as exc:
            raise DiscoveryError(f"could not read the archive listing: {exc}") from exc
        pages.append(payload)
        page_names, marker = parse_archive_listing(payload)
        names.extend(page_names)
        if not marker:
            break
    else:
        raise DiscoveryError(
            f"archive listing did not terminate within {max_pages} pages"
        )
    return tuple(sorted(set(names))), b"\n".join(pages)
