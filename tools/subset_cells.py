"""Restrict an ablation grid to some of its columns.

    PYTHONPATH=. .venv/bin/python -m tools.subset_cells \
        experiments/a1_cells.yaml build/subset/a1_cells.yaml --column tpch/p_name

Writes the grid with only the named columns (`dataset/column`), everything
else unchanged, and prints how many columns remain. The ablation drivers take
the result through their `--aN-cells` option; `reproduce.sh` does this when
COLS is set.
"""
from __future__ import annotations
import argparse
from pathlib import Path

import yaml


def subset(doc: dict, columns: list[str]) -> dict:
    keep = set(columns)
    return {**doc, "columns": [c for c in doc["columns"]
                               if f"{c['dataset']}/{c['column']}" in keep]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cells", type=Path, help="grid to read")
    ap.add_argument("out", type=Path, help="grid to write")
    ap.add_argument("--column", action="append", required=True,
                    help="dataset/column to keep (repeatable)")
    a = ap.parse_args()
    doc = subset(yaml.safe_load(a.cells.read_text()), a.column)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(yaml.safe_dump(doc, sort_keys=False))
    print(len(doc["columns"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
