"""Exchange price-grid metadata — the `tickSize` side of C-4.14.

## Why the walk needs a tick size

C-4.14 refuses a fill whose risk unit is smaller than one increment of the
exchange's price grid. Such a stop is not a *small* risk, it is an
**unrepresentable** one: no order could rest there, so no operator could have
taken the trade. The R arithmetic of C-9.6 is correct on those fills and
meaningless, because `fee / R_unit` grows without bound as `R_unit` shrinks.

## The reference snapshot, and why it is named in the config

Binance serves `exchangeInfo` for the *current* state only — `discovery.py`
says so, and `corpus.py` records that discarding a fetch "destroyed a day of
irrecoverable history". The corpus therefore holds one committed observation
(`data/stage1/reference/instrument_observations.jsonl`), dated after the
replay window, and no historical tick size exists or can be recovered.

The honest response is not to pretend the snapshot is historical truth, but to
**name it**. `OutcomeConfig.tick_reference` carries the artifact id, so the
choice enters `outcome_config_hash` and segments every dataset derived under it
(C-9.7). A future historical source becomes a hash change, never a silent
re-interpretation of records already on disk.

## Missing symbols are declared, not guessed

552 of the 563 symbols in the frozen run appear in the snapshot; the other
eleven were delisted or renamed before it was taken. No fallback tick is
invented for them — `tick_for` returns `None`, the floor is not evaluated, and
the record carries `tick_size_unavailable = True` so the omission is visible in
the data rather than hidden in a default.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from backtest.outcome.errors import OutcomeError

__all__ = [
    "REFERENCE_SUBDIR",
    "TickReferenceError",
    "TickTable",
    "load_tick_table",
]

#: Where `RawStore` commits instrument metadata, relative to a reference root.
REFERENCE_SUBDIR = "instruments"


class TickReferenceError(OutcomeError):
    """The named reference snapshot is missing, unreadable, or not what its
    content-addressed name says it is."""


@dataclass(frozen=True)
class TickTable:
    """`native_symbol -> tickSize`, read from one named `exchangeInfo` artifact.

    Excluded from equality because the mapping is large and the `reference` id
    already identifies it exactly: two tables with the same reference are the
    same table, or one of them failed its checksum and never got built.
    """

    reference: str
    ticks: Mapping[str, float] = field(default_factory=dict, compare=False,
                                       repr=False)

    def tick_for(self, native_symbol: str) -> Optional[float]:
        """The price increment, or `None` when this symbol is not in the
        snapshot. `None` means *undeclared* and never a guessed default."""
        value = self.ticks.get(native_symbol)
        if value is None or not math.isfinite(value) or value <= 0.0:
            return None
        return float(value)

    def __len__(self) -> int:
        return len(self.ticks)


def load_tick_table(reference: str, *,
                    roots: Iterable[Path | str]) -> TickTable:
    """Read `<root>/instruments/<reference>.json` from the first root holding it.

    The artifact name ends in the first 16 hex of its own sha256, so the name is
    checked against the bytes. A reference that does not hash to its name is
    refused rather than used: silently accepting it would put an unidentified
    price grid into `outcome_config_hash`.
    """
    expected = reference.rsplit("-", 1)[-1]
    searched: list[Path] = []
    for root in roots:
        path = Path(root) / REFERENCE_SUBDIR / f"{reference}.json"
        searched.append(path)
        if not path.is_file():
            continue
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if not digest.startswith(expected):
            raise TickReferenceError(
                f"{path} hashes to {digest[:16]} but its name claims "
                f"{expected}; refusing an unidentified price grid")
        return TickTable(reference=reference, ticks=_parse(payload, path))
    raise TickReferenceError(
        f"tick reference {reference!r} not found. Looked in: "
        f"{[str(p) for p in searched]}")


def _parse(payload: bytes, path: Path) -> dict[str, float]:
    """`PRICE_FILTER.tickSize` per symbol, and nothing else from the payload."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TickReferenceError(f"{path} is not readable JSON: {exc}") from exc
    symbols = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(symbols, list):
        raise TickReferenceError(f"{path} has no 'symbols' array")

    out: dict[str, float] = {}
    for entry in symbols:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("symbol", ""))
        if not name:
            continue
        for spec in entry.get("filters", ()) or ():
            if not isinstance(spec, dict):
                continue
            if spec.get("filterType") != "PRICE_FILTER":
                continue
            try:
                tick = float(spec.get("tickSize"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(tick) and tick > 0.0:
                out[name] = tick
            break
    if not out:
        raise TickReferenceError(
            f"{path} carries no usable PRICE_FILTER tickSize")
    return out
