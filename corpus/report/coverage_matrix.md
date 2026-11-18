# 语料覆盖矩阵与取舍（生成文件，请勿手改）

- 场景数：6
- 查询答案总数：48（其中不可查询/410 核对 2 条）
- 真实运行中观测到的索引路径查询 20 条、扫描路径 26 条
- 真实 HTTP 410 观测 2 条

## 判别维度覆盖

| 维度 | 证据查询数 | 代表证据 |
| --- | --- | --- |
| aggregate | 7 | f0_smoke/main: count star |
| compound | 1 | f2_low_selectivity/main: AND of two low-selectivity predicates |
| conflict | 1 | f3_high_dup/main: exactly one racer publishes the unique value |
| high_duplicate | 11 | f3_high_dup/main: high duplicate text equality alpha |
| history | 5 | f3_high_dup/main: unique lookup after in-txn swap |
| limit | 1 | f0_smoke/main: primary-key order with limit truncation |
| low_selectivity | 11 | f4_watermark/main: head sees post-wave values |
| null_semantics | 6 | f0_smoke/main: nulls last on descending order, tie-break by PK |
| ordering | 4 | f0_smoke/main: primary-key order with limit truncation |
| plan_fact | 8 | f0_smoke/main: inequality predicate never uses index |
| projection | 1 | f0_smoke/main: projection with text equality |
| range | 1 | f0_smoke/main: range below threshold |
| residual | 1 | f3_high_dup/main: duplicate text plus boolean residual |
| restart | 2 | f4_watermark/main: 410 survives restart (watermark durable) |
| snapshot_isolation | 1 | f4_watermark/main: pinned reader still sees pre-wave value |
| tombstone | 3 | f4_watermark/main: tombstoned key absent at head |
| trajectory | 8 | f1_drift_pair/a: f1-a early landmark |
| unique | 2 | f3_high_dup/main: unique lookup after in-txn swap |
| unqueryable | 2 | f4_watermark/main: snapshot below watermark must be unavailable |
| watermark | 6 | f4_watermark/main: pinned reader still sees pre-wave value |

## 真实 410（水位淘汰）观测

- f4_watermark/main: snapshot below watermark must be unavailable
- f4_watermark/main: 410 survives restart (watermark durable)

## candidates / scanned / matched 三量分离（真实运行事实）

