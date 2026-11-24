"""工作流服务：诊断 → 区域经理复核 → 门店行动承诺 → 执行 → 结果回看。

状态机
------
诊断 ``review_state``::

    draft ──submit──> reviewed ──reopen──> reopened ──resubmit──> reviewed
      │                  │
      └────reject────> rejected

行动 ``execution_state``::

    proposed ──accept(门店)──> accepted ──complete──> done（逾期完成仍为 done）
        │                         │
        └──reject(门店)──> rejected
                                  └──(逾期未完成)──> late（未完成，不计入已执行）

关键约束：
- 未获门店接受（``accepted_at`` 为空）的行动不能标记为已执行；导入资料中
  这类记录只作为警告保留，不计入执行率。
- 行动完成必须引用结果，回看必须使用诊断原本的 ``metric_version``。
- 诊断的指标口径与期间不可修改；指标口径修订只追加新记录。
"""

from __future__ import annotations

from typing import Any

from .analytics import build_evidence, evidence_hashes
from .config import (
    ACTION_CATALOG,
    ACTION_PREFIX,
    DIAGNOSIS_PREFIX,
    METRIC_LABELS,
)
from .db import Database
from .utils import (
    add_days,
    canonical_json,
    now_iso,
    parse_date,
    record_event,
    stable_id,
    today_iso,
)


class WorkflowError(ValueError):
    """不允许的状态迁移或参数。"""


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------


