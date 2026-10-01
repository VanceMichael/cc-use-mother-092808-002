"""出口景气后端的全流程测试。"""

from __future__ import annotations

import json
import unittest
from dataclasses import asdict

from src.export_sentiment import (
    Actor,
    AdjustmentStatus,
    Answer,
    CommodityNode,
    ConfirmationError,
    DomainError,
    DuplicateBatchError,
    EvidenceService,
    ForecastService,
    FrameEntry,
    FrameLockedError,
    IneligibleSampleError,
    IntakeService,
    PrivacyError,
    PublishInterrupted,
    PublishService,
    PublishStage,
    ReconciliationError,
    ReconReason,
    ReconStatus,
    ReportService,
    Role,
    RoleError,
    RoundService,
    RoundState,
    RoundStateError,
    Store,
    SurveyService,
    TradeBatch,
    TradeRecord,
    effective_scenario,
)

RESEARCHER = Actor("stat-1", Role.RESEARCHER)
APPROVER = Actor("appr-1", Role.APPROVER)
REVIEWER = Actor("rev-1", Role.REVIEWER)
ASSOC_A = Actor("assoc-a", Role.ASSOCIATION)
ASSOC_B = Actor("assoc-b", Role.ASSOCIATION)

ENTRIES = (
    FrameEntry("ENT-ALPHA", "A-U1", 1.0),
    FrameEntry("ENT-ALPHA", "A-U2", 1.0),
    FrameEntry("ENT-ALPHA", "A-U3", 1.0),
    FrameEntry("ENT-BETA", "B-U1", 2.0),
    FrameEntry("ENT-GAMMA", "G-U1", 1.0),
    FrameEntry("ENT-DELTA", "D-U1", 1.0),
    FrameEntry("ENT-ECHO", "E-U1", 1.0),
)
ALLOTMENTS = {
    "ENT-ALPHA": 30.0,
    "ENT-BETA": 30.0,
    "ENT-GAMMA": 20.0,
    "ENT-DELTA": 20.0,
    "ENT-ECHO": 10.0,
}

Q2_RECORDS = (
    TradeRecord(1, "EL-IC", "中国内地", 500.0, "件", 500.0, "HKD"),
    TradeRecord(2, "EL-PH", "美国", 100.0, "件", 100.0, "HKD"),
    TradeRecord(3, "TX", "欧盟", 400.0, "千克", 400.0, "HKD"),
)
Q3_RECORDS = (
    TradeRecord(1, "EL-IC", "中国内地", 900.0, "件", 900.0, "HKD"),
    TradeRecord(2, "EL-PH", "美国", 150.0, "件", 150.0, "HKD"),
    TradeRecord(3, "TX", "欧盟", 400.0, "千克", 400.0, "HKD"),
)


