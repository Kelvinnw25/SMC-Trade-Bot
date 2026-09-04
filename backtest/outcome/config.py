"""`OutcomeConfig` — the canonical field list of C-9.7, and nothing else.

Its hash segments every dataset: two outcome sets produced under different
configs are **never comparable without segmentation** (C-4.12). The field set is
therefore fixed by contract, and adding a field is a contract amendment, not a
code change.

Seven of the eleven fields are marked *fixed* by C-9.7 — `limit_fill_rule`,
`tie_break`, `no_m5_policy`, `funding_rate_model`, `shadow_pending_h1`,
`equity_convention` — meaning exactly one value is implemented. They are still
config fields, because they are part of the hash: a future second rule must
change the hash of every dataset produced under the first, and a constant buried
in code could not do that.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from backtest.data.repository import canonical_json
from backtest.outcome.errors import OutcomeConfigError

__all__ = [
    "CONTINUATION_FILLS",
    "OutcomeConfig",
    "config_hash",
]

#: C-4.2: the default, and the pessimistic sensitivity variant.
CONTINUATION_FILLS = ("w0_open", "w0_close")


@dataclass(frozen=True)
class OutcomeConfig:
    """Every parameter that can change an outcome (C-9.7)."""

    #: Expiry cap in walk candles, counted from the fill candle inclusive (C-4.10).
    max_holding_h1: int = 168
    #: How a CONTINUATION trade fills (C-4.2).
    continuation_fill: str = "w0_open"
    #: Fixed: the only implemented limit rule is a touch (C-4.1).
    limit_fill_rule: str = "touch"
    #: Fixed: worst case, SL first (C-4.3 rule 3).
    tie_break: str = "sl_first"
    #: Fixed: worst case when the M5 set is missing or incomplete (C-4.3 rule 4).
    no_m5_policy: str = "worst_case"
    #: Per side. Zero in the baseline (C-4.12); metrics show 0.05 % as sensitivity.
    taker_fee_rate: float = 0.0
    #: Market-fill slippage per side. Zero in the baseline (C-4.12).
    slippage_rate: float = 0.0
    #: Fixed: funding is a documented omission (C-4.12).
    funding_rate_model: str = "none"
    #: The fixed pending window for the two suppressed shadow classes (C-2.2).
    shadow_pending_h1: int = 3
    #: Lifecycle burn-in in H1 candles (C-5.7.1).
    burn_in_h1: int = 336
    #: Fixed: per-trade unit for the drawdown curve (C-9.8).
    equity_convention: str = "1R"
    #: The committed `exchangeInfo` artifact supplying `tickSize` for C-4.14's
    #: sub-tick floor, or `None` to leave the floor unevaluated. Named here
    #: rather than resolved in code precisely so the choice of price grid
    #: segments every dataset derived under it — see `backtest.outcome.ticks`.
    tick_reference: Optional[str] = None

    def __post_init__(self) -> None:
        if self.max_holding_h1 < 1:
            raise OutcomeConfigError(
                f"max_holding_h1 must be >= 1, got {self.max_holding_h1}")
        if self.continuation_fill not in CONTINUATION_FILLS:
            raise OutcomeConfigError(
                f"continuation_fill must be one of {CONTINUATION_FILLS}, got "
                f"{self.continuation_fill!r}")
        if self.limit_fill_rule != "touch":
            raise OutcomeConfigError(
                f"limit_fill_rule is fixed at 'touch' (C-4.1); "
                f"{self.limit_fill_rule!r} is not implemented")
        if self.tie_break != "sl_first":
            raise OutcomeConfigError(
                f"tie_break is fixed at 'sl_first' (C-4.3); "
                f"{self.tie_break!r} is not implemented")
        if self.no_m5_policy != "worst_case":
            raise OutcomeConfigError(
                f"no_m5_policy is fixed at 'worst_case' (C-4.3 rule 4); "
                f"{self.no_m5_policy!r} is not implemented")
        if self.funding_rate_model != "none":
            raise OutcomeConfigError(
                f"funding_rate_model is fixed at 'none' (C-4.12); "
                f"{self.funding_rate_model!r} is not implemented")
        if self.equity_convention != "1R":
            raise OutcomeConfigError(
                f"equity_convention is fixed at '1R' (C-9.8); "
                f"{self.equity_convention!r} is not implemented")
        if self.shadow_pending_h1 < 1:
            raise OutcomeConfigError(
                f"shadow_pending_h1 must be >= 1, got {self.shadow_pending_h1}")
        if self.burn_in_h1 < 0:
            raise OutcomeConfigError(
                f"burn_in_h1 must be >= 0, got {self.burn_in_h1}")
        if self.tick_reference is not None and not str(self.tick_reference):
            raise OutcomeConfigError(
                "tick_reference must be a non-empty artifact id or None; an "
                "empty string would hash differently from None while meaning "
                "the same thing")
        for name in ("taker_fee_rate", "slippage_rate"):
            value = getattr(self, name)
            if value < 0:
                raise OutcomeConfigError(f"{name} must be >= 0, got {value}")
        if self.slippage_rate != 0.0:
            # C-9.7 lists the field and C-4.12 sets its baseline to zero, but
            # C-9.6's net-R formula is defined **only** in terms of
            # `taker_fee_rate`. Applying slippage would require inventing where
            # in the fill it lands, so a non-zero value is refused rather than
            # silently ignored. See finding O5.
            raise OutcomeConfigError(
                "slippage_rate is recorded but not applied: C-9.6 defines "
                "realized_r_net only in terms of taker_fee_rate, so there is no "
                "contract rule for where slippage enters a fill. Refusing "
                f"slippage_rate={self.slippage_rate} rather than ignoring it.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        """`sha256(canonical_json(config))[:16]` (C-9.7), stamped into every
        record so outcomes from different configs are never silently mixed."""
        return config_hash(self)


def config_hash(config: OutcomeConfig) -> str:
    import hashlib

    payload = canonical_json(config.to_dict())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
