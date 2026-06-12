"""RLZ reference-size sweep driver (paper Fig. "model size vs ratio").

Sweeps RLZ's absolute reference size over a geometric series on the two
columns of the model-size study, into build/rlz_sweep2_results.sqlite.
RLZ's only ratio lever is its reference, so this curve is compared against
OnPair's dictionary-capacity sweep (A2) at matched model sizes.

Usage:
  PYTHONPATH=. .venv/bin/python -m experiments.rlz_sweep_driver
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

GRID = {
    "msmarco_urls/url": [512 << 10, 1 << 20, 2 << 20, 4 << 20,
                         8 << 20, 16 << 20, 32 << 20],
    "tpch/c_comment": [256 << 10, 512 << 10, 1 << 20, 2 << 20,
                       4 << 20, 8 << 20, 16 << 20],
}


def main() -> int:
    build = REPO_ROOT / "build"
    cells = []
    for ds, sizes in GRID.items():
        name, col = ds.split("/")
        col_path = REPO_ROOT / "datasets" / "materialized" / name / f"{col}.col"
        for rb in sizes:
            cells.append({
                "cell_id": f"rlzsweep-{name}-{col}-r{rb // 1024}k",
                "baseline": "rlz",
                "runner_path": "runners/rlz/rlz_runner",
                "config_json": ('{"reference_size_bytes": %d, "block_size": 16384,'
                                ' "seed": 42, "threads": 1, "mem_cap_gb": 64}' % rb),
                "dataset": ds, "dataset_kind": "real",
                "column_path": str(col_path),
            })
    doc = {"n_samples": 3, "min_samples": 3, "mem_cap_gb": 64,
           "require_controlled_env": True, "cells": cells}
    cells_path = build / "rlz_sweep2_cells.yaml"
    yaml.safe_dump(doc, open(cells_path, "w"), sort_keys=False)
    print(f"rlz_sweep_driver: {len(cells)} cells -> {cells_path}")
    return subprocess.call([
        sys.executable, "-m", "orchestrator.orchestrate",
        "--build-root", str(build), "--cells", str(cells_path),
        "--modes", str(REPO_ROOT / "experiments/baseline_modes.yaml"),
        "--needles", str(REPO_ROOT / "testdata/g6_needles.txt"),
        "--db", str(build / "rlz_sweep2_results.sqlite"),
        "--csv", str(build / "rlz_sweep2_results.csv"),
        "--workdir", str(build / "rlz_sweep2_workdir"),
    ], cwd=str(REPO_ROOT))


if __name__ == "__main__":
    sys.exit(main())
