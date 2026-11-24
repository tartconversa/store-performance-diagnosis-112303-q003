"""命令行入口，供数据团队与区域团队日常操作。

示例::

    python -m store_performance_diagnosis import data/
    python -m store_performance_diagnosis verify
    python -m store_performance_diagnosis show DG-005
    python -m store_performance_diagnosis diagnose --store ST-014 --metric conversion \
        --start 2026-11-15 --end 2026-11-24 --version MV-2 --actor analyst
    python -m store_performance_diagnosis submit DG-037 --actor manager
    python -m store_performance_diagnosis propose DG-037 --type display --actor manager
    python -m store_performance_diagnosis accept AC-049 --owner MGR-13 \
        --due 2026-12-20 --actor store-lead
    python -m store_performance_diagnosis complete AC-049 --outcome-ref OUT-49 --actor store-lead
    python -m store_performance_diagnosis outcome AC-049 --reviewer data-team
    python -m store_performance_diagnosis scorecard
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .analytics import compact_series
from .app import StoreDiagnosisApp
from .config import APP_VERSION, MATERIAL_SCHEMAS


def _emit(value: Any, *, as_json: bool, title: str | None = None) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    if title:
        print(title)
        print("=" * len(title))
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False, default=str)}")
            else:
                print(f"{key}: {item}")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _print_evidence(evidence: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return
    d = evidence["diagnosis"]
    summary = evidence["summary"]
    print(f"诊断 {d['diagnosis_id']}  {d['store_id']}  {d['metric_label']}({d['metric']})  "
          f"{d['period_start']} → {d['period_end']}  口径 {d['metric_version']}")
    print(f"状态: {evidence['status']}  证据等级: {evidence['evidence_grade']}")
    change = summary["change"]
    if change["delta"] is None:
        print(f"变化: 无法判断（{change['interpretation']}）")
    else:
        pct = "n/a" if change["delta_pct"] is None else f"{change['delta_pct']:+.2f}%"
        print(
            f"中位数: 当期 {summary['current_median']} / 基线 {summary['baseline_median']} "
            f"({change['delta']:+g}, {pct}) → {change['interpretation']}"
        )
    print(
        f"覆盖率: 当期 {summary['coverage']:.0%}（{summary['usable_observations']} 天） "
        f"/ 基线 {summary['baseline_coverage']:.0%}（{summary['baseline_observations']} 天）"
    )
    print("当前区间: " + compact_series(evidence["windows"]["period"]["series"]))
    if evidence["missing_runs"]:
        print("缺失时间段:")
        for run in evidence["missing_runs"]:
            print(f"  - {run['start']} → {run['end']} ({run['days']} 天) "
                  f"{run['source_states']}: {'；'.join(run['notes'])}")
    if evidence["anomalies"]:
        print("异常日期:")
        for item in evidence["anomalies"]:
            print(f"  - {item['date']}: {item['value']} ({item['reason']})")
    if evidence["confounders"]:
        print("可能的混杂因素（不是根因）:")
        for item in evidence["confounders"]:
            print(f"  - {item['label']}: {item['detail']}")
    peer = evidence["peer"]
    print(f"同业参照: {peer['peer_count']} 家可比门店，中位数 {peer['median']}，"
          f"本店偏差 {peer['store_deviation_pct']}% —— {peer['note']}")
    if evidence["candidate_causes"]:
        print("候选原因（均需本地验证，未自动认定为根因）:")
        for cause in evidence["candidate_causes"]:
            print(f"  - [{cause['confidence']}] {cause['label']}："
                  f"{'；'.join(cause['rationale'])}")
    if evidence.get("recommended_actions"):
        print("建议行动方向: " + ", ".join(
            f"{a['action_type']}" for a in evidence["recommended_actions"]
        ))
    if evidence["caveats"]:
        print("注意事项:")
        for caveat in evidence["caveats"]:
            print(f"  - {caveat}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="store_performance_diagnosis",
        description="门店经营异常诊断与行动效果回看",
    )
    parser.add_argument("--db", default="store_diagnosis.sqlite3", help="SQLite 数据库路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="导入资料目录或单文件")
    p.add_argument("path")
    p.add_argument("--file", choices=sorted(MATERIAL_SCHEMAS), help="只导入指定文件")
    p.add_argument("--actor", default="data-team")

    sub.add_parser("verify", help="跨文件一致性检查")
    p = sub.add_parser("status", help="导入与工作流概况")
    p.add_argument("--coverage-digest", action="store_true")

    p = sub.add_parser("list", help="列出诊断")
    p.add_argument("--store")
    p.add_argument("--metric")
    p.add_argument("--state", dest="review_state")
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("show", help="查看诊断证据")
    p.add_argument("diagnosis_id")
    p.add_argument("--history", action="store_true")

    p = sub.add_parser("diagnose", help="新建诊断并生成证据")
    p.add_argument("--store", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--note", default="")

    p = sub.add_parser("evidence", help="为已导入的诊断补生成证据")
    p.add_argument("diagnosis_id")
    p.add_argument("--actor", required=True)

    for name, help_text in (
        ("submit", "提交诊断供区域经理复核"),
        ("reopen", "重开诊断"),
        ("reject-diagnosis", "区域经理驳回诊断"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("diagnosis_id")
        p.add_argument("--actor", required=True)
        p.add_argument("--note", default="")

    p = sub.add_parser("propose", help="提出行动")
    p.add_argument("diagnosis_id")
    p.add_argument("--type", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--owner", default="")
    p.add_argument("--due")
    p.add_argument("--note", default="")

    p = sub.add_parser("accept", help="门店接受行动并确认责任人/期限")
    p.add_argument("action_id")
    p.add_argument("--actor", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--due", required=True)
    p.add_argument("--accepted-at")
    p.add_argument("--note", default="")

    p = sub.add_parser("reject-action", help="门店拒绝行动")
    p.add_argument("action_id")
    p.add_argument("--actor", required=True)
    p.add_argument("--note", required=True)

    p = sub.add_parser("complete", help="标记行动完成")
    p.add_argument("action_id")
    p.add_argument("--actor", required=True)
    p.add_argument("--outcome-ref", required=True)
    p.add_argument("--completed-at")
    p.add_argument("--note", default="")

    p = sub.add_parser("mark-late", help="把逾期未完成的已接受行动标记为逾期")
    p.add_argument("--as-of")

    p = sub.add_parser("outcome", help="按原指标版本回看行动结果")
    p.add_argument("action_id")
    p.add_argument("--actor", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--version")
    p.add_argument("--not-comparable", action="store_true")
    p.add_argument("--note", default="")

    p = sub.add_parser("scorecard", help="行动执行与逾期统计")
    p.add_argument("--as-of")
    p.add_argument("--diagnosis")

    p = sub.add_parser("metric", help="指标口径登记与查看")
    metric_sub = p.add_subparsers(dest="metric_command", required=True)
    m = metric_sub.add_parser("register")
    m.add_argument("metric")
    m.add_argument("version")
    m.add_argument("--actor", required=True)
    m.add_argument("--effective-from")
    m.add_argument("--definition", default="")
    m.add_argument("--note", default="")
    metric_sub.add_parser("list")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = StoreDiagnosisApp(args.db)
    as_json = args.json

    try:
        if args.command == "import":
            path = Path(args.path)
            if args.file:
                results = [app.import_file(path / args.file if path.is_dir() else path,
                                           actor=args.actor, filename=args.file)]
            elif path.is_dir():
                results = app.import_materials(path, actor=args.actor)
            else:
                results = [app.import_file(path, actor=args.actor)]
            _emit(results, as_json=as_json, title="导入结果")
            return 0
        if args.command == "verify":
            result = app.verify_materials()
            _emit(result, as_json=as_json, title="资料一致性")
            return 2 if any(i["severity"] == "error" for i in result["issues"]) else 0
        if args.command == "status":
            result = app.status()
            if args.coverage_digest:
                result["coverage_digest"] = app.coverage_digest()
            _emit(result, as_json=as_json, title="状态")
            return 0
        if args.command == "list":
            result = app.list_diagnoses(
                store_id=args.store, metric=args.metric,
                review_state=args.review_state, limit=args.limit,
            )
            _emit(result, as_json=as_json, title="诊断列表")
            return 0
        if args.command == "show":
            diagnosis = app.get_diagnosis(args.diagnosis_id)
            if args.history:
                _emit(app.evidence_history(args.diagnosis_id), as_json=as_json,
                      title="证据版本历史")
                return 0
            evidence = app.latest_evidence(args.diagnosis_id)
            if evidence is None:
                print(f"{args.diagnosis_id} 尚无证据快照，请运行 evidence 子命令", file=sys.stderr)
                return 1
            _print_evidence(evidence["snapshot"], as_json=as_json)
            return 0
        if args.command == "diagnose":
            result = app.diagnose(
                store_id=args.store, metric=args.metric,
                period_start=args.start, period_end=args.end,
                metric_version=args.version, actor=args.actor, note=args.note,
            )
            _print_evidence(result["evidence"], as_json=as_json)
            return 0
        if args.command == "evidence":
            evidence = app.refresh_evidence(args.diagnosis_id, actor=args.actor)
            _print_evidence(evidence, as_json=as_json)
            return 0
        if args.command == "submit":
            _emit(app.submit_diagnosis(args.diagnosis_id, actor=args.actor, note=args.note),
                  as_json=as_json, title="已提交复核")
            return 0
        if args.command == "reopen":
            _emit(app.reopen_diagnosis(args.diagnosis_id, actor=args.actor, note=args.note),
                  as_json=as_json, title="已重开")
            return 0
        if args.command == "reject-diagnosis":
            _emit(app.reject_diagnosis(args.diagnosis_id, actor=args.actor, note=args.note),
                  as_json=as_json, title="已驳回")
            return 0
        if args.command == "propose":
            _emit(app.propose_action(
                diagnosis_id=args.diagnosis_id, action_type=args.type,
                actor=args.actor, owner=args.owner, due_at=args.due, note=args.note,
            ), as_json=as_json, title="行动已提出")
            return 0
        if args.command == "accept":
            _emit(app.accept_action(
                args.action_id, actor=args.actor, owner=args.owner, due_at=args.due,
                accepted_at=args.accepted_at, note=args.note,
            ), as_json=as_json, title="门店已接受")
            return 0
        if args.command == "reject-action":
            _emit(app.reject_action(args.action_id, actor=args.actor, note=args.note),
                  as_json=as_json, title="门店已拒绝")
            return 0
        if args.command == "complete":
            _emit(app.complete_action(
                args.action_id, actor=args.actor, outcome_ref=args.outcome_ref,
                completed_at=args.completed_at, note=args.note,
            ), as_json=as_json, title="行动完成")
            return 0
        if args.command == "mark-late":
            _emit(app.mark_late_actions(as_of=args.as_of), as_json=as_json,
                  title="逾期行动")
            return 0
        if args.command == "outcome":
            result = app.record_outcome(
                args.action_id, actor=args.actor, reviewer=args.reviewer,
                period_start=args.start, period_end=args.end,
                metric_version=args.version, comparable=not args.not_comparable,
                note=args.note,
            )
            if as_json:
                _emit(result, as_json=True)
            else:
                snap = result["snapshot"]
                print(f"回看 {result['outcome_id']}  {snap['metric_label']} "
                      f"口径 {snap['metric_version']}")
                print(f"回看期间 {snap['review_period'][0]} → {snap['review_period'][1]}，"
                      f"观测 {snap['review_observations']} 天，覆盖率 {snap['review_coverage']:.0%}")
                print(f"可比: {'是' if snap['comparable'] else '否'}")
                if snap["comparable_note"]:
                    print(f"说明: {snap['comparable_note']}")
                comp = snap["comparison"]
                print(f"中位数 {snap['review_median']} / 诊断期 {snap['diagnosis_baseline_median']}"
                      f"：{comp['interpretation']}")
            return 0
        if args.command == "scorecard":
            _emit(app.scorecard(as_of=args.as_of, diagnosis_id=args.diagnosis),
                  as_json=as_json, title="行动执行看板")
            return 0
        if args.command == "metric":
            if args.metric_command == "register":
                _emit(app.register_metric_version(
                    metric=args.metric, version=args.version, actor=args.actor,
                    effective_from=args.effective_from, definition=args.definition,
                    note=args.note,
                ), as_json=as_json, title="口径已登记")
            else:
                _emit(app.list_metric_versions(), as_json=as_json, title="指标口径")
            return 0
    except Exception as exc:  # CLI 边界：把业务错误转成可读输出
        message = f"{type(exc).__name__}: {exc}"
        if as_json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        else:
            print(f"错误: {message}", file=sys.stderr)
        return 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
