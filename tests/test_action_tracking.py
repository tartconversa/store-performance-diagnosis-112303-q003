"""行动承诺与逾期：未接受不计执行，逾期要暴露，回看须用原口径。"""

from __future__ import annotations

import unittest

from support import AppTestCase, daily


class ActionTrackingTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_stores()
        rows = []
        for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
            rows.append(daily("ST-A", day, "sales", value=200, version="MV-1"))
        for day in ("2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07"):
            rows.append(daily("ST-A", day, "sales", value=150, version="MV-1"))
        for day in ("2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"):
            rows.append(daily("ST-A", day, "sales", value=210, version="MV-1"))
        self.seed_daily_metrics(rows)
        result = self.app.diagnose(
            store_id="ST-A",
            metric="sales",
            period_start="2026-11-04",
            period_end="2026-11-07",
            metric_version="MV-1",
            actor="analyst",
        )
        self.diagnosis_id = result["diagnosis"]["diagnosis_id"]
        self.app.submit_diagnosis(self.diagnosis_id, actor="manager", note="复核通过")

    def propose(self, action_type: str = "display") -> str:
        return self.app.propose_action(
            diagnosis_id=self.diagnosis_id,
            action_type=action_type,
            actor="manager",
            due_at="2026-11-20",
        )["action_id"]

    def test_action_cannot_complete_before_store_acceptance(self):
        action_id = self.propose()
        with self.assertRaises(Exception) as ctx:
            self.app.complete_action(action_id, actor="store-lead", outcome_ref="OUT-1")
        self.assertIn("未获门店接受", str(ctx.exception))
        action = self.app.get_action(action_id)
        self.assertEqual(action["execution_state"], "proposed")
        self.assertFalse(action["counted_as_executed"])

    def test_store_acceptance_records_owner_and_due_date(self):
        action_id = self.propose()
        accepted = self.app.accept_action(
            action_id,
            actor="store-lead",
            owner="MGR-7",
            due_at="2026-11-18",
            accepted_at="2026-11-12",
            note="门店确认安排",
        )
        self.assertEqual(accepted["execution_state"], "accepted")
        self.assertEqual(accepted["owner"], "MGR-7")
        self.assertEqual(accepted["accepted_at"], "2026-11-12")
        self.assertEqual(accepted["due_at"], "2026-11-18")
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["counted_as_executed"])

    def test_late_completion_is_counted_but_flagged(self):
        action_id = self.propose()
        self.app.accept_action(
            action_id, actor="store-lead", owner="MGR-7",
            due_at="2026-11-15", accepted_at="2026-11-12",
        )
        completed = self.app.complete_action(
            action_id, actor="store-lead", outcome_ref="OUT-1", completed_at="2026-11-19"
        )
        self.assertEqual(completed["execution_state"], "done")
        self.assertTrue(completed["completed_late"])
        self.assertTrue(completed["overdue"])
        self.assertTrue(completed["counted_as_executed"])
        scorecard = self.app.scorecard(as_of="2026-11-20")
        self.assertEqual(scorecard["executed_late"], 1)
        self.assertEqual(scorecard["executed_on_time"], 0)
        self.assertEqual(scorecard["pending"], 0)

    def test_overdue_accepted_action_is_marked_late(self):
        action_id = self.propose()
        self.app.accept_action(
            action_id, actor="store-lead", owner="MGR-7",
            due_at="2026-11-15", accepted_at="2026-11-12",
        )
        before = self.app.scorecard(as_of="2026-11-16")
        self.assertEqual(before["actions"][0]["current_state"], "overdue")
        marked = self.app.mark_late_actions(as_of="2026-11-16")
        self.assertEqual([a["action_id"] for a in marked], [action_id])
        after = self.app.scorecard(as_of="2026-11-16")
        self.assertEqual(after["executed"], 0)
        self.assertEqual(after["pending"], 1)
        self.assertEqual(after["overdue_unfinished"], 1)
        self.assertEqual(after["actions"][0]["execution_state"], "late")

    def test_rejected_action_is_not_executed(self):
        action_id = self.propose()
        self.app.reject_action(action_id, actor="store-lead", note="人手不足，无法执行")
        action = self.app.get_action(action_id)
        self.assertEqual(action["execution_state"], "rejected")
        self.assertFalse(action["counted_as_executed"])
        scorecard = self.app.scorecard()
        self.assertEqual(scorecard["rejected"], 1)
        self.assertEqual(scorecard["executed"], 0)

    def test_imported_done_without_acceptance_is_not_executed(self):
        # 资料中 AC-006 等记录标为 done 但 accepted_at 为空。
        self.import_sample_data()
        action = self.app.get_action("AC-006")
        self.assertEqual(action["execution_state"], "done")
        self.assertFalse(action["accepted"])
        self.assertFalse(action["counted_as_executed"])
        self.assertEqual(action["current_state"], "unaccepted-but-recorded")
        scorecard = self.app.scorecard(as_of="2026-11-24")
        self.assertEqual(scorecard["recorded_without_acceptance"], 8)
        # 资料没有完成日期，不能臆造为按时完成。
        self.assertEqual(scorecard["executed_on_time"], 0)
        self.assertEqual(scorecard["executed_date_unknown"], 8)
        self.assertEqual(scorecard["executed"], 8)
        # 资料中的 late 表示逾期未完成，不能计入已执行。
        self.assertEqual(scorecard["executed_late"], 0)
        # 12 条 late 中 4 条同时缺接受记录（归入 recorded_without_acceptance），
        # 其余 8 条为逾期未完成。
        self.assertEqual(scorecard["overdue_unfinished"], 8)

    def test_outcome_review_uses_original_metric_version(self):
        action_id = self.propose()
        self.app.accept_action(
            action_id, actor="store-lead", owner="MGR-7",
            due_at="2026-11-15", accepted_at="2026-11-12",
        )
        self.app.complete_action(
            action_id, actor="store-lead", outcome_ref="OUT-1", completed_at="2026-11-14"
        )
        outcome = self.app.record_outcome(
            action_id, actor="data-team", reviewer="data-team",
            period_start="2026-11-08", period_end="2026-11-11",
        )
        self.assertEqual(outcome["metric_version"], "MV-1")
        # 回看 210 vs 诊断期 150 = +40%
        self.assertEqual(outcome["snapshot"]["comparison"]["delta_pct"], 40.0)
        self.assertEqual(outcome["snapshot"]["diagnosis_period"], ["2026-11-04", "2026-11-07"])
        with self.assertRaises(Exception) as ctx:
            self.app.record_outcome(
                action_id, actor="data-team", reviewer="data-team",
                period_start="2026-11-08", period_end="2026-11-11",
                metric_version="MV-9",
            )
        self.assertIn("必须使用原诊断口径", str(ctx.exception))

    def test_metric_revision_does_not_rewrite_history(self):
        # workflow 在建模时已自动登记 sales/MV-1；先确认它不能就地改写。
        with self.assertRaises(Exception):
            self.app.register_metric_version(
                metric="sales", version="MV-1", actor="data-team",
                effective_from="2026-11-01", definition="按交易行数计",
            )
        revision = self.app.register_metric_version(
            metric="sales", version="MV-2", actor="data-team",
            effective_from="2026-11-15", definition="改为按件数计",
        )
        self.assertEqual(revision["version"], "MV-2")
        diagnosis = self.app.get_diagnosis(self.diagnosis_id)
        self.assertEqual(diagnosis["metric_version"], "MV-1")
        evidence = self.app.latest_evidence(self.diagnosis_id)
        self.assertEqual(evidence["snapshot"]["diagnosis"]["metric_version"], "MV-1")

    def test_registered_metric_version_cannot_be_silently_redefined(self):
        self.app.register_metric_version(
            metric="sales", version="MV-7", actor="data-team",
            effective_from="2026-11-01", definition="按交易行数计",
        )
        with self.assertRaises(Exception) as ctx:
            self.app.register_metric_version(
                metric="sales", version="MV-7", actor="data-team",
                effective_from="2026-11-15", definition="改为按件数计",
            )
        self.assertIn("不得悄悄改写", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
