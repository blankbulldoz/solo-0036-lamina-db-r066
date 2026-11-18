# LaminaDB 查询工作负载语料（可重建）

面向后续规划器比较的、带历史纵深的可重建查询语料。完整设计动机与取舍见
[DESIGN.md](DESIGN.md)，逐维度证据见 [report/coverage_matrix.md](report/coverage_matrix.md)。

## 一键重建

先在仓库根构建二进制（需要 Go 1.25+）：

```sh
go build -o bin/laminad ./cmd/laminad
```

然后重建整个语料（确定性，重复执行字节级一致）：

```sh
python3 -I corpus/tools/rebuild_all.py --binary bin/laminad
```

只重新生成输入与独立答案、不启动服务（离线）：

```sh
python3 -I corpus/tools/rebuild_all.py --skip-run
```

所有 Python 仅用标准库，以隔离模式（`-I`）运行；laminad 数据目录均为系统临时
目录下的全新空目录，脚本不依赖工作目录中的任何模块。

## 工件地图

| 路径 | 角色 | 谁产生 | 是否接触 laminad |
| --- | --- | --- | --- |
| `scenarios/**/*.script.json` | 输入生成材料 + 可重放操作脚本 | `build_scenarios.py`（固定 seed） | 否 |
| `answers/**/*.answers.json` | 独立答案、可查询性判定、vacuum/stats 事实 | `reference_model.py` + `derive_answers.py` | 否 |
| `runs/**/*.run.json` | 真实 HTTP 运行事实（状态码、plan、410、vacuum） | `run_corpus.py` | 是 |
| `report/verification.json` | 独立答案 vs 运行事实逐字段核对结果 | `verify_corpus.py` | 只读两侧文件 |
| `report/selection_report.json` | 三方案预算/覆盖实测、变异杀死证据 | `selection_experiment.py` + `mutants.py` | 否 |
| `report/coverage_matrix.md` | 人读覆盖矩阵与代价说明 | `coverage_matrix.py` | 否 |
| `report/manifest.json` | 机器可读清单 | `coverage_matrix.py` | 否 |

## 场景

| 场景 | 判别重点 |
| --- | --- |
| `f1_drift_pair/{a,b}` | **head 分布逐字节相同、更新轨迹不同**：历史快照答案与索引 candidates 不同 |
| `f2_low_selectivity` | 低选择性谓词（布尔、宽区间）、null 质量、聚合、scan/index 对照 |
| `f3_high_dup` | 高重复值、唯一索引、事务内置换、唯一竞争 409 |
| `f4_watermark` | 活动读者钉住水位、推进回收、墓碑、410 及其跨重启持久性 |
| `f0_smoke` | 排序/limit/投影/聚合基线与 SQL 等价性 |

当前规模：6 场景、48 条结构化查询答案、3 条 SQL 等价核对、2 条真实 410 观测；
独立模型与真实 laminad 的 48/48 条查询、全部 vacuum 与冲突事实逐字段一致。

## 可信边界

`reference_model.py` 是对公开语义的独立 Python 实现（版本链可见性、vacuum 水位、
索引历史超集、plan 选择、null 与聚合规则），不读取 Go 源码或运行结果；它与真实
服务只在 `verify_corpus.py` 中碰面。任何一侧的偏差都会让重建以非零状态失败。
