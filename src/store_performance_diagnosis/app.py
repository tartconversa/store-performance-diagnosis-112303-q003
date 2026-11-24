"""应用服务门面：把导入、诊断、行动与追踪串成可复用的 Python API。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import materials, workflow
from .config import APP_VERSION
from .db import Database
from .utils import now_iso, sha256_text


class StoreDiagnosisApp:
    """面向业务分析的入口。所有方法都返回普通字典，便于命令行或 Web 层复用。"""

    def __init__(self, database: str | Path = "store_diagnosis.sqlite3"):
        self.db = Database(database)
        self.db.initialize()

    # -- 资料 -------------------------------------------------------------
    def import_materials(
        self, directory: str | Path, *, actor: str = "data-team"
    ) -> list[dict[str, Any]]:
        results = materials.import_directory(self.db, directory, actor=actor)
        return [r.as_dict() for r in results]

    def import_file(
        self, path: str | Path, *, actor: str = "data-team", filename: str | None = None
    ) -> dict[str, Any]:
        return materials.import_file(
            self.db, path, actor=actor, filename=filename
        ).as_dict()

    def verify_materials(self) -> dict[str, Any]:
        return materials.verify_materials(self.db)

    def import_warnings(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        return materials.list_warnings(self.db, batch_id)

    # -- 诊断 -------------------------------------------------------------
    def diagnose(
        self,
        *,
        store_id: str,
        metric: str,
        period_start: str,
        period_end: str,
        metric_version: str,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        diagnosis = workflow.create_diagnosis(
            self.db,
            store_id=store_id,
            metric=metric,
            period_start=period_start,
            period_end=period_end,
            metric_version=metric_version,
            actor=actor,
            note=note,
        )
        evidence = workflow.generate_evidence(
            self.db, diagnosis["diagnosis_id"], actor=actor
        )
        return {"diagnosis": diagnosis, "evidence": evidence}

    def diagnose_with_id(
        self,
        diagnosis_id: str,
        *,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        """为已导入的诊断补生成证据。"""
        workflow.get_diagnosis(self.db, diagnosis_id)  # 不存在时抛出 WorkflowError
        evidence = workflow.generate_evidence(self.db, diagnosis_id, actor=actor)
        return {"diagnosis": workflow.get_diagnosis(self.db, diagnosis_id), "evidence": evidence}

    def get_diagnosis(self, diagnosis_id: str) -> dict[str, Any]:
        return workflow.get_diagnosis(self.db, diagnosis_id)

    def list_diagnoses(self, **filters: Any) -> list[dict[str, Any]]:
        return workflow.list_diagnoses(self.db, **filters)

    def latest_evidence(self, diagnosis_id: str) -> dict[str, Any] | None:
        return workflow.latest_evidence(self.db, diagnosis_id)

    def evidence_history(self, diagnosis_id: str) -> list[dict[str, Any]]:
        return workflow.evidence_history(self.db, diagnosis_id)

    def refresh_evidence(self, diagnosis_id: str, *, actor: str) -> dict[str, Any]:
        return workflow.generate_evidence(self.db, diagnosis_id, actor=actor)

    def submit_diagnosis(self, diagnosis_id: str, *, actor: str, note: str = "") -> dict[str, Any]:
        return workflow.submit_diagnosis(self.db, diagnosis_id, actor=actor, note=note)

    def reject_diagnosis(self, diagnosis_id: str, *, actor: str, note: str) -> dict[str, Any]:
        return workflow.reject_diagnosis(self.db, diagnosis_id, actor=actor, note=note)

    def reopen_diagnosis(self, diagnosis_id: str, *, actor: str, note: str) -> dict[str, Any]:
        return workflow.reopen_diagnosis(self.db, diagnosis_id, actor=actor, note=note)

    # -- 行动 -------------------------------------------------------------
    def propose_action(
        self,
        *,
        diagnosis_id: str,
        action_type: str,
        actor: str,
        owner: str = "",
        due_at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return workflow.propose_action(
            self.db,
            diagnosis_id=diagnosis_id,
            action_type=action_type,
            actor=actor,
            owner=owner,
            due_at=due_at,
            note=note,
        )

    def accept_action(
        self,
        action_id: str,
        *,
        actor: str,
        owner: str,
        due_at: str,
        accepted_at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return workflow.accept_action(
            self.db,
            action_id,
            actor=actor,
            owner=owner,
            due_at=due_at,
            accepted_at=accepted_at,
            note=note,
        )

    def reject_action(self, action_id: str, *, actor: str, note: str) -> dict[str, Any]:
        return workflow.reject_action(self.db, action_id, actor=actor, note=note)

    def complete_action(
        self,
        action_id: str,
        *,
        actor: str,
        outcome_ref: str,
        completed_at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return workflow.complete_action(
            self.db,
            action_id,
            actor=actor,
            outcome_ref=outcome_ref,
            completed_at=completed_at,
            note=note,
        )

    def mark_late_actions(self, *, as_of: str | None = None) -> list[dict[str, Any]]:
        return workflow.mark_late_actions(self.db, as_of=as_of)

    def record_outcome(
        self,
        action_id: str,
        *,
        actor: str,
        reviewer: str,
        period_start: str | None = None,
        period_end: str | None = None,
        metric_version: str | None = None,
        comparable: bool = True,
        note: str = "",
    ) -> dict[str, Any]:
        return workflow.record_outcome(
            self.db,
            action_id,
            actor=actor,
            reviewer=reviewer,
            period_start=period_start,
            period_end=period_end,
            metric_version=metric_version,
            comparable=comparable,
            note=note,
        )

    def get_action(self, action_id: str) -> dict[str, Any]:
        return workflow.get_action(self.db, action_id)

    def list_actions(self, **filters: Any) -> list[dict[str, Any]]:
        return workflow.list_actions(self.db, **filters)

    def scorecard(self, *, as_of: str | None = None, diagnosis_id: str | None = None) -> dict[str, Any]:
        return workflow.action_scorecard(self.db, as_of=as_of, diagnosis_id=diagnosis_id)

    def list_outcomes(self, *, action_id: str | None = None) -> list[dict[str, Any]]:
        return workflow.list_outcomes(self.db, action_id=action_id)

    # -- 指标口径 ---------------------------------------------------------
    def register_metric_version(
        self,
        *,
        metric: str,
        version: str,
        actor: str,
        effective_from: str | None = None,
        definition: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        return workflow.register_metric_revision(
            self.db,
            metric=metric,
            version=version,
            actor=actor,
            effective_from=effective_from,
            definition=definition,
            note=note,
        )

    def list_metric_versions(self) -> list[dict[str, Any]]:
        return workflow.list_metric_versions(self.db)

    # -- 元信息 -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        counts = {}
        for table in (
            "store",
            "daily_metric",
            "event",
            "diagnosis",
            "diagnosis_evidence",
            "action",
            "action_outcome",
            "import_batch",
        ):
            counts[table] = self.db.scalar(f"SELECT COUNT(*) FROM {table}") or 0
        grade_rows = self.db.fetch_all(
            "SELECT evidence_grade, COUNT(*) AS n FROM ("
            " SELECT diagnosis_id, evidence_grade FROM diagnosis_evidence e"
            " WHERE revision=(SELECT MAX(revision) FROM diagnosis_evidence x"
            "                 WHERE x.diagnosis_id=e.diagnosis_id)"
            ") GROUP BY evidence_grade"
        )
        materials = self.verify_materials()
        severity_counts: dict[str, int] = {}
        code_counts: dict[str, int] = {}
        for issue in materials["issues"]:
            severity_counts[issue["severity"]] = severity_counts.get(issue["severity"], 0) + 1
            code_counts[issue["code"]] = code_counts.get(issue["code"], 0) + 1
        return {
            "app_version": APP_VERSION,
            "generated_at": now_iso(),
            "counts": counts,
            "latest_evidence_grades": {r["evidence_grade"]: r["n"] for r in grade_rows},
            "materials": {
                "issue_count": materials["issue_count"],
                "by_severity": severity_counts,
                "by_code": code_counts,
                "hint": "运行 verify 查看逐条问题",
            },
        }

    def coverage_digest(self) -> str:
        """资料与证据的可复现摘要，便于回归比较。"""
        rows = self.db.fetch_all(
            "SELECT store_id, date, metric, value, metric_version, source_state "
            "FROM daily_metric ORDER BY store_id, date, metric, metric_version"
        )
        return sha256_text("\n".join("|".join("" if v is None else str(v) for v in row) for row in rows))


__all__ = ["StoreDiagnosisApp"]
