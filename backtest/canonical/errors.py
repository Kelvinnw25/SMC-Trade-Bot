"""The one failure base for the canonical builder.

Its own hierarchy, deliberately not rooted in `AcquisitionError`: a caller that
catches "the raw tier failed" and a caller that catches "the build failed" are
asking different questions, and the tiers are separate by construction (C-6.10).
"""
from __future__ import annotations

__all__ = ["BuildError"]


class BuildError(Exception):
    """Base for every failure raised while building a canonical dataset.

    Catching this means "the canonical dataset could not be produced from the
    raw evidence". Nothing below it is ever resolved by repairing data.
    """
