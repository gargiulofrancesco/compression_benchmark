"""Combine several independent passes of the benchmark into one build root.

A pass is a build root holding result stores (one run of the timing stages).
Timings can differ from one process to the next beyond what the repetitions
inside a process show, so the paper's numbers come from several passes. This
tool writes a build root that the other tools read like any single pass:

  results.sqlite          the first pass's store; the later passes' stores are
                          placed next to it as results_passN.sqlite, which the
                          tools pool, so a median runs over every repetition of
                          every pass
  stores with one row     point access, block size, query skew, capacity sweep,
  per cell                scale sweep: every timing column (*_ns) becomes the
                          median over the passes in which the cell is ok
  JSON results            paired decode and dictionary order: every timing
                          (*_ns) and rate (*_per_second) under `summary` and
                          `phase_metrics` becomes the median over the passes

The first pass is the reference: whatever only it holds (the ratio ablations,
cells other passes skipped) is carried over unchanged, and non-timing columns
always come from it.

Usage: PYTHONPATH=. .venv/bin/python -m tools.combine_passes \
           --pass build --pass build/verify --pass build/pass3 --out build/combined
"""
from __future__ import annotations
import argparse
import json
import shutil
import sqlite3
import statistics
from pathlib import Path

# store file -> table, for the stores that hold one row per cell (key: cell_id)
CELL_STORES = {
    "ra_headline_results.sqlite": "ra_headline_results",
    "a4_results.sqlite": "a4_results",
    "a5_results.sqlite": "a5_results",
    "a6_results.sqlite": "a6_results",
    "nsweep_results.sqlite": "nsweep_results",
}
JSON_RESULTS = ("decode/results.json",
                "dictorder/results.json")
JSON_SECTIONS = ("summary", "phase_metrics")


def _copy_store(src: Path, dst: Path) -> None:
    """Consistent copy of a SQLite file (safe even if a writer is attached)."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    source.backup(target)
    target.close()
    source.close()


def combine_cell_store(name: str, table: str, passes: list[Path], out: Path) -> int:
    """Median over the passes of every *_ns column, per ok cell. Returns the
    number of cells that more than one pass measured."""
    _copy_store(passes[0] / name, out / name)
    conn = sqlite3.connect(out / name)
    timing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")
              if r[1].endswith("_ns")]
    others = [sqlite3.connect(f"file:{p / name}?mode=ro", uri=True)
              for p in passes[1:] if (p / name).exists()]
    select = f"SELECT {', '.join(timing)} FROM {table} WHERE cell_id=? AND status='ok'"
    combined = 0
    for (cell,) in conn.execute(
            f"SELECT cell_id FROM {table} WHERE status='ok'").fetchall():
        rows = [conn.execute(select, (cell,)).fetchone()]
        rows += [r for r in (c.execute(select, (cell,)).fetchone() for c in others) if r]
        if len(rows) == 1:
            continue
        combined += 1
        for i, column in enumerate(timing):
            values = [r[i] for r in rows if r[i] is not None]
            if values:
                conn.execute(f"UPDATE {table} SET {column}=? WHERE cell_id=?",
                             (statistics.median(values), cell))
    conn.commit()
    conn.close()
    return combined


def _is_timing(key: str) -> bool:
    return key.endswith("_ns") or key.endswith("_per_second")


def combine_json(name: str, passes: list[Path], out: Path) -> int:
    """Median over the passes of the timings and rates of each dataset's
    summary and phase metrics. Returns the number of values combined."""
    docs = [json.loads((p / name).read_text()) for p in passes if (p / name).exists()]
    doc = docs[0]
    combined = 0
    for ds, entry in doc["datasets"].items():
        for section in JSON_SECTIONS:
            for group, stats in entry.get(section, {}).items():
                for key, value in stats.items():
                    if not _is_timing(key) or not isinstance(value, (int, float)):
                        continue
                    values = [d["datasets"][ds][section][group][key] for d in docs
                              if key in d["datasets"].get(ds, {}).get(section, {}).get(group, {})]
                    stats[key] = statistics.median(values)
                    combined += len(values) > 1
    doc["combined_from_passes"] = len(docs)
    target = out / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return combined


def combine(passes: list[Path], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ref = passes[0]
    # everything the reference pass holds, as it is
    for store in sorted(ref.glob("*.sqlite")):
        if store.name not in CELL_STORES:
            _copy_store(store, out / store.name)
    for extra in ("bpedecoder/results.json",
                  "prefix/results.json", "finalization/results.csv"):
        if (ref / extra).exists():
            (out / extra).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ref / extra, out / extra)
    # the main grid: the later passes' rows are pooled by the tools
    pooled = 1
    for i, p in enumerate(passes[1:], start=2):
        if (p / "results.sqlite").exists():
            _copy_store(p / "results.sqlite", out / f"results_pass{i}.sqlite")
            pooled += 1
    print(f"combine_passes: main grid pooled from {pooled} pass(es)")
    for name, table in CELL_STORES.items():
        if (ref / name).exists():
            n = combine_cell_store(name, table, passes, out)
            print(f"combine_passes: {name}: {n} cells measured in more than one pass")
    for name in JSON_RESULTS:
        if (ref / name).exists():
            n = combine_json(name, passes, out)
            print(f"combine_passes: {name}: {n} values combined")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pass", dest="passes", action="append", type=Path, required=True,
                    help="build root of one pass; repeat, reference pass first")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    combine(a.passes, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