| 场景 | 查询 | access | candidates | scanned | matched |
| --- | --- | --- | --- | --- | --- |
| f3_high_dup/main | high duplicate text equality alpha | index | 26 | 26 | 26 |
| f3_high_dup/main | high duplicate text equality beta | index | 29 | 29 | 29 |
| f3_high_dup/main | high duplicate text equality gamma | index | 9 | 9 | 9 |
| f3_high_dup/main | high duplicate text equality delta | index | 16 | 16 | 16 |
| f3_high_dup/main | duplicate text plus boolean residual | index | 26 | 26 | 4 |
| f3_high_dup/main | unique lookup after in-txn swap | index | 2 | 2 | 1 |
| f3_high_dup/main | exactly one racer publishes the uniq | index | 1 | 1 | 1 |
| f0_smoke/main | primary-key order with limit truncat | scan | 24 | 24 | 24 |
| f0_smoke/main | nulls last on descending order, tie- | scan | 24 | 24 | 24 |
| f0_smoke/main | projection with text equality | scan | 24 | 24 | 2 |
| f0_smoke/main | range below threshold | index | 4 | 4 | 4 |
| f0_smoke/main | inequality predicate never uses inde | scan | 24 | 24 | 15 |
| f0_smoke/main | count star | scan | 24 | 24 | 24 |
| f0_smoke/main | sum with nulls returns null only whe | scan | 24 | 24 | 24 |
| f4_watermark/main | pinned reader still sees pre-wave va | scan | 50 | 50 | 1 |
| f4_watermark/main | head sees post-wave values | index | 10 | 10 | 10 |
| f4_watermark/main | head query after retention and index | index | 10 | 10 | 10 |
| f4_watermark/main | tombstoned key absent at head | scan | 48 | 48 | 0 |
| f4_watermark/main | wide range exposes tombstone resurre | scan | 48 | 48 | 39 |
| f4_watermark/main | count over range containing tombston | scan | 48 | 48 | 39 |
| f4_watermark/main | head survives restart | index | 10 | 10 | 10 |
| f2_low_selectivity/main | boolean predicate ~90% selectivity | index | 108 | 108 | 108 |
| f2_low_selectivity/main | same predicate via scan | scan | 120 | 120 | 108 |
| f2_low_selectivity/main | wide range ~90% of non-null rows | index | 100 | 100 | 98 |
| f2_low_selectivity/main | wide range scan counterpart | scan | 120 | 120 | 98 |
| f2_low_selectivity/main | null-only predicate cannot use index | scan | 120 | 120 | 11 |
| f2_low_selectivity/main | count of null rows; a null-matching  | scan | 120 | 120 | 11 |
| f2_low_selectivity/main | range over non-null values, nulls mu | index | 109 | 109 | 109 |
| f2_low_selectivity/main | AND of two low-selectivity predicate | index | 108 | 108 | 98 |
| f2_low_selectivity/main | count star over full relation | scan | 120 | 120 | 120 |
| f2_low_selectivity/main | count ignores nulls | scan | 120 | 120 | 120 |
| f2_low_selectivity/main | sum ignores nulls under boolean filt | index | 108 | 108 | 108 |
| f1_drift_pair/a | f1-a early landmark | scan | 60 | 60 | 33 |
| f1_drift_pair/a | f1-a head wide predicate | index | 55 | 55 | 41 |
| f1_drift_pair/a | f1-a mid-history wide predicate | scan | 60 | 60 | 35 |
| f1_drift_pair/a | f1-a head equality | index | 2 | 2 | 0 |
| f1_drift_pair/a | f1-a boolean high duplicate | scan | 60 | 60 | 46 |
| f1_drift_pair/a | f1-a scan counterpart | scan | 60 | 60 | 41 |
| f1_drift_pair/a | f1-a order+limit | scan | 60 | 60 | 60 |
| f1_drift_pair/b | f1-b early landmark | scan | 60 | 60 | 33 |
| f1_drift_pair/b | f1-b head wide predicate | index | 54 | 54 | 41 |
| f1_drift_pair/b | f1-b mid-history wide predicate | scan | 60 | 60 | 28 |
| f1_drift_pair/b | f1-b head equality | index | 1 | 1 | 0 |
| f1_drift_pair/b | f1-b boolean high duplicate | scan | 60 | 60 | 46 |
| f1_drift_pair/b | f1-b scan counterpart | scan | 60 | 60 | 41 |
| f1_drift_pair/b | f1-b order+limit | scan | 60 | 60 | 60 |

## 预算方案实测对比（字节为序列化查询声明大小）

| 方案 | 查询数 | 字节 | 故障签名覆盖 | 层数 | 维度数 |
| --- | --- | --- | --- | --- | --- |
| A_full_expansion | 48 | 9584 | 66/66 (100%) | 17 | 20 |
| B_head_only | 6 | 1024 | 25/66 (38%) | 3 | 5 |
| C_stratified_delivered | 21 | 4280 | 66/66 (100%) | 9 | 16 |

### 取舍说明

- **B_head_only** 用约 1KB 只覆盖 38% 的故障签名：忽略墓碑、忽略水位、
  把候选当最终行、用 head 回答历史等错误解释在它上面大量漏网，证明只存
  最终行会让错误解释通过。
- **A_full_expansion** 覆盖全部签名但字节最高，是覆盖上限与成本上限参照，
  不作为交付预算。
- **C_stratified_delivered（交付）** 用 4280B
  （约全量的 45%）
  拿到 100% 故障签名覆盖：同分布异轨迹配对、不可查询快照、墓碑、null、
  残余判断各占独立层；任何一个变异解释都会在至少一个样本上暴露。
- 朴素笛卡尔展开估计 1728 条查询，
  随版本链长度组合增长，超出有界预算，故不采用。

## 残余风险（明确不覆盖）

- JOIN/GROUP BY、外部排序、在线/复合索引、串行化异常：内核未实现或不承诺。
- 容量极限（1MiB 请求体、128MiB 镜像等）：语料保持有界内存规模，不做压力外推。
- 性能耗时：plan 事实是访问路径计数而非 I/O 计时，语料不提供性能基准。