def register_metric_revision(
    db: Database,
    *,
    metric: str,
    version: str,
    actor: str,
    effective_from: str | None = None,
    definition: str = "",
    note: str = "",
) -> dict[str, Any]:
    """登记指标口径。已存在的版本不会被覆盖，只追加修订记录。"""
    if not metric or not version:
        raise WorkflowError("metric 与 version 不能为空")
    if effective_from:
        parse_date(effective_from)
    now = now_iso()
    with db.session(write=True) as conn:
        existing = conn.execute(
            "SELECT * FROM metric_version WHERE metric=? AND version=?",
            (metric, version),
        ).fetchone()
        if existing is not None:
            if (
                existing["effective_from"] != effective_from
                or existing["definition"] != definition
                or existing["note"] != note
            ):
                raise WorkflowError(
                    f"口径 {metric}/{version} 已登记且内容不同；指标修订请使用新版本号，"
                    "不得悄悄改写历史口径"
                )
            return get_metric_version(db, metric, version)
        conn.execute(
            """
            INSERT INTO metric_version(metric, version, effective_from, definition,
                                       note, registered_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (metric, version, effective_from, definition, note, now, now),
        )
        conn.execute(
            "INSERT INTO metric_version_revision(metric, version, effective_from, "
            "definition, note, recorded_at, recorded_by) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (metric, version, effective_from, definition, note, now, actor),
        )
        from .utils import audit

        audit(
            conn,
            actor=actor,
            action="register-metric-version",
            entity_type="metric_version",
            entity_id=f"{metric}/{version}",
            detail={"effective_from": effective_from, "definition": definition},
        )
    return get_metric_version(db, metric, version)


def get_metric_version(db: Database, metric: str, version: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM metric_version WHERE metric=? AND version=?", (metric, version)
    )
    if row is None:
        raise WorkflowError(f"未登记的指标口径: {metric}/{version}")
    return dict(row)


def list_metric_versions(db: Database) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in db.fetch_all(
            "SELECT * FROM metric_version ORDER BY metric, version"
        )
    ]


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------


def _ensure_metric_version(db: Database, metric: str, version: str) -> None:
    row = db.fetch_one(
        "SELECT 1 FROM metric_version WHERE metric=? AND version=?", (metric, version)
    )
    if row is None:
        # 资料中已出现但尚未登记的版本，按“沿用资料口径”登记，避免阻断分析。
        register_metric_revision(
            db,
            metric=metric,
            version=version,
            actor="system",
            note="由导入资料推断，尚未经过口径评审",
        )


def _next_id(conn, prefix: str, table: str, column: str) -> str:
    rows = conn.execute(f"SELECT {column} AS id FROM {table}").fetchall()
    largest = 0
    for row in rows:
        text = row["id"]
        if text.startswith(prefix + "-"):
            tail = text[len(prefix) + 1 :]
            if tail.isdigit():
                largest = max(largest, int(tail))
    return f"{prefix}-{largest + 1:03d}"


def create_diagnosis(
    db: Database,
    *,
    store_id: str,
    metric: str,
    period_start: str,
    period_end: str,
    metric_version: str,
    actor: str,
    diagnosis_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    if parse_date(period_start) > parse_date(period_end):
        raise WorkflowError("period_start 不能晚于 period_end")
    if db.fetch_one("SELECT 1 FROM store WHERE store_id=?", (store_id,)) is None:
        raise WorkflowError(f"门店不存在: {store_id}")
    _ensure_metric_version(db, metric, metric_version)

    with db.session(write=True) as conn:
        diagnosis_id = diagnosis_id or _next_id(conn, DIAGNOSIS_PREFIX, "diagnosis", "diagnosis_id")
        if conn.execute(
            "SELECT 1 FROM diagnosis WHERE diagnosis_id=?", (diagnosis_id,)
        ).fetchone():
            raise WorkflowError(f"诊断编号已存在: {diagnosis_id}")
        now = now_iso()
        conn.execute(
            """
            INSERT INTO diagnosis(diagnosis_id, store_id, metric, period_start,
                                  period_end, metric_version, review_state,
                                  review_note, source_batch, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, 'draft', ?, NULL, ?, ?)
            """,
            (
                diagnosis_id,
                store_id,
                metric,
                period_start,
                period_end,
                metric_version,
                note,
                now,
                now,
            ),
        )
        record_event(
            conn,
            "diagnosis_event",
            id_column="diagnosis_id",
            entity_id=diagnosis_id,
            actor=actor,
            event_type="created",
            to_state="draft",
            note=note,
        )
        from .utils import audit

        audit(
            conn,
            actor=actor,
            action="create-diagnosis",
            entity_type="diagnosis",
            entity_id=diagnosis_id,
            detail={"store_id": store_id, "metric": metric, "version": metric_version},
        )
    return get_diagnosis(db, diagnosis_id)


def get_diagnosis(db: Database, diagnosis_id: str) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM diagnosis WHERE diagnosis_id=?", (diagnosis_id,))
    if row is None:
        raise WorkflowError(f"诊断不存在: {diagnosis_id}")
    result = dict(row)
    latest = db.fetch_one(
        "SELECT MAX(revision) AS revision FROM diagnosis_evidence WHERE diagnosis_id=?",
        (diagnosis_id,),
    )
    result["evidence_revision"] = latest["revision"] if latest else None
    return result


def list_diagnoses(
    db: Database,
    *,
    store_id: str | None = None,
    metric: str | None = None,
    review_state: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if store_id:
        clauses.append("store_id=?")
        params.append(store_id)
    if metric:
        clauses.append("metric=?")
        params.append(metric)
    if review_state:
        clauses.append("review_state=?")
        params.append(review_state)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = db.fetch_all(
        f"SELECT * FROM diagnosis {where} ORDER BY diagnosis_id LIMIT ?", params
    )
    return [dict(r) for r in rows]


def generate_evidence(
    db: Database, diagnosis_id: str, *, actor: str
) -> dict[str, Any]:
    """生成并追加证据快照。旧版本保留，不可覆盖。"""
    diagnosis = get_diagnosis(db, diagnosis_id)
    evidence = build_evidence(db, diagnosis_id, generated_at=now_iso())
    policy_hash, input_digest = evidence_hashes(evidence)
    with db.session(write=True) as conn:
        revision = (
            conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM diagnosis_evidence "
                "WHERE diagnosis_id=?",
                (diagnosis_id,),
            ).fetchone()[0]
            + 1
        )
        conn.execute(
            """
            INSERT INTO diagnosis_evidence(diagnosis_id, revision, created_at,
                                           status, evidence_grade, policy_hash,
                                           input_digest, snapshot)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                diagnosis_id,
                revision,
                now_iso(),
                evidence["status"],
                evidence["evidence_grade"],
                policy_hash,
                input_digest,
                canonical_json(evidence),
            ),
        )
        record_event(
            conn,
            "diagnosis_event",
            id_column="diagnosis_id",
            entity_id=diagnosis_id,
            actor=actor,
            event_type="evidence-generated",
            to_state=diagnosis["review_state"],
            note=f"证据版本 v{revision}，等级 {evidence['evidence_grade']}",
            payload={
                "revision": revision,
                "status": evidence["status"],
                "policy_hash": policy_hash,
                "input_digest": input_digest,
            },
        )
    evidence["revision"] = revision
    evidence["policy_hash"] = policy_hash
    evidence["input_digest"] = input_digest
    return evidence


