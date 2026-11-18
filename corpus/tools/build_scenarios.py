"""Deterministic generation of LaminaDB workload scenarios.

Every random choice is driven by a fixed-seed random.Random instance, so the
generated scripts are byte-stable across machines and reruns. The output is
*generation material plus replayable operation scripts*: no laminad binary and
no network is involved here.

Scenario families (see ../DESIGN.md):
  f1_drift_pair   two instances, identical head distribution, different trails
  f2_low_select   low-selectivity predicates, wide ranges, null mass
  f3_high_dup     high duplicate values, unique/non-unique indexes, conflicts
  f4_watermark    active readers pinning vacuum, tombstones, 410, restart
  f0_smoke        ordering/limit/aggregate/SQL baseline coverage
"""
from __future__ import annotations

import json
import os
import random
from typing import Any

SCHEMA = {
    "name": "spectra",
    "primary": "sample",
    "columns": [
        {"name": "sample", "type": "int"},
        {"name": "band", "type": "text"},
        {"name": "amplitude", "type": "int", "nullable": True},
        {"name": "valid", "type": "bool"},
        {"name": "code", "type": "text", "nullable": True},
    ],
}

BANDS = ["alpha", "beta", "gamma", "delta"]
CODES = ["c01", "c02", "c03", "c04", None]


# --------------------------------------------------------------------------- #
# small deterministic row helpers
# --------------------------------------------------------------------------- #
def row(sample: int, band: str, amplitude: Any, valid: bool,
        code: Any = None) -> dict[str, Any]:
    return {"sample": sample, "band": band, "amplitude": amplitude,
            "valid": valid, "code": code}


def initial_rows(rng: random.Random, n: int, band_card: int = 4,
                 valid_p: float = 0.8, null_p: float = 0.12,
                 code_card: int = 5
                 ) -> list[dict[str, Any]]:
    rows = []
    for sample in range(1, n + 1):
        amplitude = None if rng.random() < null_p else rng.randint(0, 100)
        rows.append(row(
            sample,
            BANDS[rng.randrange(band_card)],
            amplitude,
            rng.random() < valid_p,
            CODES[rng.randrange(code_card)],
        ))
    return rows


def bulk_insert(ops: list[dict[str, Any]], tx: str,
                rows: list[dict[str, Any]], batch: int = 40) -> None:
    """Emit deterministic batched insert transactions."""
    for i in range(0, len(rows), batch):
        ops.append({"op": "begin", "tx": f"{tx}_{i // batch}"})
        for r in rows[i:i + batch]:
            ops.append({"op": "put", "tx": f"{tx}_{i // batch}",
                        "relation": SCHEMA["name"], "row": r,
                        "insert_only": True})
        ops.append({"op": "commit", "tx": f"{tx}_{i // batch}"})


def update_txn(ops: list[dict[str, Any]], tx: str,
               mutations: list[dict[str, Any]]) -> None:
    ops.append({"op": "begin", "tx": tx})
    for m in mutations:
        ops.append(m)
    ops.append({"op": "commit", "tx": tx})


# --------------------------------------------------------------------------- #
# query templates
# --------------------------------------------------------------------------- #
def q(where=None, columns=None, order=None, limit=0, at=None,
      force_scan=False, transaction=None, aggregate=None,
      note="", dims=None) -> dict[str, Any]:
    request: dict[str, Any] = {"relation": SCHEMA["name"]}
    if where:
        request["where"] = where
    if columns:
        request["columns"] = columns
    if order:
        request["order"] = order
    if limit:
        request["limit"] = limit
    if at is not None:
        request["at"] = at
    if force_scan:
        request["force_scan"] = True
    if transaction:
        request["transaction"] = transaction
    if aggregate:
        request["aggregate"] = aggregate
    return {"op": "query", "request": request, "note": note,
            "dims": dims or []}


def pred(column: str, op: str, value: Any = None) -> dict[str, Any]:
    p = {"column": column, "op": op}
    if value is not None:
        p["value"] = value
    return p


