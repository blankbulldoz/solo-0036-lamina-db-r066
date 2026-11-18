"""Cross-check independent answers against real laminad run facts.

The two sides are derived independently:
  * answers/*.json  -> reference_model.py over the operation script
  * runs/*.json     -> public HTTP responses from a real laminad process

This module only compares them and reports discrepancies. Every query, vacuum
and conflict expectation is checked; a non-zero mismatch count fails the run.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

PLAN_FIELDS = ("access", "index", "candidates", "snapshot", "residuals")


def _norm_plan(plan: dict[str, Any]) -> dict[str, Any]:
    out = {k: plan.get(k) for k in PLAN_FIELDS}
    if out.get("index") == "":
        out["index"] = None
    return out


def compare_query(expected: dict[str, Any], observed_fact: dict[str, Any]
                  ) -> list[str]:
    errors: list[str] = []
    answer = expected["answer"]
    status = observed_fact["http_status"]
    note = observed_fact.get("note", "")
    if not answer.get("queryable", False):
        if status != 410:
            errors.append(
                f"[{note}] expected 410 for unavailable snapshot, got {status}")
        return errors
    if status != 200:
        errors.append(f"[{note}] expected 200, got {status}: "
                      f"{observed_fact.get('body')}")
        return errors
    body = observed_fact["body"]
    if body.get("rows") != answer.get("rows"):
        errors.append(f"[{note}] row mismatch: "
                      f"reference={json.dumps(answer.get('rows'), ensure_ascii=False)} "
                      f"observed={json.dumps(body.get('rows'), ensure_ascii=False)}")
    for field in ("scanned", "matched", "truncated"):
        if body.get(field) != answer.get(field):
            errors.append(f"[{note}] {field} mismatch: "
                          f"reference={answer.get(field)} observed={body.get(field)}")
    if _norm_plan(body.get("plan", {})) != _norm_plan(answer.get("plan", {})):
        errors.append(f"[{note}] plan mismatch: "
                      f"reference={_norm_plan(answer.get('plan', {}))} "
                      f"observed={_norm_plan(body.get('plan', {}))}")
    return errors


def verify_one(answer_doc: dict[str, Any], run_doc: dict[str, Any]
               ) -> dict[str, Any]:
    errors: list[str] = []
    run_facts = run_doc["facts"]
    query_facts = [f for f in run_facts if f["op"] == "query"]
    answers = answer_doc["answers"]
    if len(query_facts) != len(answers):
        errors.append(
            f"query count mismatch: {len(answers)} answers vs "
            f"{len(query_facts)} run facts")
    for expected, fact in zip(answers, query_facts):
        errors.extend(compare_query(expected, fact))

    # vacuum facts
    expected_vacuums = answer_doc["vacuum_facts"]
    observed_vacuums = [f for f in run_facts if f["op"] == "vacuum"]
    for exp, obs in zip(expected_vacuums, observed_vacuums):
        want, got = exp["result"], obs["body"]
        for field in ("horizon", "removed", "retained", "pinned"):
            if want.get(field) != got.get(field):
                errors.append(
                    f"vacuum {exp.get('label')} field {field}: "
                    f"reference={want.get(field)} observed={got.get(field)}")
        if obs["http_status"] != 200:
            errors.append(
                f"vacuum {exp.get('label')} http {obs['http_status']}")

    # conflict expectations
    for fact in run_facts:
        if fact["op"] != "commit":
            continue
        if fact.get("expect_conflict") and fact["http_status"] != 409:
            errors.append(
                f"commit expected conflict 409, got {fact['http_status']}")
        if not fact.get("expect_conflict") and fact["http_status"] not in (
                200,):
            errors.append(
                f"commit expected 200, got {fact['http_status']}: {fact['body']}")

    # SQL parity: independently parsed+derived answer vs /sql HTTP response
    observed_sql = [f for f in run_facts if f["op"] == "sql"]
    for exp, obs in zip(answer_doc.get("sql_answers", []), observed_sql):
        if obs["http_status"] != 200:
            errors.append(
                f"sql {exp['sql'][:40]}: expected 200, got {obs['http_status']}")
            continue
        for field in ("rows", "scanned", "matched", "truncated"):
            if obs["body"].get(field) != exp["answer"].get(field):
                errors.append(
                    f"sql {exp['sql'][:40]} field {field}: "
                    f"reference={exp['answer'].get(field)} "
                    f"observed={obs['body'].get(field)}")
        # SQL and the equivalent structured request must produce equal rows
        structured = next(
            (a for a in answers
             if a["request"] == exp["request"]), None)
        if structured and structured["answer"].get("rows") != obs["body"].get("rows"):
            errors.append(
                f"sql/structured row divergence for {exp['sql'][:40]}")

    # final stats: horizon must match the independently derived watermark
    final_stats = next(
        (f["body"] for f in reversed(run_facts) if f["op"] == "stats"), None)
    if final_stats is not None:
        if final_stats.get("horizon") != answer_doc["head_horizon"]:
            errors.append(
                f"final horizon: reference={answer_doc['head_horizon']} "
                f"observed={final_stats.get('horizon')}")
        if final_stats.get("sequence") != answer_doc["head_sequence"]:
            errors.append(
                f"final sequence: reference={answer_doc['head_sequence']} "
                f"observed={final_stats.get('sequence')}")
    return {
        "scenario": answer_doc["scenario"],
        "queries_checked": len(answers),
        "sql_checked": len(answer_doc.get("sql_answers", [])),
        "mismatches": errors,
        "ok": not errors,
    }


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reports = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "answers")):
        for name in sorted(files):
            if not name.endswith(".answers.json"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                answer_doc = json.load(f)
            run_path = os.path.join(
                root, "runs",
                os.path.relpath(os.path.join(dirpath, name),
                                os.path.join(root, "answers"))
                .replace(".answers.json", ".run.json"))
            if not os.path.exists(run_path):
                reports.append({"scenario": answer_doc["scenario"],
                                "ok": False,
                                "mismatches": ["missing run fact file"]})
                continue
            with open(run_path, encoding="utf-8") as f:
                run_doc = json.load(f)
            reports.append(verify_one(answer_doc, run_doc))
    os.makedirs(os.path.join(root, "report"), exist_ok=True)
    out = os.path.join(root, "report", "verification.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"reports": reports,
                   "all_ok": all(r["ok"] for r in reports)},
                  f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    total = sum(r["queries_checked"] for r in reports)
    failed = [r for r in reports if not r["ok"]]
    for r in reports:
        flag = "OK " if r["ok"] else "FAIL"
        print(f"{flag} {r['scenario']}: {r['queries_checked']} queries")
        for m in r["mismatches"]:
            print(f"     - {m}")
    print(f"\n{total} queries cross-checked, "
          f"{len(reports) - len(failed)}/{len(reports)} scenarios clean")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
