"""Headline benchmark cell generator (Table II, Fig. 4).

Expands (every column tagged `headline: true` in experiments/datasets.yaml) ×
(every baseline in experiments/headline_roster.yaml) into an explicit cells YAML
that the canonical orchestrator (orchestrator.orchestrate) consumes — so every
headline cell gets the same round-trip and prefix-search verification as the
smoke run, and the headline numbers land in the headline results.sqlite (NOT in
the ablation stores).

Real cells use the orchestrator's
`dataset_kind: real` + `column_path` path (orchestrate._materialise_input_column).
The materialised columns under datasets/materialized/<dataset>/<file> do not
exist until `make datasets` has run; the generated YAML just carries the
paths, and the orchestrator raises a "run make datasets" error at run
time if a column is missing.

Modes:
  --smoke : tiny lossless trio (per headline_roster.yaml `smoke:`) backed by the
            committed synthetic 1000-row fixture as dataset_kind=real. Exercises
            the generator + real copy-from-disk path + orchestrator end-to-end on
            committed data. <~30 s wall.
  default : full grid (the six headline columns × 11 baselines) pointing at
            datasets/materialized/ (needs `make datasets`).
            --part main|last splits it: `last` holds the roster's `run_last`
            baselines (Re-Pair), `main` everything else.

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


# Headline statistical-robustness floor. The median ± MAD primary
# statistic wants an ODD repetition count so the reported median is an observed
# measurement, not the interpolation between the two central order statistics.
HEADLINE_MIN_SAMPLES = 11


def _select(wanted, known: list[str], what: str) -> list[str]:
    """`known` restricted to the names in `wanted`, in the roster's order.
    An unknown name is an error that lists the valid ones."""
    if not wanted:
        return known
    unknown = sorted(set(wanted) - set(known))
    if unknown:
        raise SystemExit(f"gen_headline_cells: unknown {what} {', '.join(unknown)}; "
                         f"choose from: {' '.join(known)}")
    return [k for k in known if k in set(wanted)]


def build_full(datasets_doc: dict, roster_doc: dict, part: str = "all",
               baselines=None, columns=None) -> dict:
    """`part` selects the cells: "main" leaves out the roster's `run_last`
    baselines, "last" keeps only those, "all" is main followed by last.
    `baselines` and `columns` (`dataset/column`) restrict the grid to a subset;
    None keeps all of them."""
    n_samples = int(roster_doc["n_samples"])
    if n_samples < HEADLINE_MIN_SAMPLES:
        raise SystemExit(
            f"gen_headline_cells: headline n_samples={n_samples} is below the "
            f"statistical-robustness floor HEADLINE_MIN_SAMPLES={HEADLINE_MIN_SAMPLES}"
            f". Raise experiments/headline_roster.yaml `n_samples`.")
    cols = _real_columns(datasets_doc)
    keep_cols = _select(columns, [f"{c['dataset']}/{c['column']}" for c in cols], "column")
    cols = [c for c in cols if f"{c['dataset']}/{c['column']}" in keep_cols]
    keep = _select(baselines, [b["baseline"] for b in roster_doc["baselines"]], "baseline")
    roster_doc = {**roster_doc,
                  "baselines": [b for b in roster_doc["baselines"] if b["baseline"] in keep]}
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
    # Stable partition: `run_last` baselines close the grid, so the slow tail
    # cannot hold up (or, if the run is interrupted, cost) the other cells.
    last = set(roster_doc.get("run_last", []))
    main_cells = [c for c in cells if c["baseline"] not in last]
    last_cells = [c for c in cells if c["baseline"] in last]
    cells = {"all": main_cells + last_cells, "main": main_cells,
             "last": last_cells}[part]
    return {
        "n_samples": n_samples,
        "min_samples": HEADLINE_MIN_SAMPLES,   # orchestrator refuses to run below this
        "require_controlled_env": True,        # refuse uncontrolled governor/turbo
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
    p.add_argument("--part", choices=("all", "main", "last"), default="all",
                   help="main: without the roster's run_last baselines; "
                        "last: only those; all: both (default)")
    p.add_argument("--baseline", action="append", default=[],
                   help="keep only this baseline (repeatable; default: all)")
    p.add_argument("--column", action="append", default=[],
                   help="keep only this dataset/column (repeatable; default: all)")
    a = p.parse_args()

    roster_doc = yaml.safe_load(a.roster.read_text())
    if a.smoke:
        doc = build_smoke(roster_doc)
    else:
        datasets_doc = yaml.safe_load(a.datasets.read_text())
        doc = build_full(datasets_doc, roster_doc, a.part, a.baseline, a.column)

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
