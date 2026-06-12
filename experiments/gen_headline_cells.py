"""— headline benchmark cell generator (§8.2 / F1 / T1 / T2).

Expands (every real column in experiments/datasets.yaml) × (every baseline in
experiments/headline_roster.yaml) into an explicit cells YAML that the canonical
orchestrator (orchestrator.orchestrate) consumes — so every headline cell gets
the same G6 + round-trip correctness verification as the smoke run, and the
headline numbers land in the headline results.sqlite (NOT in the ablation
stores).

This adds NO orchestrator change: real cells use the existing
`dataset_kind: real` + `column_path` path (orchestrate._materialise_input_column).
The materialised columns under datasets/materialized/<dataset>/<file> do not
exist until `make datasets` runs on the rig; the generated YAML just carries the
paths, and the orchestrator raises a friendly "run make datasets" error at run
time if a column is missing.

Modes:
  --smoke : tiny lossless trio (per headline_roster.yaml `smoke:`) backed by the
            committed V-D 1000-row fixture as dataset_kind=real. Exercises the
            generator + real copy-from-disk path + orchestrator end-to-end on
            committed data. <~30 s wall.
  default : full grid (all real columns × 11 baselines) pointing at
            datasets/materialized/. Rig only (needs `make datasets`).

Usage:
  python -m experiments.gen_headline_cells --out build/headline_cells.yaml [--smoke]
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASETS = REPO_ROOT / "experiments" / "datasets.yaml"
ROSTER = REPO_ROOT / "experiments" / "headline_roster.yaml"


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s)


def _real_columns(datasets_doc: dict) -> list[dict]:
    """Flatten datasets.yaml real entries → list of
    {dataset, column, column_path} pointing at datasets/materialized/.

    HIGH-CARDINALITY SCOPE: only columns explicitly tagged
    `headline: true` enter the headline grid. Out-of-scope (low-cardinality)
    columns may remain materialised — e.g. the non-headline tpch/c_address
    column — but are excluded here (high-cardinality scope).
    """
    out: list[dict] = []
    for ds in datasets_doc["datasets"]:
        if ds.get("kind") != "real":
            continue
        for col in ds["columns"]:
            if not col.get("headline", False):
                continue
            out_fname = col.get("output_filename", f"{col['name']}.col")
            out.append({
                "dataset": ds["name"],
                "column": col["name"],
                "column_path": f"datasets/materialized/{ds['name']}/{out_fname}",
            })
    return out


def _cell(cell_id: str, baseline: str, runner_path: str, dataset: str,
          column_path: str, config_json: str) -> dict:
    return {
        "cell_id": cell_id,
        "baseline": baseline,
        "runner_path": runner_path,
        "dataset": dataset,
        "dataset_kind": "real",
        "column_path": column_path,
        "config_json": config_json,
    }


# Headline statistical-robustness floor (.5). The median ± MAD primary
# statistic wants an ODD repetition count so the reported median is an observed
# measurement, not the interpolation between the two central order statistics.
HEADLINE_MIN_SAMPLES = 11


def build_full(datasets_doc: dict, roster_doc: dict) -> dict:
    n_samples = int(roster_doc["n_samples"])
    if n_samples < HEADLINE_MIN_SAMPLES:
        raise SystemExit(
            f"gen_headline_cells: headline n_samples={n_samples} is below the "
            f"statistical-robustness floor HEADLINE_MIN_SAMPLES={HEADLINE_MIN_SAMPLES} "
            f"(.5). Raise experiments/headline_roster.yaml `n_samples`.")
    cols = _real_columns(datasets_doc)
    cells = []
    for col in cols:
        # dataset label is a `ds/col` composite so the headline figures/tables can
        # pivot per column without parsing cell_id (baseline names contain dashes).
        ds_label = f"{col['dataset']}/{col['column']}"
        for b in roster_doc["baselines"]:
            cell_id = f"hl-{col['dataset']}-{_slug(col['column'])}-{b['baseline']}"
            cells.append(_cell(
                cell_id=cell_id, baseline=b["baseline"],
                runner_path=b["runner_path"], dataset=ds_label,
                column_path=col["column_path"], config_json=b["config_json"]))
    return {
        "n_samples": n_samples,
        "min_samples": HEADLINE_MIN_SAMPLES,   # orchestrator refuses to run below this
        "require_controlled_env": True,        # refuse uncontrolled governor/turbo (W-6)
        "mem_cap_gb": int(roster_doc["mem_cap_gb"]),
        "timeout_seconds": int(roster_doc["timeout_seconds"]),
        "cells": cells,
    }


def build_smoke(roster_doc: dict) -> dict:
    smoke = roster_doc["smoke"]
    by_name = {b["baseline"]: b for b in roster_doc["baselines"]}
    cells = []
    for bname in smoke["baselines"]:
        b = by_name[bname]
        cell_id = f"hl-smoke-{smoke['dataset']}-{bname}"
        cells.append(_cell(
            cell_id=cell_id, baseline=bname, runner_path=b["runner_path"],
            dataset=smoke["dataset"], column_path=smoke["fixture_path"],
            config_json=b["config_json"]))
    return {
        "n_samples": int(smoke["n_samples"]),
        "mem_cap_gb": int(roster_doc["mem_cap_gb"]),
        "timeout_seconds": int(roster_doc["timeout_seconds"]),
        "cells": cells,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--datasets", type=Path, default=DATASETS)
    p.add_argument("--roster", type=Path, default=ROSTER)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--smoke", action="store_true")
    a = p.parse_args()

    roster_doc = yaml.safe_load(a.roster.read_text())
    if a.smoke:
        doc = build_smoke(roster_doc)
    else:
        datasets_doc = yaml.safe_load(a.datasets.read_text())
        doc = build_full(datasets_doc, roster_doc)

    # Validate every config_json parses (fail at generate time, not run time).
    for c in doc["cells"]:
        json.loads(c["config_json"])

    a.out.parent.mkdir(parents=True, exist_ok=True)
    header = ("# GENERATED by experiments/gen_headline_cells.py — do not edit by "
              "hand.\n# Source: experiments/datasets.yaml + experiments/headline_roster.yaml\n")
    a.out.write_text(header + yaml.safe_dump(doc, sort_keys=False))
    print(f"gen_headline_cells: wrote {len(doc['cells'])} cells → {a.out} "
          f"(n_samples={doc['n_samples']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
