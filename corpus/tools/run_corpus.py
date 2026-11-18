"""Replay scenario scripts against a real laminad HTTP service.

Each scenario gets a brand-new empty temporary data directory and a fresh
laminad process. The runner records actual HTTP status codes, query results and
plan facts, vacuum responses and stats snapshots. Restart markers stop and
reopen the same data directory to exercise durable watermark semantics.

This module only speaks the public HTTP API; it contains no semantics of its
own beyond mapping script operations to requests.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any


class Service:
    def __init__(self, binary: str, data_dir: str) -> None:
        self.binary = binary
        self.data_dir = data_dir
        self.process: subprocess.Popen | None = None
        self.url = ""
        self.log: Any = None
        self.port = 0

    def start(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/v1/lamina"
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [self.binary, "-data", self.data_dir,
             "-listen", f"127.0.0.1:{self.port}"],
            stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                raise RuntimeError(
                    f"laminad exited early: {self.log.read().decode()}")
            try:
                self.call("GET", "/health")
                return
            except (OSError, urllib.error.URLError):
                time.sleep(0.03)
        raise RuntimeError("laminad did not become ready")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
            self.log = None
        self.process = None

    def call(self, method: str, path: str, data: Any = None
             ) -> tuple[int, Any]:
        encoded = None if data is None else json.dumps(
            data, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.url + path, data=encoded, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"raw": body.decode()}


def run_script(binary: str, script: dict[str, Any], work_root: str
               ) -> dict[str, Any]:
    data_dir = os.path.join(work_root, script["scenario"].replace("/", "_"))
    os.makedirs(data_dir, exist_ok=True)
    service = Service(binary, data_dir)
    service.start()
    facts: list[dict[str, Any]] = []
    tx_ids: dict[str, str] = {}  # script alias -> server transaction id
    try:
        for step in script["operations"]:
            facts.append(_execute(service, step, tx_ids))
        for sql in script.get("sql_checks", []):
            code, body = service.call("POST", "/sql", {"sql": sql})
            facts.append({"op": "sql", "http_status": code, "sql": sql,
                          "body": body, "label": "sql_parity"})
    finally:
        service.stop()
    return {"scenario": script["scenario"], "facts": facts}


def _normalize_stats(body: Any) -> Any:
    """Strip volatile transaction ids/timestamps from a stats response."""
    if not isinstance(body, dict):
        return body
    body = dict(body)
    body["active"] = [
        {k: ("<server-tx-id>" if k == "id" else
             "<server-timestamp>" if k == "started" else v)
         for k, v in tx.items()}
        for tx in body.get("active", [])
    ]
    return body


def _execute(service: Service, step: dict[str, Any],
             tx_ids: dict[str, str]) -> dict[str, Any]:
    op = step["op"]
    base: dict[str, Any] = {"op": op, "label": step.get("label", "")}
    if op == "create_relation":
        code, body = service.call("POST", "/relations", step["spec"])
        base["http_status"] = code
        base["body"] = body
    elif op == "create_index":
        code, body = service.call("POST", "/indexes", step["spec"])
        base["http_status"] = code
        base["body"] = body
    elif op == "begin":
        code, body = service.call(
            "POST", "/transactions",
            {"read_only": step.get("read_only", False)})
        base["http_status"] = code
        base["tx_alias"] = step["tx"]
        if code == 201:
            tx_ids[step["tx"]] = body["id"]
            # normalize server-generated volatile fields so run facts are
            # byte-stable across rebuilds; semantic fields (snapshot,
            # read_only) and every HTTP status are retained verbatim
            body = {k: ("<server-tx-id>" if k == "id" else
                        "<server-timestamp>" if k == "started" else v)
                    for k, v in body.items()}
        base["body"] = body
    elif op == "put":
        code, body = service.call(
            "POST", f"/transactions/{tx_ids[step['tx']]}/put",
            {"relation": step["relation"], "row": step["row"],
             "insert_only": step.get("insert_only", False)})
        base["http_status"] = code
        base["body"] = body
    elif op == "delete":
        code, body = service.call(
            "POST", f"/transactions/{tx_ids[step['tx']]}/delete",
            {"relation": step["relation"], "key": step["key_value"]})
        base["http_status"] = code
        base["body"] = body
    elif op == "commit":
        code, body = service.call(
            "POST", f"/transactions/{tx_ids[step['tx']]}/commit")
        base["http_status"] = code
        base["body"] = body
        base["expect_conflict"] = step.get("expect_conflict", False)
    elif op == "rollback":
        code, body = service.call(
            "POST", f"/transactions/{tx_ids[step['tx']]}/rollback")
        base["http_status"] = code
        base["body"] = body
    elif op == "vacuum":
        if step["horizon"] == "head":
            _stats_code, stats_body = service.call("GET", "/stats")
            horizon = stats_body["sequence"]
        else:
            horizon = step["horizon"]
        code, body = service.call("POST", "/vacuum",
                                  {"horizon": horizon})
        base["http_status"] = code
        base["requested_horizon"] = horizon
        base["body"] = body
    elif op == "checkpoint":
        code, body = service.call("POST", "/checkpoint")
        base["http_status"] = code
        base["body"] = body
    elif op == "stats":
        code, body = service.call("GET", "/stats")
        body = _normalize_stats(body)
        base["http_status"] = code
        base["body"] = body
    elif op == "restart":
        service.stop()
        service.start()
        base["restarted"] = True
    elif op == "query":
        request = dict(step["request"])
        recorded = dict(request)
        if request.get("transaction"):
            request["transaction"] = tx_ids[request["transaction"]]
            # keep the script alias in the recorded fact for byte stability
            recorded["transaction"] = step["request"]["transaction"]
        code, body = service.call("POST", "/query", request)
        base["http_status"] = code
        base["body"] = body
        base["note"] = step.get("note", "")
        base["dims"] = step.get("dims", [])
        base["request"] = recorded
    else:
        raise ValueError(f"unknown op {op}")
    return base


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--binary",
                        default=os.path.join(os.path.dirname(root),
                                             "bin", "laminad"))
    parser.add_argument("--root", default=root)
    args = parser.parse_args()
    runs_dir = os.path.join(args.root, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lamina-corpus-") as work:
        for dirpath, _dirs, files in os.walk(
                os.path.join(args.root, "scenarios")):
            for name in sorted(files):
                if not name.endswith(".script.json"):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    script = json.load(f)
                result = run_script(args.binary, script, work)
                rel = os.path.relpath(os.path.join(dirpath, name),
                                      os.path.join(args.root, "scenarios"))
                out = os.path.join(
                    runs_dir, rel.replace(".script.json", ".run.json"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2,
                              sort_keys=True)
                    f.write("\n")
                queries = [x for x in result["facts"] if x["op"] == "query"]
                print(f"{rel}: {len(queries)} queries executed")


if __name__ == "__main__":
    main()
