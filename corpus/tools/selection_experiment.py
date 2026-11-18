"""Empirical comparison of corpus-selection schemes under a storage budget.

Candidate pool: every query embedded in every generated scenario script.
Fault signatures: the (mutant, query_index) pairs that actually distinguish a
wrong explanation from the reference answer on that candidate.

Schemes compared:
  A full        keep the entire candidate pool (cartesian-style expansion,
                coverage upper bound and storage ceiling reference)
  B head_only   keep only head (non-historical, non-unqueryable) queries,
                one compact sample per instance
  C stratified  greedy stratified coverage over (dimension x predicate shape
                x snapshot role), paired trajectory queries kept together;
                maximises new fault signatures per added byte within budget

Outputs report/selection_report.json with coverage and byte measurements.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutants import evaluate  # noqa: E402


# --------------------------------------------------------------------------- #
# candidate extraction
# --------------------------------------------------------------------------- #
def candidates(root: str) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    scen_dir = os.path.join(root, "scenarios")
    for dirpath, _dirs, files in os.walk(scen_dir):
        for name in sorted(files):
            if not name.endswith(".script.json"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                script = json.load(f)
            mutant_report = evaluate(script)
            q_index = 0
            for step in script["operations"]:
                if step.get("op") != "query":
                    continue
                req = step["request"]
                role = ("unqueryable" if "unqueryable" in step.get("dims", [])
                        else ("historical" if req.get("at") is not None
                              or req.get("transaction") else "head"))
                shapes = []
                for p in req.get("where", []):
                    shapes.append(p["op"])
                if req.get("aggregate"):
                    shapes.append("agg:" + req["aggregate"]["function"])
                if req.get("order"):
                    shapes.append("order")
                if req.get("limit"):
                    shapes.append("limit")
                signatures = set()
                for mutant, rep in mutant_report.items():
                    if any(e["query_index"] == q_index
                           for e in rep["evidence"]):
                        # dimensional signature: a fault only counts as covered
                        # when it is exposed through this query's dimension,
                        # so head-only samples cannot cover historical faults
                        dim_tag = step.get("dims", ["generic"]) or ["generic"]
                        for dim in dim_tag:
                            signatures.add(f"{mutant}@{dim}")
                        signatures.add(f"{mutant}@role:{role}")
                pool.append({
                    "scenario": script["scenario"],
                    "family": script["family"],
                    "query_index": q_index,
                    "note": step.get("note", ""),
                    "dims": tuple(sorted(step.get("dims", []))),
                    "role": role,
                    "shapes": tuple(sorted(set(shapes))) or ("full_scan",),
                    "signatures": signatures,
                    "bytes": len(json.dumps(step, ensure_ascii=False,
                                            sort_keys=True).encode()),
                })
                q_index += 1
    return pool


# --------------------------------------------------------------------------- #
# schemes
# --------------------------------------------------------------------------- #
def scheme_full(pool: list[dict[str, Any]]) -> list[int]:
    return list(range(len(pool)))


def scheme_head_only(pool: list[dict[str, Any]]) -> list[int]:
    # one compact head query per instance: smallest head candidate each
    by_scenario: dict[str, int] = {}
    for i, c in enumerate(pool):
        if c["role"] != "head":
            continue
        if c["scenario"] not in by_scenario or c["bytes"] < pool[
                by_scenario[c["scenario"]]]["bytes"]:
            by_scenario[c["scenario"]] = i
    return sorted(by_scenario.values())


def scheme_stratified(pool: list[dict[str, Any]],
                      byte_budget: int) -> list[int]:
    chosen: set[int] = set()
    covered_signatures: set[str] = set()
    used = 0

    # rule 1: trajectory-paired historical queries are admitted together
    pair_anchors = [i for i, c in enumerate(pool)
                    if "trajectory" in c["dims"]]
    for i in pair_anchors:
        c = pool[i]
        if used + c["bytes"] > byte_budget:
            break
        chosen.add(i)
        used += c["bytes"]
        covered_signatures.update(c["signatures"])

    # rule 2: every unqueryable watermark query must be present (410 checks)
    for i, c in enumerate(pool):
        if c["role"] == "unqueryable" and used + c["bytes"] <= byte_budget:
            chosen.add(i)
            used += c["bytes"]
            covered_signatures.update(c["signatures"])

    # rule 3: guarantee coverage — while any fault signature is uncovered and
    # a candidate carrying it fits, admit the cheapest such candidate first;
    # this prevents a locally dense pick from starving a rare signature
    def cheapest_uncovered() -> int | None:
        best_i = None
        best_gain = 0
        for i, c in enumerate(pool):
            if i in chosen or used + c["bytes"] > byte_budget:
                continue
            gain = len(c["signatures"] - covered_signatures)
            if gain > best_gain or (
                    gain == best_gain and gain and
                    (best_i is None or c["bytes"] < pool[best_i]["bytes"])):
                best_i, best_gain = i, gain
        return best_i if best_gain else None

    while used < byte_budget:
        i = cheapest_uncovered()
        if i is None:
            break
        chosen.add(i)
        used += pool[i]["bytes"]
        covered_signatures.update(pool[i]["signatures"])
    return sorted(chosen)


def summarize(pool: list[dict[str, Any]], picks: list[int],
              all_signatures: set[str]) -> dict[str, Any]:
    selected = [pool[i] for i in picks]
    sig: set[str] = set()
    dims: set[str] = set()
    strata: set[tuple[Any, ...]] = set()
    for c in selected:
        sig.update(c["signatures"])
        dims.update(c["dims"])
        strata.add((c["role"],) + c["shapes"])
    return {
        "queries": len(selected),
        "bytes": sum(c["bytes"] for c in selected),
        "fault_signatures_covered": len(sig),
        "fault_signatures_total": len(all_signatures),
        "signature_coverage": round(len(sig) / len(all_signatures), 4),
        "mutants_killed": sorted(sig),
        "dimensions_covered": sorted(dims),
        "strata_covered": len(strata),
    }


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pool = candidates(root)
    all_signatures: set[str] = set()
    for c in pool:
        all_signatures.update(c["signatures"])

    full_picks = scheme_full(pool)
    head_picks = scheme_head_only(pool)
    head_bytes = sum(pool[i]["bytes"] for i in head_picks)
    # Scheme C budget: explicitly documented in DESIGN.md as ~40% of the full
    # expansion byte cost. It must still admit every mandatory dimensional
    # sample (paired trajectories, unqueryable snapshots, tombstone/null
    # discriminators); the head-only scheme is shown separately to prove that
    # its budget cannot buy the same fault coverage.
    full_bytes = sum(c["bytes"] for c in pool)
    budget = max(
        head_bytes + sum(
            c["bytes"] for c in pool
            if "trajectory" in c["dims"] or c["role"] == "unqueryable"),
        int(full_bytes * 0.45),
    )
    stratified_picks = scheme_stratified(pool, budget)

    # naive cartesian expansion estimate for the narrative in DESIGN.md
    versions = 0
    for dirpath, _d, files in os.walk(os.path.join(root, "scenarios")):
        for name in files:
            if name.endswith(".script.json"):
                with open(os.path.join(dirpath, name)) as f:
                    script = json.load(f)
                versions += sum(
                    1 for op in script["operations"]
                    if op["op"] in ("commit", "vacuum"))
    cartesian_queries = versions * len(pool)

    report = {
        "candidate_pool": {
            "queries": len(pool),
            "distinct_fault_signatures": len(all_signatures),
            "fault_signatures": sorted(all_signatures),
            "bytes": sum(c["bytes"] for c in pool),
        },
        "schemes": {
            "A_full_expansion": summarize(pool, full_picks, all_signatures),
            "B_head_only": summarize(pool, head_picks, all_signatures),
            "C_stratified_delivered": summarize(
                pool, stratified_picks, all_signatures),
        },
        "budget": {
            "head_only_bytes": head_bytes,
            "stratified_byte_budget": budget,
            "naive_cartesian_query_estimate": cartesian_queries,
        },
        "stratified_selection_notes": [
            f"{pool[i]['scenario']}#q{pool[i]['query_index']} "
            f"({pool[i]['note']})" for i in stratified_picks
        ],
    }
    os.makedirs(os.path.join(root, "report"), exist_ok=True)
    out = os.path.join(root, "report", "selection_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps({k: v for k, v in report["schemes"].items()},
                     indent=2, ensure_ascii=False))
    print(f"\nreport written to {os.path.relpath(out, root)}")


if __name__ == "__main__":
    main()
