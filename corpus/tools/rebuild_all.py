#!/usr/bin/env python3
"""End-to-end rebuild of the LaminaDB query workload corpus.

Stages (each independently rerunnable):
  1. build_scenarios      deterministic input generation material
  2. derive_answers       independent reference answers (no laminad contact)
  3. run_corpus           real laminad replay -> run facts (HTTP only)
  4. verify_corpus        cross-check answers against run facts
  5. selection_experiment budget/coverage scheme comparison
  6. coverage_matrix      human-readable coverage tradeoff document

All Python runs use isolated mode (-I semantics via the interpreter flag).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")


def run_stage(module: str, extra: list[str] | None = None) -> None:
    cmd = [sys.executable, "-I", os.path.join(TOOLS, module)] + (extra or [])
    print(f"\n=== {module} ===", flush=True)
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"stage {module} failed with {result.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary",
                        default=os.path.join(os.path.dirname(ROOT),
                                             "bin", "laminad"))
    parser.add_argument("--skip-run", action="store_true",
                        help="regenerate inputs and answers without a server")
    args = parser.parse_args()
    run_stage("build_scenarios.py")
    run_stage("derive_answers.py")
    if not args.skip_run:
        if not os.path.exists(args.binary):
            raise SystemExit(
                f"laminad binary not found at {args.binary}; "
                "build it first: go build -o bin/laminad ./cmd/laminad")
        run_stage("run_corpus.py", ["--binary", args.binary])
        run_stage("verify_corpus.py")
    run_stage("selection_experiment.py")
    run_stage("coverage_matrix.py")
    print("\ncorpus rebuild complete")


if __name__ == "__main__":
    main()