# --------------------------------------------------------------------------- #
# F1: identical head distribution, different update trails
# --------------------------------------------------------------------------- #
def f1_pair(out_dir: str) -> None:
    n = 60
    # same seed => identical initial rows for both instances
    seed_rows = initial_rows(random.Random(1101), n)
    # a shared, deterministic final state: both trajectories must converge here
    final_rng = random.Random(9090)
    final_rows = initial_rows(final_rng, n)
    final_by_key = {r["sample"]: r for r in final_rows}

    def build(kind: str, rng: random.Random) -> dict[str, Any]:
        ops: list[dict[str, Any]] = [
            {"op": "create_relation", "spec": SCHEMA, "label": "ddl"},
        ]
        bulk_insert(ops, "seed", seed_rows, batch=30)
        ops.append({"op": "stats", "label": "after_seed"})

        # queries at the shared early landmark (both instances identical here)
        early = {"op": "query", "dims": ["trajectory", "history"],
                 "note": f"f1-{kind} early landmark",
                 "request": {"relation": SCHEMA["name"],
                             "where": [pred("amplitude", ">=", 40)]}}
        ops.append(early)
        ops.append({"op": "checkpoint", "label": "landmark_early"})

        if kind == "a":
            # hot-key churn: few keys updated many times, values oscillate,
            # then every key is re-written once to converge to final_rows
            hot = [3, 7, 12, 19, 25, 33, 41, 52]
            for wave in range(6):
                muts = []
                for sample in hot:
                    src = seed_rows[sample - 1]
                    amp = (src["amplitude"] or 0) + (wave + 1) * 7
                    muts.append({"op": "put", "tx": f"churn_{wave}",
                                 "relation": SCHEMA["name"],
                                 "row": row(sample, src["band"], amp,
                                            src["valid"], src["code"])})
                update_txn(ops, f"churn_{wave}", muts)
        else:
            # broad churn: each wave touches many different keys once,
            # different intermediate values and null mass than instance a
            for wave in range(6):
                picks = rng.sample(range(1, n + 1), 12)
                muts = []
                for sample in picks:
                    src = seed_rows[sample - 1]
                    amp = None if rng.random() < 0.15 else rng.randint(0, 100)
                    muts.append({"op": "put", "tx": f"broad_{wave}",
                                 "relation": SCHEMA["name"],
                                 "row": row(sample, src["band"], amp,
                                            src["valid"], src["code"])})
                update_txn(ops, f"broad_{wave}", muts)

        # convergence transaction: identical final rows on both instances
        ops.append({"op": "begin", "tx": "converge"})
        for r in final_rows:
            ops.append({"op": "put", "tx": "converge",
                        "relation": SCHEMA["name"], "row": r})
        ops.append({"op": "commit", "tx": "converge",
                    "label": "head_convergence"})

        ops.append({"op": "create_index",
                    "spec": {"name": "by_amplitude", "relation": SCHEMA["name"],
                             "column": "amplitude", "unique": False},
                    "label": "index_after_churn"})
        # same head query on both; historical at mid and early differ
        ops.append(q(where=[pred("amplitude", ">=", 40)],
                     note=f"f1-{kind} head wide predicate",
                     dims=["trajectory", "low_selectivity"]))
        ops.append(q(where=[pred("amplitude", ">=", 40)], at=5,
                     note=f"f1-{kind} mid-history wide predicate",
                     dims=["trajectory", "history"]))
        ops.append(q(where=[pred("amplitude", "=", 42)],
                     note=f"f1-{kind} head equality",
                     dims=["trajectory", "high_duplicate"]))
        ops.append(q(where=[pred("valid", "=", True)],
                     note=f"f1-{kind} boolean high duplicate",
                     dims=["high_duplicate", "low_selectivity"]))
        ops.append(q(where=[pred("amplitude", ">=", 40)],
                     force_scan=True,
                     note=f"f1-{kind} scan counterpart",
                     dims=["plan_fact"]))
        ops.append(q(order=[{"column": "amplitude", "desc": True}],
                     columns=["sample", "amplitude"], limit=5,
                     note=f"f1-{kind} order+limit", dims=["ordering"]))
        ops.append({"op": "stats", "label": "head"})
        return {
            "scenario": f"f1_drift_pair/{kind}",
            "family": "f1_drift_pair",
            "kind": kind,
            "seed": 1101,
            "generation": {
                "keys": n,
                "pattern": "hot_key_churn" if kind == "a" else "broad_churn",
                "waves": 6,
                "head_convergence": "shared final_rows transaction",
            },
            "paired_with": f"f1_drift_pair/{'b' if kind == 'a' else 'a'}",
            "operations": ops,
        }

    write_script(out_dir, "f1_drift_pair", "a", build("a", random.Random(0)))
    write_script(out_dir, "f1_drift_pair", "b",
                 build("b", random.Random(2207)))


