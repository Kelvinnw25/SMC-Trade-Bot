"""Recorded V1 — the frozen behavioural oracle that replaces live `src.strategy`.

Why this exists
---------------
Every equivalence test in this suite proves the same thing: *V2 reproduces V1's
observable output for a given input*. Until now that proof was executed by
calling V1's real `SMCStrategy` at test time, which made the whole equivalence
argument depend on V1 source remaining in the tree forever.

`SMCStrategy` here is a **drop-in stand-in** with V1's exact method surface,
backed by a committed store of V1's recorded return values. The tests are
unchanged apart from their import line, and the equivalence claim they assert is
identical — it is simply checked against V1's *recorded* answer rather than a
freshly computed one. The proof therefore survives V1's removal from the repo.

Recording
---------
While `src/strategy.py` still exists, regenerate the store with::

    SMC_V1_RECORD=1 SMC_V1_SESSION=SNIPER venv/Scripts/python.exe -m unittest discover -s tests -t .
    SMC_V1_RECORD=1 SMC_V1_SESSION=STRICT venv/Scripts/python.exe -m unittest discover -s tests -t .

In record mode every call is delegated to the real `src.strategy.SMCStrategy`,
the return value is encoded, and `(method, args) -> value` is appended to
``archive/v1-final/equivalence-proof/goldens/<method>.jsonl``. Recording the same
key twice with *different* values raises immediately — that would mean the call
is not a pure function of its recorded inputs, which would invalidate the store.

Two passes are required because V1 reads the wall clock in exactly one place
(`get_session_mode`, strategy.py:14-19) and two methods branch on it
(`build_signal_context` strategy.py:1690, `enrich_with_m5` strategy.py:1837).
Those two methods carry the session state in their cache key, so replay picks
the golden matching the session the test is actually running in. This makes the
suite *more* deterministic than it was: previously a corpus comparison silently
compared a SNIPER-recorded V1 against a STRICT-clock V2.

`get_session_mode` itself is not recorded — freezing a wall-clock answer into a
committed file would be wrong. It is reimplemented natively below, character for
character from strategy.py:14-19.

Determinism: no network, no randomness, no state. V1's only other input-derived
memo (`_cached_bias`) is keyed by frame timestamp and is verified during
recording to produce no conflicting values for identical inputs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = _ROOT / "archive" / "v1-final" / "equivalence-proof" / "goldens"

RECORDING = os.environ.get("SMC_V1_RECORD") == "1"

# Methods whose result depends on V1's single wall-clock read.
_SESSION_SENSITIVE = frozenset({"build_signal_context", "enrich_with_m5"})


class GoldenMissError(LookupError):
    """A recorded V1 answer was requested but is not in the store."""


class GoldenConflictError(RuntimeError):
    """The same call recorded two different answers — the store would be unsound."""


# ---------------------------------------------------------------------------
# Session mode — V1's only clock read, reproduced natively (strategy.py:14-19)
# ---------------------------------------------------------------------------

def _session_mode() -> str:
    forced = os.environ.get("SMC_V1_SESSION")
    if forced:
        return forced
    # V1 wrote `datetime.utcnow()`; `now(timezone.utc)` yields the same weekday
    # and hour without the deprecation warning.
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return "STRICT"
    hour = now.hour
    if hour >= 22 or hour < 7:
        return "STRICT"
    return "SNIPER"


# ---------------------------------------------------------------------------
# Type-preserving codec
# ---------------------------------------------------------------------------
# assertEqual distinguishes tuple from list, so container kinds are tagged.
# NaN/inf are tagged because strict JSON cannot hold them. numpy scalars are
# narrowed to their Python equivalents, which compare equal to V2's values.

def encode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return {"$f": "nan"}
        if math.isinf(value):
            return {"$f": "inf" if value > 0 else "-inf"}
        return float(value)
    if isinstance(value, pd.Timestamp):
        return {"$ts": value.isoformat()}
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, pd.DataFrame):
        return {
            "$df": {
                "cols": [str(c) for c in value.columns],
                "rows": [[encode(v) for v in row] for row in value.itertuples(index=False)],
            }
        }
    if isinstance(value, pd.Series):
        return {"$s": {"idx": [encode(i) for i in value.index],
                       "vals": [encode(v) for v in value.tolist()]}}
    if isinstance(value, tuple):
        return {"$t": [encode(v) for v in value]}
    if isinstance(value, list):
        return {"$l": [encode(v) for v in value]}
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: repr(kv[0]))
        return {"$d": [[encode(k), encode(v)] for k, v in items]}
    # numpy scalars / 0-d arrays and anything else exposing .item()
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "shape", None) == ():
        return encode(value.item())
    if hasattr(value, "tolist") and hasattr(value, "shape"):
        return {"$a": [encode(v) for v in value.tolist()]}
    raise TypeError(f"v1_recorded cannot encode {type(value).__name__}: {value!r}")


def decode(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "$f" in value:
        return float(value["$f"])
    if "$ts" in value:
        return pd.Timestamp(value["$ts"])
    if "$dt" in value:
        return datetime.fromisoformat(value["$dt"])
    if "$t" in value:
        return tuple(decode(v) for v in value["$t"])
    if "$l" in value:
        return [decode(v) for v in value["$l"]]
    if "$a" in value:
        return [decode(v) for v in value["$a"]]
    if "$d" in value:
        return {decode(k): decode(v) for k, v in value["$d"]}
    if "$df" in value:
        payload = value["$df"]
        return pd.DataFrame(
            [[decode(v) for v in row] for row in payload["rows"]],
            columns=payload["cols"],
        )
    if "$s" in value:
        payload = value["$s"]
        return pd.Series(
            [decode(v) for v in payload["vals"]],
            index=[decode(i) for i in payload["idx"]],
        )
    return value


def _digest(method: str, args: tuple, kwargs: dict, *, state: dict[str, Any]) -> str:
    """Identify one V1 call by method, arguments, and the caller's instance state.

    `state` carries V1's `_bias_cache` (strategy.py:21-34). It has to be part of
    the key because `build_signal_context` is **not** a pure function of its
    arguments: the memo is keyed by `f"{tf}_{last_timestamp}"`, so two corpus
    frames that share a last timestamp return the first one's bias for the
    second. `tests/analysis/test_scoring.py` already documented this as "a latent
    V1 collision that never fires in production"; recording made it explicit —
    the first recording pass aborted on exactly this conflict.
    """
    payload: list[Any] = [method, encode(list(args)), encode(kwargs), encode(state)]
    if method in _SESSION_SENSITIVE:
        payload.append(_session_mode())
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class _Store:
    """Lazily-loaded, append-only `{digest: encoded_value}` map per method."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._loaded: dict[str, dict[str, Any]] = {}
        self._handles: dict[str, Any] = {}

    def path(self, method: str) -> Path:
        return self._dir / f"{method.lstrip('_')}.jsonl"

    def _table(self, method: str) -> dict[str, Any]:
        if method not in self._loaded:
            table: dict[str, Any] = {}
            path = self.path(method)
            if path.exists():
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            row = json.loads(line)
                            table[row["k"]] = {"v": row["v"], "m": row["m"]}
            self._loaded[method] = table
        return self._loaded[method]

    def get(self, method: str, digest: str) -> tuple[Any, Any]:
        """Return `(value, post_state)` for a recorded call."""
        table = self._table(method)
        if digest not in table:
            raise GoldenMissError(
                f"no recorded V1 answer for {method}() [key {digest[:16]}].\n"
                f"  store: {self.path(method)}\n"
                f"  This test calls V1 with inputs that were never recorded. Either the\n"
                f"  test's inputs changed, or the goldens need regenerating against V1\n"
                f"  (see the recording instructions in tests/regression/v1_recorded.py)."
            )
        row = table[digest]
        return decode(row["v"]), decode(row["m"])

    def put(self, method: str, digest: str, value: Any, post_state: Any) -> None:
        table = self._table(method)
        row = {"v": encode(value), "m": encode(post_state)}
        if digest in table:
            if table[digest] != row:
                raise GoldenConflictError(
                    f"{method}() recorded two different results for identical inputs "
                    f"and identical instance state [key {digest[:16]}]. V1 has a "
                    f"hidden input the store does not capture; it cannot be trusted."
                )
            return
        table[digest] = row
        self._dir.mkdir(parents=True, exist_ok=True)
        handle = self._handles.get(method)
        if handle is None:
            handle = self.path(method).open("a", encoding="utf-8")
            self._handles[method] = handle
        handle.write(json.dumps({"k": digest, **row}, ensure_ascii=False,
                                sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


STORE = _Store(GOLDEN_DIR)

if RECORDING:
    import atexit

    atexit.register(STORE.close)


# ---------------------------------------------------------------------------
# The stand-in
# ---------------------------------------------------------------------------

class SMCStrategy:
    """V1's `SMCStrategy` surface, answered from the recorded store.

    Any attribute access returns a callable that looks the answer up by
    `(method, arguments)`. That keeps this stand-in complete by construction —
    it needs no per-method maintenance and covers V1's private helpers
    (`_detect_sweep`, `_cluster_liquidity_levels`, `_get_previous_day_high_low`)
    identically to its public ones.
    """

    def __init__(self, swing_window: int = 2, *_args: Any, **_kwargs: Any) -> None:
        self.swing_window = swing_window
        # Replica of V1's `_bias_cache` (strategy.py:9,21-34). It is part of every
        # cache key, and replay advances it from the recorded post-call state, so
        # a warm instance replays V1's memoised answers exactly as V1 gave them.
        self._bias_cache: dict[str, Any] = {}
        self._live: Optional[Any] = None
        if RECORDING:
            try:
                from src.strategy import SMCStrategy as _LiveSMCStrategy
            except ImportError as exc:  # V1 has been retired
                raise RuntimeError(
                    "SMC_V1_RECORD=1 needs V1 source, which is no longer in the tree. "
                    "Restore it first: git checkout v1-final -- src/ main.py "
                    "(see archive/v1-final/README.md)."
                ) from exc

            # When a session is forced for this recording pass, V1 must actually
            # take that branch — otherwise the wall clock's answer would be
            # stored under the forced session's key.
            if os.environ.get("SMC_V1_SESSION"):
                _LiveSMCStrategy.get_session_mode = lambda _self: _session_mode()
            self._live = _LiveSMCStrategy()

    def get_session_mode(self) -> str:
        """V1 strategy.py:14-19, reproduced natively (a clock read, not analysis)."""
        return _session_mode()

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            state = {"bias_cache": dict(self._bias_cache),
                     "swing_window": self.swing_window}
            digest = _digest(name, args, kwargs, state=state)
            if RECORDING:
                self._live._bias_cache = dict(self._bias_cache)
                value = getattr(self._live, name)(*args, **kwargs)
                post = dict(self._live._bias_cache)
                STORE.put(name, digest, value, post)
                self._bias_cache = post
                return value
            value, post = STORE.get(name, digest)
            self._bias_cache = dict(post)
            return value

        call.__name__ = name
        return call
