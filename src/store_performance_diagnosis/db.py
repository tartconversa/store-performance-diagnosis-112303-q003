"""SQLite 存储与模式。

设计要点：
- 诊断证据和行动结果只追加、不修改（数据库触发器兜底），指标口径修订不会
  改写历史。
- 每日指标以 ``(store_id, date, metric, metric_version)`` 为主键，同一门店同一
  指标可以并存多个口径版本。
- 事实表不强制外键到门店属性，允许先导入事实、后补齐门店资料；缺失引用在导入
  时记录为警告。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store (
    store_id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    format TEXT NOT NULL,
    open_minutes INTEGER,
    renovation_state TEXT NOT NULL,
    peer_group TEXT NOT NULL,
    import_batch TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_metric (
    store_id TEXT NOT NULL,
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    metric_version TEXT NOT NULL,
    source_state TEXT NOT NULL,
    import_batch TEXT NOT NULL,
    PRIMARY KEY (store_id, date, metric, metric_version)
);

CREATE TABLE IF NOT EXISTS event (
    event_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    campaign_id TEXT NOT NULL DEFAULT '',
    inventory_state TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    import_batch TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_version (
    metric TEXT NOT NULL,
    version TEXT NOT NULL,
    effective_from TEXT,
    definition TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (metric, version)
);

CREATE TABLE IF NOT EXISTS metric_version_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL,
    version TEXT NOT NULL,
    effective_from TEXT,
    definition TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metric_revision
ON metric_version_revision(metric, version, id);

CREATE TRIGGER IF NOT EXISTS trg_metric_revision_no_update
BEFORE UPDATE ON metric_version_revision
BEGIN
    SELECT RAISE(ABORT, '指标口径修订记录为只追加记录，不能修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_metric_revision_no_delete
BEFORE DELETE ON metric_version_revision
BEGIN
    SELECT RAISE(ABORT, '指标口径修订记录为只追加记录，不能删除');
END;

CREATE TABLE IF NOT EXISTS diagnosis (
    diagnosis_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    review_state TEXT NOT NULL,
    review_note TEXT NOT NULL DEFAULT '',
    source_batch TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnosis_store ON diagnosis(store_id, metric);

CREATE TABLE IF NOT EXISTS diagnosis_evidence (
    diagnosis_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_grade TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY (diagnosis_id, revision),
    FOREIGN KEY (diagnosis_id) REFERENCES diagnosis(diagnosis_id)
);

CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update
BEFORE UPDATE ON diagnosis_evidence
BEGIN
    SELECT RAISE(ABORT, '诊断证据为只追加记录，不能修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_no_delete
BEFORE DELETE ON diagnosis_evidence
BEGIN
    SELECT RAISE(ABORT, '诊断证据为只追加记录，不能删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_diagnosis_identity_no_update
BEFORE UPDATE OF diagnosis_id, store_id, metric, period_start, period_end, metric_version
ON diagnosis
BEGIN
    SELECT RAISE(ABORT, '诊断的指标口径与期间不能改写，请新建诊断或显式重开');
END;

CREATE TABLE IF NOT EXISTS diagnosis_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnosis_id TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    note TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (diagnosis_id) REFERENCES diagnosis(diagnosis_id)
);

CREATE TABLE IF NOT EXISTS action (
    action_id TEXT PRIMARY KEY,
    diagnosis_id TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL,
    accepted_at TEXT,
    due_at TEXT,
    execution_state TEXT NOT NULL,
    outcome_ref TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    evidence_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_batch TEXT,
    FOREIGN KEY (diagnosis_id) REFERENCES diagnosis(diagnosis_id)
);

CREATE INDEX IF NOT EXISTS idx_action_diagnosis ON action(diagnosis_id);
CREATE INDEX IF NOT EXISTS idx_action_due ON action(due_at);

CREATE TABLE IF NOT EXISTS action_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    note TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (action_id) REFERENCES action(action_id)
);

CREATE TABLE IF NOT EXISTS action_outcome (
    outcome_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    comparable INTEGER NOT NULL CHECK (comparable IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (action_id) REFERENCES action(action_id)
);

CREATE INDEX IF NOT EXISTS idx_outcome_action ON action_outcome(action_id);

CREATE TRIGGER IF NOT EXISTS trg_outcome_no_update
BEFORE UPDATE ON action_outcome
BEGIN
    SELECT RAISE(ABORT, '行动结果为只追加记录，不能修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_outcome_no_delete
BEFORE DELETE ON action_outcome
BEGIN
    SELECT RAISE(ABORT, '行动结果为只追加记录，不能删除');
END;

CREATE TABLE IF NOT EXISTS import_batch (
    batch_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    warning_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_checksum ON import_batch(checksum);

CREATE TABLE IF NOT EXISTS import_warning (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    code TEXT NOT NULL,
    row_number INTEGER,
    message TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES import_batch(batch_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


class Database:
    """轻量 SQLite 连接管理。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        with self.session(write=True) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库模式版本不兼容: {row['value'] if row else '未知'}"
                )

    @contextmanager
    def session(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    # -- 便捷读取 ---------------------------------------------------------
    def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(conn.execute(sql, params).fetchall())

    def fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        with self.session() as conn:
            return conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self.session() as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]