def build_store() -> Store:
    """两套轮次：R-Q2 承载基期批次，R-Q3 走完整流程（未截止）。"""
    store = Store()
    rounds = RoundService(store)
    intake = IntakeService(store)
    survey = SurveyService(store)
    forecast = ForecastService(store)

    intake.register_commodity(CommodityNode("EL", "电子产品", None, "件", "HKD", high_value=True))
    intake.register_commodity(CommodityNode("EL-IC", "集成电路", "EL", "件", "HKD"))
    intake.register_commodity(CommodityNode("EL-PH", "智能手机", "EL", "件", "HKD"))
    intake.register_commodity(CommodityNode("TX", "纺织品", None, "千克", "HKD"))

    rounds.create_round("R-Q2", "2026-Q2", "2026-Q1")
    rounds.prepare_frame("R-Q2", (FrameEntry("ENT-ALPHA", "A-U1", 1.0),), 50.0)
    rounds.lock_frame("R-Q2", {"ENT-ALPHA": 1.0})
    intake.import_batch(TradeBatch("B-Q2", "2026-Q2", Q2_RECORDS))

    rounds.create_round("R-Q3", "2026-Q3", "2026-Q2")
    rounds.prepare_frame("R-Q3", ENTRIES, 50.0)
    rounds.lock_frame("R-Q3", ALLOTMENTS)

    survey.submit("R-Q3", "ENT-ALPHA", "A-U1", Answer.UP, Answer.UP)
    survey.submit("R-Q3", "ENT-ALPHA", "A-U2", Answer.FLAT, Answer.UP)
    survey.submit("R-Q3", "ENT-ALPHA", "A-U3", Answer.UP, Answer.FLAT)
    survey.submit("R-Q3", "ENT-BETA", "B-U1", Answer.UP, Answer.UP)
    survey.submit("R-Q3", "ENT-GAMMA", "G-U1", Answer.UP, Answer.FLAT)
    survey.submit("R-Q3", "ENT-DELTA", "D-U1", Answer.DOWN, Answer.FLAT)

    intake.import_batch(TradeBatch("B-Q3", "2026-Q3", Q3_RECORDS))

    forecast.propose(
        "R-Q3",
        "S-Q3",
        42.0,
        47.0,
        ("人工智能服务器需求旺盛", "主要市场补库存"),
        ("件数与货值信号背离", "重量数据走弱"),
        "2026-H2",
        RESEARCHER,
    )
    forecast.confirm("S-Q3", APPROVER)
    return store


def close_and_publish(store: Store, round_id: str = "R-Q3") -> None:
    rounds = RoundService(store)
    if store.rounds[round_id].state is RoundState.COLLECTING:
        rounds.close_round(round_id)
    PublishService(store).publish(round_id)


class FrameLockTest(unittest.TestCase):
    def test_weights_normalized_to_enterprise_quota(self) -> None:
        store = build_store()
        frame = store.frames["R-Q3"]
        self.assertTrue(frame.locked)
        self.assertAlmostEqual(frame.enterprise_weight("ENT-ALPHA"), 30.0)
        self.assertAlmostEqual(frame.enterprise_weight("ENT-BETA"), 30.0)
        self.assertAlmostEqual(sum(entry.weight for entry in frame.entries), 110.0)

    def test_splitting_units_cannot_inflate_weight(self) -> None:
        store = build_store()
        frame = store.frames["R-Q3"]
        units = [entry for entry in frame.entries if entry.enterprise_id == "ENT-ALPHA"]
        self.assertEqual(len(units), 3)
        self.assertAlmostEqual(sum(entry.weight for entry in units), 30.0)

    def test_frame_cannot_change_after_lock(self) -> None:
        store = build_store()
        rounds = RoundService(store)
        with self.assertRaises(FrameLockedError):
            rounds.prepare_frame("R-Q3", ENTRIES, 55.0)
        with self.assertRaises(FrameLockedError):
            rounds.lock_frame("R-Q3", ALLOTMENTS)
        self.assertEqual(store.frames["R-Q3"].threshold, 50.0)

    def test_allotment_must_match_sample_roster(self) -> None:
        store = Store()
        rounds = RoundService(store)
        rounds.create_round("R-X", "2026-Q4", "2026-Q3")
        rounds.prepare_frame("R-X", ENTRIES, 50.0)
        with self.assertRaises(IneligibleSampleError):
            rounds.lock_frame("R-X", {"ENT-ALPHA": 30.0})


