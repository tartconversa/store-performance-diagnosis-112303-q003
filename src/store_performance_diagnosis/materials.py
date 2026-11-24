"""经营资料导入与校验。

导入是幂等的：同一份文件按内容校验和识别，重复导入不会覆盖已形成的诊断或
行动记录；同一行被新版本文件替换时按业务主键更新，并保留导入批次。

校验只报告问题，不擅自修正：``device-out`` 的数值不会被当作 0，未接受却标记
完成的行动也不会被算作已执行。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import (
    EVENT_TYPES,
    EXECUTION_STATES,
    IMPORT_ORDER,
    MATERIAL_SCHEMAS,
    MISSING_SOURCE_STATES,
    REVIEW_STATES,
    SOURCE_STATES,
)
from .db import Database
from .utils import (
    audit,
    now_iso,
    parse_date,
    record_event,
    sha256_file,
)


@dataclass
class Warning:
    code: str
    row_number: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "row": self.row_number, "message": self.message}


@dataclass
class ImportResult:
    batch_id: str
    filename: str
    rows: int
    warnings: list[Warning] = field(default_factory=list)
    status: str = "imported"
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "filename": self.filename,
            "rows": self.rows,
            "status": self.status,
            "duplicate": self.duplicate,
            "warnings": [w.as_dict() for w in self.warnings],
        }


class MaterialError(ValueError):
    """资料格式或内容不可导入。"""


Header = tuple[str, ...]


def _read_rows(path: Path, expected_header: Header) -> list[dict[str, str]]:
    try:
        stream = path.open(encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise MaterialError(f"找不到资料文件: {path}") from exc
    with stream:
        reader = csv.DictReader(stream)
        actual = tuple(reader.fieldnames or ())
        if actual != expected_header:
            raise MaterialError(
                f"{path.name} 表头不符。期望 {expected_header}，实际 {actual}"
            )
        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise MaterialError(f"{path.name} 第 {line_number} 行列数过多")
            cleaned = {k: (v or "").strip() for k, v in row.items()}
            rows.append({"_line": str(line_number), **cleaned})
        return rows


def _require(row: dict[str, str], field: str, warnings: list[Warning]) -> str:
    value = row.get(field, "")
    if not value:
        warnings.append(
            Warning(
                "empty-field",
                int(row["_line"]),
                f"{field} 为空，已跳过该行",
            )
        )
    return value


def _valid_date(value: str) -> bool:
    try:
        parse_date(value)
    except ValueError:
        return False
    return True


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _existing_store_ids(conn) -> set[str]:
    return {r["store_id"] for r in conn.execute("SELECT store_id FROM store")}


def _warn_unknown_store(
    row: dict[str, str], known: set[str], warnings: list[Warning], label: str
) -> None:
    store_id = row["store_id"]
    if known and store_id not in known:
        warnings.append(
            Warning(
                "unknown-store",
                int(row["_line"]),
                f"{label} 引用了门店属性中不存在的 store_id={store_id}；已导入，"
                "但参与同业比较前需补齐门店资料",
            )
        )


# ---------------------------------------------------------------------------
# 各文件的校验与写入
# ---------------------------------------------------------------------------


def _import_store_context(conn, rows, batch, warnings) -> int:
    written = 0
    for row in rows:
        for required in ("store_id", "region", "format", "renovation_state", "peer_group"):
            if not row[required]:
                _require(row, required, warnings)
                break
        else:
            open_minutes = row["open_minutes"]
            if open_minutes and not open_minutes.isdigit():
                warnings.append(
                    Warning(
                        "invalid-open-minutes",
                        int(row["_line"]),
                        f"open_minutes={open_minutes!r} 不是非负整数，已置空",
                    )
                )
                open_minutes = ""
            conn.execute(
                """
                INSERT INTO store(store_id, region, format, open_minutes,
                                  renovation_state, peer_group, import_batch)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id) DO UPDATE SET
                    region=excluded.region,
                    format=excluded.format,
                    open_minutes=excluded.open_minutes,
                    renovation_state=excluded.renovation_state,
                    peer_group=excluded.peer_group,
                    import_batch=excluded.import_batch
                """,
                (
                    row["store_id"],
                    row["region"],
                    row["format"],
                    int(open_minutes) if open_minutes else None,
                    row["renovation_state"],
                    row["peer_group"],
                    batch,
                ),
            )
            written += 1
    return written


def _import_daily_metrics(conn, rows, batch, warnings) -> int:
    known = _existing_store_ids(conn)
    written = 0
    for row in rows:
        problem = False
        if not row["store_id"]:
            problem = not _require(row, "store_id", warnings)
        if not row["metric"]:
            problem = problem or not _require(row, "metric", warnings)
        if not _valid_date(row["date"]):
            warnings.append(
                Warning("invalid-date", int(row["_line"]), f"date={row['date']!r} 无效")
            )
            problem = True
        if row["source_state"] not in SOURCE_STATES:
            warnings.append(
                Warning(
                    "unknown-source-state",
                    int(row["_line"]),
                    f"source_state={row['source_state']!r} 未定义，按缺数处理",
                )
            )
            problem = True
        if not row["metric_version"]:
            warnings.append(
                Warning("empty-version", int(row["_line"]), "metric_version 为空")
            )
            problem = True
        if problem:
            continue

        value = _as_float(row["value"])
        missing = row["source_state"] in MISSING_SOURCE_STATES
        if missing:
            if value is not None:
                warnings.append(
                    Warning(
                        "value-on-missing-source",
                        int(row["_line"]),
                        f"source_state={row['source_state']} 表示设备缺数，"
                        f"原值 {row['value']!r} 仅保留备查，分析时按缺失处理，不按 0",
                    )
                )
            # 缺数不保存为可计算数值，避免下游误用。
            value = None
        elif value is None:
            warnings.append(
                Warning(
                    "invalid-value",
                    int(row["_line"]),
                    f"value={row['value']!r} 不是数值，按缺数处理",
                )
            )
            value = None
        _warn_unknown_store(row, known, warnings, "daily_metrics")

        conn.execute(
            """
            INSERT INTO daily_metric(store_id, date, metric, value, metric_version,
                                     source_state, import_batch)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, date, metric, metric_version) DO UPDATE SET
                value=excluded.value,
                source_state=excluded.source_state,
                import_batch=excluded.import_batch
            """,
            (
                row["store_id"],
                row["date"],
                row["metric"],
                value,
                row["metric_version"],
                row["source_state"],
                batch,
            ),
        )
        written += 1
    return written


def _import_events(conn, rows, batch, warnings) -> int:
    known = _existing_store_ids(conn)
    written = 0
    for row in rows:
        if not row["event_id"] or not row["store_id"] or not row["date"]:
            warnings.append(
                Warning(
                    "empty-field",
                    int(row["_line"]),
                    "event_id/store_id/date 存在空值，已跳过该行",
                )
            )
            continue
        if not _valid_date(row["date"]):
            warnings.append(
                Warning("invalid-date", int(row["_line"]), f"date={row['date']!r} 无效")
            )
            continue
        if row["event_type"] not in EVENT_TYPES:
            warnings.append(
                Warning(
                    "unknown-event-type",
                    int(row["_line"]),
                    f"event_type={row['event_type']!r} 未在目录中，保留原值但不用于归因",
                )
            )
        _warn_unknown_store(row, known, warnings, "events")
        conn.execute(
            """
            INSERT INTO event(event_id, store_id, date, event_type, campaign_id,
                              inventory_state, source_ref, import_batch)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                store_id=excluded.store_id,
                date=excluded.date,
                event_type=excluded.event_type,
                campaign_id=excluded.campaign_id,
                inventory_state=excluded.inventory_state,
                source_ref=excluded.source_ref,
                import_batch=excluded.import_batch
            """,
            (
                row["event_id"],
                row["store_id"],
                row["date"],
                row["event_type"],
                row["campaign_id"],
                row["inventory_state"],
                row["source_ref"],
                batch,
            ),
        )
        written += 1
    return written


DIAGNOSIS_STATES = {"draft", "reviewed", "reopened", "rejected"}


def _import_diagnoses(conn, rows, batch, warnings) -> int:
    known = _existing_store_ids(conn)
    written = 0
    for row in rows:
        if not all(
            row[f]
            for f in ("diagnosis_id", "store_id", "metric", "period_start", "period_end", "metric_version")
        ):
            warnings.append(
                Warning(
                    "empty-field",
                    int(row["_line"]),
                    "diagnosis_id/store_id/metric/period/metric_version 存在空值，已跳过",
                )
            )
            continue
        if not (_valid_date(row["period_start"]) and _valid_date(row["period_end"])):
            warnings.append(
                Warning("invalid-date", int(row["_line"]), "诊断期间日期无效，已跳过")
            )
            continue
        if parse_date(row["period_start"]) > parse_date(row["period_end"]):
            warnings.append(
                Warning(
                    "reversed-period",
                    int(row["_line"]),
                    "period_start 晚于 period_end，已跳过",
                )
            )
            continue
        if row["review_state"] not in REVIEW_STATES:
            warnings.append(
                Warning(
                    "unknown-review-state",
                    int(row["_line"]),
                    f"review_state={row['review_state']!r} 未定义，按 draft 处理",
                )
            )
            row["review_state"] = "draft"
        _warn_unknown_store(row, known, warnings, "diagnoses")

        # 同一个 diagnosis_id 的指标口径和期间不可被后续导入悄悄改写。
        existing = conn.execute(
            "SELECT store_id, metric, period_start, period_end, metric_version "
            "FROM diagnosis WHERE diagnosis_id=?",
            (row["diagnosis_id"],),
        ).fetchone()
        if existing is not None:
            identity = (
                existing["store_id"],
                existing["metric"],
                existing["period_start"],
                existing["period_end"],
                existing["metric_version"],
            )
            incoming = (
                row["store_id"],
                row["metric"],
                row["period_start"],
                row["period_end"],
                row["metric_version"],
            )
            if identity != incoming:
                warnings.append(
                    Warning(
                        "diagnosis-identity-conflict",
                        int(row["_line"]),
                        f"{row['diagnosis_id']} 已存在且指标口径/期间不同；未覆盖已有诊断",
                    )
                )
                continue
            conn.execute(
                "UPDATE diagnosis SET review_state=?, updated_at=? WHERE diagnosis_id=?",
                (row["review_state"], now_iso(), row["diagnosis_id"]),
            )
            written += 1
            continue

        now = now_iso()
        conn.execute(
            """
            INSERT INTO diagnosis(diagnosis_id, store_id, metric, period_start,
                                  period_end, metric_version, review_state,
                                  review_note, source_batch, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            """,
            (
                row["diagnosis_id"],
                row["store_id"],
                row["metric"],
                row["period_start"],
                row["period_end"],
                row["metric_version"],
                row["review_state"],
                batch,
                now,
                now,
            ),
        )
        record_event(
            conn,
            "diagnosis_event",
            id_column="diagnosis_id",
            entity_id=row["diagnosis_id"],
            actor="import",
            event_type="imported",
            to_state=row["review_state"],
            note=f"来自 {batch}",
        )
        written += 1
    return written


def _import_actions(conn, rows, batch, warnings) -> int:
    known_diagnoses = {r["diagnosis_id"] for r in conn.execute("SELECT diagnosis_id FROM diagnosis")}
    written = 0
    for row in rows:
        if not row["action_id"] or not row["diagnosis_id"] or not row["action_type"]:
            warnings.append(
                Warning(
                    "empty-field",
                    int(row["_line"]),
                    "action_id/diagnosis_id/action_type 存在空值，已跳过",
                )
            )
            continue
        if row["diagnosis_id"] not in known_diagnoses:
            warnings.append(
                Warning(
                    "unknown-diagnosis",
                    int(row["_line"]),
                    f"action 引用了不存在的诊断 {row['diagnosis_id']}，已跳过",
                )
            )
            continue
        if row["execution_state"] not in EXECUTION_STATES:
            warnings.append(
                Warning(
                    "unknown-execution-state",
                    int(row["_line"]),
                    f"execution_state={row['execution_state']!r} 未定义，按 proposed 处理",
                )
            )
            row["execution_state"] = "proposed"
        if row["accepted_at"] and not _valid_date(row["accepted_at"]):
            warnings.append(
                Warning("invalid-date", int(row["_line"]), "accepted_at 无效，已置空")
            )
            row["accepted_at"] = ""
        if row["due_at"] and not _valid_date(row["due_at"]):
            warnings.append(
                Warning("invalid-date", int(row["_line"]), "due_at 无效，已置空")
            )
            row["due_at"] = ""
        if (
            row["accepted_at"]
            and row["due_at"]
            and parse_date(row["due_at"]) < parse_date(row["accepted_at"])
        ):
            warnings.append(
                Warning(
                    "due-before-acceptance",
                    int(row["_line"]),
                    "due_at 早于 accepted_at，需门店与区域经理核对",
                )
            )
        if row["execution_state"] in {"done", "late"} and not row["accepted_at"]:
            warnings.append(
                Warning(
                    "execution-without-acceptance",
                    int(row["_line"]),
                    f"{row['action_id']} 标记为 {row['execution_state']} 但缺少"
                    " accepted_at：未获门店接受的行动不能计入已执行",
                )
            )
        if row["execution_state"] == "done" and not row["outcome_ref"]:
            warnings.append(
                Warning(
                    "done-without-outcome",
                    int(row["_line"]),
                    f"{row['action_id']} 标记为 done 但缺少 outcome_ref，需补回看结果",
                )
            )
        if row["execution_state"] == "proposed" and row["accepted_at"]:
            warnings.append(
                Warning(
                    "acceptance-without-pending",
                    int(row["_line"]),
                    f"{row['action_id']} 标为 proposed 却已有 accepted_at；"
                    "需门店与区域经理核对接受状态",
                )
            )
        if row["execution_state"] == "accepted" and not row["accepted_at"]:
            warnings.append(
                Warning(
                    "accepted-without-timestamp",
                    int(row["_line"]),
                    f"{row['action_id']} 标为 accepted 但缺少 accepted_at，"
                    "无法确认门店是否已接受",
                )
            )
        if row["execution_state"] in {"proposed", "accepted"} and row["outcome_ref"]:
            # outcome_ref 可能存在，但尚未执行；保留而不当作已完成证据。
            warnings.append(
                Warning(
                    "premature-outcome-ref",
                    int(row["_line"]),
                    f"{row['action_id']} 尚未执行却已有 outcome_ref，仅登记不视为结果",
                )
            )

        now = now_iso()
        # 资料没有完成日期字段，不得用导入日期臆造；完成时间留空，由工作流补记。
        completed = None
        conn.execute(
            """
            INSERT INTO action(action_id, diagnosis_id, owner, action_type,
                               accepted_at, due_at, execution_state, outcome_ref,
                               completed_at, evidence_ref, created_at, updated_at,
                               source_batch)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            ON CONFLICT(action_id) DO UPDATE SET
                owner=excluded.owner,
                action_type=excluded.action_type,
                accepted_at=excluded.accepted_at,
                due_at=excluded.due_at,
                execution_state=excluded.execution_state,
                outcome_ref=excluded.outcome_ref,
                source_batch=excluded.source_batch,
                updated_at=excluded.updated_at
            """,
            (
                row["action_id"],
                row["diagnosis_id"],
                row["owner"],
                row["action_type"],
                row["accepted_at"] or None,
                row["due_at"] or None,
                row["execution_state"],
                row["outcome_ref"],
                completed,
                now,
                now,
                batch,
            ),
        )
        record_event(
            conn,
            "action_event",
            id_column="action_id",
            entity_id=row["action_id"],
            actor="import",
            event_type="imported",
            to_state=row["execution_state"],
            note=f"来自 {batch}",
        )
        written += 1
    return written


_IMPORTERS: dict[str, Callable] = {
    "store_context.csv": _import_store_context,
    "daily_metrics.csv": _import_daily_metrics,
    "events.csv": _import_events,
    "diagnoses.csv": _import_diagnoses,
    "actions.csv": _import_actions,
}


def import_file(
    db: Database,
    path: str | Path,
    *,
    actor: str = "data-team",
    filename: str | None = None,
) -> ImportResult:
    """导入一个契约文件。返回批次信息与逐行警告。"""
    source = Path(path)
    name = filename or source.name
    if name not in MATERIAL_SCHEMAS:
        raise MaterialError(
            f"未知资料文件 {name!r}；支持: {', '.join(IMPORT_ORDER)}"
        )
    checksum = sha256_file(source)
    rows = _read_rows(source, MATERIAL_SCHEMAS[name])

    with db.session(write=True) as conn:
        previous = conn.execute(
            "SELECT batch_id, row_count, filename FROM import_batch "
            "WHERE checksum=? AND status='imported'",
            (checksum,),
        ).fetchone()
        if previous is not None:
            return ImportResult(
                batch_id=previous["batch_id"],
                filename=previous["filename"],
                rows=previous["row_count"],
                status="duplicate",
                duplicate=True,
            )

        batch_id = f"IMP-{checksum[:12]}"
        warnings: list[Warning] = []
        importer = _IMPORTERS[name]
        written = importer(conn, rows, batch_id, warnings)
        conn.execute(
            "INSERT INTO import_batch(batch_id, filename, imported_at, checksum, "
            "row_count, warning_count, status) VALUES(?, ?, ?, ?, ?, ?, 'imported')",
            (batch_id, name, now_iso(), checksum, written, len(warnings)),
        )
        for warning in warnings:
            conn.execute(
                "INSERT INTO import_warning(batch_id, code, row_number, message) "
                "VALUES(?, ?, ?, ?)",
                (batch_id, warning.code, warning.row_number, warning.message),
            )
        audit(
            conn,
            actor=actor,
            action="import",
            entity_type="import_batch",
            entity_id=batch_id,
            detail={
                "filename": name,
                "rows": written,
                "warnings": len(warnings),
                "checksum": checksum,
            },
        )
        return ImportResult(
            batch_id=batch_id,
            filename=name,
            rows=written,
            warnings=warnings,
        )


def import_directory(
    db: Database, directory: str | Path, *, actor: str = "data-team"
) -> list[ImportResult]:
    """按依赖顺序导入目录内全部契约文件。缺失文件不是错误。"""
    base = Path(directory)
    results: list[ImportResult] = []
    for name in IMPORT_ORDER:
        candidate = base / name
        if candidate.exists():
            results.append(import_file(db, candidate, actor=actor, filename=name))
    return results


def verify_materials(db: Database) -> dict[str, Any]:
    """跨文件一致性检查，返回可展示的问题清单。"""
    issues: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str, entity: str = "") -> None:
        issues.append(
            {"code": code, "severity": severity, "message": message, "entity": entity}
        )

    with db.session() as conn:
        known = {r["store_id"] for r in conn.execute("SELECT store_id FROM store")}
        for table, label in (
            ("daily_metric", "每日指标"),
            ("event", "事件"),
            ("diagnosis", "诊断"),
        ):
            for r in conn.execute(
                f"SELECT DISTINCT store_id FROM {table} WHERE store_id NOT IN (SELECT store_id FROM store)"
            ):
                add(
                    "orphan-store",
                    "warning",
                    f"{label}引用了门店属性中不存在的 {r['store_id']}，不能参与同业比较",
                    r["store_id"],
                )

        for r in conn.execute(
            """
            SELECT d.diagnosis_id, d.store_id, d.metric, d.metric_version,
                   COUNT(m.date) AS observations
            FROM diagnosis d
            LEFT JOIN daily_metric m
              ON m.store_id = d.store_id
             AND m.metric = d.metric
             AND m.metric_version = d.metric_version
             AND m.date BETWEEN d.period_start AND d.period_end
             AND m.value IS NOT NULL
            GROUP BY d.diagnosis_id
            """
        ):
            if r["observations"] == 0:
                add(
                    "diagnosis-no-observations",
                    "warning",
                    f"{r['diagnosis_id']} 在其指标版本 {r['metric_version']} 的"
                    f" {r['metric']} 数据中没有可用观测，无法复核",
                    r["diagnosis_id"],
                )

        for r in conn.execute(
            """
            SELECT a.action_id, a.execution_state, a.accepted_at
            FROM action a
            WHERE a.execution_state IN ('done', 'late') AND a.accepted_at IS NULL
            """
        ):
            add(
                "execution-without-acceptance",
                "warning",
                f"{r['action_id']} 标记为 {r['execution_state']} 但未记录门店接受，"
                "不计入已执行",
                r["action_id"],
            )

        for r in conn.execute(
            "SELECT action_id, outcome_ref FROM action WHERE execution_state='done' "
            "AND (outcome_ref IS NULL OR outcome_ref='')"
        ):
            add(
                "done-without-outcome",
                "warning",
                f"{r['action_id']} 已完成但没有结果引用",
                r["action_id"],
            )

        outcome_actions = {
            r["action_id"] for r in conn.execute("SELECT DISTINCT action_id FROM action_outcome")
        }
        for r in conn.execute(
            "SELECT action_id, outcome_ref FROM action WHERE outcome_ref != '' "
            "AND execution_state IN ('done','late')"
        ):
            if r["action_id"] not in outcome_actions:
                add(
                    "outcome-ref-without-record",
                    "info",
                    f"{r['action_id']} 引用了结果 {r['outcome_ref']}，但系统内尚无回看记录",
                    r["action_id"],
                )

        if not known:
            add("no-store-context", "error", "尚未导入门店属性，无法确定可比门店范围")

    return {"checked": True, "issue_count": len(issues), "issues": issues}


def list_warnings(db: Database, batch_id: str | None = None) -> list[dict[str, Any]]:
    if batch_id:
        rows = db.fetch_all(
            "SELECT * FROM import_warning WHERE batch_id=? ORDER BY id", (batch_id,)
        )
    else:
        rows = db.fetch_all("SELECT * FROM import_warning ORDER BY id")
    return [dict(r) for r in rows]


__all__ = [
    "ImportResult",
    "MaterialError",
    "Warning",
    "import_directory",
    "import_file",
    "list_warnings",
    "verify_materials",
]
