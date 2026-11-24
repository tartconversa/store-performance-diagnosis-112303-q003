"""资料导入与契约测试。"""

from __future__ import annotations

import unittest

from support import ROOT, AppTestCase, daily
from store_performance_diagnosis.materials import MaterialError


class ImportContractTests(AppTestCase):
    def test_sample_materials_import_with_documented_counts(self):
        results = self.app.import_materials(ROOT / "data")
        by_name = {r["filename"]: r for r in results}
        self.assertEqual(len(results), 5)
        self.assertEqual(by_name["store_context.csv"]["rows"], 24)
        self.assertEqual(by_name["daily_metrics.csv"]["rows"], 120)
        self.assertEqual(by_name["events.csv"]["rows"], 60)
        self.assertEqual(by_name["diagnoses.csv"]["rows"], 36)
        self.assertEqual(by_name["actions.csv"]["rows"], 48)
        status = self.app.status()
        self.assertEqual(status["counts"]["store"], 24)
        self.assertEqual(status["counts"]["diagnosis"], 36)
        self.assertEqual(status["counts"]["action"], 48)

    def test_reimporting_identical_file_is_idempotent(self):
        first = self.app.import_file(ROOT / "data" / "store_context.csv")
        second = self.app.import_file(ROOT / "data" / "store_context.csv")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(self.app.status()["counts"]["store"], 24)

    def test_device_out_value_is_stored_as_missing_not_zero(self):
        result = self.seed_daily_metrics(
            [daily("ST-A", "2026-11-01", "traffic", value=137, state="device-out")]
        )
        self.assertTrue(any(w["code"] == "value-on-missing-source" for w in result["warnings"]))
        row = self.app.db.fetch_one(
            "SELECT value, source_state FROM daily_metric "
            "WHERE store_id='ST-A' AND date='2026-11-01'"
        )
        self.assertIsNone(row["value"])
        self.assertEqual(row["source_state"], "device-out")

    def test_bad_header_is_rejected(self):
        path = self.write_csv(
            "daily_metrics.csv",
            ["store_id", "date", "metric"],
            [{"store_id": "ST-A", "date": "2026-11-01", "metric": "traffic"}],
        )
        with self.assertRaises(MaterialError):
            self.app.import_file(path, filename="daily_metrics.csv")

    def test_execution_without_acceptance_is_flagged(self):
        self.import_sample_data()
        warnings = self.app.import_warnings()
        flagged = {w["code"] for w in warnings}
        self.assertIn("execution-without-acceptance", flagged)
        issues = self.app.verify_materials()["issues"]
        unaccepted = [
            i for i in issues
            if i["code"] == "execution-without-acceptance"
        ]
        self.assertTrue(unaccepted)

    def test_orphan_store_is_reported(self):
        self.import_sample_data()
        issues = self.app.verify_materials()["issues"]
        orphan = [i for i in issues if i["code"] == "orphan-store"]
        self.assertTrue(any(i["entity"] == "ST-000" for i in orphan))

    def test_diagnoses_without_matching_version_are_reported(self):
        self.import_sample_data()
        issues = self.app.verify_materials()["issues"]
        no_obs = [i for i in issues if i["code"] == "diagnosis-no-observations"]
        self.assertEqual(len(no_obs), 36)


if __name__ == "__main__":
    unittest.main()
