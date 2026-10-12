"""Reproducible plot template — reads results_agg, writes PDF + PNG.

Produces one figure (median compress throughput per baseline); `make smoke`
renders it from the smoke store as an end-to-end check of the plotting path.

matplotlib rcParams are pinned and PYTHONHASHSEED=0 is expected in the
environment. tests/test_figure_template.py checks that a valid PNG comes out
of the CSV fixture; it does not compare pixels.
"""
from __future__ import annotations
import argparse
import csv
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["pdf.fonttype"]   = 42
matplotlib.rcParams["ps.fonttype"]    = 42
matplotlib.rcParams["svg.fonttype"]   = "none"
matplotlib.rcParams["font.family"]    = "DejaVu Sans"
matplotlib.rcParams["figure.figsize"] = (5.0, 3.0)
matplotlib.rcParams["figure.dpi"]     = 100
import matplotlib.pyplot as plt  # noqa: E402


def _load_from_sqlite(path: Path) -> list[dict]:
    conn = sqlite3.connect(path)
    cur = conn.execute("""
        SELECT baseline, op, metric_name, median, mad
        FROM results_agg
        WHERE op='compress' AND metric_name='compress_throughput_bytes_per_s'
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur]


def _load_from_csv(path: Path) -> list[dict]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row.get("op") == "compress" and row.get("metric_name") == "compress_throughput_bytes_per_s":
                out.append({"baseline": row["baseline"],
                            "median": float(row["median"]) if row["median"] else None,
                            "mad":    float(row["mad"])    if row["mad"]    else None})
    return out


def render(rows: list[dict], out_png: Path, out_pdf: Path | None = None) -> None:
    rows = sorted(rows, key=lambda r: r["baseline"])
    names  = [r["baseline"]                  for r in rows]
    medians = [(r["median"] or 0.0) / 1e9    for r in rows]
    mads    = [(r["mad"]    or 0.0) / 1e9    for r in rows]
    fig, ax = plt.subplots()
    ax.bar(range(len(names)), medians, yerr=mads, capsize=4)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=15)
    ax.set_ylabel("compress throughput (GB/s)")
    ax.set_title("smoke")
    fig.tight_layout()
    fig.savefig(out_png, dpi=100)
    if out_pdf:
        fig.savefig(out_pdf)
    plt.close(fig)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db",     type=Path)
    p.add_argument("--csv",    type=Path)
    p.add_argument("--out-png", type=Path, required=True)
    p.add_argument("--out-pdf", type=Path)
    a = p.parse_args()
    if a.csv:
        rows = _load_from_csv(a.csv)
    elif a.db:
        rows = _load_from_sqlite(a.db)
    else:
        raise SystemExit("figure_template: must pass --db or --csv")
    render(rows, a.out_png, a.out_pdf)
    print(f"figure_template: wrote {a.out_png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
