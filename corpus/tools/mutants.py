"""Wrong-explanation mutants over the independent reference model.

Each mutant is a plausible but incorrect way to answer historical snapshot
queries. The corpus must discriminate every one of them: a mutant is *killed*
when its answer differs from the correct reference answer on at least one
delivered query. These mutants live entirely on the derivation side and never
touch laminad.
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reference_model import ReferenceDB, replay, visible  # noqa: E402


MUTANTS = [
    "ignore_tombstones",
    "ignore_watermark",
    "candidates_as_rows",
    "head_answers_history",
    "null_matches_predicate",
    "no_index_recheck",
]


def _result_key(answer: dict[str, Any]) -> tuple[Any, ...]:
    """Comparable signature of an answer (status + rows + plan facts)."""
    if not answer.get("queryable", False):
    # unqueryable answers collapse to their status
        return (answer.get("status"),)
    return (
        "ok",
        jsonish(answer.get("rows")),
        answer.get("scanned"),
        answer.get("matched"),
        answer.get("plan", {}).get("access"),
        answer.get("plan", {}).get("candidates"),
        answer.get("truncated"),
    )


def jsonish(v: Any) -> str:
    import json
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


class _Replay:
    """Replay a script while collecting per-query correct answers."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.db = ReferenceDB()
        self.correct: list[dict[str, Any]] = []
        self._steps(script)

    def _steps(self, script: dict[str, Any]) -> None:
        for step in script["operations"]:
            op = step["op"]
            if op == "create_relation":
                self.db.create_relation(step["spec"])
            elif op == "create_index":
                self.db.create_index(step["spec"])
            elif op == "begin":
                self.db.begin(step["tx"], step.get("read_only", False))
            elif op == "put":
                self.db.put(step["tx"], step["relation"], step["row"],
                            step.get("insert_only", False))
            elif op == "delete":
                self.db.delete(step["tx"], step["relation"],
                               step["key_value"])
            elif op == "rollback":
                self.db.rollback(step["tx"])
            elif op == "commit":
                self.db.commit(step["tx"])
            elif op == "vacuum":
                horizon = (self.db.sequence if step["horizon"] == "head"
                           else step["horizon"])
                self.db.vacuum(horizon)
            elif op == "checkpoint":
                pass
            elif op == "restart":
                pass
            elif op == "stats":
                pass
            elif op == "query":
                self.correct.append(
                    {"step": step, "answer": self.db.query(step["request"])})
            else:
                raise ValueError(op)


# --------------------------------------------------------------------------- #
# mutant answer generators
# --------------------------------------------------------------------------- #
def m_ignore_tombstones(r: _Replay, step: dict[str, Any]) -> dict[str, Any]:
    """Pretend deletes never happened: use the full archive without tombstones."""
    answer = copy.deepcopy(r.db.query(step["request"]))
    if not answer.get("queryable"):
        return answer
    q = step["request"]
    at = q.get("at", r.db.sequence)
    rows = {}
    for key, chain in r.db.archive[q["relation"]].items():
        # take last row version regardless of deleted flag
        candidate = None
        for v in chain:
            if v.sequence <= at:
                candidate = v
        if candidate is None:
            continue
        if not candidate.deleted:
            rows[key] = dict(candidate.row)
        else:
            # mutant mistake: resurrect the row before the tombstone instead
            # of honouring the delete
            prior = [v for v in chain if v.sequence <= at and not v.deleted]
            if prior:
                rows[key] = dict(prior[-1].row)
    return _reexecute(r, q, rows, answer)


def m_ignore_watermark(r: _Replay, step: dict[str, Any]) -> dict[str, Any]:
    """Answer below-watermark snapshots from the archive instead of 410."""
    answer = r.db.query(step["request"])
    if answer.get("queryable"):
        return copy.deepcopy(answer)
    q = step["request"]
    at = q.get("at")
    if at is None:
        return copy.deepcopy(answer)
    rows = {}
    for key, chain in r.db.archive[q["relation"]].items():
        v = visible(chain, at)
        if v is not None:
            rows[key] = dict(v.row)
    fake = copy.deepcopy(answer)
    fake.update({"status": "ok", "queryable": True, "rows": [],
                 "scanned": 0, "matched": 0, "truncated": False,
                 "plan": {"access": "scan", "candidates": len(rows),
                          "snapshot": at, "residuals": len(q.get("where", []))}})
    return _reexecute(r, q, rows, fake)


def m_candidates_as_rows(r: _Replay, step: dict[str, Any]) -> dict[str, Any]:
    """Treat index candidates as final rows without visibility recheck."""
    answer = r.db.query(step["request"])
    if not answer.get("queryable"):
        return copy.deepcopy(answer)
    q = step["request"]
    plan, keys = r.db.choose(q, r.db.sequence, q.get("force_scan", False))
    if plan["access"] != "index":
        return copy.deepcopy(answer)
    rows = {}
    for key in keys:
        chain = r.db.archive[q["relation"]].get(key, [])
        # mutant mistake: take current (head) row for every historical key,
        # and never drop keys whose visible version is a tombstone/old value
        if chain:
            last = chain[-1]
            if not last.deleted:
                rows[key] = dict(last.row)
    return _reexecute(r, q, rows, copy.deepcopy(answer), force_plan=plan)


