"""Phase 1 DoD (by inspection, automated): the domain layer depends on nothing.

Architecture §4: "The `domain` layer is dependency-free." Decision #004:
analysis (and its vocabulary) stays pure — no src/ imports, no I/O modules, no
clock access. Allowed imports: stdlib type machinery (dataclasses, typing,
enum, __future__), pandas (existing project dependency, required for
MarketSnapshot frames), and intra-domain (v2.domain.*).
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Iterator, Tuple

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "v2" / "domain"

ALLOWED_ROOTS = {"__future__", "dataclasses", "typing", "enum", "pandas"}
ALLOWED_PREFIX = "v2.domain"

FORBIDDEN_SOURCE_PATTERNS = (
    "time.time(",      # clock read (Principle 10)
    "datetime.now",    # clock read
    "utcnow",          # clock read
    "open(",           # file I/O belongs to infra
    "json.load",       # persistence belongs to infra
    "json.dump",       # persistence belongs to infra
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


class TestDomainPurity(unittest.TestCase):
    def _domain_sources(self):
        files = sorted(DOMAIN_DIR.glob("*.py"))
        self.assertGreater(len(files), 0)
        return [(f, f.read_text(encoding="utf-8")) for f in files]

    def test_only_allowed_imports(self):
        for path, source in self._domain_sources():
            for line_no, module in iter_imports(source):
                root = module.split(".")[0]
                allowed = (
                    root in ALLOWED_ROOTS
                    or module == ALLOWED_PREFIX
                    or module.startswith(ALLOWED_PREFIX + ".")
                )
                self.assertTrue(
                    allowed,
                    f"{path.name}:{line_no} forbidden import '{module}' "
                    f"(domain depends on nothing — Architecture §4)",
                )

    def test_no_clock_or_io_usage(self):
        for path, source in self._domain_sources():
            for pattern in FORBIDDEN_SOURCE_PATTERNS:
                self.assertNotIn(
                    pattern, source,
                    f"{path.name} contains forbidden pattern '{pattern}'",
                )

    def test_no_v1_imports_anywhere_in_v2(self):
        v2_dir = DOMAIN_DIR.parent
        for path in sorted(v2_dir.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for line_no, module in iter_imports(source):
                self.assertNotEqual(
                    module.split(".")[0], "src",
                    f"{path}:{line_no} imports V1 code ('{module}')",
                )


if __name__ == "__main__":
    unittest.main()