# --------------------------------------------------------------------------- #
# F2: low selectivity
# --------------------------------------------------------------------------- #
def f2_low_selectivity(out_dir: str) -> None:
    n = 120
    rng = random.Random(3301)
    rows = initial_rows(rng, n, valid_p=0.9, null_p=0.12)
    ops: list[dict[str, Any]] = [
        {"op": "create_relation", "spec": SCHEMA},
    ]
    bulk_insert(ops, "seed", rows, batch=30)
    ops.append({"op": "create_index",
                "spec": {"name": "by_valid", "relation": SCHEMA["name"],
                         "column": "valid", "unique": False}})
    ops.append({"op": "create_index",
                "spec": {"name": "by_amplitude", "relation": SCHEMA["name"],
                         "column": "amplitude", "unique": False}})
    # one update wave so chains have length 2 on some keys
    muts = []
    for sample in rng.sample(range(1, n + 1), 24):
        src = rows[sample - 1]
        muts.append({"op": "put", "tx": "wave", "relation": SCHEMA["name"],
                     "row": row(sample, src["band"], rng.randint(0, 100),
                                src["valid"], src["code"])})
    update_txn(ops, "wave", muts)

    ops.append(q(where=[pred("valid", "=", True)],
                 note="boolean predicate ~90% selectivity",
                 dims=["low_selectivity", "high_duplicate"]))
    ops.append(q(where=[pred("valid", "=", True)], force_scan=True,
                 note="same predicate via scan", dims=["plan_fact"]))
    ops.append(q(where=[pred("amplitude", ">=", 10)],
                 note="wide range ~90% of non-null rows",
                 dims=["low_selectivity"]))
    ops.append(q(where=[pred("amplitude", ">=", 10)], force_scan=True,
                 note="wide range scan counterpart", dims=["plan_fact"]))
    ops.append(q(where=[pred("amplitude", "is_null")],
                 note="null-only predicate cannot use index",
                 dims=["null_semantics"]))
    ops.append(q(where=[pred("amplitude", "is_null")],
                 aggregate={"function": "count", "column": "*"},
                 note="count of null rows; a null-matching comparator "
                      "inflates ordinary comparisons but not this one",
                 dims=["null_semantics", "aggregate"]))
    ops.append(q(where=[pred("amplitude", ">=", 0)],
                 note="range over non-null values, nulls must not match",
                 dims=["null_semantics", "plan_fact", "low_selectivity"]))
    ops.append(q(where=[pred("amplitude", "not_null"),
                        pred("valid", "=", True)],
                 note="AND of two low-selectivity predicates",
                 dims=["low_selectivity", "compound"]))
    ops.append(q(aggregate={"function": "count", "column": "*"},
                 note="count star over full relation", dims=["aggregate"]))
    ops.append(q(aggregate={"function": "count", "column": "amplitude"},
                 note="count ignores nulls", dims=["aggregate", "null_semantics"]))
    ops.append(q(aggregate={"function": "sum", "column": "amplitude"},
                 where=[pred("valid", "=", True)],
                 note="sum ignores nulls under boolean filter",
                 dims=["aggregate", "low_selectivity"]))
    script = {
        "scenario": "f2_low_selectivity/main",
        "family": "f2_low_selectivity",
        "kind": "main",
        "seed": 3301,
        "generation": {"keys": n, "update_wave_keys": 24},
        "operations": ops,
    }
    write_script(out_dir, "f2_low_selectivity", "main", script)


