"""Print what a result directory holds, as plain text.

For every column and baseline measured by the `headline` stage: compression
ratio (payload only, as in Table II), compression throughput and
decompression throughput, with the number of repetitions behind each median.
If the `access` stage has run, its median and p99 point-access latency are
added. Then one block for each of the other studies whose store is present:
the ablations, the RLZ reference-size sweep, the synthetic sweep and the
scale sweep, each with the quantity the paper reports from it. Nothing is
written.

    ./reproduce.sh table                      # the default result directory
    PYTHONPATH=. .venv/bin/python -m tools.show_results --build-root build/subset
    PYTHONPATH=. .venv/bin/python -m tools.show_results --list   # names to select

Note on BPE: the table shows the decompression throughput stored by the
`headline` stage, measured with SentencePiece's own id order. Table II of the
paper prints BPE with sorted ids, which the `dictorder` stage measures.
"""
from __future__ import annotations
import argparse
import sqlite3
import statistics
from pathlib import Path

import yaml

from tools import paper_tables

REPO_ROOT = Path(__file__).resolve().parent.parent

# baseline id -> name in the paper (ids are what BASELINES=... takes)
LABELS = {
    "uncompressed": "Raw (uncompressed)", "lz4": "LZ4", "zstd_l1": "Zstd-1",
    "zstd": "Zstd-3", "zstd_l9": "Zstd-9", "fsst": "FSST",
    "bpe": "BPE, 2^14 vocabulary (not in the paper)", "bpe_v65536": "BPE",
    "rlz": "RLZ", "repair": "Re-Pair", "onpair": "OnPair",
}
ORDER = list(LABELS)


def roster() -> list[str]:
    doc = yaml.safe_load((REPO_ROOT / "experiments" / "headline_roster.yaml").read_text())
    return [b["baseline"] for b in doc["baselines"]]


def columns() -> list[str]:
    return [c for c, _ in paper_tables.COLUMNS]


def print_names() -> None:
    print("Baselines (BASELINES=\"...\"):")
    for b in sorted(roster(), key=ORDER.index):
        print(f"  {b:14} {LABELS[b]}")
    print("Columns (COLS=\"...\"):")
    for c in columns():
        print(f"  {c}")


def _count(conns, dataset, baseline) -> int:
    return sum(c.execute(
        "SELECT COUNT(*) FROM results WHERE dataset=? AND baseline=? AND "
        "op='compress' AND metric_name='t_compress_ns' AND status IN ('ok','dev-only')",
        (dataset, baseline)).fetchone()[0] for c in conns)


def _access(build_root: Path) -> dict:
    db = build_root / "ra_headline_results.sqlite"
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    return {(f"{ds}/{col}", b): (p50, p99) for ds, col, b, p50, p99 in conn.execute(
        "SELECT dataset, column_name, baseline, l_ra_p50_ns, l_ra_p99_ns FROM "
        "ra_headline_results WHERE status='ok'")}


def main_grid(build_root: Path) -> bool:
    """Ratio, throughputs and access latencies; False if none was measured."""
    stores = paper_tables._result_stores(build_root)
    conns = [sqlite3.connect(f"file:{p}?mode=ro", uri=True) for p in stores]
    access = _access(build_root)
    measured = sorted({(d, b) for c in conns for d, b in c.execute(
        "SELECT DISTINCT dataset, baseline FROM results WHERE op='compress'")} | set(access))
    if not measured:
        return False
    if len(stores) > 1:
        print(f"Main grid: {len(stores)} passes pooled")
    dev_only = any(c.execute("SELECT 1 FROM results WHERE status='dev-only' LIMIT 1").fetchone()
                   for c in conns)
    for dataset in [c for c in columns() if any(d == c for d, _ in measured)] + sorted(
            {d for d, _ in measured} - set(columns())):
        size = paper_tables.med(conns, dataset, next(b for d, b in measured if d == dataset),
                                "compress", "input_bytes") if conns else None
        print(f"\n{dataset}" + (f"  ({size / 2**20:.0f} MiB)" if size else ""))
        print(f"  {'baseline':14} {'ratio':>6} {'compress':>10} {'decompress':>11} "
              f"{'runs':>5}" + (f" {'access p50':>11} {'p99':>9}" if access else ""))
        print(f"  {'':14} {'':>6} {'MiB/s':>10} {'GiB/s':>11} {'':>5}"
              + (f" {'ns':>11} {'ns':>9}" if access else ""))
        for b in sorted({b for d, b in measured if d == dataset},
                        key=lambda x: ORDER.index(x) if x in ORDER else len(ORDER)):
            r = paper_tables.ratio(conns, dataset, b) if conns else None
            tc = paper_tables.throughput_gibs(conns, dataset, b, "compress", "t_compress_ns") if conns else None
            td = paper_tables.throughput_gibs(conns, dataset, b, "bulk_decode", "t_decode_ns") if conns else None
            n = _count(conns, dataset, b)
            line = (f"  {b:14} {paper_tables.fmt(r):>6} "
                    f"{paper_tables.fmt(None if tc is None else tc * 1024, 1):>10} "
                    f"{paper_tables.fmt(td):>11} {n if n else '--':>5}")
            if access:
                p50, p99 = access.get((dataset, b), (None, None))
                line += f" {paper_tables.fmt(p50, 0):>11} {paper_tables.fmt(p99, 0):>9}"
            print(line)
    if any(b.startswith("bpe") for _, b in measured):
        print("\nBPE's decompression above is with SentencePiece's own id order; "
              "Table II prints\nit with sorted ids, which the dictorder stage measures.")
    if dev_only:
        print("\nSome rows were recorded outside the paper's setup (labelled dev-only): "
              "their ratios\nare exact, their timings are those of the machine as it was.")
    return True


