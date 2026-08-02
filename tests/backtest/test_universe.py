"""M13 — the canonical universe resolver.

The defect this suite exists to prevent: `HistoricalMarketDataAdapter` selected
ranking candidates by the V1 name test long after Phase 0 replaced that rule in
production. On the Stage 1 corpus it admitted 121 tokenised equities,
commodities and index products into the ranked universe — up to 13 of the 30
slots — while live had excluded them since `STRATEGY_VERSION 2.1.0`.

The obvious fix is worse than the defect. Applying `is_eligible` alone deletes
every delisted instrument, because `exchangeInfo` has no entry for one: that is
9% of top-30 symbol-hours, including LUNAUSDT, MATICUSDT and EOSUSDT. So the
tests below pin *both* directions at once — non-crypto out, delisted crypto in —
because a change that satisfies only one of them is not a fix.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.data.repository import canonical_json
from backtest.universe import (
    SOURCE_SNAPSHOT,
    SOURCE_TABLE,
    Classification,
    SnapshotDisagreement,
    UniverseTableError,
    UnresolvedInstruments,
    _table_digest,
    _universe_id,
    default_table_path,
    load_table,
    read_snapshots,
    resolve,
)

CRYPTO = ("PERPETUAL", "COIN", "USDT")
EQUITY = ("TRADIFI_PERPETUAL", "EQUITY", "USDT")
INDEX = ("PERPETUAL", "INDEX", "USDT")


def _row(symbol, triple, **over):
    contract, underlying, quote = triple
    row = {
        "symbol": symbol,
        "contract_type": contract,
        "underlying_type": underlying,
        "quote_asset": quote,
        "expected_classification": (
            "CRYPTO_PERPETUAL" if triple == CRYPTO else "NOT_CRYPTO"),
        "evidence": f"{symbol} identity evidence",
        "evidence_kind": "base_asset_identity",
        "evidence_url": None,
        "confidence": "high",
        "source": "manual_curation",
        "decided_by": "test",
        "decided_at": "2026-08-01",
        "notes": "",
    }
    row.update(over)
    return row


def write_table(path: Path, rows, *, version=1, indent=2, digest=None,
                schema=1, count=None):
    document = {
        "schema_version": schema,
        "table_version": version,
        "generated_at": "2026-08-01T00:00:00Z",
        "symbol_count": len(rows) if count is None else count,
        "universe_table_sha256": (
            _table_digest(rows) if digest is None else digest),
        "rows": rows,
    }
    path.write_text(json.dumps(document, indent=indent) + "\n",
                    encoding="utf-8")
    return path


def write_snapshot(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"symbols": [
        {"symbol": symbol, "contractType": triple[0],
         "underlyingType": triple[1], "quoteAsset": triple[2]}
        for symbol, triple in entries.items()]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ResolverCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.corpus = self.root / "corpus"
        (self.corpus / "raw" / "discovery").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def snapshot(self, entries, name="20260801T000000Z-exchange_info.json"):
        return write_snapshot(
            self.corpus / "raw" / "discovery" / name, entries)

    def table(self, rows, **kw):
        return write_table(self.root / "table.json", rows, **kw)


# ==========================================================================
# Resolution precedence
# ==========================================================================

class TestResolution(ResolverCase):

    def test_snapshot_classification(self):
        self.snapshot({"BTCUSDT": CRYPTO, "TSLAUSDT": EQUITY})
        table = self.table([])
        result = resolve(["BTCUSDT", "TSLAUSDT"], corpus_root=self.corpus,
                         table_path=table)
        by = result.by_symbol()
        self.assertEqual(by["BTCUSDT"].source, SOURCE_SNAPSHOT)
        self.assertTrue(by["BTCUSDT"].eligible)
        self.assertFalse(by["TSLAUSDT"].eligible)
        self.assertEqual(result.eligible_symbols, frozenset({"BTCUSDT"}))

    def test_table_classification(self):
        self.snapshot({"BTCUSDT": CRYPTO})
        table = self.table([_row("LUNAUSDT", CRYPTO), _row("FOOTBALLUSDT", INDEX)])
        result = resolve(["BTCUSDT", "LUNAUSDT", "FOOTBALLUSDT"],
                         corpus_root=self.corpus, table_path=table)
        by = result.by_symbol()
        self.assertEqual(by["LUNAUSDT"].source, SOURCE_TABLE)
        self.assertTrue(by["LUNAUSDT"].eligible)
        self.assertFalse(by["FOOTBALLUSDT"].eligible)

    def test_snapshot_takes_precedence_over_the_table(self):
        # The table is a backfill, never an override. If a snapshot ever
        # captured the instrument, the exchange's own answer wins.
        self.snapshot({"XUSDT": EQUITY})
        table = self.table([_row("XUSDT", CRYPTO)])
        result = resolve(["XUSDT"], corpus_root=self.corpus, table_path=table)
        entry = result.by_symbol()["XUSDT"]
        self.assertEqual(entry.source, SOURCE_SNAPSHOT)
        self.assertEqual(entry.underlying_type, "EQUITY")
        self.assertFalse(entry.eligible)

    def test_an_unresolved_symbol_is_a_hard_failure_naming_itself(self):
        self.snapshot({"BTCUSDT": CRYPTO})
        table = self.table([])
        with self.assertRaises(UnresolvedInstruments) as caught:
            resolve(["BTCUSDT", "GHOSTUSDT", "OTHERUSDT"],
                    corpus_root=self.corpus, table_path=table)
        self.assertEqual(caught.exception.symbols, ("GHOSTUSDT", "OTHERUSDT"))
        self.assertIn("GHOSTUSDT", str(caught.exception))

    def test_neither_default_is_taken(self):
        # The failure above is the whole point: excluding would be survivorship
        # bias, including would admit non-crypto. Assert no result is produced.
        self.snapshot({})
        table = self.table([])
        with self.assertRaises(UnresolvedInstruments):
            resolve(["ANYUSDT"], corpus_root=self.corpus, table_path=table)

    def test_snapshot_disagreement_is_a_hard_failure(self):
        self.snapshot({"XUSDT": CRYPTO}, name="a-exchange_info.json")
        self.snapshot({"XUSDT": EQUITY}, name="b-exchange_info.json")
        table = self.table([])
        with self.assertRaises(SnapshotDisagreement) as caught:
            resolve(["XUSDT"], corpus_root=self.corpus, table_path=table)
        self.assertEqual(caught.exception.symbol, "XUSDT")
        self.assertEqual(len(caught.exception.variants), 2)

    def test_agreeing_snapshots_are_not_a_disagreement(self):
        self.snapshot({"XUSDT": CRYPTO}, name="a-exchange_info.json")
        self.snapshot({"XUSDT": CRYPTO}, name="b-exchange_info.json")
        table = self.table([])
        result = resolve(["XUSDT"], corpus_root=self.corpus, table_path=table)
        self.assertTrue(result.by_symbol()["XUSDT"].eligible)

    def test_all_snapshots_are_read_not_only_the_newest(self):
        # A symbol delisted between two captures is classified by the earlier
        # one. This is what keeps the curated table closed-ended.
        self.snapshot({"OLDUSDT": CRYPTO}, name="a-exchange_info.json")
        self.snapshot({"NEWUSDT": CRYPTO}, name="b-exchange_info.json")
        table = self.table([])
        result = resolve(["OLDUSDT", "NEWUSDT"], corpus_root=self.corpus,
                         table_path=table)
        self.assertEqual(
            {c.source for c in result.classifications}, {SOURCE_SNAPSHOT})

    def test_every_symbol_resolves_exactly_once(self):
        self.snapshot({"BTCUSDT": CRYPTO})
        table = self.table([])
        result = resolve(["BTCUSDT"] * 5, corpus_root=self.corpus,
                         table_path=table)
        self.assertEqual(len(result.classifications), 1)

    def test_a_partial_snapshot_entry_does_not_silently_exclude(self):
        # An entry missing underlyingType would compute eligible=False. Treating
        # it as classified would silently drop the instrument, so it must fall
        # through to the table — or fail.
        path = self.corpus / "raw" / "discovery" / "x-exchange_info.json"
        path.write_text(json.dumps({"symbols": [
            {"symbol": "XUSDT", "contractType": "PERPETUAL",
             "quoteAsset": "USDT"}]}), encoding="utf-8")
        table = self.table([])
        with self.assertRaises(UnresolvedInstruments):
            resolve(["XUSDT"], corpus_root=self.corpus, table_path=table)

    def test_commit_sidecars_are_not_mistaken_for_snapshots(self):
        self.snapshot({"BTCUSDT": CRYPTO})
        sidecar = (self.corpus / "reference" / "instruments"
                   / "exchangeInfo-abc.json.meta.json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"sha256": "x"}), encoding="utf-8")
        found = read_snapshots(self.corpus)
        self.assertIn("BTCUSDT", found)

    def test_the_reference_tier_is_read_too(self):
        write_snapshot(
            self.corpus / "reference" / "instruments" / "exchangeInfo-a.json",
            {"BTCUSDT": CRYPTO})
        self.assertIn("BTCUSDT", read_snapshots(self.corpus))

    def test_an_unreadable_snapshot_is_not_treated_as_absent(self):
        bad = self.corpus / "raw" / "discovery" / "z-exchange_info.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(Exception):
            read_snapshots(self.corpus)

    def test_resolution_requires_no_network(self):
        # Everything comes from committed bytes. Nothing in this module imports
        # requests, and the resolver is exercised above with no server anywhere.
        import backtest.universe as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("import requests", "urlopen", "http://", "https://f"):
            self.assertNotIn(forbidden, source)


# ==========================================================================
# universe_id
# ==========================================================================

class TestUniverseIdentity(ResolverCase):

    def _resolve(self, *, rows=None, entries=None, **kw):
        self.snapshot(entries if entries is not None else {"BTCUSDT": CRYPTO})
        table = self.table(rows if rows is not None else
                           [_row("LUNAUSDT", CRYPTO)], **kw)
        return resolve(["BTCUSDT", "LUNAUSDT"], corpus_root=self.corpus,
                       table_path=table)

    def test_it_is_deterministic(self):
        first = self._resolve()
        second = resolve(["BTCUSDT", "LUNAUSDT"], corpus_root=self.corpus,
                         table_path=self.root / "table.json")
        self.assertEqual(first.universe_id, second.universe_id)
        self.assertEqual(len(first.universe_id), 16)

    def test_it_ignores_row_ordering(self):
        rows = [_row("AUSDT", CRYPTO), _row("BUSDT", CRYPTO),
                _row("CUSDT", INDEX)]
        self.snapshot({"BTCUSDT": CRYPTO})
        names = ["BTCUSDT", "AUSDT", "BUSDT", "CUSDT"]
        forward = resolve(names, corpus_root=self.corpus,
                          table_path=self.table(rows))
        backward = resolve(names, corpus_root=self.corpus,
                           table_path=self.table(list(reversed(rows))))
        self.assertEqual(forward.universe_id, backward.universe_id)
        self.assertEqual(forward.table_sha256, backward.table_sha256)

    def test_it_ignores_json_formatting(self):
        rows = [_row("LUNAUSDT", CRYPTO)]
        self.snapshot({"BTCUSDT": CRYPTO})
        compact = resolve(["BTCUSDT", "LUNAUSDT"], corpus_root=self.corpus,
                          table_path=self.table(rows, indent=None))
        pretty = resolve(["BTCUSDT", "LUNAUSDT"], corpus_root=self.corpus,
                         table_path=self.table(rows, indent=8))
        self.assertEqual(compact.universe_id, pretty.universe_id)

    def test_it_ignores_the_order_symbols_are_requested_in(self):
        self.snapshot({"BTCUSDT": CRYPTO, "ETHUSDT": CRYPTO})
        table = self.table([])
        a = resolve(["BTCUSDT", "ETHUSDT"], corpus_root=self.corpus,
                    table_path=table)
        b = resolve(["ETHUSDT", "BTCUSDT"], corpus_root=self.corpus,
                    table_path=table)
        self.assertEqual(a.universe_id, b.universe_id)

    def test_changed_metadata_changes_it(self):
        self.snapshot({"BTCUSDT": CRYPTO})
        crypto = resolve(["BTCUSDT", "XUSDT"], corpus_root=self.corpus,
                         table_path=self.table([_row("XUSDT", CRYPTO)]))
        index = resolve(["BTCUSDT", "XUSDT"], corpus_root=self.corpus,
                        table_path=self.table([_row("XUSDT", INDEX)]))
        self.assertNotEqual(crypto.universe_id, index.universe_id)
        self.assertNotEqual(crypto.eligible_symbols, index.eligible_symbols)

    def test_a_different_symbol_set_changes_it(self):
        self.snapshot({"BTCUSDT": CRYPTO, "ETHUSDT": CRYPTO})
        table = self.table([])
        one = resolve(["BTCUSDT"], corpus_root=self.corpus, table_path=table)
        two = resolve(["BTCUSDT", "ETHUSDT"], corpus_root=self.corpus,
                      table_path=table)
        self.assertNotEqual(one.universe_id, two.universe_id)

    def test_changed_evidence_text_alone_does_not_change_eligibility(self):
        self.snapshot({"BTCUSDT": CRYPTO})
        plain = resolve(["BTCUSDT", "LUNAUSDT"], corpus_root=self.corpus,
                        table_path=self.table([_row("LUNAUSDT", CRYPTO)]))
        edited = resolve(
            ["BTCUSDT", "LUNAUSDT"], corpus_root=self.corpus,
            table_path=self.table([_row(
                "LUNAUSDT", CRYPTO,
                evidence="a much fuller citation added during review",
                evidence_url="https://example.invalid/announcement",
                notes="reviewed 2026-08-02")]))
        self.assertEqual(plain.eligible_symbols, edited.eligible_symbols)
        for symbol in plain.by_symbol():
            self.assertEqual(plain.by_symbol()[symbol].eligible,
                             edited.by_symbol()[symbol].eligible)

    def test_the_hash_input_is_the_documented_one(self):
        entry = Classification("XUSDT", *CRYPTO, source=SOURCE_TABLE)
        expected = hashlib.sha256(canonical_json({
            "universe_definition_version": 1,
            "table_version": 7,
            "universe_table_sha256": "deadbeef",
            "resolved": {"XUSDT": ["PERPETUAL", "COIN", "USDT", "table"]},
        }).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(
            _universe_id(table_version=7, table_sha256="deadbeef",
                         classifications=(entry,)),
            expected)


# ==========================================================================
# Table validation
# ==========================================================================

class TestTableValidation(ResolverCase):

    def test_a_digest_that_does_not_match_the_rows_is_refused(self):
        path = self.table([_row("XUSDT", CRYPTO)], digest="0" * 64)
        with self.assertRaises(UniverseTableError) as caught:
            load_table(path)
        self.assertIn("digest", str(caught.exception))

    def test_an_expected_classification_that_disagrees_is_refused(self):
        path = self.table([_row("XUSDT", CRYPTO,
                                expected_classification="NOT_CRYPTO")])
        with self.assertRaises(UniverseTableError) as caught:
            load_table(path)
        self.assertIn("expected_classification", str(caught.exception))

    def test_a_duplicated_symbol_is_refused(self):
        path = self.table([_row("XUSDT", CRYPTO), _row("XUSDT", INDEX)])
        with self.assertRaises(UniverseTableError):
            load_table(path)

    def test_a_missing_required_field_is_refused(self):
        path = self.table([_row("XUSDT", CRYPTO, evidence="")])
        with self.assertRaises(UniverseTableError) as caught:
            load_table(path)
        self.assertIn("evidence", str(caught.exception))

    def test_an_unknown_schema_version_is_refused(self):
        path = self.table([_row("XUSDT", CRYPTO)], schema=99)
        with self.assertRaises(UniverseTableError):
            load_table(path)

    def test_a_wrong_symbol_count_is_refused(self):
        path = self.table([_row("XUSDT", CRYPTO)], count=7)
        with self.assertRaises(UniverseTableError):
            load_table(path)

    def test_a_missing_table_is_refused(self):
        with self.assertRaises(UniverseTableError):
            load_table(self.root / "absent.json")


# ==========================================================================
# The committed table
# ==========================================================================

class TestCommittedTable(unittest.TestCase):

    def test_it_loads_and_validates(self):
        table = load_table()
        self.assertGreater(len(table), 0)
        self.assertEqual(table.version, 1)

    def test_every_row_stores_metadata_and_never_a_verdict(self):
        document = json.loads(
            default_table_path().read_text(encoding="utf-8"))
        for row in document["rows"]:
            for forbidden in ("eligible", "include", "in_universe"):
                self.assertNotIn(forbidden, row)

    def test_every_row_carries_evidence(self):
        document = json.loads(
            default_table_path().read_text(encoding="utf-8"))
        for row in document["rows"]:
            self.assertTrue(row["evidence"].strip(), row["symbol"])
            self.assertIn(row["confidence"], ("high", "medium", "low"))

    def test_ambiguous_instruments_are_backed_by_an_exchange_announcement(self):
        # Tier B: naming heuristics are never sufficient. FOOTBALLUSDT reached
        # the top-30 and is a fan-token index, which no name rule reveals.
        tier_b = {"FOOTBALLUSDT", "DOTECOUSDT", "BLUEBIRDUSDT", "BTCSTUSDT"}
        rows = {r["symbol"]: r for r in json.loads(
            default_table_path().read_text(encoding="utf-8"))["rows"]}
        for symbol in tier_b:
            self.assertIn(symbol, rows)
            self.assertEqual(rows[symbol]["evidence_kind"],
                             "exchange_announcement", symbol)
            self.assertTrue(rows[symbol]["evidence_url"], symbol)

    def test_the_known_index_products_are_not_eligible(self):
        table = load_table()
        for symbol in ("FOOTBALLUSDT", "DOTECOUSDT", "BLUEBIRDUSDT"):
            self.assertFalse(table.rows[symbol].eligible, symbol)

    def test_the_delisted_crypto_majors_remain_eligible(self):
        table = load_table()
        for symbol in ("LUNAUSDT", "MATICUSDT", "EOSUSDT", "SXPUSDT",
                       "ANCUSDT", "RNDRUSDT"):
            self.assertTrue(table.rows[symbol].eligible, symbol)


# ==========================================================================
# Injection — neither consumer classifies anything itself
# ==========================================================================

class TestInjection(unittest.TestCase):

    def test_replay_receives_an_injected_eligible_set(self):
        from backtest.replay.adapter import HistoricalMarketDataAdapter

        class FakeRepo:
            data_snapshot_id = "x"

            def symbols(self, timeframe):
                return ("BTCUSDT", "TSLAUSDT", "LUNAUSDT", "BTCUSDT_240628")

        adapter = HistoricalMarketDataAdapter(
            FakeRepo(), eligible=frozenset({"BTCUSDT", "LUNAUSDT"}))
        self.assertEqual(adapter._universe_candidates(),
                         ("BTCUSDT", "LUNAUSDT"))

    def test_replay_still_removes_dated_delivery_contracts(self):
        from backtest.replay.adapter import HistoricalMarketDataAdapter

        class FakeRepo:
            data_snapshot_id = "x"

            def symbols(self, timeframe):
                return ("BTCUSDT", "BTCUSDT_240628")

        adapter = HistoricalMarketDataAdapter(
            FakeRepo(), eligible=frozenset({"BTCUSDT", "BTCUSDT_240628"}))
        self.assertEqual(adapter._universe_candidates(), ("BTCUSDT",))

    def test_a_static_universe_still_bypasses_ranking(self):
        from backtest.replay.adapter import HistoricalMarketDataAdapter

        class FakeRepo:
            data_snapshot_id = "x"

            def symbols(self, timeframe):
                return ("BTCUSDT",)

        adapter = HistoricalMarketDataAdapter(
            FakeRepo(), static_universe=("PINNEDUSDT",),
            eligible=frozenset({"BTCUSDT"}))
        self.assertEqual(adapter._universe_candidates(), ("PINNEDUSDT",))

    def test_the_driver_forwards_the_injected_set_to_the_adapter(self):
        import inspect

        from backtest.replay import driver as module
        source = inspect.getsource(module.ReplayDriver.run)
        self.assertIn("eligible=config.eligible", source)
        # The driver forwards; it must never resolve.
        self.assertNotIn("resolve(", source)

    def test_the_replay_manifest_records_universe_id(self):
        import inspect

        from backtest.replay import driver as module
        source = inspect.getsource(module.ReplayDriver._write_manifest)
        self.assertIn('"universe_id"', source)

    def test_universe_id_is_not_in_the_c16_2_core(self):
        # C-16.2 defines `core` as a closed list, so `universe_id` is recorded
        # beside it rather than inside it. Consequence, pinned here so it cannot
        # be forgotten: two runs differing only in universe_id share a run_id.
        from backtest.replay.config import ReplayConfig
        config = ReplayConfig(
            repository_root=Path("x"), output_root=Path("y"),
            start=0, end=3600, universe_id="abc", eligible=frozenset({"A"}))
        self.assertNotIn("universe_id", config.core(data_snapshot_id="s"))
        other = ReplayConfig(
            repository_root=Path("x"), output_root=Path("y"),
            start=0, end=3600, universe_id="zzz", eligible=frozenset({"B"}))
        self.assertEqual(config.run_id(data_snapshot_id="s"),
                         other.run_id(data_snapshot_id="s"))

    def test_coverage_requires_an_injected_resolution(self):
        import inspect

        from backtest.coverage import analyze_coverage
        signature = inspect.signature(analyze_coverage)
        parameter = signature.parameters["resolution"]
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_coverage_performs_no_classification_of_its_own(self):
        # Behavioural, not prose: coverage may *describe* exchangeInfo in a
        # docstring, but it must never read one, and must never restate the
        # eligibility triple — either would be a second rule free to drift.
        import backtest.coverage as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ('exchange_info.json', '"contractType"',
                          '"underlyingType"', '"quoteAsset"', '== "COIN"'):
            self.assertNotIn(forbidden, source)

    def test_only_the_resolver_computes_eligibility(self):
        # Exactly one module under `backtest/` *calls* the rule. Others may name
        # it in a docstring; none may apply it. Live's single caller is
        # `v2/infra/market_data.py`, on the other side of the layer boundary.
        callers = sorted(
            path.as_posix() for path in Path("backtest").rglob("*.py")
            if "is_eligible(" in path.read_text(encoding="utf-8"))
        self.assertEqual(callers, ["backtest/universe.py"])


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
