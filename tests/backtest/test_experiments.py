"""Phase 3.5 experiment infrastructure: identity, config, tier-0.

Three properties carry the weight here, and each has a test that fails loudly if
it stops holding:

1. `identity` must *detect* drift, not merely tolerate it. A test that only ever
   asserts "passes on good input" would pass against a harness that always
   returns True, so the mutation cases matter more than the happy path.
2. `variant_id` must ignore metadata and honour strategy inputs. Both directions
   are asserted; one alone is satisfiable by a constant.
3. Tier-0's identity rule must promote nothing. That is the whole validation
   argument, and it is asserted directly rather than inferred from a delta.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.experiments.config import (
    CONTROL_VARIANT_ID,
    ExperimentConfig,
    ExperimentConfigError,
    Preregistration,
    load_config,
    load_preregistration,
    variant_id,
    variant_version,
)
from backtest.experiments.identity import rescore_ledger
from backtest.experiments.tier0 import (
    AdmissionRule,
    Clause,
    Occupancy,
    Thesis,
    Tier0Error,
    load_occupancy,
    simulate,
)
from backtest.outcome.record import OutcomeRecord

RUN_ID = "replay-test0000000000"
SNAPSHOT = "c8542cb360d13207"
H1 = 3600
BASE = 1672617600


# ===========================================================================
# fixtures
# ===========================================================================

def _row(**over) -> dict:
    """A ledger row that reaches the score engine, with 2.2.0's own outputs.

    The recorded score/decision/notes are *not* hand-computed: the scoring
    fixtures elsewhere pin the engine, and here the row is built by asking the
    engine what it produces, so this file cannot drift from the engine while
    still appearing to test it. What it pins is the reconstruction — the mapping
    from ledger fields back into the engine's three dicts.
    """
    from v2.analysis.scoring import score_signal

    row = {
        "schema_version": 2, "strategy_version": "2.2.0",
        "recorded_at": float(BASE), "symbol": "AAAUSDT",
        "h1_last_ts": float(BASE - H1), "run_id": RUN_ID, "cycle_index": 1,
        "universe_rank": 1,
        "entry_mode": "RETRACEMENT", "regime": "expansion_up",
        "session_active": True, "live_price": 100.0,
        "direction": "LONG", "reason": None,
        "weekly_bias": "BULLISH", "d1_bias": "BULLISH", "h4_bias": "BULLISH",
        "location": "premium", "momentum_weakening": False,
        "anchor_override": False, "veto_active": False,
        "sweep": True, "fvg": True, "displacement": True, "fresh_ob": True,
        "volume_spike": True, "target_clear": True, "htf_context_clean": False,
        "entry_ready": True, "entry_price": 100.0, "sl": 99.0, "tp": 102.5,
        "entry_source": "retracement_ob", "tp_source": "SWING_HIGH",
        "tp_rr": 2.5, "m5_choch": None, "atr_last": 1.0,
    }
    row.update(over)

    htf = {k: row.get(k) for k in ("weekly_bias", "d1_bias", "h4_bias",
                                   "location", "momentum_weakening")}
    setup = {
        "direction": row["direction"], "entry_mode": row["entry_mode"],
        "entry_source": row["entry_source"], "sweep_valid": row["sweep"],
        "fvg_valid": row["fvg"], "displacement_valid": row["displacement"],
        "fresh_ob": row["fresh_ob"], "volume_spike": row["volume_spike"],
        "target_clear": row["target_clear"],
        "session_active": row["session_active"],
        "htf_context_clean": row["htf_context_clean"],
        "entry_price": row["entry_price"], "sl": row["sl"], "tp": row["tp"],
        "tp_source": row["tp_source"], "tp_rr": row["tp_rr"],
    }
    m5 = (None if row["m5_choch"] is None
          else {"choch_confirmed": row["m5_choch"]})
    result = score_signal(htf, row["regime"], setup, m5)

    row["score"] = int(result.get("score") or 0)
    row["decision"] = str(result.get("decision") or "SKIP")
    row["notes"] = list(result.get("notes") or [])
    row["rr"] = result.get("rr")
    return row


def _write_ledger(directory: Path, rows: list[dict]) -> Path:
    path = directory / "signal_ledger.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8")
    return path


def _outcome(index: int, *, realized_r=1.0, shadow_kind=None, symbol="AAAUSDT",
             direction="LONG", cycle=0, rank=1, signal_id=None,
             in_burn_in=False, exit_offset=4) -> OutcomeRecord:
    boundary = BASE + cycle * H1
    return OutcomeRecord(
        run_id=RUN_ID, source="replay",
        thesis_key=f"{symbol}|{direction}|RETRACEMENT", symbol=symbol,
        direction=direction, entry_mode="RETRACEMENT", boundary=boundary,
        h1_last_ts=boundary - H1, cycle_index=cycle, universe_rank=rank,
        alert_tier="A+", shadow_kind=shadow_kind, in_burn_in=in_burn_in,
        entry_price=100.0, sl=99.0, tp=102.0, structural_rr=2.0,
        tp_source="SWING_HIGH", entry_source="retracement_ob", score=12,
        filled=True, fill_kind="LIMIT_TOUCH", fill_T=boundary + H1,
        fill_price=100.0, candles_to_fill=1,
        state="WIN" if realized_r > 0 else "LOSS", cancel_reason=None,
        exit_reason="TP" if realized_r > 0 else "SL",
        exit_T=boundary + exit_offset * H1,
        exit_price=102.0, realized_r=realized_r, realized_r_net=realized_r,
        holding_candles=exit_offset, mae_r=-0.4, mfe_r=1.2,
        excursion_degraded=False, contract_version="1.1",
        strategy_version="2.2.0", ledger_schema_version=2,
        outcome_schema_version=2, outcome_config_hash="39761eb79ae5432e",
        data_snapshot_id=SNAPSHOT, env={"python": "3.13.5"}, burn_in_h1=0,
        universe_mode="volume", signal_id=signal_id or f"{symbol}|RETRACEMENT|{index}",
    )


# ===========================================================================
# identity
# ===========================================================================

class ScoringIdentityTests(unittest.TestCase):

    def _report(self, rows):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_ledger(directory, rows)
            return rescore_ledger(directory)

    def test_a_faithful_ledger_reproduces_itself(self):
        report = self._report([_row(), _row(direction="SHORT",
                                           weekly_bias="BEARISH",
                                           d1_bias="BEARISH",
                                           h4_bias="BEARISH",
                                           location="discount",
                                           entry_price=100.0, sl=101.0,
                                           tp=97.5)])
        self.assertTrue(report.passed)
        self.assertEqual(report.compared, 2)
        self.assertEqual(report.mismatch_count, 0)

    def test_a_changed_score_is_detected(self):
        row = _row()
        row["score"] = int(row["score"]) + 1
        report = self._report([row])
        self.assertFalse(report.passed)
        self.assertEqual([m.field_name for m in report.mismatches], ["score"])

    def test_a_changed_decision_is_detected(self):
        row = _row()
        row["decision"] = "A+" if row["decision"] != "A+" else "WATCH"
        report = self._report([row])
        self.assertFalse(report.passed)
        self.assertIn("decision", [m.field_name for m in report.mismatches])

    def test_a_dropped_note_is_detected(self):
        row = _row()
        self.assertTrue(row["notes"])
        row["notes"] = row["notes"][:-1]
        report = self._report([row])
        self.assertFalse(report.passed)
        self.assertIn("notes", [m.field_name for m in report.mismatches])

    def test_the_choppy_penalty_is_reversed_not_recomputed(self):
        """A choppy row records score-2 and a trailing note; both must reverse."""
        clean = _row()
        choppy = _row()
        choppy["score"] = int(choppy["score"]) - 2
        choppy["notes"] = list(choppy["notes"]) + ["choppy_penalty"]
        report = self._report([clean, choppy])
        self.assertTrue(report.passed, report.to_dict()["mismatches"])
        self.assertEqual(report.compared, 2)

    def test_an_m5_enriched_row_is_rescored_with_its_flag(self):
        enriched = _row(m5_choch=True)
        plain = _row(m5_choch=None)
        # The flag has to change something, or this test proves nothing.
        self.assertNotEqual(enriched["score"], plain["score"])
        self.assertTrue(self._report([enriched]).passed)

    def test_pre_scoring_exits_are_classified_and_never_compared(self):
        rows = [
            _row(),
            {**_row(), "direction": None, "score": 0, "decision": "SKIP",
             "notes": [], "rr": None},
            {**_row(), "veto_active": True, "score": 0, "decision": "SKIP",
             "notes": [], "rr": None},
            {**_row(), "entry_ready": False, "score": 0, "decision": "SKIP",
             "notes": [], "rr": None},
            {**_row(), "weekly_bias": None, "d1_bias": None, "score": 0,
             "decision": "SKIP", "notes": [], "rr": None},
        ]
        report = self._report(rows)
        self.assertTrue(report.passed)
        self.assertEqual(report.compared, 1)
        self.assertEqual(report.skipped, 4)
        self.assertEqual(
            report.census.exits,
            {"no_direction": 1, "veto": 1, "not_ready": 1, "invalid_htf": 1})

    def test_a_watch_only_row_is_scored_even_when_not_ready(self):
        """WATCH_ONLY does not take the not-ready exit (scoring.py:757)."""
        report = self._report([_row(entry_mode="WATCH_ONLY", entry_ready=False)])
        self.assertEqual(report.compared, 1)
        self.assertEqual(report.skipped, 0)

    def test_the_census_reports_watched_branches_that_never_fire(self):
        report = self._report([_row()])
        census = report.census.to_dict()
        # `discount` cannot fire for a LONG in premium, and a zero must be
        # *reported* rather than absent — that is what makes it evidence.
        self.assertEqual(census["counts"]["discount"], 0)
        self.assertIn("discount", census["never_fired_in_this_dataset"])
        self.assertEqual(census["counts"]["d1_aligned"], 1)

    def test_the_census_denominator_excludes_pre_scoring_exits(self):
        report = self._report([_row(), {**_row(), "direction": None, "score": 0,
                                       "decision": "SKIP", "notes": [],
                                       "rr": None}])
        self.assertEqual(report.census.denominator, 1)

    def test_the_report_carries_its_own_scope(self):
        payload = self._report([_row()]).to_dict()
        scope = payload["scope"]
        self.assertTrue(scope["proves"])
        # The limits must name the stages this check cannot see, and must point
        # at the thing that does cover them.
        self.assertTrue(any("htf" in s for s in scope["does_not_prove"]))
        self.assertIn("replay", " ".join(scope).lower()
                      + scope["only_a_full_replay_covers"])

    def test_an_empty_population_does_not_pass(self):
        """Zero compared rows must not read as success."""
        report = self._report([{**_row(), "direction": None, "score": 0,
                                "decision": "SKIP", "notes": [], "rr": None}])
        self.assertFalse(report.passed)

    def test_a_missing_ledger_is_an_error(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                rescore_ledger(Path(tmp))


# ===========================================================================
# config / preregistration
# ===========================================================================

class VariantIdentityTests(unittest.TestCase):

    def test_variant_id_is_stable_and_16_hex(self):
        first = variant_id({"b": 2, "a": 1})
        self.assertEqual(first, variant_id({"a": 1, "b": 2}))
        self.assertEqual(len(first), 16)
        int(first, 16)

    def test_metadata_does_not_change_the_variant_id(self):
        inputs = {"rr_floor": 1.2}
        a = ExperimentConfig(slug="exp1", base_strategy_version="2.2.0",
                             strategy_inputs=inputs,
                             metadata={"title": "first draft"})
        b = ExperimentConfig(slug="exp1", base_strategy_version="2.2.0",
                             strategy_inputs=inputs,
                             metadata={"title": "second draft",
                                       "author": "someone"})
        self.assertEqual(a.variant_id, b.variant_id)

    def test_a_changed_strategy_input_changes_the_variant_id(self):
        a = ExperimentConfig(slug="exp1", base_strategy_version="2.2.0",
                             strategy_inputs={"rr_floor": 1.2})
        b = ExperimentConfig(slug="exp1", base_strategy_version="2.2.0",
                             strategy_inputs={"rr_floor": 1.3})
        self.assertNotEqual(a.variant_id, b.variant_id)

    def test_the_control_is_recognised_by_its_id_not_its_label(self):
        control = ExperimentConfig(slug="control",
                                   base_strategy_version="2.2.0")
        self.assertTrue(control.is_control)
        self.assertEqual(control.variant_id, CONTROL_VARIANT_ID)
        mislabelled = ExperimentConfig(
            slug="control", base_strategy_version="2.2.0",
            strategy_inputs={"rr_floor": 9.9},
            metadata={"title": "definitely the control"})
        self.assertFalse(mislabelled.is_control)

    def test_the_version_string_carries_base_slug_and_hash(self):
        config = ExperimentConfig(slug="a1", base_strategy_version="2.2.0",
                                  strategy_inputs={"x": 1})
        expected = variant_version("2.2.0", "a1", config.variant_id)
        self.assertEqual(config.variant_version, expected)
        self.assertTrue(config.variant_version.startswith("2.2.0+exp.a1."))
        self.assertIn(config.variant_id[:8], config.variant_version)

    def test_a_rebased_variant_gets_a_different_version(self):
        inputs = {"x": 1}
        old = ExperimentConfig(slug="a1", base_strategy_version="2.2.0",
                               strategy_inputs=inputs)
        new = ExperimentConfig(slug="a1", base_strategy_version="2.3.0",
                               strategy_inputs=inputs)
        self.assertEqual(old.variant_id, new.variant_id)
        self.assertNotEqual(old.variant_version, new.variant_version)

    def test_a_slug_with_a_version_or_path_separator_is_refused(self):
        for bad in ("a+b", "a.b", "a/b", "a b", "a\\b", "a@b"):
            with self.assertRaises(ExperimentConfigError):
                ExperimentConfig(slug=bad, base_strategy_version="2.2.0")

    def test_a_hand_edited_variant_id_is_refused_on_load(self):
        config = ExperimentConfig(slug="a1", base_strategy_version="2.2.0",
                                  strategy_inputs={"x": 1})
        data = config.to_dict()
        data["strategy_inputs"] = {"x": 2}          # edited without rehashing
        with self.assertRaises(ExperimentConfigError):
            ExperimentConfig.from_dict(data)

    def test_a_config_round_trips_through_disk(self):
        with TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                slug="a1", base_strategy_version="2.2.0",
                strategy_inputs={"rr_floor": 1.2}, metadata={"why": "because"})
            path = config.write(Path(tmp) / "config.json")
            loaded = load_config(path)
            self.assertEqual(loaded.variant_id, config.variant_id)
            self.assertEqual(loaded.metadata, {"why": "because"})
            with self.assertRaises(ExperimentConfigError):
                config.write(path)


class PreregistrationTests(unittest.TestCase):

    def _prereg(self, **over) -> Preregistration:
        data = {
            "hypothesis": "dropping LONG in premium raises expectancy",
            "predicted_direction": "increase",
            "predicted_effect_r": 0.05,
            "primary_metric": "expectancy",
            "evaluation_tier": "tier0",
            "decision_rule": "promote if delta > 0 and CI excludes zero",
            "control_run_id": RUN_ID,
            "comparison_key_digest": "0880d4d7557d067f",
            "registered_at": "2026-08-08T00:00:00Z",
        }
        data.update(over)
        return Preregistration(**data)

    def test_a_valid_preregistration_has_a_stable_digest(self):
        self.assertEqual(self._prereg().digest, self._prereg().digest)
        self.assertEqual(len(self._prereg().digest), 16)

    def test_an_edited_prediction_changes_its_digest(self):
        self.assertNotEqual(self._prereg().digest,
                            self._prereg(predicted_effect_r=0.2).digest)

    def test_an_unknown_direction_or_tier_is_refused(self):
        with self.assertRaises(ExperimentConfigError):
            self._prereg(predicted_direction="up")
        with self.assertRaises(ExperimentConfigError):
            self._prereg(evaluation_tier="tier9")

    def test_the_null_is_a_permitted_prediction(self):
        self.assertEqual(self._prereg(predicted_direction="none",
                                      predicted_effect_r=0.0).predicted_direction,
                         "none")

    def test_a_blank_required_field_is_refused(self):
        for name in ("hypothesis", "primary_metric", "decision_rule",
                     "control_run_id", "comparison_key_digest"):
            with self.assertRaises(ExperimentConfigError):
                self._prereg(**{name: "  "})

    def test_it_is_written_once(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "preregistration.json"
            self._prereg().write(path)
            with self.assertRaises(ExperimentConfigError):
                self._prereg(predicted_effect_r=0.9).write(path)
            self.assertEqual(load_preregistration(path).predicted_effect_r, 0.05)

    def test_a_missing_preregistration_is_an_error(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ExperimentConfigError):
                load_preregistration(Path(tmp) / "nope.json")


# ===========================================================================
# tier 0
# ===========================================================================

class ClauseTests(unittest.TestCase):

    def test_an_unset_field_is_a_wildcard(self):
        clause = Clause(direction="LONG")
        self.assertTrue(clause.matches("LONG", {"location": "premium"}))
        self.assertTrue(clause.matches("LONG", {}))
        self.assertFalse(clause.matches("SHORT", {"location": "premium"}))

    def test_a_conjunction_needs_every_stated_field(self):
        clause = Clause(direction="LONG", location="premium")
        self.assertTrue(clause.matches("LONG", {"location": "premium"}))
        self.assertFalse(clause.matches("LONG", {"location": "discount"}))
        self.assertFalse(clause.matches("SHORT", {"location": "premium"}))

    def test_an_empty_clause_is_refused(self):
        with self.assertRaises(Tier0Error):
            Clause()

    def test_a_rule_rejects_on_any_clause(self):
        rule = AdmissionRule("two", (Clause(direction="LONG"),
                                     Clause(d1_bias="CHOPPY")))
        self.assertTrue(rule.rejects("LONG", {}))
        self.assertTrue(rule.rejects("SHORT", {"d1_bias": "CHOPPY"}))
        self.assertFalse(rule.rejects("SHORT", {"d1_bias": "BEARISH"}))
        self.assertFalse(rule.is_identity)


class Tier0SimulationTests(unittest.TestCase):
    """A 2-slot world so substitution is reachable in a handful of records."""

    def _occupancy(self, theses, cycles=6, limit=2, counts=None):
        return Occupancy(
            theses=tuple(theses), cycles=cycles,
            full_cycles=sum(1 for c in (counts or []) if c >= limit),
            slot_limit=limit, non_contiguous=0,
            active_counts=tuple(counts or [limit] * cycles))

    def test_the_identity_rule_promotes_nothing(self):
        """The validation argument, asserted directly."""
        residents = [_outcome(1, realized_r=-1.0, signal_id="s1", cycle=0),
                     _outcome(2, realized_r=-1.0, signal_id="s2", cycle=0,
                              symbol="BBBUSDT")]
        capped = [_outcome(3, realized_r=5.0, shadow_kind="CAPPED", cycle=0,
                           symbol="CCCUSDT", rank=9)]
        occupancy = self._occupancy([
            Thesis("s1", "AAAUSDT", 0, 3), Thesis("s2", "BBBUSDT", 0, 3)])
        result = simulate(residents + capped, occupancy,
                          AdmissionRule("identity"))
        self.assertEqual(result.promotions, 0)
        self.assertEqual(result.slack, 0)
        self.assertEqual(result.delta_net_r, 0.0)
        self.assertEqual(len(result.kept), 2)

    def test_rejecting_a_resident_lets_a_capped_candidate_take_the_slot(self):
        residents = [_outcome(1, realized_r=-1.0, signal_id="s1", cycle=0,
                              direction="LONG"),
                     _outcome(2, realized_r=-1.0, signal_id="s2", cycle=0,
                              symbol="BBBUSDT", direction="SHORT")]
        capped = [_outcome(3, realized_r=4.0, shadow_kind="CAPPED", cycle=0,
                           symbol="CCCUSDT", direction="SHORT", rank=9)]
        occupancy = self._occupancy([
            Thesis("s1", "AAAUSDT", 0, 3), Thesis("s2", "BBBUSDT", 0, 3)])
        result = simulate(residents + capped, occupancy,
                          AdmissionRule("no_long", (Clause(direction="LONG"),)))
        self.assertEqual(result.promotions, 1)
        self.assertEqual(result.refilled, [4.0])
        self.assertEqual(result.rejected, [-1.0])
        # Substitution is the whole point: the naive delta ignores the refill.
        self.assertEqual(result.naive_delta_net_r, 1.0)
        self.assertEqual(result.delta_net_r, 5.0)

    def test_substitution_can_make_a_filter_look_worse_than_subsetting_says(self):
        """A losing refill eats the gain from removing a loser."""
        residents = [_outcome(1, realized_r=-1.0, signal_id="s1", cycle=0,
                              direction="LONG"),
                     _outcome(2, realized_r=2.0, signal_id="s2", cycle=0,
                              symbol="BBBUSDT", direction="SHORT")]
        capped = [_outcome(3, realized_r=-3.0, shadow_kind="CAPPED", cycle=0,
                           symbol="CCCUSDT", direction="SHORT", rank=9)]
        occupancy = self._occupancy([
            Thesis("s1", "AAAUSDT", 0, 3), Thesis("s2", "BBBUSDT", 0, 3)])
        result = simulate(residents + capped, occupancy,
                          AdmissionRule("no_long", (Clause(direction="LONG"),)))
        self.assertEqual(result.naive_delta_net_r, 1.0)
        self.assertEqual(result.delta_net_r, -2.0)
        self.assertLess(result.delta_net_r, result.naive_delta_net_r)

    def test_promotion_respects_universe_rank_within_a_cycle(self):
        """Only one slot frees, so the earlier-ranked candidate must win it."""
        residents = [_outcome(1, realized_r=-1.0, signal_id="s1", cycle=0,
                              direction="LONG"),
                     _outcome(2, realized_r=1.0, signal_id="s2", cycle=0,
                              symbol="BBBUSDT", direction="SHORT")]
        capped = [
            _outcome(4, realized_r=9.0, shadow_kind="CAPPED", cycle=0,
                     symbol="ZZZUSDT", direction="SHORT", rank=12),
            _outcome(3, realized_r=7.0, shadow_kind="CAPPED", cycle=0,
                     symbol="CCCUSDT", direction="SHORT", rank=5),
        ]
        occupancy = self._occupancy([
            Thesis("s1", "AAAUSDT", 0, 3), Thesis("s2", "BBBUSDT", 0, 3)])
        result = simulate(residents + capped, occupancy,
                          AdmissionRule("no_long", (Clause(direction="LONG"),)))
        self.assertEqual(result.promotions, 1)
        # rank 5 beats rank 12 — not the larger R, which would be look-ahead.
        self.assertEqual(result.refilled, [7.0])

    def test_a_candidate_is_blocked_when_its_symbol_is_already_resident(self):
        residents = [_outcome(1, realized_r=-1.0, signal_id="s1", cycle=0,
                              direction="LONG"),
                     _outcome(2, realized_r=1.0, signal_id="s2", cycle=0,
                              symbol="BBBUSDT", direction="SHORT")]
        capped = [_outcome(3, realized_r=8.0, shadow_kind="CAPPED", cycle=0,
                           symbol="BBBUSDT", direction="SHORT", rank=9)]
        occupancy = self._occupancy([
            Thesis("s1", "AAAUSDT", 0, 3), Thesis("s2", "BBBUSDT", 0, 3)])
        result = simulate(residents + capped, occupancy,
                          AdmissionRule("no_long", (Clause(direction="LONG"),)))
        self.assertEqual(result.promotions, 0)
        self.assertEqual(result.symbol_busy, 1)

    def test_a_vacancy_the_rule_did_not_create_is_not_filled(self):
        """Observed occupancy below the cap is not spare capacity.

        A CAPPED candidate exists because the cap bound at its arrival, so with
        nothing rejected there is no slot for it even where the end-of-cycle
        snapshot shows one — the intra-cycle ordering artifact this gate closes.
        """
        residents = [_outcome(1, realized_r=1.0, signal_id="s1", cycle=0)]
        capped = [_outcome(2, realized_r=5.0, shadow_kind="CAPPED", cycle=0,
                           symbol="CCCUSDT", rank=9)]
        occupancy = self._occupancy(
            [Thesis("s1", "AAAUSDT", 0, 3)], limit=2, counts=[1] * 6)
        result = simulate(residents + capped, occupancy,
                          AdmissionRule("identity"))
        self.assertEqual(result.promotions, 0)
        self.assertEqual(result.still_capped, 1)

    def test_promoted_residency_bounds_how_many_can_be_promoted(self):
        residents = [_outcome(i, realized_r=-1.0, signal_id=f"s{i}", cycle=0,
                              symbol=f"R{i}USDT", direction="LONG")
                     for i in (1, 2)]
        capped = [_outcome(10 + c, realized_r=1.0, shadow_kind="CAPPED",
                           cycle=c, symbol=f"C{c}USDT", direction="SHORT",
                           rank=9)
                  for c in range(4)]
        occupancy = self._occupancy(
            [Thesis("s1", "R1USDT", 0, 3), Thesis("s2", "R2USDT", 0, 3)])
        rule = AdmissionRule("no_long", (Clause(direction="LONG"),))
        short = simulate(residents + capped, occupancy, rule,
                         promoted_residency_cycles=1)
        long = simulate(residents + capped, occupancy, rule,
                        promoted_residency_cycles=4)
        self.assertGreater(short.promotions, long.promotions)

    def test_burn_in_trades_hold_slots_but_contribute_no_r(self):
        residents = [_outcome(1, realized_r=-1.0, signal_id="s1", cycle=0,
                              in_burn_in=True)]
        result = simulate(
            residents,
            self._occupancy([Thesis("s1", "AAAUSDT", 0, 3)]),
            AdmissionRule("identity"))
        self.assertEqual(result.baseline, [])
        self.assertEqual(result.kept, [])
        self.assertEqual(result.resident_theses, 1)

    def test_a_label_rule_without_a_label_source_is_refused(self):
        with self.assertRaises(Tier0Error):
            simulate([], self._occupancy([]),
                     AdmissionRule("loc", (Clause(location="premium"),)))

    def test_the_result_is_deterministic(self):
        residents = [_outcome(1, realized_r=-1.0, signal_id="s1", cycle=0,
                              direction="LONG"),
                     _outcome(2, realized_r=2.0, signal_id="s2", cycle=0,
                              symbol="BBBUSDT", direction="SHORT")]
        capped = [_outcome(3, realized_r=4.0, shadow_kind="CAPPED", cycle=0,
                           symbol="CCCUSDT", direction="SHORT", rank=9)]
        occupancy = self._occupancy([
            Thesis("s1", "AAAUSDT", 0, 3), Thesis("s2", "BBBUSDT", 0, 3)])
        rule = AdmissionRule("no_long", (Clause(direction="LONG"),))
        first = simulate(residents + capped, occupancy, rule).to_dict()
        second = simulate(list(reversed(residents + capped)), occupancy,
                          rule).to_dict()
        self.assertEqual(first, second)

    def test_the_payload_says_it_is_not_a_replay_and_lists_limitations(self):
        payload = simulate([], self._occupancy([]),
                           AdmissionRule("identity")).to_dict()
        self.assertFalse(payload["is_replay"])
        self.assertTrue(payload["limitations"])
        self.assertIn("promoted_residency_cycles", payload)

    def test_a_bad_slot_limit_or_residency_is_refused(self):
        with self.assertRaises(Tier0Error):
            simulate([], self._occupancy([]), AdmissionRule("identity"),
                     slot_limit=0)
        with self.assertRaises(Tier0Error):
            simulate([], self._occupancy([]), AdmissionRule("identity"),
                     promoted_residency_cycles=0)


class OccupancyLoadingTests(unittest.TestCase):

    def _snapshots(self, directory: Path, rows) -> Path:
        path = directory / "memory_snapshots.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def test_intervals_are_reduced_from_the_per_cycle_resident_set(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._snapshots(directory, [
                {"cycle_index": 0, "active": {
                    "A/USDT": {"signal_id": "a1"}}},
                {"cycle_index": 1, "active": {
                    "A/USDT": {"signal_id": "a1"},
                    "B/USDT": {"signal_id": "b1"}}},
                {"cycle_index": 2, "active": {"B/USDT": {"signal_id": "b1"}}},
            ])
            occupancy = load_occupancy(directory, slot_limit=2)
            spans = {t.signal_id: (t.first_cycle, t.last_cycle)
                     for t in occupancy.theses}
            self.assertEqual(spans, {"a1": (0, 1), "b1": (1, 2)})
            self.assertEqual(occupancy.cycles, 3)
            self.assertEqual(occupancy.full_cycles, 1)
            self.assertEqual(occupancy.active_counts, (1, 2, 1))
            self.assertEqual(occupancy.non_contiguous, 0)

    def test_a_gap_in_a_residency_is_counted_not_hidden(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._snapshots(directory, [
                {"cycle_index": 0, "active": {"A/USDT": {"signal_id": "a1"}}},
                {"cycle_index": 1, "active": {}},
                {"cycle_index": 2, "active": {"A/USDT": {"signal_id": "a1"}}},
            ])
            self.assertEqual(load_occupancy(directory).non_contiguous, 1)

    def test_a_missing_snapshot_file_is_an_error_not_an_empty_occupancy(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(Tier0Error):
                load_occupancy(Path(tmp))


class BoundaryTests(unittest.TestCase):
    """The package is reader-only; `replay` must stay out of it."""

    def test_no_module_imports_the_replay_package(self):
        import ast

        root = Path(__file__).resolve().parents[2] / "backtest" / "experiments"
        offenders = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.startswith("backtest.replay"):
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [])

    def test_only_identity_reaches_into_v2(self):
        """Only the modules that must touch the score engine do.

        `identity` re-runs `score_signal`; `scoring_variant` builds the
        `ScoringConfig` that function takes; `anchor` derives replacement
        thresholds from the default table; `screen` applies a resolved config to
        re-decide admission. All four are deliberate exceptions and all four point
        the same way — `v2` never imports `backtest` (rule A8), which is why config
        loading, threshold derivation and variant screening live on this side of
        the boundary rather than next to the seam.

        The list that matters is the complement: `config`, `tier0`, `rescore`,
        `partition` and `phase_a` import no strategy code at all. `tier0` in
        particular must stay a pure capacity model — it takes precomputed reject
        keys rather than evaluating a score — and this test is what defends that.
        """
        import ast

        root = Path(__file__).resolve().parents[2] / "backtest" / "experiments"
        reaching = set()
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    module = node.names[0].name
                if module and (module == "v2" or module.startswith("v2.")):
                    reaching.add(path.name)
        self.assertEqual(
            reaching,
            {"anchor.py", "identity.py", "screen.py", "scoring_variant.py"})
        # The complement is the real assertion: the capacity model and the
        # decomposition arithmetic must never learn about the strategy.
        self.assertEqual(
            {p.name for p in sorted(root.glob("*.py"))} - reaching,
            {"__init__.py", "config.py", "partition.py", "phase_a.py",
             "rescore.py", "tier0.py"})


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
