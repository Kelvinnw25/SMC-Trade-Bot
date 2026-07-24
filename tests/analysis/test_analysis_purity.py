"""Phase 3.0 architecture: the analysis layer is pure and correctly bounded.

Architecture ref: PROJECT_STATE.md §5 rules A2/A4/A7/A8 and §11 (Phase 3 DoD:
"No analysis module references memory, persistence, notification, clock, or
prior-signal state"). This is the analysis-layer counterpart to
`tests/domain/test_domain_purity.py`, and it is the test that keeps the layer
diffable against V1 — impurity would make analysis non-deterministic.

Allowed imports: stdlib type machinery, `v2.domain`, and intra-layer
`v2.analysis`. Extending `ALLOWED_IMPORT_ROOTS` is a deliberate, reviewable
architectural decision — not a routine fix for a failing test.
"""
from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path
from typing import Iterator, Tuple

ANALYSIS_DIR = Path(__file__).resolve().parents[2] / "v2" / "analysis"

# `pandas` and `math` are pure computation dependencies: pandas is already a
# domain dependency (MarketSnapshot carries DataFrames) and analysis must read
# those frames; neither performs I/O nor compromises determinism. Extending this
# set is a deliberate, reviewable architectural decision — not a routine fix.
ALLOWED_IMPORT_ROOTS = {
    "__future__", "dataclasses", "typing", "collections", "math", "pandas",
}
ALLOWED_MODULE_PREFIXES = ("v2.domain", "v2.analysis")

# Layers analysis must never reach into: V1, the I/O boundary, the layers above
# it, and the verification harness (rule A8 — runtime never imports tests).
FORBIDDEN_MODULE_PREFIXES = ("src", "v2.infra", "v2.lifecycle", "v2.app", "tests")

# Impurity markers: clock reads and I/O have no place in a pure layer.
FORBIDDEN_SOURCE_PATTERNS = (
    "time.time(",
    "datetime.now",
    "utcnow",
    "open(",
    "json.load",
    "json.dump",
    "requests.",
)


def iter_imports(source: str) -> Iterator[Tuple[int, str]]:
    """Yield (line_no, module_name) for every real import statement."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, node.module or ""


def analysis_sources() -> list[tuple[Path, str]]:
    files = sorted(ANALYSIS_DIR.glob("*.py"))
    return [(f, f.read_text(encoding="utf-8")) for f in files]


class TestAnalysisPurity(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = analysis_sources()
        self.assertGreater(len(self.sources), 0, "no analysis modules found")

    def test_only_allowed_imports(self):
        for path, source in self.sources:
            for line_no, module in iter_imports(source):
                root = module.split(".")[0]
                allowed = root in ALLOWED_IMPORT_ROOTS or module.startswith(
                    ALLOWED_MODULE_PREFIXES
                )
                self.assertTrue(
                    allowed,
                    f"{path.name}:{line_no} imports '{module}', which is outside "
                    f"the analysis layer's allowed dependencies "
                    f"(domain + typing + stdlib)",
                )

    def test_no_forbidden_layer_imports(self):
        for path, source in self.sources:
            for line_no, module in iter_imports(source):
                self.assertFalse(
                    module.startswith(FORBIDDEN_MODULE_PREFIXES),
                    f"{path.name}:{line_no} imports '{module}' — analysis must not "
                    f"depend on V1, infra, lifecycle, app, or the test harness",
                )

    def test_no_clock_or_io_usage(self):
        for path, source in self.sources:
            for pattern in FORBIDDEN_SOURCE_PATTERNS:
                self.assertNotIn(
                    pattern,
                    source,
                    f"{path.name} contains forbidden pattern '{pattern}' — the "
                    f"analysis layer must stay pure",
                )

    def test_every_module_imports_cleanly(self):
        for path, _ in self.sources:
            name = f"v2.analysis.{path.stem}" if path.stem != "__init__" else "v2.analysis"
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(name))


class TestNoPriorSignalState(unittest.TestCase):
    """Phase 3 DoD: analysis never references previous-signal state."""

    def test_context_carries_no_prior_signal(self):
        import dataclasses

        from v2.analysis.context import AnalysisContext

        field_names = {f.name for f in dataclasses.fields(AnalysisContext)}
        self.assertNotIn(
            "prior",
            field_names,
            "AnalysisContext must not carry prior-signal state — the engine "
            "accepts `prior` only for interface compatibility and must never "
            "thread it into the pipeline",
        )


if __name__ == "__main__":
    unittest.main()