# ---------------------------------------------------------------------------
# The other studies. Each block prints what the paper reports from the store.

def _store(build_root: Path, name: str):
    path = build_root / name
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True) if path.exists() else None


def _cells(conn, sql: str) -> dict:
    """{key columns: median of the last column} over the rows of a query."""
    groups: dict = {}
    for *key, value in conn.execute(sql):
        if value is not None:
            groups.setdefault(tuple(key), []).append(value)
    return {k: statistics.median(v) for k, v in groups.items()}


def _order(names) -> list[str]:
    """Columns in the paper's order, any other after them."""
    known = columns()
    return sorted(set(names), key=lambda c: (known.index(c) if c in known else len(known), c))


def _grid(title: str, corner: str, heads: list[str], rows: list[tuple[str, list[str]]]) -> None:
    """A titled grid: one label and one cell per head in every row."""
    wide = max([len(corner)] + [len(label) for label, _ in rows])
    widths = [max([len(h)] + [len(cells[i]) for _, cells in rows]) for i, h in enumerate(heads)]
    print(f"\n{title}")
    print(f"  {corner:{wide}}  " + "  ".join(f"{h:>{w}}" for h, w in zip(heads, widths)))
    for label, cells in rows:
        print((f"  {label:{wide}}  "
               + "  ".join(f"{c:>{w}}" for c, w in zip(cells, widths))).rstrip())


def _ns(v) -> str:
    """A latency with the unit that suits it."""
    if v is None:
        return "--"
    if v < 1e3:
        return f"{v:.0f} ns"
    if v < 1e6:
        return f"{v / 1e3:.1f} us"
    return f"{v / 1e6:.2f} ms"


def _num(v, digits=2, sign=False) -> str:
    return "--" if v is None else f"{v:{'+' if sign else ''}.{digits}f}"


def _by_column(cells: dict) -> dict:
    """{(dataset, column, x): v} -> {"dataset/column": {x: v}}."""
    out: dict = {}
    for (dataset, column, x), v in cells.items():
        out.setdefault(f"{dataset}/{column}", {})[x] = v
    return out


def thresholds(build_root: Path) -> bool:
    db = _store(build_root, "a1_results.sqlite")
    if db is None:
        return False
    gap = _by_column(_cells(db, "SELECT dataset, column_name, fixed_threshold_T, "
                                "100.0 * (heldout_ratio_dyn - heldout_ratio_fixed) / "
                                "heldout_ratio_fixed FROM a1_results WHERE status='ok'"))
    ts = sorted({t for d in gap.values() for t in d})
    _grid("Thresholds (a1): held-out ratio of the adaptive threshold against a fixed "
          "threshold T, in percent", "column", [f"T={t}" for t in ts],
          [(c, [_num(gap[c].get(t), 1, sign=True) for t in ts]) for c in _order(gap)])
    return True


def budget(build_root: Path) -> bool:
    db = _store(build_root, "a3_results.sqlite")
    if db is None:
        return False
    r = _by_column(_cells(db, "SELECT dataset, column_name, sample_fraction, heldout_ratio "
                              "FROM a3_results WHERE status='ok'"))
    fs = sorted({f for d in r.values() for f in d})
    _grid("Training budget (a3): held-out compression ratio by the fraction f of the "
          "column given to the trainer", "column", [f"f={f:g}" for f in fs],
          [(c, [_num(r[c].get(f)) for f in fs]) for c in _order(r)])
    return True