class SurveyTest(unittest.TestCase):
    def test_ineligible_unit_rejected(self) -> None:
        store = build_store()
        survey = SurveyService(store)
        with self.assertRaises(IneligibleSampleError):
            survey.submit("R-Q3", "ENT-ALPHA", "A-U9", Answer.UP, Answer.UP)
        with self.assertRaises(IneligibleSampleError):
            survey.submit("R-Q3", "ENT-UNKNOWN", "X-U1", Answer.UP, Answer.UP)

    def test_identical_resend_does_not_duplicate(self) -> None:
        store = build_store()
        survey = SurveyService(store)
        again = survey.submit("R-Q3", "ENT-BETA", "B-U1", Answer.UP, Answer.UP)
        self.assertIs(again, store.responses["R-Q3"][("ENT-BETA", "B-U1")])
        self.assertEqual(len(store.responses["R-Q3"]), 6)
        with self.assertRaises(DomainError):
            survey.submit("R-Q3", "ENT-BETA", "B-U1", Answer.DOWN, Answer.UP)

    def test_late_report_only_enters_unpublished_watermark(self) -> None:
        store = build_store()
        rounds = RoundService(store)
        survey = SurveyService(store)
        rounds.close_round("R-Q3")
        late = survey.submit_late("R-Q3", "ENT-ECHO", "E-U1", Answer.UP, Answer.UP)
        self.assertTrue(late.late)
        PublishService(store).publish("R-Q3")
        with self.assertRaises(RoundStateError):
            survey.submit_late("R-Q3", "ENT-ECHO", "E-U1", Answer.UP, Answer.UP)

    def test_late_report_flows_to_next_unpublished_round(self) -> None:
        store = build_store()
        rounds = RoundService(store)
        survey = SurveyService(store)
        rounds.create_round("R-Q4", "2026-Q4", "2026-Q3")
        rounds.prepare_frame("R-Q4", ENTRIES, 50.0)
        rounds.lock_frame("R-Q4", ALLOTMENTS)
        rounds.close_round("R-Q4")
        late = survey.submit_late("R-Q4", "ENT-ECHO", "E-U1", Answer.DOWN, Answer.FLAT)
        self.assertTrue(late.late)
        close_and_publish(store)
        with self.assertRaises(RoundStateError):
            survey.submit_late("R-Q3", "ENT-ECHO", "E-U1", Answer.DOWN, Answer.FLAT)


class EvidenceAccessTest(unittest.TestCase):
    def test_association_submits_but_cannot_read_responses(self) -> None:
        store = build_store()
        evidence = EvidenceService(store)
        item = evidence.submit("R-Q3", ASSOC_A, "EV-1", "会员反映订单回升", "会员月报与订单抽样")
        self.assertEqual(item.association_id, "assoc-a")
        with self.assertRaises(RoleError):
            evidence.submit("R-Q3", RESEARCHER, "EV-2", "越权", "越权")
        survey = SurveyService(store)
        with self.assertRaises(RoleError):
            survey.list_responses("R-Q3", ASSOC_A)
        self.assertEqual(len(survey.list_responses("R-Q3", RESEARCHER)), 6)

    def test_association_sees_only_own_evidence(self) -> None:
        store = build_store()
        evidence = EvidenceService(store)
        evidence.submit("R-Q3", ASSOC_A, "EV-1", "甲会依据", "甲会依据内容")
        evidence.submit("R-Q3", ASSOC_B, "EV-2", "乙会依据", "乙会依据内容")
        own = evidence.list_for_round("R-Q3", ASSOC_A)
        self.assertEqual([item.evidence_id for item in own], ["EV-1"])
        self.assertEqual(len(evidence.list_for_round("R-Q3", RESEARCHER)), 2)