# --------------------------------------------------------------------------- #
# F3: high duplicate values and unique indexes
# --------------------------------------------------------------------------- #
def f3_high_dup(out_dir: str) -> None:
    n = 80
    rng = random.Random(4409)
    rows = initial_rows(rng, n, band_card=4, code_card=4)
    # make code values globally unique so a unique index can build
    for r in rows:
        r["code"] = f"u{r['sample']:03d}"
    ops: list[dict[str, Any]] = [
        {"op": "create_relation", "spec": SCHEMA},
    ]
    bulk_insert(ops, "seed", rows, batch=40)
    ops.append({"op": "create_index",
                "spec": {"name": "by_band", "relation": SCHEMA["name"],
                         "column": "band", "unique": False}})
    ops.append({"op": "create_index",
                "spec": {"name": "by_code_unique", "relation": SCHEMA["name"],
                         "column": "code", "unique": True}})

    # duplicate band equality: ~20 keys per band
    for band in BANDS:
        ops.append(q(where=[pred("band", "=", band)],
                     note=f"high duplicate text equality {band}",
                     dims=["high_duplicate"]))
    ops.append(q(where=[pred("band", "=", "alpha"),
                        pred("valid", "=", False)],
                 note="duplicate text plus boolean residual",
                 dims=["high_duplicate", "residual"]))

    # unique swap inside one transaction must succeed
    swap_rows = [
        row(1, rows[0]["band"], rows[0]["amplitude"], rows[0]["valid"],
            "u002"),
        row(2, rows[1]["band"], rows[1]["amplitude"], rows[1]["valid"],
            "u001"),
    ]
    update_txn(ops, "swap", [
        {"op": "put", "tx": "swap", "relation": SCHEMA["name"],
         "row": swap_rows[0]},
        {"op": "put", "tx": "swap", "relation": SCHEMA["name"],
         "row": swap_rows[1]},
    ])
    ops.append(q(where=[pred("code", "=", "u001")],
                 note="unique lookup after in-txn swap",
                 dims=["unique", "history"]))

    # two concurrent writers racing for the same unique value: one loses
    ops.append({"op": "begin", "tx": "racer_a"})
    ops.append({"op": "put", "tx": "racer_a", "relation": SCHEMA["name"],
                "row": row(3, rows[2]["band"], rows[2]["amplitude"],
                           rows[2]["valid"], "race")})
    ops.append({"op": "begin", "tx": "racer_b"})
    ops.append({"op": "put", "tx": "racer_b", "relation": SCHEMA["name"],
                "row": row(4, rows[3]["band"], rows[3]["amplitude"],
                           rows[3]["valid"], "race")})
    ops.append({"op": "commit", "tx": "racer_a"})
    ops.append({"op": "commit", "tx": "racer_b", "expect_conflict": True,
                "label": "unique_race_loser"})
    ops.append(q(where=[pred("code", "=", "race")],
                 note="exactly one racer publishes the unique value",
                 dims=["unique", "conflict"]))
    ops.append({"op": "stats", "label": "post_race"})
    script = {
        "scenario": "f3_high_dup/main",
        "family": "f3_high_dup",
        "kind": "main",
        "seed": 4409,
        "generation": {"keys": n, "band_cardinality": 4},
        "operations": ops,
    }
    write_script(out_dir, "f3_high_dup", "main", script)


