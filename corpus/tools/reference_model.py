"""Independent reference model for LaminaDB query workloads.

This module re-derives snapshot visibility, vacuum retention, index candidate
supersets and plan facts *only* from an operation script. It never imports Go
code, never opens a laminad data directory and never reads run observations.

Semantics mirrored from the product's documented behaviour:
  * per-key version chains ordered by commit sequence, tombstone aware
  * snapshot `at` sees the last version with sequence <= at
  * vacuum horizon = min(requested, lowest active transaction snapshot),
    monotone; chains keep the last version at/before the horizon plus later
    versions; chains ending in a tombstone at/before the horizon are removed
  * secondary indexes hold historical (non-null value, key) supersets and are
    rebuilt from retained chains after a vacuum
  * planner picks the fewest-candidate usable index; candidates are always
    re-checked against real visible versions (residual predicates)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# datum layer
# --------------------------------------------------------------------------- #
def value_key(v: Any) -> str:
    """Mirror of datum.Value.Key(): kind + ':' + canonical JSON."""
    if v is None:
        return "null:null"
    if isinstance(v, bool):
        return "bool:" + ("true" if v else "false")
    if isinstance(v, int):
        return f"int:{v}"
    if isinstance(v, str):
        return "text:" + json.dumps(v, ensure_ascii=False)
    raise TypeError(f"unsupported value {v!r}")


def value_order(a: Any, b: Any) -> int:
    """Mirror of datum.Order: nulls sort first, then typed comparison."""
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    if type(a) is not type(b):
        raise TypeError(f"incompatible types {type(a)} {type(b)}")
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def matches(row: dict[str, Any], predicates: list[dict[str, Any]]) -> bool:
    """Mirror of query.Matches (AND-connected predicates, null rules)."""
    for p in predicates:
        v = row.get(p["column"])
        op = p["op"]
        if op == "is_null":
            if v is not None:
                return False
            continue
        if op == "not_null":
            if v is None:
                return False
            continue
        cmp_value = p.get("value")
        if v is None or cmp_value is None:
            return False
        n = value_order(v, cmp_value)
        if op == "=":
            ok = n == 0
        elif op == "!=":
            ok = n != 0
        elif op == "<":
            ok = n < 0
        elif op == ">":
            ok = n > 0
        elif op == "<=":
            ok = n <= 0
        elif op == ">=":
            ok = n >= 0
        else:
            raise ValueError(f"unsupported op {op}")
        if not ok:
            return False
    return True


# --------------------------------------------------------------------------- #
# versions + vacuum
# --------------------------------------------------------------------------- #
@dataclass
class Version:
    sequence: int
    row: Optional[dict[str, Any]]
    deleted: bool = False


def visible(chain: list[Version], at: int) -> Optional[Version]:
    """Last version with sequence <= at; None when absent or tombstoned."""
    chosen: Optional[Version] = None
    for v in chain:
        if v.sequence <= at:
            chosen = v
        else:
            break
    if chosen is None or chosen.deleted:
        return None
    return chosen


def vacuum_chain(chain: list[Version], horizon: int) -> Optional[list[Version]]:
    """Mirror versions.Vacuum for one chain; None means the chain is removed."""
    keep = 0
    while keep + 1 < len(chain) and chain[keep + 1].sequence <= horizon:
        keep += 1
    last = chain[-1]
    if last.sequence <= horizon and last.deleted:
        return None
    return chain[keep:]


# --------------------------------------------------------------------------- #
# index layer (historical supersets)
# --------------------------------------------------------------------------- #
class Tree:
    def __init__(self) -> None:
        self.entries: list[tuple[Any, str]] = []
        self.seen: set[str] = set()

    def add(self, value: Any, key: str) -> None:
        if value is None:
            return
        token = value_key(value) + "\x00" + key
        if token in self.seen:
            return
        self.seen.add(token)
        lo, hi = 0, len(self.entries)
        while lo < hi:
            mid = (lo + hi) // 2
            ev, ek = self.entries[mid]
            n = value_order(ev, value)
            if n > 0 or (n == 0 and ek >= key):
                hi = mid
            else:
                lo = mid + 1
        self.entries.insert(lo, (value, key))

    def candidates(self, op: str, value: Any) -> list[str]:
        if value is None:
            return []
        lo = 0
        hi = len(self.entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if value_order(self.entries[mid][0], value) >= 0:
                hi = mid
            else:
                lo = mid + 1
        left = lo
        lo, hi = 0, len(self.entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if value_order(self.entries[mid][0], value) > 0:
                hi = mid
            else:
                lo = mid + 1
        right = lo
        begin, end = 0, len(self.entries)
        if op == "=":
            begin, end = left, right
        elif op == ">":
            begin = right
        elif op == ">=":
            begin = left
        elif op == "<":
            end = left
        elif op == "<=":
            end = right
        else:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for _v, key in self.entries[begin:end]:
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def size(self) -> int:
        return len(self.entries)


# --------------------------------------------------------------------------- #
# reference database
# --------------------------------------------------------------------------- #
@dataclass
class Txn:
    snapshot: int
    read_only: bool
    writes: dict[str, dict[str, Version]] = field(default_factory=dict)


class ReferenceDB:
    def __init__(self) -> None:
        self.sequence = 0
        self.horizon = 0
        self.relations: dict[str, dict[str, Any]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}
        # retained chains (pruned by vacuum)
        self.tables: dict[str, dict[str, list[Version]]] = {}
        # full history archive, never pruned (used only by mutant experiments)
        self.archive: dict[str, dict[str, list[Version]]] = {}
        self.trees: dict[str, Tree] = {}
        self.txns: dict[str, Txn] = {}
        self.events: list[dict[str, Any]] = []  # publish ledger

    # --------------------------------------------------------------- schema
    def normalize(self, relation: str, row: dict[str, Any]) -> dict[str, Any]:
        schema = self.relations[relation]
        out: dict[str, Any] = {}
        for col in schema["columns"]:
            name = col["name"]
            v = row.get(name)
            if v is None:
                if not col.get("nullable", False):
                    raise ValueError(f"column {name} cannot be null")
                v = None
            elif not self._type_matches(v, col["type"]):
                raise ValueError(f"column {name} requires {col['type']}")
            out[name] = v
        return out

    @staticmethod
    def _type_matches(v: Any, t: str) -> bool:
        if t == "int":
            return isinstance(v, int) and not isinstance(v, bool)
        if t == "text":
            return isinstance(v, str)
        if t == "bool":
            return isinstance(v, bool)
        return False

    def _publish(self, record: dict[str, Any]) -> None:
        self.sequence += 1
        record = dict(record)
        record["sequence"] = self.sequence
        self.events.append(record)
        self._apply(record)

    def _apply(self, record: dict[str, Any]) -> None:
        kind = record["kind"]
        seq = record["sequence"]
        if kind == "relation":
            r = record["relation"]
            self.relations[r["name"]] = r
            self.tables[r["name"]] = {}
            self.archive[r["name"]] = {}
        elif kind == "index":
            idx = record["index"]
            self.indexes[idx["name"]] = idx
            self._rebuild_indexes()
        elif kind == "commit":
            for m in record["writes"]:
                self._apply_mutation(m, seq)
            for m in record["writes"]:
                self._index_apply(m)
        elif kind == "vacuum":
            self._apply_vacuum(record["horizon"])
        else:
            raise ValueError(f"unknown record kind {kind}")

    def _apply_mutation(self, m: dict[str, Any], seq: int) -> None:
        table, key = m["relation"], m["key"]
        version = Version(
            sequence=seq,
            row=None if m.get("deleted") else m["row"],
            deleted=bool(m.get("deleted")),
        )
        self.tables[table].setdefault(key, []).append(version)
        self.archive[table].setdefault(key, []).append(
            Version(version.sequence,
                    None if version.deleted else dict(version.row),
                    version.deleted)
        )

    def _index_apply(self, m: dict[str, Any]) -> None:
        if m.get("deleted"):
            return
        for idx in self.indexes.values():
            if idx["relation"] == m["relation"]:
                self.trees[idx["name"]].add(m["row"][idx["column"]], m["key"])

    def _rebuild_indexes(self) -> None:
        self.trees = {}
        for idx in self.indexes.values():
            tree = Tree()
            for key, chain in self.tables[idx["relation"]].items():
                for v in chain:
                    if not v.deleted:
                        tree.add(v.row[idx["column"]], key)
            self.trees[idx["name"]] = tree

    def _apply_vacuum(self, horizon: int) -> None:
        for rows in self.tables.values():
            for key in list(rows):
                remaining = vacuum_chain(rows[key], horizon)
                if remaining is None:
                    del rows[key]
                else:
                    rows[key] = remaining
        self.horizon = horizon
        self._rebuild_indexes()

    # ------------------------------------------------------------- operations
    def create_relation(self, spec: dict[str, Any]) -> int:
        relation = {
            "name": spec["name"],
            "primary": spec["primary"],
            "columns": spec["columns"],
            "created": self.sequence + 1,
        }
        self._publish({"kind": "relation", "relation": relation})
        return self.sequence

    def create_index(self, spec: dict[str, Any]) -> int:
        idx = {
            "name": spec["name"],
            "relation": spec["relation"],
            "column": spec["column"],
            "unique": bool(spec.get("unique", False)),
            "created": self.sequence + 1,
        }
        if idx["unique"]:
            rows = self.scan(idx["relation"], self.sequence)
            seen: dict[str, str] = {}
            for key, row in rows.items():
                v = row[idx["column"]]
                if v is None:
                    continue
                token = value_key(v)
                if token in seen and seen[token] != key:
                    raise ValueError("unique constraint violated")
                seen[token] = key
        self._publish({"kind": "index", "index": idx})
        return self.sequence

    def begin(self, tx: str, read_only: bool = False) -> None:
        self.txns[tx] = Txn(snapshot=self.sequence, read_only=read_only)

    def put(self, tx: str, relation: str, row: dict[str, Any],
            insert_only: bool = False) -> None:
        t = self.txns[tx]
        if t.read_only:
            raise ValueError("read-only transaction")
        normalized = self.normalize(relation, row)
        key = value_key(normalized[self.relations[relation]["primary"]])
        if insert_only:
            current = self.scan(relation, t.snapshot)
            current.update(
                (k, v.row) for k, v in t.writes.get(relation, {}).items()
                if not v.deleted
            )
            if key in current:
                raise ValueError("primary already exists")
        t.writes.setdefault(relation, {})[key] = Version(
            self.sequence, normalized, False
        )

    def delete(self, tx: str, relation: str, key_value: Any) -> None:
        t = self.txns[tx]
        if t.read_only:
            raise ValueError("read-only transaction")
        key = value_key(key_value)
        t.writes.setdefault(relation, {})[key] = Version(
            self.sequence, None, True
        )

    def rollback(self, tx: str) -> None:
        del self.txns[tx]

    def commit(self, tx: str) -> dict[str, Any]:
        """Return {'published': bool, 'sequence': int, 'conflict': bool}."""
        t = self.txns.pop(tx)
        mutations: list[dict[str, Any]] = []
        for table, staged in t.writes.items():
            for key, version in staged.items():
                chain = self.tables[table].get(key, [])
                latest = chain[-1].sequence if chain else 0
                if latest > t.snapshot:
                    return {"published": False, "sequence": self.sequence,
                            "conflict": True, "reason": "write conflict"}
                mutations.append({
                    "relation": table,
                    "key": key,
                    "row": None if version.deleted else dict(version.row),
                    "deleted": version.deleted,
                })
        # unique constraint validation against resulting visible state
        for table in t.writes:
            for idx in self.indexes.values():
                if idx["relation"] == table and idx["unique"]:
                    rows = self.scan(table, self.sequence)
                    for key, version in t.writes[table].items():
                        if version.deleted:
                            rows.pop(key, None)
                        else:
                            rows[key] = version.row
                    seen = {}
                    for key, row in rows.items():
                        v = row[idx["column"]]
                        if v is None:
                            continue
                        token = value_key(v)
                        if token in seen and seen[token] != key:
                            return {"published": False,
                                    "sequence": self.sequence,
                                    "conflict": True,
                                    "reason": "unique constraint"}
                        seen[token] = key
        if not mutations:
            return {"published": False, "sequence": t.snapshot,
                    "conflict": False, "reason": "empty write set"}
        mutations.sort(key=lambda m: (m["relation"], m["key"]))
        self._publish({"kind": "commit", "transaction": tx,
                       "writes": mutations})
        return {"published": True, "sequence": self.sequence,
                "conflict": False, "reason": ""}

    def vacuum(self, requested: int) -> dict[str, Any]:
        if requested > self.sequence or requested < self.horizon:
            raise ValueError("vacuum horizon outside retained interval")
        horizon = requested
        for t in self.txns.values():
            if t.snapshot < horizon:
                horizon = t.snapshot
        before = self._version_count()
        self._publish({"kind": "vacuum", "horizon": horizon})
        retained = self._version_count()
        return {
            "horizon": horizon,
            "removed": before - retained,
            "retained": retained,
            "pinned": horizon < requested,
        }

    # ---------------------------------------------------------------- reads
    def _read(self, table: str, key: str, at: int,
              store: Optional[dict[str, dict[str, list[Version]]]] = None
              ) -> Optional[dict[str, Any]]:
        tables = store if store is not None else self.tables
        v = visible(tables[table].get(key, []), at)
        return None if v is None else dict(v.row)

    def scan(self, table: str, at: int,
             store: Optional[dict[str, dict[str, list[Version]]]] = None
             ) -> dict[str, dict[str, Any]]:
        tables = store if store is not None else self.tables
        out: dict[str, dict[str, Any]] = {}
        for key, chain in tables[table].items():
            v = visible(chain, at)
            if v is not None:
                out[key] = dict(v.row)
        return out

    def snapshot_status(self, relation: str, at: int) -> Optional[str]:
        """None means queryable; otherwise the engine error code (410 class)."""
        if at < self.horizon or at > self.sequence:
            return "snapshot"
        if relation not in self.relations or at < self.relations[relation]["created"]:
            return "snapshot"
        return None

    def choose(self, q: dict[str, Any], at: int,
               force_scan: bool = False) -> tuple[dict[str, Any], list[str]]:
        relation = q["relation"]
        total = len(self.tables[relation])
        plan = {"access": "scan", "index": "", "candidates": total,
                "snapshot": at, "residuals": len(q.get("where", []))}
        if force_scan:
            return plan, []
        chosen: list[str] = []
        for name in sorted(self.indexes):
            idx = self.indexes[name]
            if idx["relation"] != relation or idx["created"] > at:
                continue
            for p in q.get("where", []):
                if p["column"] != idx["column"] or p.get("value") is None:
                    continue
                if p["op"] not in ("=", "<", ">", "<=", ">="):
                    continue
                cands = self.trees[name].candidates(p["op"], p["value"])
                if (len(cands) < plan["candidates"]
                        or (plan["access"] == "scan" and len(cands) == 0)):
                    plan = {"access": "index", "index": name,
                            "candidates": len(cands), "snapshot": at,
                            "residuals": len(q.get("where", []))}
                    chosen = cands
        return plan, chosen

    def query(self, q: dict[str, Any]) -> dict[str, Any]:
        relation = q["relation"]
        tx_name = q.get("transaction") or ""
        at = q.get("at")
        tx = self.txns.get(tx_name) if tx_name else None
        if tx_name and at is not None:
            raise ValueError("at and transaction are mutually exclusive")
        if tx is not None:
            at = tx.snapshot
        if at is None:
            at = self.sequence
        status = self.snapshot_status(relation, at)
        if status is not None:
            return {"status": status, "queryable": False}
        force_scan = bool(q.get("force_scan", False))
        if tx is not None and tx.writes.get(relation):
            force_scan = True
        plan, keys = self.choose(q, at, force_scan)
        if plan["access"] == "index":
            rows = {}
            for key in keys:
                row = self._read(relation, key, at)
                if row is not None:
                    rows[key] = row
        else:
            rows = self.scan(relation, at)
        if tx is not None:
            for key, version in tx.writes.get(relation, {}).items():
                if version.deleted:
                    rows.pop(key, None)
                else:
                    rows[key] = dict(version.row)
        result_rows: list[dict[str, Any]] = []
        scanned = 0
        if plan["access"] == "scan":
            keys = sorted(rows)
        selected: list[dict[str, Any]] = []
        for key in keys:
            row = rows.get(key)
            if row is None:
                continue
            scanned += 1
            if matches(row, q.get("where", [])):
                selected.append(row)
        matched = len(selected)
        aggregate = q.get("aggregate")
        if aggregate:
            result_rows = [self._reduce(selected, aggregate)]
        else:
            selected = self._order(selected, q.get("order", []),
                                   self.relations[relation]["primary"])
            limit = q.get("limit") or 1000
            truncated = len(selected) > limit
            if truncated:
                selected = selected[:limit]
            cols = q.get("columns") or []
            for row in selected:
                if not cols:
                    result_rows.append(dict(row))
                else:
                    result_rows.append({c: row.get(c) for c in cols})
            return {"status": "ok", "queryable": True, "rows": result_rows,
                    "plan": plan, "scanned": scanned, "matched": matched,
                    "truncated": truncated}
        return {"status": "ok", "queryable": True, "rows": result_rows,
                "plan": plan, "scanned": scanned, "matched": matched,
                "truncated": False}

    @staticmethod
    def _order(rows: list[dict[str, Any]], order: list[dict[str, Any]],
               primary: str) -> list[dict[str, Any]]:
        import functools

        def cmp(a: dict[str, Any], b: dict[str, Any]) -> int:
            for o in order:
                n = value_order(a.get(o["column"]), b.get(o["column"]))
                if n != 0:
                    return -n if o.get("desc") else n
            return value_order(a.get(primary), b.get(primary))

        return sorted(rows, key=functools.cmp_to_key(cmp))

    @staticmethod
    def _reduce(rows: list[dict[str, Any]], agg: dict[str, Any]) -> dict[str, Any]:
        fn, col = agg["function"], agg.get("column", "*")
        total = 0
        count = 0
        for row in rows:
            v = row.get(col)
            if col not in ("", "*") and v is None:
                continue
            count += 1
            if fn == "sum":
                total += v
        if fn == "count":
            return {"count": count}
        if count == 0:
            return {"sum": None}
        return {"sum": total}

    # ---------------------------------------------------------------- stats
    def _version_count(self) -> int:
        return sum(len(chain) for rows in self.tables.values()
                   for chain in rows.values())

    def stats(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "horizon": self.horizon,
            "keys": sum(len(rows) for rows in self.tables.values()),
            "versions": self._version_count(),
            "index_entries": {name: t.size()
                              for name, t in sorted(self.trees.items())},
            "active": sorted(self.txns),
        }


# --------------------------------------------------------------------------- #
# script replay
# --------------------------------------------------------------------------- #
def replay(script: dict[str, Any], db: Optional[ReferenceDB] = None
           ) -> tuple[ReferenceDB, list[dict[str, Any]]]:
    """Replay an operation script; return the db and per-op expectations."""
    db = db or ReferenceDB()
    expectations: list[dict[str, Any]] = []
    for step in script["operations"]:
        op = step["op"]
        record: dict[str, Any] = {"op": op, "label": step.get("label", "")}
        if op == "create_relation":
            db.create_relation(step["spec"])
            record["sequence"] = db.sequence
        elif op == "create_index":
            db.create_index(step["spec"])
            record["sequence"] = db.sequence
        elif op == "begin":
            db.begin(step["tx"], step.get("read_only", False))
        elif op == "put":
            db.put(step["tx"], step["relation"], step["row"],
                   step.get("insert_only", False))
        elif op == "delete":
            db.delete(step["tx"], step["relation"], step["key_value"])
        elif op == "rollback":
            db.rollback(step["tx"])
        elif op == "commit":
            outcome = db.commit(step["tx"])
            record["outcome"] = outcome
            if step.get("expect_conflict"):
                assert outcome["conflict"], (
                    f"{step.get('label')}: expected conflict, got {outcome}")
            else:
                assert not outcome["conflict"], (
                    f"{step.get('label')}: unexpected conflict {outcome}")
        elif op == "vacuum":
            horizon = db.sequence if step["horizon"] == "head" else step["horizon"]
            record["result"] = db.vacuum(horizon)
        elif op == "checkpoint":
            record["sequence"] = db.sequence
        elif op == "restart":
            # an in-process model has no process boundary; durability is
            # exercised by run_corpus.py, here it is a recorded no-op
            record["sequence"] = db.sequence
        elif op == "query":
            record["answer"] = db.query(step["request"])
        elif op == "stats":
            record["stats"] = db.stats()
        else:
            raise ValueError(f"unknown script op {op}")
        expectations.append(record)
    return db, expectations
