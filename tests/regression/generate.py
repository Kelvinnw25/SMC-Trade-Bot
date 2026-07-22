"""Snapshot generator — capture inputs for the regression harness.

Two subcommands:

- ``live``      — fetch one symbol's closed D1/H1(/M5) from the *real* V1 stack
                  (via the market-data adapter) and save it as an `InputSnapshot`.
                  This is the documented "generate a snapshot from V1" path. It
                  performs one network round-trip; it is a manual tool, never run
                  by the test suite.
- ``synthetic`` — (re)build the deterministic, network-free fixture the tests
                  load. `build_synthetic_snapshot` is pure and importable, so the
                  fixture is reproducible and the tests can rebuild it in memory.

Examples (run from the repo root):

    python -m tests.regression.generate synthetic \
        --out tests/regression/fixtures/synthetic_short.json

    python -m tests.regression.generate live --symbol BTC/USDT \
        --out tests/regression/fixtures/btcusdt.json --m5 --golden
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from tests.regression.snapshot import FRAME_COLUMNS, InputSnapshot

_DEFAULT_SYNTHETIC = Path(__file__).resolve().parent / "fixtures" / "synthetic_short.json"


# --------------------------------------------------------------------------
# Synthetic fixture (deterministic, no network)
# --------------------------------------------------------------------------

def _ohlc_frame(timestamps: pd.DatetimeIndex, closes: list[float]) -> pd.DataFrame:
    """Build a V1-shaped OHLCV+ATR frame from a close series, deterministically.

    Open follows the prior close; highs/lows bracket the body with a bounded
    oscillation; volume varies smoothly; ATR is a 14-period mean of the true
    range (NaN for the first 13 rows, exactly like real V1 data — which
    exercises the snapshot's NaN round-trip)."""
    n = len(closes)
    opens = [closes[0] - 0.2] + [closes[i - 1] for i in range(1, n)]
    highs, lows, volumes = [], [], []
    for i in range(n):
        body_hi = max(opens[i], closes[i])
        body_lo = min(opens[i], closes[i])
        highs.append(body_hi + 0.8 + 0.5 * abs(math.sin(i / 4.0)))
        lows.append(body_lo - 0.8 - 0.5 * abs(math.cos(i / 4.0)))
        volumes.append(1000.0 + (i % 10) * 50.0 + 200.0 * abs(math.sin(i / 7.0)))

    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "v": volumes,
        }
    )
    true_range = frame["high"] - frame["low"]
    frame["atr"] = true_range.rolling(14).mean()
    return frame[list(FRAME_COLUMNS)]


def build_synthetic_snapshot(symbol: str = "SYNTH/USDT") -> InputSnapshot:
    """A deterministic uptrending snapshot (no randomness, no network).

    Sized past every V1 minimum (D1 ≥ 30, H1 ≥ 50) so the full pipeline runs.
    The gentle uptrend yields stable, reproducible HTF biases and a determined
    direction — a meaningful, fixed observable target for the tests.
    """
    d1_ts = pd.date_range("2026-01-01", periods=40, freq="D")
    d1_closes = [100.0 + 1.6 * i + 5.0 * math.sin(i / 5.0) for i in range(len(d1_ts))]
    d1 = _ohlc_frame(d1_ts, d1_closes)

    h1_ts = pd.date_range("2026-02-01 00:00", periods=220, freq="h")
    h1_closes = [100.0 + 0.35 * i + 3.0 * math.sin(i / 9.0) for i in range(len(h1_ts))]
    h1 = _ohlc_frame(h1_ts, h1_closes)

    # Forming-candle live price = the closed H1 trend carried one step forward.
    live_price = h1_closes[-1] + 0.35

    return InputSnapshot.from_frames(
        symbol=symbol,
        d1=d1,
        h1=h1,
        live_price=live_price,
        source="synthetic",
        note="Deterministic uptrend fixture; regenerate with "
        "`python -m tests.regression.generate synthetic`.",
    )


# --------------------------------------------------------------------------
# Live capture (network — manual use only)
# --------------------------------------------------------------------------

def capture_live_snapshot(symbol: str, *, include_m5: bool = False) -> Optional[InputSnapshot]:
    """Fetch one closed snapshot from the real V1 stack via the adapter.

    Imported lazily so the test suite (which never calls this) does not drag in
    the exchange client. Returns None when V1 has no data for the symbol.
    """
    from datetime import datetime, timezone

    from v2.infra.market_data import MarketDataAdapter

    adapter = MarketDataAdapter.create()
    market = adapter.get_market_snapshot(symbol, include_m5=include_m5)
    if market is None:
        return None
    return InputSnapshot.from_market_snapshot(
        market,
        source="live-v1",
        note=f"Captured from live V1 for {symbol}.",
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_synthetic(args: argparse.Namespace) -> int:
    snapshot = build_synthetic_snapshot(args.symbol)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    snapshot.save(out)
    print(f"wrote synthetic snapshot -> {out}")
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    snapshot = capture_live_snapshot(args.symbol, include_m5=args.m5)
    if snapshot is None:
        print(f"no data for {args.symbol}; nothing written.")
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    snapshot.save(out)
    print(f"wrote live snapshot -> {out}")

    if args.golden:
        # Local imports so `synthetic` and `--help` never load V1 analysis.
        from tests.regression.oracle import V1Oracle
        from tests.regression.runner import save_golden

        golden_path = out.with_suffix(".golden.json")
        save_golden(golden_path, V1Oracle().analyze(snapshot.to_market_snapshot()))
        print(f"wrote golden observable -> {golden_path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.regression.generate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    synthetic = subparsers.add_parser("synthetic", help="rebuild the deterministic test fixture")
    synthetic.add_argument("--out", default=str(_DEFAULT_SYNTHETIC))
    synthetic.add_argument("--symbol", default="SYNTH/USDT")
    synthetic.set_defaults(func=_cmd_synthetic)

    live = subparsers.add_parser("live", help="capture a snapshot from the live V1 stack")
    live.add_argument("--symbol", required=True)
    live.add_argument("--out", required=True)
    live.add_argument("--m5", action="store_true", help="attach closed M5 candles")
    live.add_argument("--golden", action="store_true", help="also write the V1 golden observable")
    live.set_defaults(func=_cmd_live)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