class ForecastAdjustmentTest(unittest.TestCase):
    def test_propose_requires_researcher_and_three_elements(self) -> None:
        store = build_store()
        forecast = ForecastService(store)
        with self.assertRaises(RoleError):
            forecast.propose("R-Q3", "S-X", 1.0, 2.0, ("推动",), ("相反",), "2026-H2", APPROVER)
        with self.assertRaises(DomainError):
            forecast.propose("R-Q3", "S-X", 1.0, 2.0, ("推动",), (), "2026-H2", RESEARCHER)
        with self.assertRaises(DomainError):
            forecast.propose("R-Q3", "S-X", 1.0, 2.0, ("推动",), ("相反",), " ", RESEARCHER)
        with self.assertRaises(DomainError):
            forecast.propose("R-Q3", "S-X", 5.0, 2.0, ("推动",), ("相反",), "2026-H2", RESEARCHER)

    def test_confirmation_needs_another_role(self) -> None:
        store = build_store()
        forecast = ForecastService(store)
        forecast.propose(
            "R-Q3", "S-Y", 40.0, 45.0, ("需求",), ("背离",), "2026-H2",
            Actor("user-7", Role.RESEARCHER),
        )
        with self.assertRaises(RoleError):
            forecast.confirm("S-Y", RESEARCHER)
        with self.assertRaises(ConfirmationError):
            forecast.confirm("S-Y", Actor("user-7", Role.APPROVER))
        confirmed = forecast.confirm("S-Y", APPROVER)
        self.assertEqual(confirmed.status, AdjustmentStatus.CONFIRMED)
        self.assertEqual(confirmed.confirmed_by, "appr-1")

    def test_effective_scenario_is_latest_confirmed(self) -> None:
        store = build_store()
        forecast = ForecastService(store)
        self.assertEqual(effective_scenario(store, "R-Q3").scenario_id, "S-Q3")
        forecast.propose("R-Q3", "S-Q3B", 43.0, 48.0, ("上调",), ("背离",), "2026-H2", RESEARCHER)
        self.assertEqual(effective_scenario(store, "R-Q3").scenario_id, "S-Q3")
        forecast.confirm("S-Q3B", APPROVER)
        self.assertEqual(effective_scenario(store, "R-Q3").scenario_id, "S-Q3B")


class IntakeReconTest(unittest.TestCase):
    def _bad_batch(self) -> TradeBatch:
        return TradeBatch(
            "B-BAD",
            "2026-Q3",
            (
                TradeRecord(1, "EL-IC", "中国内地", 10.0, "箱", 60.0, "HKD"),
                TradeRecord(2, "TX", "欧盟", 5.0, "千克", 50.0, "USD"),
                TradeRecord(3, "XX-9", "美国", 1.0, "件", 10.0, "HKD"),
                TradeRecord(4, "EL-PH", "美国", 8.0, "件", 80.0, "HKD"),
            ),
        )

    def test_mismatched_unit_or_currency_stays_in_reconciliation(self) -> None:
        store = build_store()
        intake = IntakeService(store)
        before = len(store.trade_records["2026-Q3"])
        result = intake.import_batch(self._bad_batch())
        self.assertEqual((result.accepted, result.quarantined), (1, 3))
        self.assertEqual(len(store.trade_records["2026-Q3"]), before + 1)
        self.assertEqual(store.recon_items["B-BAD:1"].reason, ReconReason.UNIT_MISMATCH)
        self.assertEqual(store.recon_items["B-BAD:2"].reason, ReconReason.CURRENCY_MISMATCH)
        self.assertEqual(store.recon_items["B-BAD:3"].reason, ReconReason.UNKNOWN_COMMODITY)

    def test_reconcile_release_and_reject(self) -> None:
        store = build_store()
        intake = IntakeService(store)
        intake.import_batch(self._bad_batch())
        with self.assertRaises(RoleError):
            intake.reconcile("B-BAD:1", RESEARCHER, approve=True, unit="件")
        with self.assertRaises(ReconciliationError):
            intake.reconcile("B-BAD:2", REVIEWER, approve=True)
        released = intake.reconcile("B-BAD:1", REVIEWER, approve=True, unit="件")
        self.assertEqual(released.status, ReconStatus.RELEASED)
        rejected = intake.reconcile("B-BAD:3", REVIEWER, approve=False)
        self.assertEqual(rejected.status, ReconStatus.REJECTED)
        with self.assertRaises(ReconciliationError):
            intake.reconcile("B-BAD:1", REVIEWER, approve=True, unit="件")

    def test_resent_batch_does_not_create_second_sample(self) -> None:
        store = build_store()
        intake = IntakeService(store)
        before = len(store.trade_records["2026-Q3"])
        resent = intake.import_batch(TradeBatch("B-Q3", "2026-Q3", Q3_RECORDS))
        self.assertTrue(resent.duplicate)
        self.assertEqual(len(store.trade_records["2026-Q3"]), before)
        tampered = TradeBatch(
            "B-Q3", "2026-Q3", (TradeRecord(1, "EL-IC", "中国内地", 1.0, "件", 1.0, "HKD"),)
        )
        with self.assertRaises(DuplicateBatchError):
            intake.import_batch(tampered)

    def test_batch_cannot_enter_published_round(self) -> None:
        store = build_store()
        close_and_publish(store)
        intake = IntakeService(store)
        late_batch = TradeBatch(
            "B-LATE", "2026-Q3", (TradeRecord(1, "EL-IC", "中国内地", 1.0, "件", 1.0, "HKD"),)
        )
        with self.assertRaises(RoundStateError):
            intake.import_batch(late_batch)


