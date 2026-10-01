"""出口景气后端的领域规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.export_confidence import (
    Actor,
    AccessDeniedError,
    DomainError,
    Store,
)

STAT = Actor("statistician", "S1")
STAT_OTHER = Actor("statistician", "S2")
REVIEWER = Actor("reviewer", "R1")
APPROVER = Actor("approver", "A1")
ASSOC_A = Actor("association", "AS1", association_id="ASSOC-A")
ASSOC_B = Actor("association", "AS2", association_id="ASSOC-B")

FRAME = [
    {"enterprise_id": "E1", "unit_id": "E1-HQ", "weight": 10.0, "share": 0.6, "eligible": True},
    {"enterprise_id": "E1", "unit_id": "E1-SZ", "weight": 10.0, "share": 0.4, "eligible": True},
    {"enterprise_id": "E2", "unit_id": "E2", "weight": 20.0, "share": 1.0, "eligible": True},
    {"enterprise_id": "E3", "unit_id": "E3", "weight": 30.0, "share": 1.0, "eligible": True},
    {"enterprise_id": "E4", "unit_id": "E4", "weight": 5.0, "share": 1.0, "eligible": False},
]


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self._seed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self) -> None:
        s = self.store
        for code, name, parent, hv, unit in (
            ("85", "电子产品", None, False, "PCE"),
            ("8542", "集成电路", "85", True, "PCE"),
            ("8517", "通信设备", "85", True, "PCE"),
            ("8471", "自动数据处理设备", None, True, "PCE"),
            ("99", "其他杂货", None, False, "PCE"),
        ):
            s.register_commodity(STAT, code, name, parent=parent, high_value=hv, count_unit=unit)
        s.create_cycle(STAT, "C2026Q1", "2026Q1")

    def _lock(self, frame=None, threshold=50.0) -> None:
        self.store.lock_frame(STAT, "C2026Q1", threshold, frame or FRAME)
        self.store.start_cycle(STAT, "C2026Q1")
        self.store.open_level(STAT, "C2026Q1", "初版水位")

    # ----------------------------------------------------------- 锁框与计权

    def test_frame_must_lock_before_cycle_starts(self) -> None:
        s = Store(self._tmp.name + "/x")
        s.register_commodity(STAT, "8542", "集成电路", high_value=True)
        s.create_cycle(STAT, "C1", "2026Q1")
        s.lock_frame(
            STAT, "C1", 50.0,
            [{"enterprise_id": "E1", "unit_id": "U1", "weight": 1.0, "share": 1.0, "eligible": True}],
        )
        s.start_cycle(STAT, "C1")
        with self.assertRaisesRegex(DomainError, "已锁定"):
            s.lock_frame(
                STAT, "C1", 50.0,
                [{"enterprise_id": "E1", "unit_id": "U1", "weight": 2.0, "share": 1.0, "eligible": True}],
            )

    def test_split_units_cannot_exceed_full_enterprise_weight(self) -> None:
        cheating = [
            {"enterprise_id": "E1", "unit_id": "E1-HQ", "weight": 10.0, "share": 0.8, "eligible": True},
            {"enterprise_id": "E1", "unit_id": "E1-SZ", "weight": 10.0, "share": 0.8, "eligible": True},
        ]
        with self.assertRaisesRegex(DomainError, "份额合计超过 1"):
            self.store.lock_frame(STAT, "C2026Q1", 50.0, cheating)

    def test_confidence_uses_share_weighted_enterprise_units(self) -> None:
        s = self.store
        self._lock()
        # E1 两个业务单元合计权重 = 10*(0.6+0.4) = 10，未被拆分放大
        s.submit_response(STAT, "C2026Q1", "E1-HQ", 55, 60, ref="R1")
        s.submit_response(STAT, "C2026Q1", "E1-SZ", 55, 60, ref="R2")
        s.submit_response(STAT, "C2026Q1", "E2", 40, 55, ref="R3")
        s.submit_response(STAT, "C2026Q1", "E3", 30, 30, ref="R4")
        self._propose_and_confirm()
        s.begin_publication(STAT, "C2026Q1")
        s.confirm_level(STAT, "C2026Q1", 1)
        report = s.build_report(APPROVER, "C2026Q1")
        conf = report["confidence"]
        # 总覆盖权重 = 10 + 20 + 30 = 60
        self.assertEqual(conf["covered_weight"], 60.0)
        self.assertEqual(conf["respondent_enterprises"], 3)
        # 现状好淡：E1(10) 高于50；E2/E3 低于 → 10/60
        self.assertEqual(conf["current_index"], 16.6667)
        # 预期好淡：E1+E2 = 30/60
        self.assertEqual(conf["expected_index"], 50.0)

    def test_ineligible_unit_response_rejected(self) -> None:
        self._lock()
        with self.assertRaisesRegex(DomainError, "资格不合格"):
            self.store.submit_response(STAT, "C2026Q1", "E4", 60, 60, ref="RX")

    def test_enterprise_may_only_answer_for_itself(self) -> None:
        self._lock()
        e1 = Actor("enterprise", "U-E1", enterprise_id="E1")
        e2 = Actor("enterprise", "U-E2", enterprise_id="E2")
        self.store.submit_response(e1, "C2026Q1", "E1-HQ", 55, 55, ref="R1")
        with self.assertRaises(AccessDeniedError):
            self.store.submit_response(e2, "C2026Q1", "E1-SZ", 55, 55, ref="R2")

    # ----------------------------------------------------------- 答卷与补报

    def test_resubmitted_response_is_idempotent(self) -> None:
        self._lock()
        first = self.store.submit_response(STAT, "C2026Q1", "E2", 55, 60, ref="R3")
        again = self.store.submit_response(STAT, "C2026Q1", "E2", 55, 60, ref="R3")
        self.assertEqual(first["seq"], again["seq"])
        with self.assertRaisesRegex(DomainError, "内容不同"):
            self.store.submit_response(STAT, "C2026Q1", "E2", 40, 60, ref="R3")

    def test_late_response_moves_to_unpublished_level(self) -> None:
        s = self.store
        self._lock()
        s.submit_response(STAT, "C2026Q1", "E2", 55, 60, ref="R3")
        self._propose_and_confirm()
        s.begin_publication(STAT, "C2026Q1")
        s.confirm_level(STAT, "C2026Q1", 1)
        # 水位1已确认；发布进程仍在进行，打开补报水位后
        # 指向水位1的补报必须转入水位2
        s.open_level(STAT, "C2026Q1", "补报水位")
        late = s.submit_response(
            STAT, "C2026Q1", "E2", 40, 45, ref="B", intended_level_seq=1
        )
        self.assertTrue(late["late"])
        self.assertEqual(late["level_seq"], 2)

    # ----------------------------------------------------------- 导入与核对区

    def _batch(self, batch_id: str, enterprise="E2", commodity="8542", market="US",
               qty=100, weight=200.0, value=1000.0, currency="HKD",
               prior_qty=90, prior_weight=190.0, prior_value=800.0, **extra):
        row = {
            "batch_id": batch_id, "enterprise_id": enterprise, "unit_id": None,
            "commodity": commodity, "market": market, "qty": qty, "weight": weight,
            "value": value, "currency": currency, "prior_qty": prior_qty,
            "prior_weight": prior_weight, "prior_value": prior_value,
        }
        row.update(extra)
        if enterprise == "E2":
            row["unit_id"] = "E2"
        return row

    def test_unit_or_currency_mismatch_quarantines_only_that_row(self) -> None:
        s = self.store
        self._lock()
        good = self._batch("B1")
        bad_ccy = self._batch("B2", value=500.0, currency="USD")
        bad_unit = self._batch("B3", commodity="8517", count_unit="KG")
        other_category = self._batch("B4", commodity="99", market="EU", value=300.0,
                                     prior_value=300.0)
        summary = s.import_batches(
            STAT, "C2026Q1", "IMP-1", [good, bad_ccy, bad_unit, other_category]
        )
        self.assertEqual(summary["accepted"], ["B1", "B4"])
        self.assertEqual({h["batch_id"] for h in summary["held"]}, {"B2", "B3"})
        held = s.held_items(STAT, "C2026Q1")
        self.assertEqual(len(held), 2)
        self.assertTrue(any("币种不一致" in r for h in held for r in h["reasons"]))
        self.assertTrue(any("计量单位不一致" in r for h in held for r in h["reasons"]))

    def test_resubmitting_same_import_batch_does_not_duplicate_sample(self) -> None:
        s = self.store
        self._lock()
        rows = [self._batch("B1"), self._batch("B2", market="EU")]
        first = s.import_batches(STAT, "C2026Q1", "IMP-1", rows)
        second = s.import_batches(STAT, "C2026Q1", "IMP-1", rows)
        self.assertEqual(first, second)
        # 行级也幂等：混在新导入里重发 B1，不会产生第二份
        mixed = s.import_batches(
            STAT, "C2026Q1", "IMP-2", [self._batch("B1"), self._batch("B9", market="JP")]
        )
        self.assertEqual(mixed["accepted"], ["B1", "B9"])
        self.assertEqual(len(self.store.held_items(STAT, "C2026Q1")), 0)

    def test_quarantined_row_can_be_corrected_into_unpublished_level(self) -> None:
        s = self.store
        self._lock()
        s.import_batches(STAT, "C2026Q1", "IMP-1", [self._batch("B2", currency="USD")])
        hold_id = s.held_items(STAT, "C2026Q1")[0]["hold_id"]
        # 原水位发布后再更正
        self._propose_and_confirm()
        s.begin_publication(STAT, "C2026Q1")
        s.confirm_level(STAT, "C2026Q1", 1)
        s.open_level(STAT, "C2026Q1", "补报水位")
        target = s.resolve_hold(STAT, hold_id, self._batch("B2"))
        self.assertEqual(target, 2)
        self.assertEqual(s.held_items(STAT, "C2026Q1"), [])

    # ----------------------------------------------------------- 协会边界

    def test_association_submits_evidence_but_cannot_see_responses(self) -> None:
        s = self.store
        self._lock()
        s.submit_response(STAT, "C2026Q1", "E2", 55, 60, ref="R3")
        evidence_id = s.submit_evidence(
            ASSOC_A, "C2026Q1", "commodity", "8542", "AI服务器拉货，交单期延长"
        )
        self.assertTrue(evidence_id.startswith("EV-"))
        with self.assertRaises(AccessDeniedError):
            s.get_responses(ASSOC_A, "C2026Q1")
        with self.assertRaises(AccessDeniedError):
            s.import_batches(ASSOC_A, "C2026Q1", "X", [self._batch("B1")])
        # 协会只能看到自己的依据
        s.submit_evidence(ASSOC_B, "C2026Q1", "market", "US", "另一协会依据")
        own = s.list_evidence(ASSOC_A, "C2026Q1")
        self.assertEqual([e["association_id"] for e in own], ["ASSOC-A"])
        with self.assertRaises(AccessDeniedError):
            s.build_report(ASSOC_A, "C2026Q1")  # 未发布

    # ----------------------------------------------------------- 预测双确认

    def _propose_and_confirm(self, store=None, cycle_id="C2026Q1",
                             proposer=STAT, confirmer=REVIEWER):
        s = store or self.store
        adj = s.propose_forecast(
            proposer, cycle_id, "基准情景", 42.0, 47.0,
            drivers=["集成电路与通信设备货值上行", "高价值机型均价提升"],
            opposing_signals=["件数与重量增速低于货值，存在单价扰动"],
            applicable_from="2026-01-01", applicable_to="2026-12-31",
        )
        s.confirm_forecast(
            confirmer, cycle_id,
            drivers=["集成电路与通信设备货值上行", "高价值机型均价提升"],
            opposing_signals=["件数与重量增速低于货值，存在单价扰动"],
            applicable_from="2026-01-01", applicable_to="2026-12-31",
        )
        return adj

    def test_forecast_requires_two_roles_and_three_elements(self) -> None:
        s = self.store
        self._lock()
        with self.assertRaisesRegex(DomainError, "推动因素"):
            s.propose_forecast(
                STAT, "C2026Q1", "基准", 42, 47,
                drivers=[], opposing_signals=["x"],
                applicable_from="2026-01-01", applicable_to="2026-12-31",
            )
        with self.assertRaisesRegex(DomainError, "相反信号"):
            s.propose_forecast(
                STAT, "C2026Q1", "基准", 42, 47,
                drivers=["d"], opposing_signals=[],
                applicable_from="2026-01-01", applicable_to="2026-12-31",
            )
        s.propose_forecast(
            STAT, "C2026Q1", "基准", 42, 47,
            drivers=["d"], opposing_signals=["o"],
            applicable_from="2026-01-01", applicable_to="2026-12-31",
        )
        # 同一人不能确认
        with self.assertRaises(AccessDeniedError):
            s.confirm_forecast(
                STAT, "C2026Q1", drivers=["d"], opposing_signals=["o"],
                applicable_from="2026-01-01", applicable_to="2026-12-31",
            )
        # 三要素必须逐项一致
        with self.assertRaisesRegex(DomainError, "不一致"):
            s.confirm_forecast(
                REVIEWER, "C2026Q1", drivers=["d-篡改"], opposing_signals=["o"],
                applicable_from="2026-01-01", applicable_to="2026-12-31",
            )
        with self.assertRaisesRegex(DomainError, "不一致"):
            s.confirm_forecast(
                REVIEWER, "C2026Q1", drivers=["d"], opposing_signals=["o"],
                applicable_from="2026-01-01", applicable_to="2026-11-30",
            )
        # 协会不能确认
        with self.assertRaises(AccessDeniedError):
            s.confirm_forecast(
                ASSOC_A, "C2026Q1", drivers=["d"], opposing_signals=["o"],
                applicable_from="2026-01-01", applicable_to="2026-12-31",
            )

    def test_publication_blocked_until_forecast_confirmed(self) -> None:
        self._lock()
        with self.assertRaisesRegex(DomainError, "另一角色确认"):
            self.store.begin_publication(STAT, "C2026Q1")

    # ----------------------------------------------------------- 崩溃恢复

    def test_publication_resumes_from_last_confirmed_level(self) -> None:
        s = self.store
        self._lock()
        s.submit_response(STAT, "C2026Q1", "E2", 55, 60, ref="R3")
        s.import_batches(STAT, "C2026Q1", "IMP-1", [self._batch("B1")])
        self._propose_and_confirm()
        s.begin_publication(STAT, "C2026Q1")
        s.confirm_level(STAT, "C2026Q1", 1)
        s.open_level(STAT, "C2026Q1", "补报水位")
        s.confirm_level(STAT, "C2026Q1", 2)
        # 发布进程在完成前“终止”：重新从日志装载
        revived = Store(self._tmp.name)
        point = revived.recovery_point("C2026Q1")
        self.assertEqual(point["status"], "publishing")
        self.assertEqual(point["confirmed_levels"], [1, 2])
        self.assertIsNone(point["next_level_seq"])
        revived.begin_publication(STAT, "C2026Q1")  # 重入幂等
        revived.complete_publication(STAT, "C2026Q1")
        self.assertEqual(revived.recovery_point("C2026Q1")["status"], "published")
        # 恢复期间的数据状态完整
        report = revived.build_report(APPROVER, "C2026Q1")
        self.assertEqual(report["recompute"]["accepted_batch_count"], 1)

    def test_levels_must_confirm_in_order(self) -> None:
        s = self.store
        self._lock()
        s.open_level(STAT, "C2026Q1", "第二水位")
        self._propose_and_confirm()
        s.begin_publication(STAT, "C2026Q1")
        with self.assertRaisesRegex(DomainError, "按顺序"):
            s.confirm_level(STAT, "C2026Q1", 2)
        s.confirm_level(STAT, "C2026Q1", 1)
        s.confirm_level(STAT, "C2026Q1", 1)  # 重复确认幂等
        s.confirm_level(STAT, "C2026Q1", 2)

    # ----------------------------------------------------------- 报告联查

    def _published_cycle_with_trade(self):
        s = self.store
        self._lock()
        s.submit_response(STAT, "C2026Q1", "E1-HQ", 55, 60, ref="R1")
        s.submit_response(STAT, "C2026Q1", "E1-SZ", 55, 60, ref="R2")
        s.submit_response(STAT, "C2026Q1", "E2", 58, 62, ref="R3")
        s.submit_response(STAT, "C2026Q1", "E3", 52, 55, ref="R4")
        ev = s.submit_evidence(
            ASSOC_A, "C2026Q1", "commodity", "8542", "AI芯片出口排期至季末"
        )
        rows = [
            # 三家企业、两个市场，避免最小披露压制
            self._batch("B1", enterprise="E1", unit_id="E1-HQ", commodity="8542",
                        market="US", value=1000.0, prior_value=600.0,
                        qty=100, prior_qty=110, weight=200.0, prior_weight=230.0),
            self._batch("B2", enterprise="E2", commodity="8542",
                        market="US", value=800.0, prior_value=700.0,
                        qty=80, prior_qty=75, weight=160.0, prior_weight=155.0),
            self._batch("B3", enterprise="E3", commodity="8542",
                        market="EU", value=600.0, prior_value=500.0,
                        qty=60, prior_qty=60, weight=120.0, prior_weight=125.0),
            self._batch("B4", enterprise="E1", unit_id="E1-SZ", commodity="8517",
                        market="JP", value=400.0, prior_value=500.0,
                        qty=40, prior_qty=30, weight=80.0, prior_weight=70.0),
            self._batch("B5", enterprise="E2", commodity="8517",
                        market="JP", value=300.0, prior_value=250.0),
            self._batch("B6", enterprise="E1", commodity="8517",
                        market="EU", value=300.0, prior_value=250.0),
            # 非高价值对照：每个市场凑齐三家企业，避免被最小披露合并
            self._batch("B7", enterprise="E3", commodity="99",
                        market="US", value=100.0, prior_value=90.0),
            self._batch("B8", enterprise="E2", commodity="99",
                        market="EU", value=200.0, prior_value=90.0),
            self._batch("B9", enterprise="E3", commodity="99",
                        market="JP", value=100.0, prior_value=90.0),
        ]
        s.import_batches(STAT, "C2026Q1", "IMP-1", rows)
        s.propose_forecast(
            STAT, "C2026Q1", "基准情景", 42.0, 47.0,
            drivers=["集成电路与通信设备货值上行"],
            opposing_signals=["件数与重量增速低于货值，存在单价扰动"],
            applicable_from="2026-01-01", applicable_to="2026-12-31",
            evidence_refs=[ev],
        )
        s.confirm_forecast(
            REVIEWER, "C2026Q1",
            drivers=["集成电路与通信设备货值上行"],
            opposing_signals=["件数与重量增速低于货值，存在单价扰动"],
            applicable_from="2026-01-01", applicable_to="2026-12-31",
        )
        s.begin_publication(STAT, "C2026Q1")
        s.confirm_level(STAT, "C2026Q1", 1)
        s.complete_publication(STAT, "C2026Q1")
        return s

    def test_report_links_range_confidence_markets_and_high_value_goods(self) -> None:
        s = self._published_cycle_with_trade()
        report = s.build_report(APPROVER, "C2026Q1")

        rng = report["growth_range"]
        self.assertEqual((rng["low"], rng["high"]), (42.0, 47.0))
        self.assertEqual(rng["proposed_by_role"], "statistician")
        self.assertEqual(rng["confirmed_by_role"], "reviewer")
        self.assertEqual(rng["applicable_from"], "2026-01-01")

        conf = report["confidence"]
        self.assertGreater(conf["current_index"], 50)
        self.assertGreater(conf["expected_index"], 50)
        self.assertEqual(conf["threshold"], 50.0)

        recompute = report["recompute"]
        self.assertEqual(recompute["total_value"], 3800.0)
        self.assertEqual(recompute["total_prior_value"], 3070.0)
        # 审核者可复算的总货值增速
        self.assertEqual(recompute["observed_growth_pct"], 23.7785)

        markets = {m["name"]: m for m in report["markets"]}
        self.assertEqual(markets["US"]["value"], 1900.0)
        # 贡献百分点 = (本期-上期)/上期总货值*100
        # US: 1900-1390=510 → 510/3070*100
        self.assertEqual(markets["US"]["contribution_points"], 16.6124)
        shares = sum(m["share"] for m in report["markets"])
        self.assertAlmostEqual(shares, 1.0)

        hv = report["high_value_goods"]
        self.assertEqual(hv["value"], 3400.0)
        goods = {g["name"]: g for g in hv["goods"]}
        # 集成电路：件数 240 vs 245（降）、重量 480 vs 510（降）、货值 2400 vs 1800（升）
        ic = goods["8542"]
        self.assertEqual(ic["signals"]["qty_change_pct"], -2.0408)
        self.assertEqual(ic["signals"]["weight_change_pct"], -5.8824)
        self.assertEqual(ic["signals"]["value_change_pct"], 33.3333)
        self.assertEqual(ic["contribution_points"], 19.544)
        self.assertEqual(hv["share"], 0.8947)

        # 依据随报告带出
        self.assertEqual(report["evidence"][0]["summary"], "AI芯片出口排期至季末")

    def test_report_never_exposes_single_enterprise_identity(self) -> None:
        s = self.store
        self._lock()
        s.submit_response(STAT, "C2026Q1", "E2", 55, 60, ref="R3")
        # E2 独占某市场 → 该组应进入“其他（最小披露合并）”
        s.import_batches(
            STAT, "C2026Q1", "IMP-1",
            [self._batch("B1", enterprise="E2", market="MO", value=1000.0,
                         prior_value=800.0)],
        )
        self._propose_and_confirm()
        s.begin_publication(STAT, "C2026Q1")
        s.confirm_level(STAT, "C2026Q1", 1)
        s.complete_publication(STAT, "C2026Q1")
        import json
        blob = json.dumps(s.build_report(APPROVER, "C2026Q1"), ensure_ascii=False)
        self.assertNotIn("E2", blob)
        self.assertNotIn("B1", blob)
        names = {m["name"] for m in json.loads(blob)["markets"]}
        self.assertIn("其他（最小披露合并）", names)

    def test_association_sees_published_report_without_responses(self) -> None:
        s = self._published_cycle_with_trade()
        report = s.build_report(ASSOC_A, "C2026Q1")
        self.assertEqual(report["status"], "published")
        self.assertNotIn("enterprise_id", report)


if __name__ == "__main__":
    unittest.main()
