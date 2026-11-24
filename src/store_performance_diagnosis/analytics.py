"""可追溯的经营诊断。

分析遵循几条硬约束：

1. **缺数不是零**：``source_state=device-out`` 的日期记为缺失；覆盖率不足时
   结论为“证据不足”，而不是“客流为零”。
2. **口径不混用**：只在诊断声明的 ``metric_version`` 内比较，同比窗口、同业
   门店也必须是同一版本。
3. **相关不等于因果**：促销、缺货、翻新、闭店只作为可能的混杂因素列出，
   候选原因一律标记 ``is_root_cause=False``，需由区域经理结合本地运营事实
   判断，再由门店确认。
4. **同业差异不是能力排名**：只提供同 peer_group、非翻新门店的中位数和
   偏差，不排序、不打分。

所有阈值来自 :mod:`config`，并写入证据快照，保证将来回看时知道当时的判断口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Any

from .config import (
    ANALYSIS_POLICY,
    APP_VERSION,
    GOOD_COVERAGE_THRESHOLD,
    INVENTORY_RISK_STATES,
    LOW_COVERAGE_THRESHOLD,
    MATERIAL_CHANGE_PCT,
    METRIC_DIRECTION,
    METRIC_LABELS,
    PEER_DIVERGENCE_PP,
    PEER_MIN_WINDOW_OBSERVATIONS,
    RELATED_METRICS,
    USABLE_SOURCE_STATES,
)
from .db import Database
from .utils import canonical_json, now_iso, parse_date, sha256_text


DISCLAIMERS = [
    "指标变化本身不构成根因；候选原因需由区域经理结合本地运营事实验证。",
    "同 peer_group 门店差异只用于可比范围参照，不表示经营能力排名。",
    "任何缺失、部分采集或设备停机的日期均不按零值参与计算。",
    "指标口径修订不会改写已生成的诊断证据。",
]


@dataclass
class Observation:
    date: str
    value: float | None
    source_state: str

    @property
    def usable(self) -> bool:
        return self.value is not None and self.source_state in USABLE_SOURCE_STATES


@dataclass
class WindowStats:
    start: str
    end: str
    days: int
    observations: list[Observation]

    @property
    def usable(self) -> list[Observation]:
        return [o for o in self.observations if o.usable]

    @property
    def values(self) -> list[float]:
        return [o.value for o in self.usable if o.value is not None]

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def coverage(self) -> float:
        return self.count / self.days if self.days else 0.0

    @property
    def median(self) -> float | None:
        return median(self.values) if self.values else None

    @property
    def missing_states(self) -> dict[str, int]:
        """不可用于计算的日期计数。"""
        counts: dict[str, int] = {}
        for observation in self.observations:
            if observation.usable:
                continue
            counts[observation.source_state] = counts.get(observation.source_state, 0) + 1
        return counts

    @property
    def source_state_counts(self) -> dict[str, int]:
        """全部日期的采集状态计数，包含可用与不可用。"""
        counts: dict[str, int] = {}
        for observation in self.observations:
            counts[observation.source_state] = counts.get(observation.source_state, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "days": self.days,
            "observation_count": self.count,
            "coverage": round(self.coverage, 4),
            "median": self.median,
            **({"missing_states": self.missing_states} if self.missing_states else {}),
            "series": [
                {
                    "date": o.date,
                    "value": o.value,
                    "source_state": o.source_state,
                    "used": o.usable,
                }
                for o in self.observations
            ],
        }


def build_window(
    observations_by_date: dict[str, Observation],
    start: date,
    end: date,
) -> WindowStats:
    ordered: list[Observation] = []
    cursor = start
    while cursor <= end:
        iso = cursor.isoformat()
        ordered.append(
            observations_by_date.get(iso, Observation(iso, None, "no-record"))
        )
        cursor += timedelta(days=1)
    return WindowStats(start.isoformat(), end.isoformat(), len(ordered), ordered)


def load_series(
    db: Database, store_id: str, metric: str, version: str, start: str, end: str
) -> dict[str, Observation]:
    rows = db.fetch_all(
        """
        SELECT date, value, source_state FROM daily_metric
        WHERE store_id=? AND metric=? AND metric_version=? AND date BETWEEN ? AND ?
        ORDER BY date
        """,
        (store_id, metric, version, start, end),
    )
    return {
        r["date"]: Observation(r["date"], r["value"], r["source_state"]) for r in rows
    }


def _prior_window(start: date, days: int) -> tuple[date, date]:
    end = start - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def _direction(metric: str) -> int:
    return METRIC_DIRECTION.get(metric, 0)


def classify_change(
    metric: str,
    current: float | None,
    baseline: float | None,
    *,
    material: float = MATERIAL_CHANGE_PCT,
) -> dict[str, Any]:
    if current is None or baseline is None:
        return {
            "delta": None,
            "delta_pct": None,
            "direction": "unknown",
            "material": None,
            "interpretation": "缺少可比较的基线或当期观测，不能判断变化方向",
        }
    delta = current - baseline
    if baseline == 0:
        delta_pct = None
    else:
        delta_pct = delta / abs(baseline) * 100
    if delta == 0:
        direction = "flat"
    else:
        direction = "up" if delta > 0 else "down"
    if delta_pct is None:
        is_material = abs(delta) > 0
    else:
        is_material = abs(delta_pct) >= material
    prior = _direction(metric)
    if not is_material or direction == "flat":
        business = "无实质变化" if not is_material else "持平"
    elif prior == 0:
        business = "变化方向需结合指标定义判断"
    elif (prior > 0 and direction == "up") or (prior < 0 and direction == "down"):
        business = "朝有利方向变化"
    else:
        business = "朝不利方向变化"
    return {
        "delta": round(delta, 4),
        "delta_pct": None if delta_pct is None else round(delta_pct, 2),
        "direction": direction,
        "material": is_material,
        "favorable": None if prior == 0 else (
            True if not is_material else (
                (prior > 0 and direction == "up") or (prior < 0 and direction == "down")
            )
        ),
        "interpretation": business,
    }


def _missing_runs(window: WindowStats) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    current: list[Observation] = []
    for observation in window.observations:
        if observation.usable:
            if current:
                runs.append(_run_records(current))
                current = []
        else:
            current.append(observation)
    if current:
        runs.append(_run_records(current))
    return [r for r in runs if r["days"] >= 1]


def _run_records(observations: list[Observation]) -> dict[str, Any]:
    states: dict[str, int] = {}
    for observation in observations:
        states[observation.source_state] = states.get(observation.source_state, 0) + 1
    reasons = []
    if "device-out" in states:
        reasons.append("设备停机，客流/指标采集缺失，不能按零解释")
    if "no-record" in states:
        reasons.append("该日无记录，不能按零解释")
    if "partial" in states:
        reasons.append("部分采集")
    return {
        "start": observations[0].date,
        "end": observations[-1].date,
        "days": len(observations),
        "source_states": states,
        "notes": reasons or ["缺失原因未标注"],
    }


def _peer_scope(
    db: Database,
    store_id: str,
    metric: str,
    version: str,
    window: WindowStats,
    *,
    peer_group: str | None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "peer_group": peer_group,
        "criterion": "同一 peer_group、非翻新、同一指标版本、同期间",
        "peer_count": 0,
        "peers": [],
        "median": None,
        "store_deviation_pct": None,
        "sufficient": False,
        "note": "仅作可比范围参照，不构成经营能力排名",
    }
    if not peer_group:
        base["note"] = "门店缺少 peer_group，无法确定可比范围"
        return base

    rows = db.fetch_all(
        """
        SELECT s.store_id, m.value
        FROM store s
        JOIN daily_metric m ON m.store_id = s.store_id
        WHERE s.peer_group = ?
          AND s.store_id <> ?
          AND s.renovation_state = 'normal'
          AND m.metric = ?
          AND m.metric_version = ?
          AND m.date BETWEEN ? AND ?
          AND m.value IS NOT NULL
          AND m.source_state IN ('complete', 'partial')
        ORDER BY s.store_id, m.date
        """,
        (peer_group, store_id, metric, version, window.start, window.end),
    )
    per_store: dict[str, list[float]] = {}
    for row in rows:
        per_store.setdefault(row["store_id"], []).append(row["value"])
    eligible = {
        sid: median(values)
        for sid, values in per_store.items()
        if len(values) >= PEER_MIN_WINDOW_OBSERVATIONS
    }
    base["peers"] = [
        {"store_id": sid, "observations": len(per_store[sid]), "median": round(value, 4)}
        for sid, value in sorted(eligible.items())
    ]
    base["peer_count"] = len(eligible)
    if len(eligible) >= 2:
        base["sufficient"] = True
        base["median"] = round(median(eligible.values()), 4)
        store_median = window.median
        if store_median is not None and base["median"]:
            base["store_deviation_pct"] = round(
                (store_median - base["median"]) / abs(base["median"]) * 100, 2
            )
    else:
        base["note"] = (
            "可比门店中同口径观测不足 2 家，同业参照不足以支持判断；"
            "这不是门店表现结论，也不构成经营能力排名"
        )
    return base


def _anomalies(
    period: WindowStats, baseline: WindowStats
) -> list[dict[str, Any]]:
    """以基线中位数与 MAD 标记异常日期；缺失日期单独列出。

    基线少于 3 个观测时不标记异常：用 1–2 个点估计波动会制造噪音。
    """
    findings: list[dict[str, Any]] = []
    base_values = baseline.values
    if len(base_values) < 3:
        return findings
    base_median = median(base_values)
    deviations = [abs(v - base_median) for v in base_values]
    mad = median(deviations)
    for observation in period.observations:
        if not observation.usable or observation.value is None:
            continue
        if mad > 0:
            score = 0.6745 * (observation.value - base_median) / mad
            if abs(score) >= 3.5:
                findings.append(
                    {
                        "date": observation.date,
                        "value": observation.value,
                        "baseline_median": base_median,
                        "modified_z": round(score, 2),
                        "reason": "相对基线中位数的稳健偏离",
                    }
                )
        else:
            threshold = max(1.0, abs(base_median) * 0.1)
            if abs(observation.value - base_median) > threshold:
                findings.append(
                    {
                        "date": observation.date,
                        "value": observation.value,
                        "baseline_median": base_median,
                        "reason": "基线波动为 0，按中位数 10% 阈值标记，仅供参考",
                    }
                )
    return findings


def _confounders(
    store: dict[str, Any],
    events: list[dict[str, Any]],
    period: WindowStats,
    metric: str,
) -> list[dict[str, Any]]:
    confounders: list[dict[str, Any]] = []
    missing = period.missing_states
    states = period.source_state_counts

    if missing.get("device-out"):
        confounders.append(
            {
                "type": "device-out",
                "label": "计数设备停机导致缺数",
                "detail": f"{missing['device-out']} 天的指标采集缺失；这些日期未按零值参与计算",
                "affects": ["coverage", "change_estimate"],
                "evidence": "daily_metrics.source_state=device-out",
            }
        )
    if states.get("partial"):
        confounders.append(
            {
                "type": "partial-capture",
                "label": "部分采集",
                "detail": f"{states['partial']} 天为部分采集，数值可能偏低",
                "affects": ["coverage", "change_estimate"],
                "evidence": "daily_metrics.source_state=partial",
            }
        )
    if store.get("renovation_state") not in (None, "normal"):
        confounders.append(
            {
                "type": "renovation",
                "label": "门店翻新/改造",
                "detail": f"门店状态为 {store['renovation_state']}，客流与转化可能受施工影响",
                "affects": ["traffic", "conversion", "sales"],
                "evidence": "store_context.renovation_state",
            }
        )

    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(event["event_type"], []).append(event)

    if by_type.get("promotion"):
        risky = [
            e
            for e in by_type["promotion"]
            if e["inventory_state"] in INVENTORY_RISK_STATES
        ]
        confounders.append(
            {
                "type": "promotion",
                "label": "促销活动",
                "detail": (
                    f"期间有 {len(by_type['promotion'])} 条促销记录，其中 "
                    f"{len(risky)} 条伴随低库存或缺货；促销与缺货会同时影响销量和转化"
                    if risky
                    else f"期间有 {len(by_type['promotion'])} 条促销记录"
                ),
                "affects": ["sales", "conversion", "traffic", "availability"],
                "evidence": "events.event_type=promotion",
                "inventory_risk": bool(risky),
            }
        )
    if by_type.get("stockout"):
        confounders.append(
            {
                "type": "stockout",
                "label": "缺货事件",
                "detail": f"期间有 {len(by_type['stockout'])} 条缺货记录",
                "affects": ["sales", "conversion", "availability"],
                "evidence": "events.event_type=stockout",
            }
        )
    if by_type.get("closure"):
        confounders.append(
            {
                "type": "closure",
                "label": "闭店/停业",
                "detail": f"期间有 {len(by_type['closure'])} 条闭店记录，营业时间可能变化",
                "affects": ["traffic", "sales", "conversion"],
                "evidence": "events.event_type=closure",
            }
        )
    if by_type.get("price-change"):
        confounders.append(
            {
                "type": "price-change",
                "label": "价格调整",
                "detail": f"期间有 {len(by_type['price-change'])} 条价格调整记录",
                "affects": ["sales", "conversion"],
                "evidence": "events.event_type=price-change",
            }
        )
    return confounders


def _cross_metric_context(
    db: Database,
    store_id: str,
    metric: str,
    version: str,
    period: WindowStats,
    baseline: WindowStats,
) -> list[dict[str, Any]]:
    """关联指标只作背景，不能单独证明原因。"""
    context: list[dict[str, Any]] = []
    for related in RELATED_METRICS.get(metric, ()):
        rows = db.fetch_all(
            "SELECT DISTINCT metric_version FROM daily_metric "
            "WHERE store_id=? AND metric=? AND date BETWEEN ? AND ?",
            (store_id, related, period.start, period.end),
        )
        versions = [r["metric_version"] for r in rows]
        if not versions:
            context.append(
                {
                    "metric": related,
                    "status": "no-data",
                    "note": "期间无该指标数据，不能用于交叉验证",
                }
            )
            continue
        chosen = version if version in versions else versions[0]
        current = build_window(
            load_series(db, store_id, related, chosen, period.start, period.end),
            parse_date(period.start),
            parse_date(period.end),
        )
        prior = build_window(
            load_series(db, store_id, related, chosen, baseline.start, baseline.end),
            parse_date(baseline.start),
            parse_date(baseline.end),
        )
        change = classify_change(related, current.median, prior.median)
        context.append(
            {
                "metric": related,
                "label": METRIC_LABELS.get(related, related),
                "metric_version": chosen,
                "observations": current.count,
                "current_median": current.median,
                "baseline_median": prior.median,
                "change": change,
                "role": "context_only",
                "note": "关联指标变化仅提供背景，不能单独证明因果关系",
            }
        )
    return context


def _candidate_causes(
    *,
    metric: str,
    period: WindowStats,
    baseline: WindowStats,
    confounders: list[dict[str, Any]],
    peers: dict[str, Any],
    change: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(
        key: str,
        label: str,
        category: str,
        confidence: str,
        rationale: list[str],
        evidence: list[str],
        action_types: list[str],
    ) -> None:
        candidates.append(
            {
                "candidate_id": key,
                "label": label,
                "category": category,
                "confidence": confidence,
                "is_root_cause": False,
                "requires_local_validation": True,
                "rationale": rationale,
                "supporting_evidence": evidence,
                "suggested_action_types": action_types,
            }
        )

    missing = period.missing_states
    states = period.source_state_counts
    if missing.get("device-out") or states.get("partial"):
        add(
            "data-quality",
            "数据采集异常可能夸大了变化幅度",
            "data",
            "high" if change["delta"] is not None else "medium",
            [
                f"期间缺失 {missing.get('device-out', 0)} 天设备停机、"
                f"{states.get('partial', 0)} 天部分采集",
                "缺失日期未按零值参与计算，但覆盖率下降会降低结论稳定性",
            ],
            ["daily_metrics.source_state"],
            ["device-repair", "data-audit", "observe"],
        )
    for confounder in confounders:
        if confounder["type"] == "promotion" and change["delta"] is not None:
            add(
                "promotion-stockout",
                "促销与缺货叠加影响需求与转化",
                "operations",
                "medium" if confounder.get("inventory_risk") else "low",
                [
                    "促销会改变进店与购买行为",
                    "促销期缺货会同时压低销量与转化，容易被误判为需求下降",
                ]
                if confounder.get("inventory_risk")
                else ["促销活动改变了当期需求节奏"],
                [confounder["evidence"]],
                ["replenish", "promotion", "observe"],
            )
        elif confounder["type"] == "renovation" and change["delta"] is not None:
            add(
                "renovation",
                "门店翻新影响可比性与客流",
                "environment",
                "medium",
                ["翻新期间门店动线、营业面积或进出通道可能变化"],
                [confounder["evidence"]],
                ["observe", "hours"],
            )
        elif confounder["type"] == "closure" and change["delta"] is not None:
            add(
                "closure-hours",
                "闭店或营业时间调整影响营业时长",
                "operations",
                "medium",
                ["闭店直接减少可营业时间，需核对是否已在指标口径中体现"],
                [confounder["evidence"]],
                ["hours", "observe"],
            )
        elif confounder["type"] == "stockout" and change["delta"] is not None:
            add(
                "stockout",
                "缺货影响销量与转化",
                "operations",
                "medium",
                ["缺货会让可售商品减少，销量下降未必代表需求变化"],
                [confounder["evidence"]],
                ["replenish", "observe"],
            )
        elif confounder["type"] == "price-change" and change["delta"] is not None:
            add(
                "price-change",
                "价格调整影响销量与转化",
                "operations",
                "low",
                ["价格变化会改变购买决策，需与销量、转化同时观察"],
                [confounder["evidence"]],
                ["pricing", "observe"],
            )
        elif confounder["type"] == "partial-capture" and change["delta"] is not None:
            add(
                "partial-capture",
                "部分采集导致数值偏低",
                "data",
                "medium",
                ["部分采集的数值可能系统性偏低，变化幅度需谨慎解读"],
                [confounder["evidence"]],
                ["data-audit", "observe"],
            )

    if change.get("material") and peers.get("sufficient"):
        deviation = peers.get("store_deviation_pct")
        if deviation is not None and abs(deviation) >= PEER_DIVERGENCE_PP:
            add(
                "peer-divergence",
                "与本店变化方向或幅度区别于可比门店",
                "context",
                "low",
                [
                    "同 peer_group、非翻新门店的中位数提供了参照",
                    "门店间差异可能是品类、客群或本地事件，不能直接视为经营能力差异",
                ],
                ["peer comparison"],
                ["observe"],
            )

    if not candidates and change.get("material"):
        add(
            "unexplained-change",
            "暂无可由资料支持的候选原因",
            "unknown",
            "low",
            ["指标出现实质变化，但现有资料不足以指向具体原因"],
            [],
            ["observe", "data-audit"],
        )
    return candidates


def _recommendations(
    confounders: list[dict[str, Any]], metric: str, *, has_estimate: bool
) -> list[dict[str, Any]]:
    """建议方向。没有可用变化估计时只建议补数据，不猜经营动作。"""
    if not has_estimate:
        types = ["data-audit", "observe"]
        if any(c["type"] == "device-out" for c in confounders):
            types.insert(0, "device-repair")
        return [
            {
                "action_type": action_type,
                "note": "证据不足，先补齐数据或核对口径，不据此调整经营",
            }
            for action_type in types
        ]
    types: list[str] = []
    if any(c["type"] == "device-out" for c in confounders):
        types.append("device-repair")
    if any(c["type"] == "promotion" for c in confounders):
        types.extend(["promotion", "replenish"])
    if any(c["type"] in {"stockout"} for c in confounders):
        types.append("replenish")
    if any(c["type"] == "closure" for c in confounders):
        types.append("hours")
    if any(c["type"] == "price-change" for c in confounders):
        types.append("pricing")
    if metric in {"traffic", "conversion"}:
        types.append("display")
    if not types:
        types.append("observe")
    seen: list[str] = []
    for action_type in types:
        if action_type not in seen:
            seen.append(action_type)
    return [
        {
            "action_type": action_type,
            "note": "建议方向，需区域经理选择、门店确认责任人与期限",
        }
        for action_type in seen
    ]


def _grade(
    period: WindowStats,
    baseline: WindowStats,
    peers: dict[str, Any],
    confounders: list[dict[str, Any]],
) -> tuple[str, str, list[str]]:
    reasons: list[str] = []
    if period.count < 2:
        reasons.append(f"当期仅 {period.count} 个可用观测，少于最低要求 2")
    if baseline.count < 1:
        reasons.append("缺少可比较的基线观测，无法估计变化")
    if period.coverage < LOW_COVERAGE_THRESHOLD:
        reasons.append(
            f"当期覆盖率 {period.coverage:.0%}，低于 {LOW_COVERAGE_THRESHOLD:.0%}"
        )
    if reasons:
        return "insufficient", "insufficient_evidence", reasons

    if (
        period.coverage >= GOOD_COVERAGE_THRESHOLD
        and baseline.count >= 3
        and period.count >= 3
        and peers.get("sufficient")
        and not confounders
    ):
        return "high", "diagnosed", []
    if baseline.count < 2:
        return "low", "diagnosed", ["基线观测偏少，变化估计不稳定"]
    if not peers.get("sufficient"):
        return "low", "diagnosed", ["同业可比观测不足，无法进行同范围参照"]
    if confounders:
        return "medium", "diagnosed", ["存在混杂因素，变化不能直接归因"]
    return "medium", "diagnosed", []


def build_evidence(
    db: Database,
    diagnosis_id: str,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """生成（并不落库）某次诊断的证据快照。"""
    diagnosis = db.fetch_one(
        "SELECT * FROM diagnosis WHERE diagnosis_id=?", (diagnosis_id,)
    )
    if diagnosis is None:
        raise KeyError(f"诊断不存在: {diagnosis_id}")
    store = db.fetch_one(
        "SELECT * FROM store WHERE store_id=?", (diagnosis["store_id"],)
    )
    store_dict = dict(store) if store else {"store_id": diagnosis["store_id"]}

    start = parse_date(diagnosis["period_start"])
    end = parse_date(diagnosis["period_end"])
    days = (end - start).days + 1
    baseline_start, baseline_end = _prior_window(start, days)

    version = diagnosis["metric_version"]
    metric = diagnosis["metric"]
    period = build_window(
        load_series(db, diagnosis["store_id"], metric, version, start.isoformat(), end.isoformat()),
        start,
        end,
    )
    baseline = build_window(
        load_series(
            db,
            diagnosis["store_id"],
            metric,
            version,
            baseline_start.isoformat(),
            baseline_end.isoformat(),
        ),
        baseline_start,
        baseline_end,
    )

    events = [
        dict(r)
        for r in db.fetch_all(
            "SELECT * FROM event WHERE store_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (diagnosis["store_id"], start.isoformat(), end.isoformat()),
        )
    ]
    confounders = _confounders(store_dict, events, period, metric)
    peers = _peer_scope(
        db,
        diagnosis["store_id"],
        metric,
        version,
        period,
        peer_group=store_dict.get("peer_group"),
    )
    change = classify_change(metric, period.median, baseline.median)
    grade, status, grade_reasons = _grade(period, baseline, peers, confounders)
    candidates = _candidate_causes(
        metric=metric,
        period=period,
        baseline=baseline,
        confounders=confounders,
        peers=peers,
        change=change,
    )
    anomalies = _anomalies(period, baseline)
    missing_runs = _missing_runs(period)
    cross_metric = _cross_metric_context(
        db, diagnosis["store_id"], metric, version, period, baseline
    )

    caveats: list[str] = list(grade_reasons)
    if missing_runs:
        caveats.append("当期存在缺失时间段，详见 missing_runs；缺失不等于零")
    if peers.get("sufficient") and peers.get("store_deviation_pct") is not None:
        caveats.append("同业偏差只反映可比范围内的数值差异，不用于评价经营能力")
    if store is None:
        caveats.append("门店属性缺失，翻新状态与 peer_group 无法核实")

    evidence = {
        "schema": "diagnosis-evidence/1",
        "app_version": APP_VERSION,
        "diagnosis_id": diagnosis_id,
        "generated_at": generated_at or now_iso(),
        "diagnosis": {
            "diagnosis_id": diagnosis_id,
            "store_id": diagnosis["store_id"],
            "metric": metric,
            "metric_label": METRIC_LABELS.get(metric, metric),
            "period_start": diagnosis["period_start"],
            "period_end": diagnosis["period_end"],
            "metric_version": version,
            "review_state": diagnosis["review_state"],
        },
        "store": {
            "store_id": store_dict.get("store_id"),
            "region": store_dict.get("region"),
            "format": store_dict.get("format"),
            "open_minutes": store_dict.get("open_minutes"),
            "renovation_state": store_dict.get("renovation_state"),
            "peer_group": store_dict.get("peer_group"),
        },
        "policy": ANALYSIS_POLICY,
        "status": status,
        "evidence_grade": grade,
        "summary": {
            "current_median": period.median,
            "baseline_median": baseline.median,
            "change": change,
            "coverage": round(period.coverage, 4),
            "baseline_coverage": round(baseline.coverage, 4),
            "usable_observations": period.count,
            "baseline_observations": baseline.count,
        },
        "windows": {"period": period.as_dict(), "baseline": baseline.as_dict()},
        "missing_runs": missing_runs,
        "anomalies": anomalies,
        "confounders": confounders,
        "peer": peers,
        "cross_metric_context": cross_metric,
        "events": events,
        "candidate_causes": candidates,
        "recommended_actions": _recommendations(
            confounders, metric, has_estimate=change["delta"] is not None
        ),
        "caveats": caveats,
        "disclaimers": DISCLAIMERS,
        "evidence_refs": {
            "daily_metrics": {
                "store_id": diagnosis["store_id"],
                "metric": metric,
                "metric_version": version,
                "period": [diagnosis["period_start"], diagnosis["period_end"]],
                "baseline": [baseline_start.isoformat(), baseline_end.isoformat()],
            },
            "events": [e["event_id"] for e in events],
            "store_context": diagnosis["store_id"] if store else None,
        },
    }
    return evidence


def evidence_hashes(evidence: dict[str, Any]) -> tuple[str, str]:
    """返回 (policy_hash, input_digest)。"""
    policy_hash = sha256_text(canonical_json(evidence["policy"]))
    digest_source = {
        "policy_hash": policy_hash,
        "windows": evidence["windows"],
        "events": evidence["events"],
        "peer": evidence["peer"],
        "store": evidence["store"],
    }
    return policy_hash, sha256_text(canonical_json(digest_source))


def compact_series(series: list[dict[str, Any]]) -> str:
    """把序列渲染成便于终端阅读的单行摘要。"""
    parts = []
    for item in series:
        if item["used"]:
            parts.append(f"{item['date'][5:]}={item['value']:g}")
        else:
            parts.append(f"{item['date'][5:]}=缺({item['source_state']})")
    return " ".join(parts)


__all__ = [
    "DISCLAIMERS",
    "Observation",
    "WindowStats",
    "build_evidence",
    "build_window",
    "classify_change",
    "compact_series",
    "evidence_hashes",
    "load_series",
]