class PublishRecoveryTest(unittest.TestCase):
    def test_crash_resumes_from_confirmed_watermark(self) -> None:
        store = build_store()
        RoundService(store).close_round("R-Q3")
        with self.assertRaises(PublishInterrupted):
            PublishService(store, fail_at=PublishStage.ATTRIBUTION).publish("R-Q3")
        self.assertEqual(store.watermarks["R-Q3"], PublishStage.INDEX)
        self.assertEqual(store.rounds["R-Q3"].state, RoundState.PUBLISHING)
        PublishService(store).publish("R-Q3")
        self.assertEqual(store.watermarks["R-Q3"], PublishStage.REPORT)
        self.assertEqual(store.rounds["R-Q3"].state, RoundState.PUBLISHED)

    def test_crash_before_any_stage_leaves_no_watermark(self) -> None:
        store = build_store()
        RoundService(store).close_round("R-Q3")
        with self.assertRaises(PublishInterrupted):
            PublishService(store, fail_at=PublishStage.FREEZE).publish("R-Q3")
        self.assertNotIn("R-Q3", store.watermarks)
        PublishService(store).publish("R-Q3")
        self.assertEqual(store.rounds["R-Q3"].state, RoundState.PUBLISHED)

    def test_republish_is_idempotent(self) -> None:
        store = build_store()
        close_and_publish(store)
        snapshot = dict(store.stage_outputs["R-Q3"])
        PublishService(store).publish("R-Q3")
        self.assertIs(store.stage_outputs["R-Q3"]["report"], snapshot["report"])

    def test_publish_requires_confirmed_scenario(self) -> None:
        store = build_store()
        rounds = RoundService(store)
        survey = SurveyService(store)
        rounds.create_round("R-Q4", "2026-Q4", "2026-Q3")
        rounds.prepare_frame("R-Q4", ENTRIES, 50.0)
        rounds.lock_frame("R-Q4", ALLOTMENTS)
        for enterprise_id, unit_id in (
            ("ENT-ALPHA", "A-U1"),
            ("ENT-BETA", "B-U1"),
            ("ENT-GAMMA", "G-U1"),
        ):
            survey.submit("R-Q4", enterprise_id, unit_id, Answer.UP, Answer.UP)
        IntakeService(store).import_batch(
            TradeBatch(
                "B-Q4",
                "2026-Q4",
                (TradeRecord(1, "EL-IC", "中国内地", 1000.0, "件", 1000.0, "HKD"),),
            )
        )
        rounds.close_round("R-Q4")
        with self.assertRaisesRegex(DomainError, "预测情景"):
            PublishService(store).publish("R-Q4")


