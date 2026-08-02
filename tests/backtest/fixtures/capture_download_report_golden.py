"""Capture the T3a.1 golden `DownloadReport`.

Run **before** Step 3a modifies `Downloader.run`. The golden records what the
pre-change implementation produced for a fixed plan; the committed test then
asserts the post-change implementation reproduces it byte for byte.

The plan's own words: *"Capture the golden before editing — it cannot be
reconstructed afterwards."* Regenerating it from post-change code would make the
test tautological, which is why this script exists as a one-shot artifact rather
than as a fixture the suite builds on demand.

    venv/Scripts/python.exe tests/backtest/fixtures/capture_download_report_golden.py

Deterministic by construction: a `FakeClock`, an injected sleeper that never
sleeps, and a payload table keyed by URL. Nothing here reads the wall clock, the
network or the filesystem outside a temp directory.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backtest.acquire import Downloader, RawStore, SymbolInfo, plan_downloads
from backtest.acquire.storage import sha256_hex
from backtest.acquire.transport import TransportError, TransportNotFound
from v2.infra.clock import FakeClock

GOLDEN = Path(__file__).with_name("download_report_golden.json")
FIXED_NOW = datetime(2026, 3, 15, 9, 30, 0, tzinfo=timezone.utc)

#: Two symbols over a window that straddles a month boundary, so the plan mixes
#: monthly ZIPs with daily items that fall through to the REST candidate. That
#: mixture is what makes the golden exercise every status the report can carry.
SYMBOLS = [
    SymbolInfo(symbol="AAAUSDT", listing_state="TRADING", in_exchange_info=True,
               in_archive=True, onboard_date_ms=1698796800000),
    SymbolInfo(symbol="BBBUSDT", listing_state="TRADING", in_exchange_info=True,
               in_archive=True, onboard_date_ms=1698796800000),
]
START, END = date(2023, 11, 1), date(2023, 12, 3)


def _zip_for(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        rows = "\n".join(
            f"{1700000000000 + i * 3600000},1.0,2.0,0.5,1.5,10.0,"
            f"{1700000000000 + (i + 1) * 3600000 - 1},15.0,3,5.0,7.5,0"
            for i in range(24))
        archive.writestr(name[: -len(".zip")] + ".csv", rows + "\n")
    return buf.getvalue()


class GoldenTransport:
    """Serves a fixed table. Deterministic: same URL, same bytes, every run.

    Deliberately produces all four outcome statuses:

    - `downloaded` — monthly ZIPs, verified against a published checksum;
    - `empty`      — the first REST fallback answers `[]`;
    - `absent`     — one symbol's daily ZIP *and* its REST fallback are 404;
    - `failed`     — one URL raises a transport error on every attempt.
    """

    def __init__(self, plan):
        self.payloads: dict[str, bytes] = {}
        self.always_fail: set[str] = set()
        self.absent: set[str] = set()
        rest_seen = 0
        for item in plan.items:
            for candidate in item.candidates:
                base = candidate.url.split("/")[-1]
                if candidate.kind == "monthly_zip":
                    data = _zip_for(base)
                    self.payloads[candidate.url] = data
                    self.payloads[candidate.checksum_url] = (
                        f"{sha256_hex(data)}  {base}\n".encode())
                elif candidate.kind == "daily_zip":
                    self.absent.add(candidate.url)
                    self.absent.add(candidate.checksum_url)
                elif candidate.kind == "rest_json":
                    rest_seen += 1
                    if rest_seen == 1:
                        self.payloads[candidate.url] = b"[]"          # -> empty
                    elif rest_seen == 2:
                        self.always_fail.add(candidate.url)           # -> failed
                    elif rest_seen == 3:
                        self.absent.add(candidate.url)                # -> absent
                    else:
                        self.payloads[candidate.url] = json.dumps(
                            [[1700000000000, "1.0", "2.0", "0.5", "1.5", "10.0",
                              1700003599999, "15.0", 3, "5.0", "7.5", "0"]]
                        ).encode()

    def get(self, url: str) -> bytes:
        if url in self.always_fail:
            raise TransportError(f"simulated permanent fault for {url}")
        if url in self.absent or url not in self.payloads:
            raise TransportNotFound(f"404 {url}")
        return self.payloads[url]


def build_report() -> dict:
    root = Path(tempfile.mkdtemp(prefix="golden_report_"))
    try:
        store = RawStore(root)
        plan = plan_downloads(
            store, SYMBOLS, timeframes=("1h",), start=START, end=END,
            archive_evidence=True)
        report = Downloader(
            GoldenTransport(plan), store, clock=FakeClock(FIXED_NOW),
            sleeper=lambda _seconds: None,
        ).run(plan)
        payload = report.to_dict()
        # `started_at` / `finished_at` come from the injected FakeClock and so are
        # stable here, but they are provenance rather than identity everywhere
        # else in this project. Dropping them keeps the golden about ordering and
        # content, which is what Step 3a changes.
        payload.pop("started_at", None)
        payload.pop("finished_at", None)
        return payload
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    payload = build_report()
    if GOLDEN.exists():
        print(f"REFUSING to overwrite {GOLDEN.name}: the golden must be captured "
              f"once, before the change it protects.")
        return 1
    GOLDEN.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts = {k: payload[k] for k in
              ("planned", "downloaded", "empty", "absent", "failed", "retries")
              if k in payload}
    print(f"captured {GOLDEN}")
    print(f"  outcomes : {len(payload['outcomes'])}")
    print(f"  breakdown: downloaded={payload['downloaded']} "
          f"empty={payload['empty']} absent={payload['absent']} "
          f"failed={payload['failed']} retries={payload['retries']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
