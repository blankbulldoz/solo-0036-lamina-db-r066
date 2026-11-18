"""Derive independent answers for every scenario script.

Runs the reference model over each script and emits one answer file per
scenario. The answer file contains the per-query expected result, the
queryability decision derived from the modeled watermark, and vacuum/stats
facts. Nothing in this module contacts laminad.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reference_model import ReferenceDB, replay  # noqa: E402


def parse_sql(sql: str) -> dict[str, Any]:
    """Minimal independent parser for the single-SELECT subset used in
    sql_checks. It exists only to derive expected answers independently of
    the Go syntax package."""
    text = sql.strip().rstrip(";").strip()
    m = re.match(r"select\s+(.*?)\s+from\s+([a-z][a-z0-9_]*)"
                 r"(?:\s+where\s+(.*?))?(?:\s+order\s+by\s+(.*?))?"
                 r"(?:\s+limit\s+(\d+))?$", text, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError(f"cannot parse SQL: {sql}")
    projection, relation, where, order, limit = m.groups()
    request: dict[str, Any] = {"relation": relation}
    if re.match(r"count\s*\(\s*\*\s*\)", projection, re.I):
        request["aggregate"] = {"function": "count", "column": "*"}
    elif re.match(r"(count|sum)\s*\(\s*([a-z][a-z0-9_]*)\s*\)",
                  projection, re.I):
        agg = re.match(r"(count|sum)\s*\(\s*([a-z][a-z0-9_]*)\s*\)",
                       projection, re.I)
        request["aggregate"] = {"function": agg.group(1).lower(),
                                "column": agg.group(2)}
    elif projection.strip() != "*":
        request["columns"] = [c.strip() for c in projection.split(",")]
    if where:
        predicates = []
        for part in re.split(r"\s+and\s+", where, flags=re.I):
            part = part.strip()
            null_m = re.match(r"([a-z][a-z0-9_]*)\s+is\s+(not\s+)?null$",
                              part, re.I)
            if null_m:
                predicates.append({"column": null_m.group(1),
                                   "op": "is_null" if not null_m.group(2)
                                   else "not_null"})
                continue
            cmp_m = re.match(r"([a-z][a-z0-9_]*)\s*(>=|<=|!=|<>|=|<|>)\s*"
                             r"(-?\d+|'((?:[^']|'')*)'|true|false|null)$",
                             part, re.I)
            if not cmp_m:
                raise ValueError(f"cannot parse predicate: {part}")
            col, op, raw, _str = cmp_m.groups()
            op = "!=" if op == "<>" else op
            value: Any
            if raw.lower() == "null":
                value = None
            elif raw.lower() in ("true", "false"):
                value = raw.lower() == "true"
            elif raw.startswith("'"):
                value = raw[1:-1].replace("''", "'")
            else:
                value = int(raw)
            predicates.append({"column": col, "op": op, "value": value})
        request["where"] = predicates
    if order:
        orderings = []
        for part in order.split(","):
            tokens = part.strip().split()
            orderings.append({"column": tokens[0],
                              "desc": len(tokens) > 1
                              and tokens[1].lower() == "desc"})
        request["order"] = orderings
    if limit:
        request["limit"] = int(limit)
    return request


def derive(script_path: str, out_path: str) -> dict[str, Any]:
    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)
    db, expectations = replay(script)
    answers = []
    for record in expectations:
        if record["op"] != "query":
            continue
        answers.append({
            "label": record["label"],
            "note": next((s.get("note", "") for s in script["operations"]
                          if s.get("op") == "query"), ""),
            "answer": record["answer"],
        })
    # rebuild query notes by walking in order (answers preserve order)
    notes = [s for s in script["operations"] if s.get("op") == "query"]
    for entry, step in zip(answers, notes):
        entry["note"] = step.get("note", "")
        entry["dims"] = step.get("dims", [])
        entry["request"] = step["request"]

    vacuums = [{"label": r.get("label", ""), "result": r["result"]}
               for r in expectations if r["op"] == "vacuum"]
    stats = [{"label": r.get("label", ""), "stats": r["stats"]}
             for r in expectations if r["op"] == "stats"]
    commits = [r["outcome"] for r in expectations
               if r["op"] == "commit"]
    sql_answers = []
    for sql in script.get("sql_checks", []):
        sql_request = parse_sql(sql)
        # head-state answers: sql_checks run after the script completes
        sql_answers.append({
            "sql": sql,
            "request": sql_request,
            "answer": db.query(sql_request),
        })
    document = {
        "scenario": script["scenario"],
        "family": script["family"],
        "head_sequence": db.sequence,
        "head_horizon": db.horizon,
        "final_stats": db.stats(),
        "vacuum_facts": vacuums,
        "stats_facts": stats,
        "commit_outcomes": commits,
        "answers": answers,
        "sql_answers": sql_answers,
        "provenance": {
            "derived_by": "corpus/tools/reference_model.py",
            "source_script": os.path.basename(script_path),
            "independence": "no laminad binary, no run observations consulted",
        },
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(document, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return document


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scenarios = os.path.join(root, "scenarios")
    answers_dir = os.path.join(root, "answers")
    total_queries = 0
    for dirpath, _dirs, files in os.walk(scenarios):
        for name in sorted(files):
            if not name.endswith(".script.json"):
                continue
            script_path = os.path.join(dirpath, name)
            rel = os.path.relpath(script_path, scenarios)
            out_path = os.path.join(
                answers_dir, rel.replace(".script.json", ".answers.json"))
            doc = derive(script_path, out_path)
            total_queries += len(doc["answers"])
            print(f"{rel}: {len(doc['answers'])} queries, "
                  f"head seq={doc['head_sequence']} horizon={doc['head_horizon']}")
    print(f"derived {total_queries} independent answers")


if __name__ == "__main__":
    main()