def seeds(build_root: Path) -> bool:
    db = _store(build_root, "a7_results.sqlite")
    if db is None:
        return False
    r = _by_column(_cells(db, "SELECT dataset, column_name, seed, 1.0 * input_bytes / "
                              "(dict_bytes + tokens_bytes) FROM a7_results WHERE status='ok'"))
    _grid("Shuffle seeds (a7): compression ratio over the seeds of the training shuffle",
          "column", ["seeds", "lowest", "highest", "spread"],
          [(c, [str(len(v)), _num(min(v.values()), 3), _num(max(v.values()), 3),
                f"{100 * (max(v.values()) - min(v.values())) / min(v.values()):.2f}%"])
           for c, v in ((c, r[c]) for c in _order(r))])
    return True


def capacity(build_root: Path) -> bool:
    db = _store(build_root, "a2_results.sqlite")
    if db is None:
        return False
    r = _by_column(_cells(db, "SELECT dataset, column_name, bits, 1.0 * input_bytes / "
                              "(dict_bytes + tokens_bytes) FROM a2_results WHERE status='ok'"))
    bits = sorted({b for d in r.values() for b in d})
    _grid("Dictionary capacity (a2): compression ratio by the number of tokens",
          "column", [f"2^{b}" for b in bits],
          [(c, [_num(r[c].get(b)) for b in bits]) for c in _order(r)])
    return True


def _size(n: float) -> str:
    for unit, name in ((2**30, "GiB"), (2**20, "MiB")):
        if n >= unit:
            return f"{n / unit:.3g} {name}"
    return f"{n / 2**10:.3g} KiB"


def model_size(build_root: Path) -> bool:
    db = _store(build_root, "rlz_sweep2_results.sqlite")
    if db is None:
        return False
    m = _cells(db, "SELECT dataset, cell_id, metric_name, metric_value FROM results WHERE "
                   "op='compress' AND metric_name IN ('input_bytes','dict_bytes',"
                   "'tokens_bytes') AND status IN ('ok','dev-only')")
    cells: dict = {}
    for (dataset, cell, name), v in m.items():
        cells.setdefault((dataset, cell), {})[name] = v
    by_column: dict = {}
    for (dataset, _), v in cells.items():
        if len(v) == 3:
            by_column.setdefault(dataset, {})[v["dict_bytes"]] = (
                v["input_bytes"] / (v["dict_bytes"] + v["tokens_bytes"]))
    if not by_column:
        return False
    print("\nModel size (modelsize): RLZ's compression ratio by the size of its reference")
    for c in _order(by_column):
        refs = sorted(by_column[c])
        widths = [max(len(_size(r)), 4) for r in refs]
        print(f"  {c}")
        print("    reference  " + "  ".join(f"{_size(r):>{w}}" for r, w in zip(refs, widths)))
        print("    ratio      " + "  ".join(f"{by_column[c][r]:>{w}.2f}" for r, w in zip(refs, widths)))
    return True


def block_size(build_root: Path) -> bool:
    db = _store(build_root, "a5_results.sqlite")
    if db is None:
        return False
    r = _cells(db, "SELECT dataset, column_name, baseline, block_size_strings, l_ra_p50_ns "
                   "FROM a5_results WHERE cache_state='warm' AND status='ok'")
    if not r:
        return False
    sizes = sorted({k[3] for k in r if k[3] is not None})
    print("\nBlock size (a5): median point-access latency by the number of strings in a block")
    for c in _order({f"{k[0]}/{k[1]}" for k in r}):
        dataset, column = c.split("/")
        rows = []
        for b in sorted({k[2] for k in r if (k[0], k[1]) == (dataset, column)},
                        key=lambda x: ORDER.index(x) if x in ORDER else len(ORDER)):
            flat = r.get((dataset, column, b, None))
            if flat is not None:        # a per-string scheme has no block
                rows.append((b, [_ns(flat)] + [""] * (len(sizes) - 1)))
            else:
                rows.append((b, [_ns(r.get((dataset, column, b, s))) for s in sizes]))
        _grid(f"  {c}", "  baseline", [str(s) for s in sizes],
              [("  " + label, cells) for label, cells in rows])
    return True


def query_skew(build_root: Path) -> bool:
    db = _store(build_root, "a6_results.sqlite")
    if db is None:
        return False
    r = _cells(db, "SELECT dataset, column_name, query_distribution, query_layout, baseline, "
                   "l_ra_p50_ns FROM a6_results WHERE cache_state='warm' AND status='ok'")
    if not r:
        return False
    print("\nQuery skew (a6): median point-access latency by the distribution of the "
          "queried rows\n(identity: popular rows are neighbours; scramble: they are spread "
          "over the column)")
    for c in _order({f"{k[0]}/{k[1]}" for k in r}):
        dataset, column = c.split("/")
        mine = {k[2:]: v for k, v in r.items() if (k[0], k[1]) == (dataset, column)}
        bases = sorted({k[2] for k in mine},
                       key=lambda x: ORDER.index(x) if x in ORDER else len(ORDER))
        kinds = list(dict.fromkeys((k[0], k[1]) for k in sorted(
            mine, key=lambda k: (k[0] != "uniform", k[0], k[1]))))
        _grid(f"  {c}", "  queries", bases,
              [(f"  {d} {layout}", [_ns(mine.get((d, layout, b))) for b in bases])
               for d, layout in kinds])
    return True


