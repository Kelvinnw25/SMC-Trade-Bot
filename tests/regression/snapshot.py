"""InputSnapshot — a deterministic, file-backed capture of one analysis input.

The migration's supreme rule is behavioral equivalence, and that is only
provable if V1 and V2 are fed *byte-identical* inputs. `InputSnapshot` is that
frozen input: the exact closed-candle D1/H1/(M5) frames, the forming-candle live
price, and the H1 reference timestamp that V1's pipeline received for one symbol
at one moment (Roadmap §6 "Inputs: the exact raw D1/H1/M5 data V1 fetched").

It serializes to plain JSON (no pickle, no network) so snapshots live in local
files, are diffable, and reload deterministically. `to_market_snapshot()`
reconstructs the shared `v2.domain.MarketSnapshot` — the one contract both the
V1 oracle and any future V2 analyzer consume — guaranteeing identical inputs.

Fidelity notes:
- Frames keep V1's exact column set/order (timestamp, open, high, low, close,
  v, atr). ATR is NaN for the first `ATR_PERIOD` rows in real V1 data; NaN is
  preserved losslessly (stored as JSON ``null``, restored as ``float('nan')``).
- `h1_last_ts` is stored as captured (V1's `_to_epoch_ts` result) rather than
  recomputed, so it matches V1 to the bit. Synthetic snapshots that supply no
  timestamp compute it via `_epoch`, the same tz-naive-as-UTC method V1 uses.
- Float values round-trip exactly (Python's `repr`-based JSON float encoding is
  lossless for IEEE-754 doubles).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from v2.domain.market import MarketSnapshot

# V1's OHLCV+ATR columns (src/api_handler.fetch_data_with_atr + the ATR column).
FRAME_COLUMNS: tuple[str, ...] = ("timestamp", "open", "high", "low", "close", "v", "atr")
SNAPSHOT_FORMAT = 1

Row = dict[str, Any]


def _epoch(ts_value: Any) -> float:
    """Normalize a timestamp to a POSIX epoch float.

    Behaviorally identical to V1's `_to_epoch_ts` (main.py) and the market-data
    adapter's `_to_epoch`: a tz-naive candle timestamp is treated as UTC, and
    any failure yields 0.0. Used only when a synthetic snapshot supplies no
    captured `h1_last_ts`; captured snapshots store V1's exact value.
    """
    try:
        if ts_value is None:
            return 0.0
        ts = pd.to_datetime(ts_value)
        if pd.isna(ts):
            return 0.0
        return float(ts.timestamp())
    except Exception:
        return 0.0


def _frame_to_rows(df: pd.DataFrame) -> list[Row]:
    """Serialize a DataFrame to JSON-safe row dicts (timestamps → ISO strings,
    NaN → None), preserving the frame's column order."""
    rows: list[Row] = []
    for _, r in df.iterrows():
        row: Row = {}
        for col in df.columns:
            value = r[col]
            if col == "timestamp":
                ts = pd.to_datetime(value)
                row[col] = None if pd.isna(ts) else ts.isoformat()
            else:
                num = float(value)
                row[col] = None if math.isnan(num) else num
        rows.append(row)
    return rows


def _rows_to_frame(columns: tuple[str, ...], rows: list[Row]) -> pd.DataFrame:
    """Reconstruct a DataFrame from serialized rows, restoring datetime and NaN."""
    data: dict[str, list[Any]] = {c: [] for c in columns}
    for row in rows:
        for col in columns:
            value = row.get(col)
            if col == "timestamp":
                data[col].append(pd.NaT if value is None else pd.to_datetime(value))
            else:
                data[col].append(float("nan") if value is None else float(value))
    frame = pd.DataFrame(data, columns=list(columns))
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


