"""设备缺数场景：缺失不能按零解释，证据必须标明区间和等级。"""

from __future__ import annotations

import unittest

from support import AppTestCase, daily


class DeviceOutageTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_stores()
        rows = []
        # 基线 11-01..11-03：稳定在 100
        for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
            rows.append(daily("ST-A", day, value=100))
        # 诊断期 11-04..11-06：两天设备停机 + 一天真实值
        rows.append(daily("ST-A", "2026-11-04", value=140, state="device-out"))
        rows.append(daily("ST-A", "2026-11-05", value=999, state="device-out"))
        rows.append(daily("ST-A", "2026-11-06", value=102))
        self.seed_daily_metrics(rows)

    def _diagnose(self):
        return self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-04",
            period_end="2026-11-06",
            metric_version="MV-1",
            actor="analyst",
        )["evidence"]

    def test_missing_days_are_not_treated_as_zero(self):
        evidence = self._diagnose()
        self.assertEqual(evidence["summary"]["current_median"], 102)
        self.assertEqual(evidence["summary"]["usable_observations"], 1)
        self.assertEqual(evidence["windows"]["period"]["observation_count"], 1)
        used = [s for s in evidence["windows"]["period"]["series"] if s["used"]]
        self.assertEqual([s["value"] for s in used], [102])

    def test_coverage_is_based_on_calendar_days(self):
        evidence = self._diagnose()
        self.assertAlmostEqual(evidence["summary"]["coverage"], 1 / 3, places=4)
        self.assertEqual(
            evidence["windows"]["period"]["missing_states"], {"device-out": 2}
        )

    def test_evidence_marks_insufficient_and_device_interval(self):
        evidence = self._diagnose()
        self.assertEqual(evidence["status"], "insufficient_evidence")
        self.assertEqual(evidence["evidence_grade"], "insufficient")
        runs = evidence["missing_runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["start"], "2026-11-04")
        self.assertEqual(runs[0]["end"], "2026-11-05")
        self.assertEqual(runs[0]["source_states"], {"device-out": 2})
        self.assertIn("不能按零解释", " ".join(runs[0]["notes"]))
        self.assertIn("device-out", {c["type"] for c in evidence["confounders"]})
        self.assertTrue(
            any("缺失不等于零" in caveat for caveat in evidence["caveats"])
        )

    def test_no_root_cause_asserted_even_with_data_loss(self):
        evidence = self._diagnose()
        self.assertFalse(any(c["is_root_cause"] for c in evidence["candidate_causes"]))
        self.assertFalse(evidence["policy"]["root_cause_asserted"])
        self.assertIn("指标变化本身不构成根因", evidence["disclaimers"][0])

    def test_insufficient_evidence_only_allows_data_actions(self):
        result = self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-04",
            period_end="2026-11-06",
            metric_version="MV-1",
            actor="analyst",
        )
        diagnosis_id = result["diagnosis"]["diagnosis_id"]
        self.app.submit_diagnosis(diagnosis_id, actor="manager-1")
        with self.assertRaises(Exception):
            self.app.propose_action(
                diagnosis_id=diagnosis_id,
                action_type="display",
                actor="manager-1",
            )
        action = self.app.propose_action(
            diagnosis_id=diagnosis_id,
            action_type="device-repair",
            actor="manager-1",
            due_at="2026-11-20",
        )
        self.assertEqual(action["execution_state"], "proposed")

    def test_evidence_snapshot_is_append_only(self):
        result = self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-04",
            period_end="2026-11-06",
            metric_version="MV-1",
            actor="analyst",
        )
        diagnosis_id = result["diagnosis"]["diagnosis_id"]
        self.app.refresh_evidence(diagnosis_id, actor="analyst")
        history = self.app.evidence_history(diagnosis_id)
        self.assertEqual([h["revision"] for h in history], [1, 2])
        with self.assertRaises(Exception):
            with self.app.db.session(write=True) as conn:
                conn.execute(
                    "UPDATE diagnosis_evidence SET status='diagnosed' WHERE diagnosis_id=?",
                    (diagnosis_id,),
                )


if __name__ == "__main__":
    unittest.main()
