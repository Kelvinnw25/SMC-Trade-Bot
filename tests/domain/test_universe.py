"""The frozen Universe Definition — `v2/domain/universe.py`.

These tests exist to make an accidental scope change fail loudly. The universe
rule decides which symbols the bot may trade and which symbols replay may rank;
a silent change to it would invalidate every comparison against the baseline
while leaving all metrics looking healthy.
"""
from __future__ import annotations

import unittest

from v2.domain.universe import (
    UNIVERSE_DEFINITION_VERSION,
    InstrumentMetadata,
    is_eligible,
    is_plausible_name,
)


def _entry(symbol, contract="PERPETUAL", underlying="COIN", quote="USDT"):
    return {"symbol": symbol, "contractType": contract,
            "underlyingType": underlying, "quoteAsset": quote}


class TestEligibility(unittest.TestCase):

    def test_a_crypto_perpetual_is_eligible(self):
        self.assertTrue(is_eligible(
            InstrumentMetadata.from_exchange_info(_entry("BTCUSDT"))))

    def test_a_tokenised_equity_is_not(self):
        # Real shape: `AAPLUSDT` is TRADIFI_PERPETUAL / EQUITY / USDT and passes
        # every name-based test.
        self.assertFalse(is_eligible(InstrumentMetadata.from_exchange_info(
            _entry("AAPLUSDT", "TRADIFI_PERPETUAL", "EQUITY"))))

    def test_index_commodity_and_premarket_are_not(self):
        for underlying in ("INDEX", "COMMODITY", "PREMARKET", "HK_EQUITY",
                           "KR_EQUITY"):
            with self.subTest(underlying=underlying):
                self.assertFalse(is_eligible(
                    InstrumentMetadata.from_exchange_info(
                        _entry("XUSDT", underlying=underlying))))

    def test_a_dated_delivery_contract_is_not(self):
        self.assertFalse(is_eligible(InstrumentMetadata.from_exchange_info(
            _entry("BTCUSDT_240628", "CURRENT_QUARTER"))))

    def test_a_non_usdt_quote_is_not(self):
        self.assertFalse(is_eligible(InstrumentMetadata.from_exchange_info(
            _entry("BTCUSDC", quote="USDC"))))

    def test_all_three_conditions_are_required(self):
        # Guards against a future contributor relaxing the rule to one field.
        for override in ({"contract": "TRADIFI_PERPETUAL"},
                         {"underlying": "EQUITY"},
                         {"quote": "USDC"}):
            with self.subTest(**override):
                self.assertFalse(is_eligible(
                    InstrumentMetadata.from_exchange_info(
                        _entry("XUSDT", **override))))

    def test_an_unclassified_symbol_is_not_eligible(self):
        # A delisted symbol has no exchangeInfo entry. Unknown is never treated
        # as permitted, and never guessed.
        metadata = InstrumentMetadata(symbol="ANCUSDT")
        self.assertFalse(metadata.classified)
        self.assertFalse(is_eligible(metadata))


class TestNamePreFilter(unittest.TestCase):
    """`is_plausible_name` is a pre-filter, never the rule."""

    def test_it_drops_dated_contracts_and_foreign_quotes(self):
        self.assertFalse(is_plausible_name("BTCUSDT_240628"))
        self.assertFalse(is_plausible_name("BTCUSDC"))
        self.assertTrue(is_plausible_name("BTCUSDT"))

    def test_it_is_not_sufficient_on_its_own(self):
        # The whole reason the rule moved to metadata: these pass the name test.
        for symbol in ("AAPLUSDT", "AMZNUSDT", "BTCDOMUSDT"):
            with self.subTest(symbol=symbol):
                self.assertTrue(is_plausible_name(symbol))


class TestFrozenSurface(unittest.TestCase):
    """Changing any of this is a production change requiring a version bump."""

    def test_the_definition_version_is_pinned(self):
        self.assertEqual(UNIVERSE_DEFINITION_VERSION, 1)

    def test_strategy_version_reflects_the_universe_freeze(self):
        from v2.app.ledger import STRATEGY_VERSION

        # 2.1.0 was the Phase 0 freeze; 2.2.0 adds finding A1's recency test to
        # replay's C-6.9 reconstruction. Both change which symbols are scanned.
        self.assertEqual(STRATEGY_VERSION, "2.2.0")

    def test_replay_and_backtest_agree_on_strategy_version(self):
        # They drifted once: manifests said "v2.0" while the ledger rows inside
        # them said "2.0.1". One definition makes that unrepresentable.
        from backtest.lineage import STRATEGY_VERSION as replay_version
        from v2.app.ledger import STRATEGY_VERSION as live_version

        self.assertEqual(replay_version, live_version)


class TestLiveSourceAppliesTheRule(unittest.TestCase):
    """The production change: the live universe is metadata-filtered."""

    def setUp(self):
        from v2.infra.market_data import BinanceMarketDataSource

        self.source_class = BinanceMarketDataSource
        self._saved = BinanceMarketDataSource._eligible
        BinanceMarketDataSource._eligible = None

    def tearDown(self):
        self.source_class._eligible = self._saved

    def test_an_ineligible_symbol_is_excluded_even_at_the_top_of_volume(self):
        source = self.source_class()
        self.source_class._eligible = frozenset({"BTCUSDT", "ETHUSDT"})

        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url

            class Response:
                @staticmethod
                def json():
                    return [
                        {"symbol": "AAPLUSDT", "quoteVolume": "9e9"},
                        {"symbol": "BTCUSDT", "quoteVolume": "5e9"},
                        {"symbol": "ETHUSDT", "quoteVolume": "1e9"},
                    ]
            return Response()

        import v2.infra.market_data as module
        original, module.requests.get = module.requests.get, fake_get
        try:
            coins = source.get_top_volume_coins(limit=10)
        finally:
            module.requests.get = original

        self.assertEqual([c["symbol"] for c in coins],
                         ["BTC/USDT", "ETH/USDT"])

    def test_an_undetermined_universe_yields_nothing_rather_than_everything(self):
        # A transient exchangeInfo failure must not silently widen the traded
        # universe back to the name test.
        source = self.source_class()

        def fail(url, **kwargs):
            raise OSError("network down")

        import v2.infra.market_data as module
        original, module.requests.get = module.requests.get, fail
        try:
            self.assertIsNone(source.eligible_symbols())
            self.assertEqual(source.get_top_volume_coins(limit=10), [])
        finally:
            module.requests.get = original


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