class ReportQueryTest(unittest.TestCase):
    def test_report_links_growth_index_markets_and_goods(self) -> None:
        store = build_store()
        close_and_publish(store)
        report = ReportService(store).report("R-Q3")
        self.assertEqual((report.growth_low, report.growth_high), (42.0, 47.0))
        self.assertEqual(report.applicable_period, "2026-H2")
        self.assertEqual((report.current_index, report.expected_index), (75.0, 75.0))
        self.assertEqual(report.threshold, 50.0)
        self.assertEqual(report.current_sentiment, "好")
        self.assertEqual(report.expected_sentiment, "好")
        self.assertEqual(report.major_markets[0].market, "中国内地")
        self.assertAlmostEqual(report.major_markets[0].contribution_pp, 40.0)
        self.assertAlmostEqual(sum(m.contribution_pp for m in report.major_markets), 45.0)
        self.assertEqual(len(report.high_value_goods), 1)
        self.assertEqual(report.high_value_goods[0].code, "EL")
        self.assertAlmostEqual(report.high_value_goods[0].contribution_pp, 45.0)

    def test_auditor_can_recompute_without_exposing_enterprises(self) -> None:
        store = build_store()
        close_and_publish(store)
        service = ReportService(store)
        check = service.recompute_indices("R-Q3", REVIEWER)
        self.assertTrue(check.matches)
        report = service.report("R-Q3")
        audit = report.audit
        self.assertAlmostEqual(
            100.0 * audit.current_weighted_score / audit.total_weight,
            report.current_index,
            places=2,
        )
        self.assertEqual(audit.enterprise_count, 4)
        payload = json.dumps(asdict(report), ensure_ascii=False)
        for enterprise in ("ENT-ALPHA", "ENT-BETA", "ENT-GAMMA", "ENT-DELTA", "ENT-ECHO"):
            self.assertNotIn(enterprise, payload)
        with self.assertRaises(RoleError):
            service.recompute_indices("R-Q3", ASSOC_A)

    def test_small_answer_cells_are_merged(self) -> None:
        store = build_store()
        close_and_publish(store)
        report = ReportService(store).report("R-Q3")
        current = {cell.label: cell.enterprise_count for cell in report.audit.current_distribution}
        self.assertEqual(current, {"上升": 3, "已合并（小样本）": 1})
        expected = {cell.label: cell.enterprise_count for cell in report.audit.expected_distribution}
        self.assertEqual(expected, {"已合并（小样本）": 4})

    def test_index_ignores_unit_splitting(self) -> None:
        store = build_store()
        close_and_publish(store)
        check = ReportService(store).recompute_indices("R-Q3", REVIEWER)
        self.assertAlmostEqual(check.recomputed.total_weight, 100.0)
        self.assertAlmostEqual(check.recomputed.current_index, 75.0)

    def test_report_requires_published_round(self) -> None:
        store = build_store()
        with self.assertRaises(RoundStateError):
            ReportService(store).report("R-Q3")

    def test_too_few_enterprises_cannot_publish(self) -> None:
        store = build_store()
        rounds = RoundService(store)
        survey = SurveyService(store)
        forecast = ForecastService(store)
        rounds.create_round("R-Q5", "2027-Q1", "2026-Q3")
        entries = (FrameEntry("ENT-ONE", "O-U1", 1.0), FrameEntry("ENT-TWO", "T-U1", 1.0))
        rounds.prepare_frame("R-Q5", entries, 50.0)
        rounds.lock_frame("R-Q5", {"ENT-ONE": 50.0, "ENT-TWO": 50.0})
        survey.submit("R-Q5", "ENT-ONE", "O-U1", Answer.UP, Answer.UP)
        survey.submit("R-Q5", "ENT-TWO", "T-U1", Answer.UP, Answer.UP)
        IntakeService(store).import_batch(
            TradeBatch(
                "B-Q5",
                "2027-Q1",
                (TradeRecord(1, "EL-IC", "中国内地", 1200.0, "件", 1200.0, "HKD"),),
            )
        )
        forecast.propose("R-Q5", "S-Q5", 10.0, 12.0, ("推动",), ("相反",), "2027-H1", RESEARCHER)
        forecast.confirm("S-Q5", APPROVER)
        rounds.close_round("R-Q5")
        with self.assertRaises(PrivacyError):
            PublishService(store).publish("R-Q5")


if __name__ == "__main__":
    unittest.main()
