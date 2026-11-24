"""同业可比范围、证据可复现性与数据库约束。"""

from __future__ import annotations

import unittest

from support import AppTestCase, daily
from store_performance_diagnosis.analytics import build_evidence


class PeerScopeTests(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_stores()
        rows = []
        peers = {"ST-B": 100, "ST-C": 100, "ST-D": 100}
        for day in ("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"):
            rows.append(daily("ST-A", day, value=50))
            for store_id, value in peers.items():
                rows.append(daily(store_id, day, value=value))
        self.seed_daily_metrics(rows)

    def test_peer_median_and_deviation_are_reference_only(self):
        evidence = self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-01",
            period_end="2026-11-04",
            metric_version="MV-1",
            actor="analyst",
        )["evidence"]
        peer = evidence["peer"]
        self.assertTrue(peer["sufficient"])
        self.assertEqual(peer["peer_count"], 3)
        self.assertEqual(peer["median"], 100)
        self.assertEqual(peer["store_deviation_pct"], -50.0)
        self.assertNotIn("rank", peer)
        self.assertIn("不构成经营能力排名", peer["note"])

    def test_renovating_store_is_excluded_from_peer_pool(self):
        self.app.import_file(
            self.write_csv(
                "store_context.csv",
                ["store_id", "region", "format", "open_minutes",
                 "renovation_state", "peer_group"],
                [{"store_id": "ST-B", "region": "R-1", "format": "standard",
                  "open_minutes": 600, "renovation_state": "renovating",
                  "peer_group": "PG-1"}],
            ),
            filename="store_context.csv",
        )
        evidence = self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-01",
            period_end="2026-11-04",
            metric_version="MV-1",
            actor="analyst",
        )["evidence"]
        peer_ids = {p["store_id"] for p in evidence["peer"]["peers"]}
        self.assertNotIn("ST-B", peer_ids)
        self.assertEqual(evidence["peer"]["peer_count"], 2)

    def test_evidence_is_reproducible_for_identical_inputs(self):
        self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-01",
            period_end="2026-11-04",
            metric_version="MV-1",
            actor="analyst",
        )
        diagnosis = self.app.list_diagnoses()[0]["diagnosis_id"]
        first = build_evidence(self.app.db, diagnosis, generated_at="fixed")
        second = build_evidence(self.app.db, diagnosis, generated_at="fixed")
        self.assertEqual(first, second)
        from store_performance_diagnosis.analytics import evidence_hashes

        self.assertEqual(evidence_hashes(first), evidence_hashes(second))

    def test_diagnosis_identity_cannot_be_rewritten(self):
        self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-01",
            period_end="2026-11-04",
            metric_version="MV-1",
            actor="analyst",
        )
        diagnosis = self.app.list_diagnoses()[0]["diagnosis_id"]
        with self.assertRaises(Exception):
            with self.app.db.session(write=True) as conn:
                conn.execute(
                    "UPDATE diagnosis SET metric_version='MV-9' WHERE diagnosis_id=?",
                    (diagnosis,),
                )

    def test_metric_versions_are_not_mixed(self):
        # 同一门店新增一个版本但数值不同，诊断仍只取声明版本。
        self.seed_daily_metrics(
            [daily("ST-A", "2026-11-01", value=999, version="MV-2")]
        )
        evidence = self.app.diagnose(
            store_id="ST-A",
            metric="traffic",
            period_start="2026-11-01",
            period_end="2026-11-04",
            metric_version="MV-1",
            actor="analyst",
        )["evidence"]
        values = [s["value"] for s in evidence["windows"]["period"]["series"] if s["used"]]
        self.assertEqual(values, [50, 50, 50, 50])
        self.assertNotIn(999, values)

    def test_imported_diagnosis_with_no_matching_version_is_insufficient(self):
        self.import_sample_data()
        self.app.diagnose_with_id("DG-005", actor="analyst")
        evidence = self.app.latest_evidence("DG-005")["snapshot"]
        self.assertEqual(evidence["status"], "insufficient_evidence")
        self.assertEqual(evidence["summary"]["usable_observations"], 0)


class TransactionTests(AppTestCase):
    def test_failed_workflow_leaves_no_partial_state(self):
        self.seed_stores()
        self.seed_daily_metrics(
            [daily("ST-A", f"2026-11-0{d}", value=100) for d in range(1, 5)]
        )
        with self.assertRaises(Exception):
            self.app.propose_action(
                diagnosis_id="DG-NOT-EXIST", action_type="display", actor="manager"
            )
        self.assertEqual(self.app.status()["counts"]["action"], 0)

    def test_accept_requires_positive_due_date_after_acceptance(self):
        self.seed_stores()
        self.seed_daily_metrics(
            [daily("ST-A", f"2026-11-0{d}", value=100) for d in range(1, 5)]
        )
        result = self.app.diagnose(
            store_id="ST-A", metric="traffic",
            period_start="2026-11-01", period_end="2026-11-04",
            metric_version="MV-1", actor="analyst",
        )
        diagnosis_id = result["diagnosis"]["diagnosis_id"]
        self.app.submit_diagnosis(diagnosis_id, actor="manager")
        action = self.app.propose_action(
            diagnosis_id=diagnosis_id, action_type="observe", actor="manager",
            due_at="2026-11-10",
        )
        with self.assertRaises(Exception):
            self.app.accept_action(
                action["action_id"], actor="store", owner="MGR-1",
                due_at="2026-11-01", accepted_at="2026-11-05",
            )
        self.assertEqual(self.app.get_action(action["action_id"])["execution_state"], "proposed")


if __name__ == "__main__":
    unittest.main()
