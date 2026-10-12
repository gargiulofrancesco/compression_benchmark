"""Synthetic-sweep grid driver (paper Fig. "synthetic sweep").

Runs {OnPair, FSST, Zstd-3, LZ4} over the parametric synthetic columns
(mean string length L x intra-row repetitiveness rho, constant volume per
cell) produced by tools/gen_vd_columns.py, into build/vd_results.sqlite.

Usage:
  PYTHONPATH=. .venv/bin/python tools/gen_vd_columns.py      # once
  PYTHONPATH=. .venv/bin/python -m experiments.f6_driver
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINES = {
    "onpair": ("runners/onpair/onpair_runner",
               '{"bits":16,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":64}'),
    "fsst":   ("runners/fsst/fsst_runner",
               '{"variant":"fsst","training":"full_column"}'),
    "zstd":   ("runners/zstd/zstd_runner",
               '{"level":3,"block_budget_bytes":1048576}'),
    "lz4":    ("runners/lz4/lz4_runner",
               '{"tier":"fast","accel":1,"block_budget_bytes":1048576}'),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "build",
                    help="directory for the result store (default: build)")
    out = ap.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    build = REPO_ROOT / "build"
    cols = sorted((REPO_ROOT / "datasets" / "synthetic_vd").glob("f6__*.col"))
    if not cols:
        print("f6_driver: no synthetic columns found; run "
              "`python tools/gen_vd_columns.py --set sweep` first", file=sys.stderr)
        return 1
    cells = []
    for col in cols:
        for b, (rp, cfg) in BASELINES.items():
            cells.append({
                "cell_id": f"f6-{col.stem.replace('__', '-')}-{b}",
                "baseline": b, "runner_path": rp, "config_json": cfg,
                "dataset": f"synthetic_vd/{col.stem}",
                "dataset_kind": "real", "column_path": str(col),
            })
    doc = {"n_samples": 3, "min_samples": 3, "mem_cap_gb": 64,
           "require_controlled_env": True, "cells": cells}
    cells_path = out / "f6_cells.yaml"
    yaml.safe_dump(doc, open(cells_path, "w"), sort_keys=False)
    print(f"f6_driver: {len(cells)} cells -> {cells_path}")
    return subprocess.call([
        sys.executable, "-m", "orchestrator.orchestrate",
        "--build-root", str(build), "--cells", str(cells_path),
        "--modes", str(REPO_ROOT / "experiments/baseline_modes.yaml"),
        "--needles", str(REPO_ROOT / "testdata/g6_needles.txt"),
        "--db", str(out / "vd_results.sqlite"),
        "--csv", str(out / "vd_results.csv"),
        "--workdir", str(out / "f6_workdir"),
    ], cwd=str(REPO_ROOT))


if __name__ == "__main__":
    sys.exit(main())
