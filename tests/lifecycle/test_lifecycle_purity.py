"""lifecycle layer boundaries: pure state logic, correctly bounded.

Architecture ref: PROJECT_STATE.md §5 (A2/A4/A7/A8), 02-software-architecture.md
§3/§4/§10 (Principles 1/4/10). The lifecycle layer holds state but performs no
I/O and reads no wall clock (time comes only through the injected ``Clock``); it
may depend on ``domain``, ``infra.clock``, and consume ``analysis`` OUTPUTS, but
never on ``analysis`` internals beyond the Signal/HTFContext data it is handed,
never on ``app``, and never on ``src`` (Rule A2 — only infra imports V1) or the
test harness (A8).
"""
from __future__ import annotations

import ast
import importlib
import re
import unittest
from pathlib import Path
from typing import Iterator, Tuple

LIFECYCLE_DIR = Path(__file__).resolve().parents[2] / "v2" / "lifecycle"

ALLOWED_IMPORT_ROOTS = {
    "__future__", "dataclasses", "typing", "collections", "math", "enum",
}
# domain (vocabulary), infra.clock (injected time contract) — nothing else.
ALLOWED_MODULE_PREFIXES = ("v2.domain", "v2.lifecycle", "v2.infra.clock")

# Layers/packages the lifecycle must never import.
FORBIDDEN_MODULE_PREFIXES = (
    "src",
    "v2.analysis",
    "v2.app",
    "v2.infra.market_data",
    "v2.infra.persistence",
    "v2.infra.notifier",
    "tests",
)

# Impurity markers: filesystem, network, and — critically — direct clock reads.
# Word-boundary regexes so an identifier like ``h1_gate_open`` is not a false
# positive for the builtin ``open(``.
FORBIDDEN_SOURCE_PATTERNS = (
    r"\btime\.time\s*\(",
    r"\bdatetime\.now",
    r"\butcnow",
    r"\bopen\s*\(",
    r"\bjson\.load",
    r"\bjson\.dump",
    r"\brequests\.",
    r"\bsocket\.",
)


def iter_imports(source: str) -> Iterator[Tuple[int, str]]:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, node.module or ""


def lifecycle_sources() -> list[tuple[Path, str]]:
    files = sorted(LIFECYCLE_DIR.glob("*.py"))
    return [(f, f.read_text(encoding="utf-8")) for f in files]


class TestLifecyclePurity(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = lifecycle_sources()
        self.assertGreater(len(self.sources), 0)

    def test_only_allowed_imports(self):
        for path, source in self.sources:
            for line_no, module in iter_imports(source):
                root = module.split(".")[0]
                allowed = root in ALLOWED_IMPORT_ROOTS or module.startswith(
                    ALLOWED_MODULE_PREFIXES
                )
                self.assertTrue(
                    allowed,
                    f"{path.name}:{line_no} imports '{module}', outside the "
                    f"lifecycle layer's allowed dependencies",
                )

    def test_no_forbidden_layer_imports(self):
        for path, source in self.sources:
            for line_no, module in iter_imports(source):
                self.assertFalse(
                    module.startswith(FORBIDDEN_MODULE_PREFIXES),
                    f"{path.name}:{line_no} imports '{module}' — lifecycle must "
                    f"not depend on V1, analysis internals, app, I/O infra, or tests",
                )

    def test_no_io_or_wall_clock(self):
        for path, source in self.sources:
            for pattern in FORBIDDEN_SOURCE_PATTERNS:
                self.assertIsNone(
                    re.search(pattern, source),
                    f"{path.name} matches forbidden pattern '{pattern}' — the "
                    f"lifecycle layer must stay pure (time only via injected Clock)",
                )

    def test_every_module_imports_cleanly(self):
        for path, _ in self.sources:
            name = (
                f"v2.lifecycle.{path.stem}" if path.stem != "__init__" else "v2.lifecycle"
            )
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(name))


class TestModuleOwnership(unittest.TestCase):
    """Each lifecycle rule has exactly ONE implementation, and it lives in
    ``lifecycle.rules`` — not duplicated across reconciler/store."""

    def _defs(self, stem: str) -> set[str]:
        source = (LIFECYCLE_DIR / f"{stem}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        return {
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        }

    def test_predicates_defined_only_in_rules(self):
        rules = self._defs("rules")
        for predicate in (
            "is_poi_invalidated", "levels_valid", "is_same_thesis",
            "alert_tier", "alert_rank", "ttl_for",
        ):
            self.assertIn(predicate, rules, f"{predicate} must live in rules")
        # not redefined in reconciler or store
        for stem in ("reconciler", "store"):
            other = self._defs(stem)
            for predicate in (
                "is_poi_invalidated", "levels_valid", "is_same_thesis",
                "alert_tier", "alert_rank", "ttl_for",
            ):
                self.assertNotIn(
                    predicate, other,
                    f"{predicate} is duplicated in {stem} — rules is the single owner",
                )


if __name__ == "__main__":
    unittest.main()
