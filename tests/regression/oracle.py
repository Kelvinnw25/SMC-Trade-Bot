"""The golden oracle — V1's observable behaviour, answered from recorded goldens.

`V1Oracle` is the reference the whole harness is built around. Its extraction
logic is unchanged and lives in `tests.regression.v1_projection`; this module is
only the binding that says *which* V1 answers the questions.

That binding used to be `src.strategy.SMCStrategy`, executed live. It is now
`tests.regression.v1_recorded.SMCStrategy`, which replays V1's recorded return
values from `archive/v1-final/equivalence-proof/goldens/`. The equivalence claim
is identical — V2 must reproduce V1's observable output for a given input — but
it no longer requires V1 source to be present.

The live-V1 binding is preserved verbatim at
`archive/v1-final/tests/oracle_live_v1.py` and is what the goldens were recorded
through; see `tests/regression/v1_recorded.py` for how to re-record them.

`V1Oracle` still accepts an injected strategy, so tests that exercise the
projection with a fake (see `test_oracle.py`) are unaffected.
"""
from __future__ import annotations

from typing import Any, Optional

from tests.regression.v1_projection import (  # re-exported for existing importers
    Analyzer,
    StrategyOracle,
    _alert_rank,
    _alert_tier,
    _levels_valid,
)
from tests.regression.v1_recorded import SMCStrategy

__all__ = ["Analyzer", "V1Oracle", "_alert_rank", "_alert_tier", "_levels_valid"]


class V1Oracle(StrategyOracle):
    """Reference `Analyzer` backed by V1's recorded behaviour."""

    def __init__(self, strategy: Optional[Any] = None) -> None:
        super().__init__(strategy if strategy is not None else SMCStrategy())