@dataclass
class InputSnapshot:
    """A frozen, replayable analysis input for one symbol.

    Fields:
        symbol: pair symbol, V1 format (e.g. ``"BTC/USDT"``).
        columns: the frame column order shared by d1/h1/m5.
        d1_rows / h1_rows: closed-candle rows (post forming-candle strip).
        m5_rows: closed M5 rows, or None when M5 was not attached.
        live_price: close of the forming H1 candle (V1 ``market_price``).
        h1_last_ts: epoch of the last closed H1 candle (V1 ``_to_epoch_ts``).
        captured_at / source / note: provenance metadata (not analysis input).
    """

    symbol: str
    columns: tuple[str, ...]
    d1_rows: list[Row]
    h1_rows: list[Row]
    live_price: float
    h1_last_ts: float
    m5_rows: Optional[list[Row]] = None
    captured_at: Optional[str] = None
    source: str = "synthetic"
    note: str = ""

    # -- reconstruction -----------------------------------------------------

    def to_market_snapshot(self) -> MarketSnapshot:
        """Rebuild the shared `MarketSnapshot` both V1 and V2 analysis consume."""
        d1 = _rows_to_frame(self.columns, self.d1_rows)
        h1 = _rows_to_frame(self.columns, self.h1_rows)
        m5 = _rows_to_frame(self.columns, self.m5_rows) if self.m5_rows is not None else None
        h1_last_ts_dt = pd.to_datetime(h1["timestamp"].iloc[-1])
        return MarketSnapshot(
            symbol=self.symbol,
            d1=d1,
            h1=h1,
            h1_last_ts=self.h1_last_ts,
            h1_last_ts_dt=h1_last_ts_dt,
            live_price=self.live_price,
            m5=m5,
        )

    # -- construction -------------------------------------------------------

    @classmethod
    def from_frames(
        cls,
        symbol: str,
        d1: pd.DataFrame,
        h1: pd.DataFrame,
        live_price: float,
        *,
        m5: Optional[pd.DataFrame] = None,
        h1_last_ts: Optional[float] = None,
        source: str = "synthetic",
        note: str = "",
        captured_at: Optional[str] = None,
    ) -> "InputSnapshot":
        """Build a snapshot from closed-candle frames (e.g. synthetic fixtures)."""
        columns = tuple(h1.columns)
        resolved_ts = h1_last_ts if h1_last_ts is not None else _epoch(h1["timestamp"].iloc[-1])
        return cls(
            symbol=symbol,
            columns=columns,
            d1_rows=_frame_to_rows(d1),
            h1_rows=_frame_to_rows(h1),
            m5_rows=_frame_to_rows(m5) if m5 is not None else None,
            live_price=float(live_price),
            h1_last_ts=float(resolved_ts),
            source=source,
            note=note,
            captured_at=captured_at,
        )

    @classmethod
    def from_market_snapshot(
        cls,
        snapshot: MarketSnapshot,
        *,
        source: str = "live-v1",
        note: str = "",
        captured_at: Optional[str] = None,
    ) -> "InputSnapshot":
        """Build a snapshot from a `MarketSnapshot` (e.g. one the market-data
        adapter produced from live V1 data)."""
        return cls(
            symbol=snapshot.symbol,
            columns=tuple(snapshot.h1.columns),
            d1_rows=_frame_to_rows(snapshot.d1),
            h1_rows=_frame_to_rows(snapshot.h1),
            m5_rows=_frame_to_rows(snapshot.m5) if snapshot.m5 is not None else None,
            live_price=float(snapshot.live_price),
            h1_last_ts=float(snapshot.h1_last_ts),
            source=source,
            note=note,
            captured_at=captured_at,
        )

    # -- serialization ------------------------------------------------------

    def to_json_obj(self) -> dict[str, Any]:
        return {
            "format": SNAPSHOT_FORMAT,
            "symbol": self.symbol,
            "source": self.source,
            "note": self.note,
            "captured_at": self.captured_at,
            "live_price": self.live_price,
            "h1_last_ts": self.h1_last_ts,
            "columns": list(self.columns),
            "d1_rows": self.d1_rows,
            "h1_rows": self.h1_rows,
            "m5_rows": self.m5_rows,
        }

    @classmethod
    def from_json_obj(cls, obj: dict[str, Any]) -> "InputSnapshot":
        fmt = obj.get("format")
        if fmt != SNAPSHOT_FORMAT:
            raise ValueError(f"unsupported snapshot format: {fmt!r} (expected {SNAPSHOT_FORMAT})")
        return cls(
            symbol=obj["symbol"],
            columns=tuple(obj["columns"]),
            d1_rows=list(obj["d1_rows"]),
            h1_rows=list(obj["h1_rows"]),
            m5_rows=list(obj["m5_rows"]) if obj.get("m5_rows") is not None else None,
            live_price=float(obj["live_price"]),
            h1_last_ts=float(obj["h1_last_ts"]),
            captured_at=obj.get("captured_at"),
            source=obj.get("source", "unknown"),
            note=obj.get("note", ""),
        )

    def save(self, path: Any) -> None:
        """Write the snapshot to `path` as strict, human-diffable JSON."""
        text = json.dumps(self.to_json_obj(), indent=2, ensure_ascii=False, allow_nan=False)
        Path(path).write_text(text, encoding="utf-8")

    @classmethod
    def load(cls, path: Any) -> "InputSnapshot":
        return cls.from_json_obj(json.loads(Path(path).read_text(encoding="utf-8")))