def latest_evidence(db: Database, diagnosis_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        "SELECT * FROM diagnosis_evidence WHERE diagnosis_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (diagnosis_id,),
    )
    if row is None:
        return None
    import json

    result = dict(row)
    result["snapshot"] = json.loads(result["snapshot"])
    return result


def evidence_history(db: Database, diagnosis_id: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT diagnosis_id, revision, created_at, status, evidence_grade, "
        "policy_hash, input_digest FROM diagnosis_evidence "
        "WHERE diagnosis_id=? ORDER BY revision",
        (diagnosis_id,),
    )
    return [dict(r) for r in rows]


def _transition_diagnosis(
    db: Database,
    diagnosis_id: str,
    *,
    actor: str,
    target: str,
    allowed_from: set[str],
    note: str,
) -> dict[str, Any]:
    diagnosis = get_diagnosis(db, diagnosis_id)
    if diagnosis["review_state"] not in allowed_from:
        raise WorkflowError(
            f"{diagnosis_id} 当前状态 {diagnosis['review_state']} 不能转为 {target}"
        )
    with db.session(write=True) as conn:
        conn.execute(
            "UPDATE diagnosis SET review_state=?, review_note=?, updated_at=? "
            "WHERE diagnosis_id=?",
            (target, note, now_iso(), diagnosis_id),
        )
        record_event(
            conn,
            "diagnosis_event",
            id_column="diagnosis_id",
            entity_id=diagnosis_id,
            actor=actor,
            event_type="review",
            from_state=diagnosis["review_state"],
            to_state=target,
            note=note,
        )
        from .utils import audit

        audit(
            conn,
            actor=actor,
            action=f"diagnosis-{target}",
            entity_type="diagnosis",
            entity_id=diagnosis_id,
            detail={"from": diagnosis["review_state"], "note": note},
        )
    return get_diagnosis(db, diagnosis_id)


def submit_diagnosis(
    db: Database, diagnosis_id: str, *, actor: str, note: str = ""
) -> dict[str, Any]:
    return _transition_diagnosis(
        db,
        diagnosis_id,
        actor=actor,
        target="reviewed",
        allowed_from={"draft", "reopened"},
        note=note,
    )


def reject_diagnosis(
    db: Database, diagnosis_id: str, *, actor: str, note: str
) -> dict[str, Any]:
    if not note.strip():
        raise WorkflowError("驳回诊断必须写明理由")
    return _transition_diagnosis(
        db,
        diagnosis_id,
        actor=actor,
        target="rejected",
        allowed_from={"reviewed"},
        note=note,
    )


def reopen_diagnosis(
    db: Database, diagnosis_id: str, *, actor: str, note: str
) -> dict[str, Any]:
    if not note.strip():
        raise WorkflowError("重开诊断必须写明原因")
    return _transition_diagnosis(
        db,
        diagnosis_id,
        actor=actor,
        target="reopened",
        allowed_from={"reviewed"},
        note=note,
    )


