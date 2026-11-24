"""业务常量与可调策略。

这里的策略只依赖标准库，并且会被写入每份诊断证据快照，便于以后回看时
知道当时的分析口径。
"""

from __future__ import annotations

from typing import Final

APP_NAME: Final = "store_performance_diagnosis"
APP_VERSION: Final = "0.1.0"

# 资料契约：文件名 -> 表头顺序。
MATERIAL_SCHEMAS: Final[dict[str, tuple[str, ...]]] = {
    "daily_metrics.csv": (
        "store_id",
        "date",
        "metric",
        "value",
        "metric_version",
        "source_state",
    ),
    "store_context.csv": (
        "store_id",
        "region",
        "format",
        "open_minutes",
        "renovation_state",
        "peer_group",
    ),
    "events.csv": (
        "event_id",
        "store_id",
        "date",
        "event_type",
        "campaign_id",
        "inventory_state",
        "source_ref",
    ),
    "diagnoses.csv": (
        "diagnosis_id",
        "store_id",
        "metric",
        "period_start",
        "period_end",
        "metric_version",
        "review_state",
    ),
    "actions.csv": (
        "action_id",
        "diagnosis_id",
        "owner",
        "action_type",
        "accepted_at",
        "due_at",
        "execution_state",
        "outcome_ref",
    ),
}

# 导入顺序体现依赖关系：门店属性与事实先入，工作流记录后入。
IMPORT_ORDER: Final[tuple[str, ...]] = (
    "store_context.csv",
    "daily_metrics.csv",
    "events.csv",
    "diagnoses.csv",
    "actions.csv",
)

# 指标口径。未知指标仍然可以导入，但不会假定越高越好。
METRIC_LABELS: Final[dict[str, str]] = {
    "sales": "销量",
    "traffic": "客流",
    "conversion": "转化率",
    "availability": "有货率",
    "stockout": "缺货率",
    "transactions": "交易笔数",
    "basket_size": "客单价",
    "sales_amount": "销售额",
}
# 1 表示越高越好，-1 表示越低越好，0 表示没有先验方向。
METRIC_DIRECTION: Final[dict[str, int]] = {
    "sales": 1,
    "traffic": 1,
    "conversion": 1,
    "availability": 1,
    "stockout": -1,
    "transactions": 1,
    "basket_size": 1,
    "sales_amount": 1,
}
COUNT_METRICS: Final[frozenset[str]] = frozenset(
    {"sales", "traffic", "transactions", "stockout"}
)

SOURCE_STATES: Final[frozenset[str]] = frozenset(
    {"complete", "partial", "device-out"}
)
# 只有这两种状态可以提供数值；device-out 明确是缺数，绝不能按 0 处理。
USABLE_SOURCE_STATES: Final[frozenset[str]] = frozenset({"complete", "partial"})
MISSING_SOURCE_STATES: Final[frozenset[str]] = frozenset({"device-out"})

REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {"draft", "reviewed", "reopened", "rejected"}
)
EXECUTION_STATES: Final[frozenset[str]] = frozenset(
    {"proposed", "accepted", "done", "late", "rejected"}
)

EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"promotion", "stockout", "closure", "price-change"}
)
INVENTORY_RISK_STATES: Final[frozenset[str]] = frozenset({"low", "out"})

# 行动目录。区域经理只能在已审阅的诊断上提出行动；门店在承诺前可以改责任人与期限。
ACTION_CATALOG: Final[dict[str, str]] = {
    "hours": "调整营业时间",
    "display": "调整陈列与动线",
    "replenish": "补货并核对库存",
    "observe": "观察并复核数据",
    "device-repair": "修复客流计数设备",
    "data-audit": "核对指标口径与采集链路",
    "staffing": "调整现场人员安排",
    "pricing": "复核价格策略",
    "promotion": "调整促销执行",
}

# 诊断区间与基线窗口的可比条件。
MIN_WINDOW_OBSERVATIONS: Final = 2
PEER_MIN_WINDOW_OBSERVATIONS: Final = 2
LOW_COVERAGE_THRESHOLD: Final = 0.50
GOOD_COVERAGE_THRESHOLD: Final = 0.80
MATERIAL_CHANGE_PCT: Final = 5.0
PEER_DIVERGENCE_PP: Final = 10.0

# 跨指标关联只用于提供上下文，不能直接构成根因。
RELATED_METRICS: Final[dict[str, tuple[str, ...]]] = {
    "sales": ("traffic", "conversion", "availability", "stockout", "transactions"),
    "traffic": ("sales", "conversion", "transactions"),
    "conversion": ("traffic", "sales", "availability", "stockout"),
    "availability": ("stockout", "sales", "conversion"),
    "stockout": ("availability", "sales", "conversion"),
}

# 分析策略会原样写入证据快照。
ANALYSIS_POLICY: Final[dict[str, object]] = {
    "min_window_observations": MIN_WINDOW_OBSERVATIONS,
    "peer_min_window_observations": PEER_MIN_WINDOW_OBSERVATIONS,
    "low_coverage_threshold": LOW_COVERAGE_THRESHOLD,
    "good_coverage_threshold": GOOD_COVERAGE_THRESHOLD,
    "material_change_pct": MATERIAL_CHANGE_PCT,
    "peer_divergence_pp": PEER_DIVERGENCE_PP,
    "usable_source_states": sorted(USABLE_SOURCE_STATES),
    "missing_source_states": sorted(MISSING_SOURCE_STATES),
    "missing_is_zero": False,
    "aggregation": "median",
    "comparison": "equal-length immediately preceding window",
    "peer_scope": "same peer_group, non-renovating, same metric_version",
    "root_cause_asserted": False,
}

# 生成诊断/行动编号时使用的默认前缀（导入资料中的编号保持不变）。
DIAGNOSIS_PREFIX: Final = "DG"
ACTION_PREFIX: Final = "AC"
