"""The execution environment is part of the scientific result — assert it.

REPLAY_CONTRACT.md C-19 makes the project's float-determinism guarantee conditional:
*"Same operation order and same `numpy`/`pandas`/`pandas_ta` versions => identical
IEEE-754 results."* C-16.2 records the environment in every run manifest, which makes
a drift **detectable after the fact** — it does not make it **impossible**. A `pip
install` between two baselines would silently invalidate the comparison, and the
manifest would faithfully record two different environments while nothing refused the
diff.

This module closes that gap. It reads `requirements.txt` and asserts the running
interpreter matches it, so drift fails at test time rather than at interpretation time.

## Why `bottleneck` gets its own test

C-8.5 mandates that `identify_swings` keep computing `atr_mean` with
`df["atr"].dropna().mean()` — *"(pandas' summation order) rather than a numpy
equivalent"*. That mandate is currently satisfied by an accident of the install:
`pd.options.compute.use_bottleneck` is `True`, and pandas dispatches the reduction to
`bottleneck` whenever it is importable. `bottleneck` sums sequentially; numpy sums
pairwise. Measured on this environment, the two orders disagree in ~90% of realistic
499-element ATR arrays.

So installing `bottleneck` — a transitive dependency of several scientific packages —
would change `atr_mean`, hence `min_prominence`, hence potentially the swing set, with
no code change anywhere. `test_bottleneck_is_absent` is what converts C-8.5's mandate
from a comment into a checked property.

Setting `pd.options.compute.use_bottleneck = False` in `v2/` would be the other way to
enforce it, and is deliberately **not** done: C-16.3.1 authorizes only the M1 lineage
change set and the C-8.5 performance work as production edits. The guard belongs here,
where it changes no runtime behavior.

Purity: reads `requirements.txt` and installed metadata. No network, no clock, no
writes.
"""
from __future__ import annotations

import platform
import re
import unittest
from importlib import metadata
from pathlib import Path

#: Repo root — this file lives at `tests/test_environment.py`.
REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

#: The interpreter cannot be expressed as a pip requirement, so it is pinned as a
#: structured comment and parsed from it. One file remains the single source.
_PYTHON_PIN = re.compile(r"^#\s*python==(?P<version>\S+)\s*$")

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>\S+)\s*$")

#: Packages that must NOT be installed, with the clause each one would violate.
#: `bottleneck` changes pandas' reduction dispatch (C-8.5); `numexpr` changes
#: expression evaluation for the same class of reason.
FORBIDDEN = {
    "bottleneck": (
        "pandas dispatches .mean() to bottleneck when it is importable, which sums "
        "sequentially where numpy sums pairwise. REPLAY_CONTRACT.md C-8.5 requires "
        "identify_swings' atr_mean to keep pandas' current summation order."
    ),
    "numexpr": (
        "pandas routes expression evaluation through numexpr when it is importable, "
        "changing arithmetic grouping. Same class of hazard as bottleneck (C-8.5)."
    ),
}


def _parse_requirements() -> tuple[dict[str, str], str]:
    """Return `({distribution: version}, python_version)` from `requirements.txt`.

    Raises rather than returning empties: a requirements file that parsed to nothing
    would make every assertion below vacuously true, which is the one failure mode a
    pinning test must not have.
    """
    if not REQUIREMENTS.is_file():
        raise AssertionError(f"{REQUIREMENTS} is missing; the environment is unpinned")

    pins: dict[str, str] = {}
    python_version = ""
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        python_match = _PYTHON_PIN.match(line)
        if python_match:
            python_version = python_match.group("version")
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN.match(stripped)
        if match is None:
            raise AssertionError(
                f"unparseable requirement line {line!r}; every pin must be "
                f"`name==version` so drift cannot hide behind a range"
            )
        pins[match.group("name")] = match.group("version")

    if not pins:
        raise AssertionError(f"{REQUIREMENTS} declares no pins")
    if not python_version:
        raise AssertionError(
            f"{REQUIREMENTS} has no `# python==X.Y.Z` line; the interpreter is "
            f"part of the environment C-19 conditions determinism on"
        )
    return pins, python_version


class TestEnvironmentMatchesRequirements(unittest.TestCase):
    """T0.1 — the running environment is exactly the pinned one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pins, cls.python_version = _parse_requirements()

    def test_every_pin_is_installed_at_the_pinned_version(self):
        mismatches: list[str] = []
        for name, expected in sorted(self.pins.items()):
            try:
                found = metadata.version(name)
            except metadata.PackageNotFoundError:
                mismatches.append(f"{name}: pinned {expected}, NOT INSTALLED")
                continue
            if found != expected:
                mismatches.append(f"{name}: pinned {expected}, installed {found}")
        self.assertEqual(
            [], mismatches,
            "environment drifted from requirements.txt. REPLAY_CONTRACT.md C-19 "
            "conditions float determinism on version identity; a drifted "
            "environment makes prior baselines incomparable (C-5.7.2).\n  "
            + "\n  ".join(mismatches),
        )

    def test_interpreter_matches_the_pin(self):
        self.assertEqual(
            self.python_version, platform.python_version(),
            "interpreter drifted from the `# python==` pin in requirements.txt",
        )

    def test_the_pin_set_is_not_empty(self):
        # Guards the guard: a parse regression that silently yielded {} would make
        # every assertion above pass while checking nothing.
        self.assertGreaterEqual(len(self.pins), 30)
        for anchor in ("numpy", "pandas", "pandas-ta", "requests"):
            self.assertIn(anchor, self.pins)


class TestForbiddenAccelerators(unittest.TestCase):
    """T0.2 — packages that would silently change a frozen reduction order."""

    def test_forbidden_accelerators_are_not_installed(self):
        for name, reason in sorted(FORBIDDEN.items()):
            with self.subTest(package=name):
                try:
                    metadata.version(name)
                except metadata.PackageNotFoundError:
                    continue
                self.fail(f"{name} is installed. {reason}")

    def test_forbidden_accelerators_are_not_importable(self):
        # Metadata absence and import failure are different facts: a vendored or
        # path-injected module has no distribution metadata but pandas would still
        # import and dispatch to it.
        for name, reason in sorted(FORBIDDEN.items()):
            with self.subTest(package=name):
                with self.assertRaises(ImportError, msg=f"{name}: {reason}"):
                    __import__(name)

    def test_forbidden_accelerators_are_absent_from_requirements(self):
        pins, _ = _parse_requirements()
        for name in FORBIDDEN:
            self.assertNotIn(
                name, pins,
                f"{name} must never be pinned — see C-8.5 and this module's docstring",
            )


if __name__ == "__main__":          # pragma: no cover
    unittest.main()
