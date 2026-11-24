"""促销期缺货场景：同时影响销量与转化，且不能把相关当根因。"""

from __future__ import annotations

import unittest

from support import AppTestCase, daily


class PromotionStockoutTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_stores()
        rows = []
        # 基线：销量 200，转化 20
        for day in ("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"):
            rows.append(daily("ST-A", day, "sales", value=200, version="MV-2"))
            rows.append(daily("ST-A", day, "conversion", value=20, version="MV-2"))
        # 诊断期：促销 + 缺货，销量 120，转化 9
        for day in ("2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"):
            rows.append(daily("ST-A", day, "sales", value=120, version="MV-2"))
            rows.append(daily("ST-A", day, "conversion", value=9, version="MV-2"))
        self.seed_daily_metrics(rows)
        self.seed_events(
            [
                {"event_id": "EV-1", "store_id": "ST-A", "date": "2026-11-06",
                 "event_type": "promotion", "campaign_id": "C-1",
                 "inventory_state": "low", "source_ref": "SRC-1"},
                {"event_id": "EV-2", "store_id": "ST-A", "date": "2026-11-08",
                 "event_type": "stockout", "campaign_id": "",
                 "inventory_state": "out", "source_ref": "SRC-2"},
            ]
        )

    def _diagnose(self, metric: str):
        return self.app.diagnose(
            store_id="ST-A",
            metric=metric,
            period_start="2026-11-06",
            period_end="2026-11-10",
            metric_version="MV-2",
            actor="analyst",
        )["evidence"]

    def test_promotion_with_low_inventory_is_a_confounder(self):
        evidence = self._diagnose("sales")
        types = {c["type"] for c in evidence["confounders"]}
        self.assertIn("promotion", types)
        self.assertIn("stockout", types)
        promotion = next(c for c in evidence["confounders"] if c["type"] == "promotion")
        self.assertTrue(promotion["inventory_risk"])
        self.assertIn("同时影响销量和转化", promotion["detail"])

    def test_sales_drop_is_material_but_not_asserted_as_caused_by_stockout(self):
        evidence = self._diagnose("sales")
        self.assertAlmostEqual(evidence["summary"]["change"]["delta_pct"], -40.0)
        self.assertTrue(evidence["summary"]["change"]["material"])
        self.assertEqual(evidence["status"], "diagnosed")
        candidates = {c["candidate_id"]: c for c in evidence["candidate_causes"]}
        self.assertIn("promotion-stockout", candidates)
        for candidate in candidates.values():
            self.assertFalse(candidate["is_root_cause"])
            self.assertTrue(candidate["requires_local_validation"])
        labels = " ".join(c["label"] for c in candidates.values())
        self.assertIn("促销与缺货", labels)

    def test_conversion_is_checked_jointly_with_sales(self):
        evidence = self._diagnose("sales")
        context = {c["metric"]: c for c in evidence["cross_metric_context"]}
        self.assertIn("conversion", context)
        self.assertEqual(context["conversion"]["role"], "context_only")
        self.assertLess(context["conversion"]["change"]["delta_pct"], 0)
        self.assertIn("不能单独证明因果", context["conversion"]["note"])

    def test_confounder_requires_local_validation_before_action(self):
        result = self.app.diagnose(
            store_id="ST-A",
            metric="sales",
            period_start="2026-11-06",
            period_end="2026-11-10",
            metric_version="MV-2",
            actor="analyst",
        )
        diagnosis_id = result["diagnosis"]["diagnosis_id"]
        # 区域经理必须先审阅，才能提出行动
        with self.assertRaises(Exception):
            self.app.propose_action(
                diagnosis_id=diagnosis_id, action_type="replenish", actor="manager"
            )
        self.app.submit_diagnosis(diagnosis_id, actor="manager", note="与本地促销事实一致")
        action = self.app.propose_action(
            diagnosis_id=diagnosis_id,
            action_type="replenish",
            actor="manager",
            due_at="2026-11-20",
        )
        self.assertEqual(action["execution_state"], "proposed")
        # 门店未接受前不得计为已执行
        with self.assertRaises(Exception):
            self.app.complete_action(action["action_id"], actor="store", outcome_ref="OUT-1")

    def test_peer_comparison_is_reference_not_ranking(self):
        evidence = self._diagnose("sales")
        self.assertIn("不构成经营能力排名", evidence["peer"]["note"])
        self.assertNotIn("rank", evidence["peer"])
        self.assertFalse(evidence["peer"]["sufficient"])
        self.assertIn("不是门店表现结论", evidence["peer"]["note"])


if __name__ == "__main__":
    unittest.main()
