"""The canonical Universe Definition — what this strategy is allowed to trade.

Frozen in Phase 0, before the first official corpus. Changing anything in this
module changes what the live bot trades and what replay ranks, so it is a
**production change** and requires a `STRATEGY_VERSION` bump. See
`PHASE0_FREEZE.md`.

## Three universes, deliberately different sizes

| Universe | What it is | Where it is applied |
|---|---|---|
| **Historical** | every symbol the archive has ever held, delisted included | acquisition (`discover_symbols(include_archive=True)`) |
| **Replay** | the eligible set, ranked per C-6.9 | corpus construction, then adapter ranking |
| **Live** | **identical to Replay, by requirement** | `v2/infra/market_data.py` |

`Historical ⊇ Replay ≡ Live`.

Historical is *deliberately wider*: it is what removes survivorship bias, and it
must never be narrowed to the eligible set at acquisition time, because a symbol
that was delisted is still evidence about the market that existed. The narrowing
happens once, at corpus construction, where the exchange metadata that justifies
each exclusion is already committed as evidence.

Replay and Live must stay **identical**. C-6.9 requires replay's candidate set to
mirror the live adapter exactly; if they drift, every replay result silently
describes a universe the bot does not trade, and no metric would reveal it.

## Why exchange-declared metadata rather than the symbol name

The historical rule was `name.endswith("USDT") and "_" not in name`. Measured
against a real `exchangeInfo` snapshot (851 entries), that rule admits:

    EQUITY 131 · HK_EQUITY 6 · KR_EQUITY 3 · COMMODITY 8 · INDEX 3 · PREMARKET 2

`AAPLUSDT`, `AMZNUSDT` and `BTCDOMUSDT` all pass a name test and none of them is
a crypto perpetual. Tokenized equities additionally trade on exchange hours, so
they carry systematic multi-hour flat and zero-volume stretches that the analyzer
has never been designed for.

Excluding them is **scope, not survivorship**: survivorship bias is selection on
*outcome*, decided after the fact; this is selection on *instrument type*,
declared in advance and independent of any performance.

## The classification gap, stated rather than hidden

`exchangeInfo` describes only *currently listed* symbols. A delisted symbol has
no entry, so it **cannot** be classified from exchange metadata at all. Such a
symbol is `unknown`, never guessed: `is_eligible` returns False for it, and the
committed classification table (Phase 0) is what resolves it. An inferred
classification that looks authoritative is worse than an absent one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "ELIGIBLE_CONTRACT_TYPE",
    "ELIGIBLE_QUOTE_ASSET",
    "ELIGIBLE_UNDERLYING_TYPE",
    "UNIVERSE_DEFINITION_VERSION",
    "InstrumentMetadata",
    "is_eligible",
    "is_plausible_name",
]

#: Bump when the rule below changes. A change here is a production change.
UNIVERSE_DEFINITION_VERSION = 1

ELIGIBLE_CONTRACT_TYPE = "PERPETUAL"
ELIGIBLE_UNDERLYING_TYPE = "COIN"
ELIGIBLE_QUOTE_ASSET = "USDT"


@dataclass(frozen=True)
class InstrumentMetadata:
    """The three `exchangeInfo` fields the universe rule reads, and nothing else.

    Deliberately narrow. A wider object would invite a future contributor to add
    a fourth condition without noticing that doing so changes what the bot
    trades.
    """

    symbol: str
    contract_type: Optional[str] = None
    underlying_type: Optional[str] = None
    quote_asset: Optional[str] = None

    @classmethod
    def from_exchange_info(cls, entry: dict[str, Any]) -> "InstrumentMetadata":
        return cls(
            symbol=str(entry.get("symbol", "")),
            contract_type=entry.get("contractType"),
            underlying_type=entry.get("underlyingType"),
            quote_asset=entry.get("quoteAsset"),
        )

    @property
    def classified(self) -> bool:
        """False for a delisted symbol, which has no `exchangeInfo` entry."""
        return self.underlying_type is not None


def is_eligible(metadata: InstrumentMetadata) -> bool:
    """The canonical rule. Unclassified is **not** eligible.

    All three conditions are required. `contract_type` alone is insufficient
    (`TRADIFI_PERPETUAL` exists, and tokenized equities use it), and
    `underlying_type` alone is insufficient (a COIN could be quoted in USDC).
    """
    return (metadata.contract_type == ELIGIBLE_CONTRACT_TYPE
            and metadata.underlying_type == ELIGIBLE_UNDERLYING_TYPE
            and metadata.quote_asset == ELIGIBLE_QUOTE_ASSET)


def is_plausible_name(symbol: str) -> bool:
    """The legacy name test, kept **only** as a cheap pre-filter.

    It removes dated delivery contracts (`BTCUSDT_240628`) and non-USDT quotes
    before the metadata lookup. It is *not* the universe rule and must never be
    used as one — on a real snapshot it admits 150 non-crypto instruments. It
    survives because the archive listing has no metadata at all, so Historical
    discovery has nothing else to filter on.
    """
    return symbol.endswith("USDT") and "_" not in symbol