def synthetic(build_root: Path) -> bool:
    db = _store(build_root, "vd_results.sqlite")
    if db is None:
        return False
    m = _cells(db, "SELECT dataset, baseline, op, metric_name, metric_value FROM results "
                   "WHERE status IN ('ok','dev-only') AND metric_name IN ('input_bytes',"
                   "'dict_bytes','tokens_bytes','t_decode_ns')")
    cells: dict = {}
    for (dataset, baseline, op, name), v in m.items():
        if (op, name) in (("compress", "input_bytes"), ("compress", "dict_bytes"),
                          ("compress", "tokens_bytes"), ("bulk_decode", "t_decode_ns")):
            cells.setdefault((dataset, baseline), {})[name] = v
    if not cells:
        return False

    def shape(dataset: str):            # synthetic_vd/f6__L32_rho0.95 -> (32, 0.95)
        length, rho = dataset.rsplit("__L", 1)[1].split("_rho")
        return int(length), float(rho)

    datasets = sorted({d for d, _ in cells}, key=shape)
    bases = sorted({b for _, b in cells}, key=lambda x: ORDER.index(x) if x in ORDER else len(ORDER))

    def ratio(d, b):
        v = cells.get((d, b), {})
        if {"input_bytes", "dict_bytes", "tokens_bytes"} <= set(v):
            return v["input_bytes"] / (v["dict_bytes"] + v["tokens_bytes"])

    def decode(d, b):
        v = cells.get((d, b), {})
        if {"input_bytes", "t_decode_ns"} <= set(v):
            return v["input_bytes"] / 2**30 / (v["t_decode_ns"] / 1e9)

    label = lambda d: "L={} rho={:g}".format(*shape(d))
    _grid("Synthetic sweep (synthetic): compression ratio by string length L and "
          "repetitiveness rho", "strings", bases,
          [(label(d), [_num(ratio(d, b)) for b in bases]) for d in datasets])
    _grid("Synthetic sweep (synthetic): decompression throughput, GiB/s", "strings", bases,
          [(label(d), [_num(decode(d, b)) for b in bases]) for d in datasets])
    return True


def scale(build_root: Path) -> bool:
    db = _store(build_root, "nsweep_results.sqlite")
    if db is None:
        return False
    rows = db.execute("SELECT baseline, n_rows, status, input_bytes, t_compress_ns "
                      "FROM nsweep_results").fetchall()
    if not rows:
        return False
    cell = {(b, n): (f"{ib / 2**20 / (t / 1e9):.2f}" if status == "ok" and ib and t else status)
            for b, n, status, ib, t in rows}
    size = {n: ib for _, n, _, ib, _ in rows if ib}
    bases = sorted({b for b, _ in cell}, key=lambda x: ORDER.index(x) if x in ORDER else len(ORDER))
    _grid("Scale (scale): compression throughput by column size, MiB/s", "rows",
          ["size"] + bases,
          [(f"{n:,}", [_size(size[n]) if n in size else "--"]
            + [cell.get((b, n), "--") for b in bases]) for n in sorted({n for _, n in cell})])
    return True


def others(build_root: Path) -> list[str]:
    """Studies that have results but no block here."""
    found = []
    if (build_root / "a4_results.sqlite").exists():
        found.append("a4 (whole-operation times and peak memory by capacity; not used by "
                     "the paper): a4_results.sqlite")
    for name, stage in (("decode", "decode"), ("dictorder", "dictorder"),
                        ("bpedecoder", "bpedecoder"), ("prefix", "prefix"),
                        ("finalization", "finalize")):
        if (build_root / name).is_dir() and any((build_root / name).glob("results.*")):
            found.append(f"{stage}: `./reproduce.sh paper` prints its summary")
    return found


def show(build_root: Path) -> int:
    print(f"Results in {build_root}")
    blocks = [main_grid, thresholds, budget, seeds, capacity, model_size, block_size,
              query_skew, synthetic, scale]
    shown = [block(build_root) for block in blocks]
    rest = others(build_root)
    if rest:
        print("\nAlso measured:")
        for line in rest:
            print(f"  {line}")
    if not any(shown) and not rest:
        print(f"show_results: no measurement in {build_root}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build-root", type=Path, default=REPO_ROOT / "build")
    ap.add_argument("--list", action="store_true",
                    help="print the baseline and column names that can be selected")
    a = ap.parse_args()
    if a.list:
        print_names()
        return 0
    return show(a.build_root)


if __name__ == "__main__":
    raise SystemExit(main())
