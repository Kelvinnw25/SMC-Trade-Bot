"""Outcome Engine failures — its own hierarchy, rooted nowhere else.

The engine is an offline derivation stage: a caller catching "the outcome could
not be derived" is asking a different question from "the replay failed" or "the
raw tier is corrupt", so the bases stay separate (as they do between the raw and
canonical tiers).
"""
from __future__ import annotations

__all__ = [
    "OutcomeConfigError",
    "OutcomeDataError",
    "OutcomeError",
    "TradeIntentError",
]


class OutcomeError(Exception):
    """Base for every Outcome Engine failure."""


class OutcomeConfigError(OutcomeError, ValueError):
    """The `OutcomeConfig` describes a model that is not implemented."""


class TradeIntentError(OutcomeError, ValueError):
    """A `TradeIntent` is not well formed. Distinct from `INVALID_LEVELS`, which
    is a *trade outcome* the contract requires to be recorded and counted
    (C-4.14): a malformed intent is a caller bug, inverted levels are data."""


class OutcomeDataError(OutcomeError):
    """The repository cannot supply the prices a walk requires — the reference
    candle is missing, or the dataset is corrupt. Never raised for an ordinary
    end of data, which is the `OPEN` label (C-4.10)."""