# --------------------------------------------------------------------------- #
# F4: watermark lifecycle, tombstones, 410, restart
# --------------------------------------------------------------------------- #
def f4_watermark(out_dir: str) -> None:
    n = 50
    rng = random.Random(5503)
    rows = initial_rows(rng, n, null_p=0.1)
    ops: list[dict[str, Any]] = [
        {"op": "create_relation", "spec": SCHEMA},
    ]
    bulk_insert(ops, "seed", rows, batch=25)
    ops.append({"op": "create_index",
                "spec": {"name": "by_amplitude", "relation": SCHEMA["name"],
                         "column": "amplitude", "unique": False}})
    seed_head = {"op": "stats", "label": "seed_head"}
    ops.append(seed_head)

    # long-lived reader pins every subsequent vacuum
    ops.append({"op": "begin", "tx": "reader", "read_only": True,
                "label": "pinning_reader"})
    # writer: updates + deletes producing tombstones and long chains
    muts = []
    for sample in range(1, 11):
        src = rows[sample - 1]
        muts.append({"op": "put", "tx": "wave1", "relation": SCHEMA["name"],
                     "row": row(sample, src["band"], 900 + sample,
                                src["valid"], src["code"])})
    update_txn(ops, "wave1", muts)
    update_txn(ops, "wave2", [
        {"op": "put", "tx": "wave2", "relation": SCHEMA["name"],
         "row": row(1, rows[0]["band"], 1000, rows[0]["valid"],
                    rows[0]["code"])},
        {"op": "put", "tx": "wave2", "relation": SCHEMA["name"],
         "row": row(2, rows[1]["band"], 1001, rows[1]["valid"],
                    rows[1]["code"])},
    ])
    update_txn(ops, "deaths", [
        {"op": "delete", "tx": "deaths", "relation": SCHEMA["name"],
         "key_value": 40},
        {"op": "delete", "tx": "deaths", "relation": SCHEMA["name"],
         "key_value": 41},
    ])
    ops.append({"op": "stats", "label": "pre_vacuum"})

    # first vacuum must be pinned to the reader's snapshot
    ops.append({"op": "vacuum", "horizon": "head", "label": "pinned_vacuum"})
    ops.append(q(where=[pred("sample", "=", 1)], transaction="reader",
                 note="pinned reader still sees pre-wave value",
                 dims=["watermark", "snapshot_isolation"]))
    ops.append(q(where=[pred("amplitude", ">=", 900)],
                 note="head sees post-wave values",
                 dims=["watermark", "low_selectivity"]))
    ops.append({"op": "checkpoint", "label": "checkpoint_pinned"})

    # close reader, vacuum can advance past tombstones and old versions
    ops.append({"op": "rollback", "tx": "reader", "label": "release_pin"})
    ops.append({"op": "vacuum", "horizon": "head",
                "label": "advancing_vacuum"})
    ops.append({"op": "stats", "label": "post_vacuum"})
    # queries at pruned snapshots must be refused; head must still answer.
    # at=1 is the DDL/early snapshot that the advancing watermark eliminates
    ops.append(q(where=[pred("amplitude", ">=", 900)], at=1,
                 note="snapshot below watermark must be unavailable",
                 dims=["watermark", "unqueryable"]))
    ops.append(q(where=[pred("amplitude", ">=", 900)],
                 note="head query after retention and index rebuild",
                 dims=["watermark", "plan_fact"]))
    ops.append(q(where=[pred("sample", "=", 40)],
                 note="tombstoned key absent at head",
                 dims=["tombstone"]))
    # wide predicate over the deleted key range: an implementation that
    # resurrects tombstones must return keys 40/41 here, the model does not
    ops.append(q(where=[pred("sample", "<=", 41)],
                 note="wide range exposes tombstone resurrection",
                 dims=["tombstone", "low_selectivity", "plan_fact"]))
    ops.append(q(where=[pred("sample", "<=", 41)],
                 aggregate={"function": "count", "column": "*"},
                 note="count over range containing tombstoned keys",
                 dims=["tombstone", "aggregate"]))
    ops.append({"op": "checkpoint", "label": "checkpoint_final"})
    # restart marker: run_corpus stops and reopens the instance here
    ops.append({"op": "restart", "label": "restart_after_vacuum"})
    ops.append(q(where=[pred("amplitude", ">=", 900)], at=1,
                 note="410 survives restart (watermark durable)",
                 dims=["watermark", "restart", "unqueryable"]))
    ops.append(q(where=[pred("amplitude", ">=", 900)],
                 note="head survives restart",
                 dims=["watermark", "restart"]))
    ops.append({"op": "stats", "label": "after_restart"})
    script = {
        "scenario": "f4_watermark/main",
        "family": "f4_watermark",
        "kind": "main",
        "seed": 5503,
        "generation": {"keys": n, "updated_keys": 10, "deleted_keys": 2},
        "operations": ops,
    }
    write_script(out_dir, "f4_watermark", "main", script)