def m_head_answers_history(r: _Replay, step: dict[str, Any]) -> dict[str, Any]:
    """Always answer with head rows even when `at` requests history."""
    answer = r.db.query(step["request"])
    if not answer.get("queryable"):
        return copy.deepcopy(answer)
    q = step["request"]
    if q.get("at") is None:
        return copy.deepcopy(answer)
    head_rows = r.db.scan(q["relation"], r.db.sequence)
    fake = copy.deepcopy(answer)
    return _reexecute(r, q, head_rows, fake)


def m_null_matches_predicate(r: _Replay, step: dict[str, Any]) -> dict[str, Any]:
    """Null values incorrectly satisfy ordinary comparisons."""
    answer = r.db.query(step["request"])
    if not answer.get("queryable"):
        return copy.deepcopy(answer)
    q = step["request"]
    if not any(p["op"] not in ("is_null", "not_null")
               for p in q.get("where", [])):
        return copy.deepcopy(answer)
    at = q.get("at", r.db.sequence)
    rows = r.db.scan(q["relation"], at)
    from reference_model import matches as correct_matches

    def broken_matches(row):
        for p in q.get("where", []):
            v = row.get(p["column"])
            if p["op"] == "is_null":
                if v is not None:
                    return False
                continue
            if p["op"] == "not_null":
                if v is None:
                    return False
                continue
            if v is None:
                continue  # mutant mistake: null slips through
            if not correct_matches(row, [p]):
                return False
        return True

    selected = [row for row in rows.values() if broken_matches(row)]
    fake = copy.deepcopy(answer)
    fake["matched"] = len(selected)
    fake["scanned"] = len(rows)
    ordered = r.db._order(selected, q.get("order", []),
                          r.db.relations[q["relation"]]["primary"])
    limit = q.get("limit") or 1000
    fake["truncated"] = len(ordered) > limit
    cols = q.get("columns") or []
    fake["rows"] = [({c: row.get(c) for c in cols} if cols else dict(row))
                    for row in ordered[:limit]]
    return fake


def m_no_index_recheck(r: _Replay, step: dict[str, Any]) -> dict[str, Any]:
    """Skip residual predicates on index paths (matched == candidates)."""
    answer = r.db.query(step["request"])
    if not answer.get("queryable"):
        return copy.deepcopy(answer)
    if answer["plan"]["access"] != "index":
        return copy.deepcopy(answer)
    fake = copy.deepcopy(answer)
    fake["matched"] = answer["plan"]["candidates"]
    return fake


def _reexecute(r: _Replay, q: dict[str, Any], rows: dict[str, Any],
               fake: dict[str, Any], force_plan=None) -> dict[str, Any]:
    from reference_model import matches
    plan = force_plan or fake["plan"]
    keys = sorted(rows) if plan["access"] == "scan" else list(rows)
    selected = [rows[k] for k in keys if matches(rows[k], q.get("where", []))]
    fake["scanned"] = len(keys)
    fake["matched"] = len(selected)
    if q.get("aggregate"):
        fake["rows"] = [r.db._reduce(selected, q["aggregate"])]
        fake["truncated"] = False
        return fake
    ordered = r.db._order(selected, q.get("order", []),
                          r.db.relations[q["relation"]]["primary"])
    limit = q.get("limit") or 1000
    fake["truncated"] = len(ordered) > limit
    cols = q.get("columns") or []
    fake["rows"] = [({c: row.get(c) for c in cols} if cols else dict(row))
                    for row in ordered[:limit]]
    return fake


MUTANT_FUNCS = {
    "ignore_tombstones": m_ignore_tombstones,
    "ignore_watermark": m_ignore_watermark,
    "candidates_as_rows": m_candidates_as_rows,
    "head_answers_history": m_head_answers_history,
    "null_matches_predicate": m_null_matches_predicate,
    "no_index_recheck": m_no_index_recheck,
}


def evaluate(script: dict[str, Any]) -> dict[str, Any]:
    """Return per-mutant kill evidence over one script."""
    r = _Replay(script)
    report: dict[str, Any] = {}
    for name in MUTANTS:
        func = MUTANT_FUNCS[name]
        kills = []
        for idx, item in enumerate(r.correct):
            mutated = func(r, item["step"])
            if _result_key(mutated) != _result_key(item["answer"]):
                kills.append({
                    "query_index": idx,
                    "note": item["step"].get("note", ""),
                    "dims": item["step"].get("dims", []),
                })
        report[name] = {"killed": bool(kills), "evidence": kills[:5],
                        "kill_count": len(kills)}
    return report