# ---------------------------------------------------------------------------
# 行动
# ---------------------------------------------------------------------------


def propose_action(
    db: Database,
    *,
    diagnosis_id: str,
    action_type: str,
    actor: str,
    owner: str = "",
    due_at: str | None = None,
    action_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    diagnosis = get_diagnosis(db, diagnosis_id)
    if diagnosis["review_state"] != "reviewed":
        raise WorkflowError(
            f"诊断 {diagnosis_id} 尚未通过区域经理审阅（当前 {diagnosis['review_state']}），"
            "不能提出行动"
        )
    if action_type not in ACTION_CATALOG:
        raise WorkflowError(f"未知行动类型: {action_type}")
    if due_at:
        parse_date(due_at)
    if latest_evidence_grade(db, diagnosis_id) == "insufficient":
        # 允许提出“补数据/修复设备”类行动，但观察或经营类行动需先补证据。
        if action_type not in {"device-repair", "data-audit", "observe"}:
            raise WorkflowError(
                "诊断证据不足，只能提出修复设备、核对口径或继续观察类行动"
            )
    with db.session(write=True) as conn:
        action_id = action_id or _next_id(conn, ACTION_PREFIX, "action", "action_id")
        if conn.execute("SELECT 1 FROM action WHERE action_id=?", (action_id,)).fetchone():
            raise WorkflowError(f"行动编号已存在: {action_id}")
        now = now_iso()
        conn.execute(
            """
            INSERT INTO action(action_id, diagnosis_id, owner, action_type,
                               accepted_at, due_at, execution_state, outcome_ref,
                               completed_at, evidence_ref, created_at, updated_at,
                               source_batch)
            VALUES(?, ?, ?, ?, NULL, ?, 'proposed', '', NULL, '', ?, ?, NULL)
            """,
            (action_id, diagnosis_id, owner, action_type, due_at, now, now),
        )
        record_event(
            conn,
            "action_event",
            id_column="action_id",
            entity_id=action_id,
            actor=actor,
            event_type="proposed",
            to_state="proposed",
            note=note,
        )
        from .utils import audit

        audit(
            conn,
            actor=actor,
            action="propose-action",
            entity_type="action",
            entity_id=action_id,
            detail={"diagnosis_id": diagnosis_id, "action_type": action_type},
        )
    return get_action(db, action_id)


def latest_evidence_grade(db: Database, diagnosis_id: str) -> str | None:
    row = db.fetch_one(
        "SELECT evidence_grade FROM diagnosis_evidence WHERE diagnosis_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (diagnosis_id,),
    )
    return None if row is None else row["evidence_grade"]


def get_action(db: Database, action_id: str) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM action WHERE action_id=?", (action_id,))
    if row is None:
        raise WorkflowError(f"行动不存在: {action_id}")
    result = dict(row)
    result.update(_derive_action_state(result, today_iso()))
    outcomes = db.fetch_all(
        "SELECT * FROM action_outcome WHERE action_id=? ORDER BY recorded_at",
        (action_id,),
    )
    result["outcomes"] = [dict(r) for r in outcomes]
    return result


def _derive_action_state(action: dict[str, Any], as_of: str) -> dict[str, Any]:
    """派生状态。

    ``late`` 在业务上表示“已逾期但尚未完成”（不是“完成得晚”）；完成得晚
    仍记为 ``done``，另以 ``completed_late`` 标记。
    """
    state = action["execution_state"]
    accepted = action.get("accepted_at") not in (None, "")
    due_at = action.get("due_at")
    completed_at = action.get("completed_at")
    derived = {
        "accepted": accepted,
        "counted_as_executed": False,
        "overdue": False,
        "completed_late": False,
        "current_state": state,
        "state_issue": None,
    }
    if state in {"done", "late"} and not accepted:
        derived["state_issue"] = "缺少门店接受记录，不能计入已执行"
        derived["current_state"] = "unaccepted-but-recorded"
        if state == "late":
            derived["overdue"] = True
        return derived
    if state == "done":
        derived["counted_as_executed"] = True
        if not completed_at:
            derived["state_issue"] = "缺少完成日期，无法判断是否按期，不能计入按时完成"
        elif due_at and completed_at > due_at:
            derived["completed_late"] = True
            derived["overdue"] = True
        return derived
    if state == "late":
        derived["overdue"] = True
        derived["current_state"] = "overdue"
        return derived
    if state == "accepted":
        if due_at and as_of > due_at:
            derived["overdue"] = True
            derived["current_state"] = "overdue"
        elif not due_at:
            derived["state_issue"] = "缺少期限，无法判断是否按期"
    return derived


def list_actions(
    db: Database,
    *,
    diagnosis_id: str | None = None,
    as_of: str | None = None,
    include_proposed: bool = True,
) -> list[dict[str, Any]]:
    as_of = as_of or today_iso()
    if diagnosis_id:
        rows = db.fetch_all(
            "SELECT * FROM action WHERE diagnosis_id=? ORDER BY action_id",
            (diagnosis_id,),
        )
    else:
        rows = db.fetch_all("SELECT * FROM action ORDER BY action_id")
    results = []
    for row in rows:
        item = dict(row)
        item.update(_derive_action_state(item, as_of))
        if not include_proposed and item["execution_state"] == "proposed":
            continue
        results.append(item)
    return results


def accept_action(
    db: Database,
    action_id: str,
    *,
    actor: str,
    owner: str,
    due_at: str,
    accepted_at: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """门店负责人确认责任、期限与接受。"""
    action = get_action(db, action_id)
    if action["execution_state"] != "proposed":
        raise WorkflowError(
            f"{action_id} 当前状态 {action['execution_state']} 不能由门店接受"
        )
    if not owner.strip():
        raise WorkflowError("门店接受行动时必须确认责任人")
    accepted_on = accepted_at or today_iso()
    if parse_date(due_at) < parse_date(accepted_on):
        raise WorkflowError("期限不能早于接受日期")
    diagnosis = get_diagnosis(db, action["diagnosis_id"])
    if diagnosis["review_state"] not in {"reviewed", "reopened"}:
        raise WorkflowError("所属诊断不在可执行状态")
    with db.session(write=True) as conn:
        conn.execute(
            "UPDATE action SET owner=?, accepted_at=?, due_at=?, "
            "execution_state='accepted', updated_at=? WHERE action_id=?",
            (owner, accepted_on, due_at, now_iso(), action_id),
        )
        record_event(
            conn,
            "action_event",
            id_column="action_id",
            entity_id=action_id,
            actor=actor,
            event_type="accepted",
            from_state="proposed",
            to_state="accepted",
            note=note,
            payload={"owner": owner, "due_at": due_at},
        )
        from .utils import audit

        audit(
            conn,
            actor=actor,
            action="accept-action",
            entity_type="action",
            entity_id=action_id,
            detail={"owner": owner, "due_at": due_at},
        )
    return get_action(db, action_id)


def reject_action(
    db: Database, action_id: str, *, actor: str, note: str
) -> dict[str, Any]:
    action = get_action(db, action_id)
    if action["execution_state"] != "proposed":
        raise WorkflowError("只有待接受行动可以由门店拒绝")
    if not note.strip():
        raise WorkflowError("门店拒绝行动必须写明理由，区域经理需重新选择")
    with db.session(write=True) as conn:
        conn.execute(
            "UPDATE action SET execution_state='rejected', updated_at=? "
            "WHERE action_id=?",
            (now_iso(), action_id),
        )
        record_event(
            conn,
            "action_event",
            id_column="action_id",
            entity_id=action_id,
            actor=actor,
            event_type="rejected",
            from_state="proposed",
            to_state="rejected",
            note=note,
        )
    return get_action(db, action_id)


def complete_action(
    db: Database,
    action_id: str,
    *,
    actor: str,
    outcome_ref: str,
    completed_at: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    action = get_action(db, action_id)
    if action["execution_state"] not in {"accepted", "late"}:
        raise WorkflowError(
            f"{action_id} 未获门店接受（当前 {action['execution_state']}），不能标记为已执行"
        )
    if action.get("accepted_at") in (None, ""):
        raise WorkflowError("缺少门店接受记录，不能计入已执行")
    if not outcome_ref.strip():
        raise WorkflowError("完成行动必须登记结果引用")
    completed_on = completed_at or today_iso()
    # 逾期完成仍然是“已完成”；是否迟于期限由 completed_at 与 due_at 派生。
    late = bool(action["due_at"] and completed_on > action["due_at"])
    state = "done"
    with db.session(write=True) as conn:
        conn.execute(
            "UPDATE action SET execution_state=?, outcome_ref=?, completed_at=?, "
            "updated_at=? WHERE action_id=?",
            (state, outcome_ref, completed_on, now_iso(), action_id),
        )
        record_event(
            conn,
            "action_event",
            id_column="action_id",
            entity_id=action_id,
            actor=actor,
            event_type="completed",
            from_state=action["execution_state"],
            to_state=state,
            note=note,
            payload={"outcome_ref": outcome_ref, "completed_late": late},
        )
        from .utils import audit

        audit(
            conn,
            actor=actor,
            action="complete-action",
            entity_type="action",
            entity_id=action_id,
            detail={"outcome_ref": outcome_ref, "late": late},
        )
    return get_action(db, action_id)


def mark_late_actions(db: Database, *, actor: str = "system", as_of: str | None = None) -> list[dict[str, Any]]:
    """把已过期限仍未完成的已接受行动标记为逾期。"""
    as_of = as_of or today_iso()
    with db.session(write=True) as conn:
        rows = conn.execute(
            "SELECT * FROM action WHERE execution_state='accepted' "
            "AND due_at IS NOT NULL AND due_at < ?",
            (as_of,),
        ).fetchall()
        marked = []
        for row in rows:
            conn.execute(
                "UPDATE action SET execution_state='late', updated_at=? "
                "WHERE action_id=?",
                (now_iso(), row["action_id"]),
            )
            record_event(
                conn,
                "action_event",
                id_column="action_id",
                entity_id=row["action_id"],
                actor=actor,
                event_type="marked-late",
                from_state="accepted",
                to_state="late",
                note=f"截至 {as_of} 仍未完成",
            )
            marked.append(dict(row))
    return [get_action(db, r["action_id"]) for r in marked]


# ---------------------------------------------------------------------------
# 结果回看
# ---------------------------------------------------------------------------


def record_outcome(
    db: Database,
    action_id: str,
    *,
    actor: str,
    reviewer: str,
    outcome_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    metric_version: str | None = None,
    comparable: bool = True,
    note: str = "",
    evidence_ref: str = "",
) -> dict[str, Any]:
    """按原指标版本回看行动结果。

    默认回看窗口从行动完成日的次日起、长度与诊断期间相同；调用方可以显式
    指定，但不能使用诊断声明以外的指标版本。
    """
    action = get_action(db, action_id)
    diagnosis = get_diagnosis(db, action["diagnosis_id"])
    if action["execution_state"] != "done" or not action["counted_as_executed"]:
        raise WorkflowError("行动尚未完成，不能登记结果回看")
    original_version = diagnosis["metric_version"]
    version = metric_version or original_version
    if version != original_version:
        raise WorkflowError(
            f"结果回看必须使用原诊断口径 {original_version}，收到 {version}；"
            "口径修订不能悄悄改写历史诊断"
        )

    diag_start = parse_date(diagnosis["period_start"])
    diag_end = parse_date(diagnosis["period_end"])
    length = (diag_end - diag_start).days + 1
    window_note: str | None = None
    if period_start is None:
        base_day = action.get("completed_at") or action.get("due_at")
        if not base_day:
            raise WorkflowError("无法推断回看起点，请显式提供 period_start")
        if not action.get("completed_at"):
            # 资料只有 done 状态、没有完成日期时，回看以期限为最早起点。
            window_note = "行动缺少完成日期，回看窗口按期限次日估算，需人工核对"
        period_start = add_days(base_day, 1)
    if period_end is None:
        period_end = add_days(period_start, length - 1)
    if parse_date(period_start) > parse_date(period_end):
        raise WorkflowError("回看期间起止顺序错误")

    evidence = build_evidence(db, diagnosis["diagnosis_id"])
    windows = evidence["windows"]
    review_start = parse_date(period_start)
    review_end = parse_date(period_end)
    from .analytics import build_window, load_series

    review_current = build_window(
        load_series(
            db,
            diagnosis["store_id"],
            diagnosis["metric"],
            version,
            period_start,
            period_end,
        ),
        review_start,
        review_end,
    )
    review_baseline = build_window(
        load_series(
            db,
            diagnosis["store_id"],
            diagnosis["metric"],
            version,
            diagnosis["period_start"],
            diagnosis["period_end"],
        ),
        diag_start,
        diag_end,
    )
    from .analytics import classify_change

    comparison = classify_change(
        diagnosis["metric"], review_current.median, review_baseline.median
    )
    sufficient = review_current.count >= 2 and review_current.coverage > 0
    comparable_flag = bool(comparable and sufficient)

    snapshot = {
        "schema": "outcome-review/1",
        "action_id": action_id,
        "diagnosis_id": diagnosis["diagnosis_id"],
        "metric": diagnosis["metric"],
        "metric_label": METRIC_LABELS.get(diagnosis["metric"], diagnosis["metric"]),
        "metric_version": version,
        "diagnosis_period": [diagnosis["period_start"], diagnosis["period_end"]],
        "review_period": [period_start, period_end],
        "window_note": window_note,
        "comparison": comparison,
        "diagnosis_baseline_median": windows["period"]["median"],
        "review_median": review_current.median,
        "review_observations": review_current.count,
        "review_coverage": round(review_current.coverage, 4),
        "comparable": comparable_flag,
        "comparable_note": (
            None
            if comparable_flag
            else "回看窗口观测不足或条件不可比，不能据此判断行动效果"
        ),
        "confounders": evidence["confounders"],
        "caveats": list(evidence["caveats"])
        + ([] if sufficient else ["回看期间可用观测不足 2 天"])
        + ([window_note] if window_note else []),
    }

    oid = outcome_id or stable_id("OUT", action_id, period_start, period_end, version)
    with db.session(write=True) as conn:
        if conn.execute(
            "SELECT 1 FROM action_outcome WHERE outcome_id=?", (oid,)
        ).fetchone():
            raise WorkflowError(f"结果编号已存在: {oid}")
        conn.execute(
            """
            INSERT INTO action_outcome(outcome_id, action_id, recorded_at, reviewer,
                                       period_start, period_end, metric_version,
                                       comparable, note, evidence_ref, snapshot)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                oid,
                action_id,
                now_iso(),
                reviewer,
                period_start,
                period_end,
                version,
                1 if comparable_flag else 0,
                note,
                evidence_ref,
                canonical_json(snapshot),
            ),
        )
        record_event(
            conn,
            "action_event",
            id_column="action_id",
            entity_id=action_id,
            actor=actor,
            event_type="outcome-recorded",
            note=note,
            payload={"outcome_id": oid, "comparable": comparable_flag},
        )
        from .utils import audit

        audit(
            conn,
            actor=actor,
            action="record-outcome",
            entity_type="action_outcome",
            entity_id=oid,
            detail={"action_id": action_id, "version": version},
        )
    return get_outcome(db, oid)


def get_outcome(db: Database, outcome_id: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM action_outcome WHERE outcome_id=?", (outcome_id,)
    )
    if row is None:
        raise WorkflowError(f"回看结果不存在: {outcome_id}")
    import json

    result = dict(row)
    result["snapshot"] = json.loads(result["snapshot"])
    return result


def list_outcomes(db: Database, *, action_id: str | None = None) -> list[dict[str, Any]]:
    if action_id:
        rows = db.fetch_all(
            "SELECT * FROM action_outcome WHERE action_id=? ORDER BY recorded_at",
            (action_id,),
        )
    else:
        rows = db.fetch_all("SELECT * FROM action_outcome ORDER BY recorded_at")
    import json

    results = []
    for row in rows:
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"])
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def action_scorecard(
    db: Database, *, as_of: str | None = None, diagnosis_id: str | None = None
) -> dict[str, Any]:
    """执行统计。未获接受的“已完成”记录不计入已执行。"""
    as_of = as_of or today_iso()
    actions = list_actions(db, as_of=as_of, diagnosis_id=diagnosis_id)
    rows = []
    executed = on_time = late = unaccepted = pending = rejected = overdue = undated = 0
    for action in actions:
        state = action["execution_state"]
        accepted = action["accepted"]
        if state == "rejected":
            rejected += 1
            bucket = "rejected"
        elif state in {"done", "late"} and not accepted:
            unaccepted += 1
            bucket = "recorded-without-acceptance"
        elif state == "done":
            executed += 1
            if not action["completed_at"]:
                undated += 1
                bucket = "executed-date-unknown"
            elif action["completed_late"]:
                late += 1
                bucket = "executed-late"
            else:
                on_time += 1
                bucket = "executed-on-time"
        elif state == "late":
            pending += 1
            overdue += 1
            bucket = "overdue-unfinished"
        elif state == "accepted":
            pending += 1
            if action["overdue"]:
                overdue += 1
                bucket = "overdue-unfinished"
            else:
                bucket = "in-progress"
        else:
            pending += 1
            bucket = "proposed"
        rows.append(
            {
                "action_id": action["action_id"],
                "diagnosis_id": action["diagnosis_id"],
                "owner": action["owner"],
                "action_type": action["action_type"],
                "execution_state": state,
                "current_state": action["current_state"],
                "bucket": bucket,
                "accepted_at": action["accepted_at"],
                "due_at": action["due_at"],
                "completed_at": action["completed_at"],
                "overdue": action["overdue"],
                "outcome_ref": action["outcome_ref"],
            }
        )
    return {
        "as_of": as_of,
        "total": len(actions),
        "executed": executed,
        "executed_on_time": on_time,
        "executed_late": late,
        "executed_date_unknown": undated,
        "overdue_unfinished": overdue,
        "pending": pending,
        "rejected": rejected,
        "recorded_without_acceptance": unaccepted,
        "execution_rate": round(executed / len(actions), 4) if actions else None,
        "note": (
            "未获门店接受的记录不计入 executed；late 表示逾期未完成，"
            "完成得晚仍记为 done 并计入 executed_late；缺少完成日期的 done "
            "计入 executed_date_unknown，不当作按时完成"
        ),
        "actions": rows,
    }


__all__ = [
    "WorkflowError",
    "accept_action",
    "action_scorecard",
    "complete_action",
    "create_diagnosis",
    "evidence_history",
    "generate_evidence",
    "get_action",
    "get_diagnosis",
    "get_metric_version",
    "get_outcome",
    "latest_evidence",
    "list_actions",
    "list_diagnoses",
    "list_metric_versions",
    "list_outcomes",
    "mark_late_actions",
    "propose_action",
    "record_outcome",
    "register_metric_revision",
    "reject_action",
    "reject_diagnosis",
    "reopen_diagnosis",
    "submit_diagnosis",
]