# --------------------------------------------------------------------------- #
# F0: smoke coverage for ordering, limit, aggregates, SQL parity
# --------------------------------------------------------------------------- #
def f0_smoke(out_dir: str) -> None:
    n = 24
    rng = random.Random(77)
    rows = initial_rows(rng, n, null_p=0.2)
    ops: list[dict[str, Any]] = [
        {"op": "create_relation", "spec": SCHEMA},
    ]
    bulk_insert(ops, "seed", rows, batch=24)
    ops.append({"op": "create_index",
                "spec": {"name": "by_amplitude", "relation": SCHEMA["name"],
                         "column": "amplitude", "unique": False}})
    ops.append(q(order=[{"column": "sample"}], limit=10,
                 note="primary-key order with limit truncation",
                 dims=["ordering", "limit"]))
    ops.append(q(order=[{"column": "amplitude", "desc": True},
                        {"column": "sample", "desc": True}],
                 note="nulls last on descending order, tie-break by PK",
                 dims=["ordering", "null_semantics"]))
    ops.append(q(columns=["sample", "band"],
                 where=[pred("band", "=", "gamma")],
                 note="projection with text equality",
                 dims=["projection", "high_duplicate"]))
    ops.append(q(where=[pred("amplitude", "<", 25)],
                 note="range below threshold", dims=["range"]))
    ops.append(q(where=[pred("amplitude", "!=", 50)],
                 note="inequality predicate never uses index",
                 dims=["plan_fact"]))
    ops.append(q(aggregate={"function": "count", "column": "*"},
                 note="count star", dims=["aggregate"]))
    ops.append(q(aggregate={"function": "sum", "column": "amplitude"},
                 note="sum with nulls returns null only when empty",
                 dims=["aggregate", "null_semantics"]))
    script = {
        "scenario": "f0_smoke/main",
        "family": "f0_smoke",
        "kind": "main",
        "seed": 77,
        "generation": {"keys": n},
        "operations": ops,
        "sql_checks": [
            "SELECT sample, amplitude FROM spectra "
            "WHERE amplitude >= 20 ORDER BY amplitude DESC LIMIT 5;",
            "SELECT count(*) FROM spectra WHERE valid = true;",
            "SELECT sum(amplitude) FROM spectra;",
        ],
    }
    write_script(out_dir, "f0_smoke", "main", script)


def write_script(out_dir: str, family: str, kind: str,
                 script: dict[str, Any]) -> None:
    family_dir = os.path.join(out_dir, family)
    os.makedirs(family_dir, exist_ok=True)
    path = os.path.join(family_dir, f"{kind}.script.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def build_all(corpus_root: str) -> list[str]:
    out_dir = os.path.join(corpus_root, "scenarios")
    os.makedirs(out_dir, exist_ok=True)
    f0_smoke(out_dir)
    f1_pair(out_dir)
    f2_low_selectivity(out_dir)
    f3_high_dup(out_dir)
    f4_watermark(out_dir)
    paths = []
    for dirpath, _dirs, files in os.walk(out_dir):
        for name in sorted(files):
            if name.endswith(".script.json"):
                paths.append(os.path.join(dirpath, name))
    return sorted(paths)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    generated = build_all(root)
    for p in generated:
        print(os.path.relpath(p, root))
