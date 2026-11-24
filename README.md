# 门店经营异常诊断与行动效果回看

本项目附带的业务记录均为本地合成数据，标识和数值不对应真实个人、企业或交易。
应用使用 Python 3.11 至 3.14 和标准库（SQLite 内置），无需额外服务。

## 快速开始

```sh
python3 -m unittest discover -s tests -v          # 运行测试
python3 -m store_performance_diagnosis import data/
python3 -m store_performance_diagnosis verify
python3 -m store_performance_diagnosis status
```

在 `src` 布局下未安装包时，先设置 `PYTHONPATH=src`（Windows PowerShell：
`$env:PYTHONPATH="src"`），或执行 `python -m pip install -e .` 后直接使用命令。
Windows PowerShell 可把 `python3` 换成 `python`。

## 业务闭环

| 阶段 | 角色 | 命令 | 约束 |
| --- | --- | --- | --- |
| 导入资料 | 数据团队 | `import` | 幂等；缺数与状态异常只告警，不修正 |
| 生成诊断证据 | 分析师 | `diagnose` / `evidence` | 只追加；写明口径、覆盖率、缺失区间 |
| 区域复核 | 区域经理 | `submit` / `reopen` / `reject-diagnosis` | 只有 `reviewed` 的诊断能提出行动 |
| 提出行动 | 区域经理 | `propose` | 证据不足时只能补数/观察类行动 |
| 门店承诺 | 门店负责人 | `accept` / `reject-action` | 必须确认责任人与期限 |
| 执行与逾期 | 门店负责人 | `complete` / `mark-late` | 未接受不能完成；`late` 表示逾期未完成 |
| 结果回看 | 数据团队 | `outcome` | 必须使用原诊断的指标版本 |
| 执行看板 | 区域经理 | `scorecard` | 未接受记录不计入已执行 |

完整示例：

```sh
B="python3 -m store_performance_diagnosis"
$B --db ops.sqlite3 import data/
$B --db ops.sqlite3 diagnose --store ST-005 --metric traffic \
    --start 2026-11-02 --end 2026-11-06 --version MV-2 --actor analyst
# 记下输出的诊断编号，例如 DG-037
$B --db ops.sqlite3 submit DG-037 --actor manager --note "与本地促销记录一致"
$B --db ops.sqlite3 propose DG-037 --type device-repair --actor manager --due 2026-12-01
# 记下行动编号，例如 AC-049
$B --db ops.sqlite3 accept AC-049 --actor store-lead --owner MGR-05 \
    --due 2026-12-05 --accepted-at 2026-11-28
$B --db ops.sqlite3 complete AC-049 --actor store-lead --outcome-ref OUT-1 \
    --completed-at 2026-12-10
$B --db ops.sqlite3 outcome AC-049 --actor data-team --reviewer data-team \
    --start 2026-12-06 --end 2026-12-15
$B --db ops.sqlite3 scorecard --as-of 2026-12-15
```

所有命令都支持 `--json`，便于接入其他系统。
`python -m store_performance_diagnosis --help` 列出全部子命令。

## 分析口径与边界

- **缺数不是零**：`source_state=device-out` 的日期按缺失处理，原值仅备查；
  覆盖率按自然日计算，低于 50% 直接判定“证据不足”。
- **口径不混用**：诊断、基线窗口、同业门店都只在声明的 `metric_version` 内比较。
- **相关不是因果**：促销、缺货、闭店、翻新、价格调整只列为“可能的混杂因素”，
  候选原因一律 `is_root_cause=False`，需区域经理结合本地运营事实验证、门店确认。
- **同业不是排名**：只提供同 `peer_group`、非翻新门店的中位数和偏差，不排序、不打分。
- **承诺才计执行**：未获门店接受（`accepted_at` 为空）的记录不计入已执行；
  缺少完成日期的 `done` 计入 `executed_date_unknown`，不当作按时完成。
- **历史不可改写**：诊断证据、行动结果、指标口径修订均为只追加记录，数据库触发器
  会拒绝就地修改；指标修订必须使用新版本号。

每次诊断证据都带有 `policy`（当时的阈值）、`policy_hash` 与 `input_digest`，
相同输入可重复生成一致的快照，便于回归与审计。

## 资料

`docs/资料契约.md` 说明各 CSV 文件的字段。`verify` 会报告跨文件问题，包括：
门店属性缺失的孤儿门店、诊断声明版本下没有可用观测、标记完成却缺少门店接受、
完成却缺少结果引用等。

## 代码结构

```
src/store_performance_diagnosis/
  config.py      契约、指标目录、分析阈值
  db.py          SQLite 模式、只追加触发器
  materials.py   CSV 导入与一致性校验
  analytics.py   缺失感知的窗口统计、混杂因素、候选原因、证据快照
  workflow.py    诊断/行动/回看状态机与执行看板
  app.py         服务门面（Python API）
  cli.py         命令行入口
```

Python 调用示例：

```python
from store_performance_diagnosis import StoreDiagnosisApp

app = StoreDiagnosisApp("ops.sqlite3")
app.import_materials("data")
result = app.diagnose(
    store_id="ST-005", metric="traffic",
    period_start="2026-11-02", period_end="2026-11-06",
    metric_version="MV-2", actor="analyst",
)
evidence = result["evidence"]
print(evidence["status"], evidence["missing_runs"])
```

## 已知限制

- 本地示例资料里，诊断声明的指标版本与门店实际数据版本几乎都不匹配，
  因此 `evidence` 会对 36 条已有诊断给出“证据不足”。这是刻意保留的诚实结果，
  不是程序错误；口径不一致应由数据团队核对后再新建诊断。
- 同业比较需要同一 peer_group 内至少 2 家非翻新门店的同口径观测；示例资料
  不满足，故同业参照多为“不足”。
