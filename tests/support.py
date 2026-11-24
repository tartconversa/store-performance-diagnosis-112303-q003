"""共享测试夹具：临时数据库 + 可重复导入的资料目录。"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
# 未安装包时，让 `python -m unittest discover -s tests` 直接可用。
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from store_performance_diagnosis.app import StoreDiagnosisApp  # noqa: E402


class AppTestCase(unittest.TestCase):
    """每个测试使用独立的临时数据库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.app = StoreDiagnosisApp(self.tmpdir / "test.sqlite3")

    # -- 资料构造 ---------------------------------------------------------
    def write_csv(self, filename: str, columns: Iterable[str], rows: Iterable[dict]) -> Path:
        directory = self.tmpdir / "materials"
        directory.mkdir(exist_ok=True)
        path = directory / filename
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(columns))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def import_material(self, filename: str, columns: Iterable[str], rows: Iterable[dict]) -> dict:
        path = self.write_csv(filename, columns, rows)
        return self.app.import_file(path, filename=filename)

    def seed_stores(self) -> None:
        self.import_material(
            "store_context.csv",
            ["store_id", "region", "format", "open_minutes", "renovation_state", "peer_group"],
            [
                {"store_id": "ST-A", "region": "R-1", "format": "standard",
                 "open_minutes": 600, "renovation_state": "normal", "peer_group": "PG-1"},
                {"store_id": "ST-B", "region": "R-1", "format": "standard",
                 "open_minutes": 600, "renovation_state": "normal", "peer_group": "PG-1"},
                {"store_id": "ST-C", "region": "R-1", "format": "standard",
                 "open_minutes": 600, "renovation_state": "normal", "peer_group": "PG-1"},
                {"store_id": "ST-D", "region": "R-1", "format": "standard",
                 "open_minutes": 600, "renovation_state": "normal", "peer_group": "PG-1"},
            ],
        )

    def seed_daily_metrics(self, rows: list[dict]) -> dict:
        return self.import_material(
            "daily_metrics.csv",
            ["store_id", "date", "metric", "value", "metric_version", "source_state"],
            rows,
        )

    def seed_events(self, rows: list[dict]) -> dict:
        return self.import_material(
            "events.csv",
            ["event_id", "store_id", "date", "event_type", "campaign_id",
             "inventory_state", "source_ref"],
            rows,
        )

    def import_sample_data(self) -> None:
        self.app.import_materials(ROOT / "data")


def daily(
    store_id: str,
    date: str,
    metric: str = "traffic",
    value: float | None = 100,
    version: str = "MV-1",
    state: str = "complete",
) -> dict:
    return {
        "store_id": store_id,
        "date": date,
        "metric": metric,
        "value": "" if value is None else value,
        "metric_version": version,
        "source_state": state,
    }
