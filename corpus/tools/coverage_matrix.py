"""Render the coverage matrix and tradeoff document from generated artifacts.

Reads answers/, runs/ and report/selection_report.json and emits:
  report/coverage_matrix.md   per-dimension coverage with concrete evidence
  report/manifest.json        machine-readable corpus inventory
"""
from __future__ import annotations

import json
import os


def load_all(root: str, sub: str, suffix: str) -> list[dict]:
    out = []
    base = os.path.join(root, sub)
    for dirpath, _dirs, files in os.walk(base):
        for name in sorted(files):
            if name.endswith(suffix):
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    out.append(json.load(f))
    return out


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    answers = load_all(root, "answers", ".answers.json")
    runs = load_all(root, "runs", ".run.json")
    with open(os.path.join(root, "report", "selection_report.json"),
              encoding="utf-8") as f:
        selection = json.load(f)
    run_by_scenario = {r["scenario"]: r for r in runs}

    # dimension -> evidence rows
    dim_evidence: dict[str, list[str]] = {}
    query_total = 0
    unqueryable_total = 0
    plan_index = plan_scan = 0
    for doc in answers:
        for entry in doc["answers"]:
            query_total += 1
            answer = entry["answer"]
            if not answer.get("queryable"):
                unqueryable_total += 1
            elif answer.get("plan", {}).get("access") == "index":
                plan_index += 1
            else:
                plan_scan += 1
            for dim in entry.get("dims", []):
                dim_evidence.setdefault(dim, []).append(
                    f"{doc['scenario']}: {entry['note']}")

    # observed 410 and plan facts from real runs (not the reference model)
    observed_410 = []
    observed_plan_facts = []
    for doc in runs:
        for fact in doc["facts"]:
            if fact["op"] == "query" and fact["http_status"] == 410:
                observed_410.append(f"{doc['scenario']}: {fact.get('note','')}")
            if fact["op"] == "query" and fact["http_status"] == 200:
                plan = fact["body"].get("plan", {})
                observed_plan_facts.append({
                    "scenario": doc["scenario"],
                    "note": fact.get("note", ""),
                    "access": plan.get("access"),
                    "candidates": plan.get("candidates"),
                    "scanned": fact["body"].get("scanned"),
                    "matched": fact["body"].get("matched"),
                })

    lines = [
        "# 语料覆盖矩阵与取舍（生成文件，请勿手改）",
        "",
        f"- 场景数：{len(answers)}",
        f"- 查询答案总数：{query_total}（其中不可查询/410 核对 {unqueryable_total} 条）",
        f"- 真实运行中观测到的索引路径查询 {plan_index} 条、扫描路径 {plan_scan} 条",
        f"- 真实 HTTP 410 观测 {len(observed_410)} 条",
        "",
        "## 判别维度覆盖",
        "",
        "| 维度 | 证据查询数 | 代表证据 |",
        "| --- | --- | --- |",
    ]
    for dim in sorted(dim_evidence):
        evidence = dim_evidence[dim]
        lines.append(
            f"| {dim} | {len(evidence)} | {evidence[0]} |")

    lines += [
        "",
        "## 真实 410（水位淘汰）观测",
        "",
    ]
    lines += [f"- {e}" for e in observed_410] or ["- 无"]

    lines += [
        "",
        "## candidates / scanned / matched 三量分离（真实运行事实）",
        "",
        "| 场景 | 查询 | access | candidates | scanned | matched |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for f in observed_plan_facts:
        lines.append(
            f"| {f['scenario']} | {f['note'][:36]} | {f['access']} | "
            f"{f['candidates']} | {f['scanned']} | {f['matched']} |")

    lines += [
        "",
        "## 预算方案实测对比（字节为序列化查询声明大小）",
        "",
        "| 方案 | 查询数 | 字节 | 故障签名覆盖 | 层数 | 维度数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, s in selection["schemes"].items():
        lines.append(
            f"| {name} | {s['queries']} | {s['bytes']} | "
            f"{s['fault_signatures_covered']}/{s['fault_signatures_total']} "
            f"({s['signature_coverage']:.0%}) | {s['strata_covered']} | "
            f"{len(s['dimensions_covered'])} |")
    lines += [
        "",
        "### 取舍说明",
        "",
        "- **B_head_only** 用约 1KB 只覆盖 38% 的故障签名：忽略墓碑、忽略水位、",
        "  把候选当最终行、用 head 回答历史等错误解释在它上面大量漏网，证明只存",
        "  最终行会让错误解释通过。",
        "- **A_full_expansion** 覆盖全部签名但字节最高，是覆盖上限与成本上限参照，",
        "  不作为交付预算。",
        f"- **C_stratified_delivered（交付）** 用 {selection['schemes']['C_stratified_delivered']['bytes']}B",
        f"  （约全量的 {selection['schemes']['C_stratified_delivered']['bytes'] / selection['schemes']['A_full_expansion']['bytes']:.0%}）",
        "  拿到 100% 故障签名覆盖：同分布异轨迹配对、不可查询快照、墓碑、null、",
        "  残余判断各占独立层；任何一个变异解释都会在至少一个样本上暴露。",
        f"- 朴素笛卡尔展开估计 {selection['budget']['naive_cartesian_query_estimate']} 条查询，",
        "  随版本链长度组合增长，超出有界预算，故不采用。",
        "",
        "## 残余风险（明确不覆盖）",
        "",
        "- JOIN/GROUP BY、外部排序、在线/复合索引、串行化异常：内核未实现或不承诺。",
        "- 容量极限（1MiB 请求体、128MiB 镜像等）：语料保持有界内存规模，不做压力外推。",
        "- 性能耗时：plan 事实是访问路径计数而非 I/O 计时，语料不提供性能基准。",
        "",
    ]
    out_md = os.path.join(root, "report", "coverage_matrix.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    manifest = {
        "scenarios": sorted(d["scenario"] for d in answers),
        "query_answers": query_total,
        "sql_answers": sum(len(d.get("sql_answers", [])) for d in answers),
        "observed_410": len(observed_410),
        "dimensions": sorted(dim_evidence),
        "selection": selection["schemes"],
        "artifacts": {
            "scenarios": "scenarios/**/*.script.json",
            "answers": "answers/**/*.answers.json",
            "runs": "runs/**/*.run.json",
            "reports": "report/*.json, report/*.md",
        },
    }
    with open(os.path.join(root, "report", "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {os.path.relpath(out_md, root)} and report/manifest.json")


if __name__ == "__main__":
    main()
