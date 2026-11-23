import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'daily_metrics.csv': (['store_id', 'date', 'metric', 'value', 'metric_version', 'source_state'], 120), 'store_context.csv': (['store_id', 'region', 'format', 'open_minutes', 'renovation_state', 'peer_group'], 24), 'events.csv': (['event_id', 'store_id', 'date', 'event_type', 'campaign_id', 'inventory_state', 'source_ref'], 60), 'diagnoses.csv': (['diagnosis_id', 'store_id', 'metric', 'period_start', 'period_end', 'metric_version', 'review_state'], 36), 'actions.csv': (['action_id', 'diagnosis_id', 'owner', 'action_type', 'accepted_at', 'due_at', 'execution_state', 'outcome_ref'], 48)}

class MaterialTests(unittest.TestCase):
    def test_declared_csv_contracts(self):
        for filename, (columns, count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertEqual(len(rows), count)
                self.assertTrue(all(None not in row for row in rows))

if __name__ == "__main__":
    unittest.main()
