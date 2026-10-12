"""Shared figure/table helpers.

Every F*/T*/A* renderer imports this for: pinned matplotlib rcParams (mirrors
figure_template.py), safe sqlite access
(returns None when a store is absent so `make figures` runs after a partial set
of benchmark runs), derived-quantity helpers (throughput / ratio are NOT runner
metrics — they are derived here from the runners' byte sub-counters and codec
timers), a stable baseline ordering, and CSV+Markdown table writers.

Renderers do not raise on a missing store; they return [] so render_all can skip
cleanly. They DO raise on a malformed-but-present store (a real bug).
"""
from __future__ import annotations
import csv
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
matplotlib.rcParams["svg.fonttype"] = "none"
matplotlib.rcParams["font.family"] = "DejaVu Sans"
matplotlib.rcParams["figure.figsize"] = (6.0, 3.6)
matplotlib.rcParams["figure.dpi"] = 100
import matplotlib.pyplot as plt  # noqa: E402


# Stable left-to-right / legend ordering across every figure (lineup order;
# OnPair last). Unknown baselines sort
# to the end alphabetically.
BASELINE_ORDER = [
    "uncompressed", "zstd", "lz4", "fsst", "fsst_scalar", "repair", "bpe",
    "bpe_v65536", "rlz", "onpair",
]


def baseline_key(name: str):
    return (BASELINE_ORDER.index(name) if name in BASELINE_ORDER
            else len(BASELINE_ORDER), name)


def open_db(path: Path) -> sqlite3.Connection | None:
    """Open a sqlite store read-only; return None if the file is absent."""
    path = Path(path)
    if not path.exists():
        return None
    return sqlite3.connect(str(path))


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def agg_metric(conn: sqlite3.Connection, baseline: str, op: str,
               metric: str, dataset: str | None = None,
               stat: str = "median") -> float | None:
    """One aggregated value from results_agg (headline store)."""
    sql = (f"SELECT {stat} FROM results_agg WHERE baseline=? AND op=? "
           f"AND metric_name=?")
    params: tuple = (baseline, op, metric)
    if dataset is not None:
        sql += " AND dataset=?"
        params = params + (dataset,)
    r = conn.execute(sql, params).fetchone()
    return r[0] if r and r[0] is not None else None


def ratio(conn: sqlite3.Connection, baseline: str,
          dataset: str | None = None) -> float | None:
    """PAYLOAD-ONLY compression ratio  R = input_bytes / (dict_bytes + tokens_bytes).

    This benchmark measures STRING-PAYLOAD compression and
    does NOT compress the per-row string offsets (the (N+1)*4 boundary array that
    every baseline carries verbatim in `boundaries_bytes`). We therefore EXCLUDE
    string offsets from the ratio: the denominator is the compressed model +
    stream only — `dict_bytes` (token content + the baseline's DICTIONARY offsets,
    which ARE part of the model) plus `tokens_bytes` (the compressed token/codeword
    stream). `boundaries_bytes` (string offsets) is dropped. This is symmetric with
    the payload-only decode-throughput numerator.

    Previously the ratio used `output_bytes = dict + tokens + boundaries`, charging
    the uncompressed string offsets as a cost — a different (stricter) choice.

    Uncompressed ⇒ R = 1.0 by identity (its only stored "compressed" bytes are the
    verbatim payload; dict=0, tokens=payload). Falls back to the old `output_bytes`
    denominator for legacy stores that lack the split sub-counters."""
    ib = agg_metric(conn, baseline, "compress", "input_bytes", dataset)
    if ib is None:
        return None
    dict_b   = agg_metric(conn, baseline, "compress", "dict_bytes", dataset)
    tokens_b = agg_metric(conn, baseline, "compress", "tokens_bytes", dataset)
    if dict_b is not None or tokens_b is not None:
        denom = (dict_b or 0.0) + (tokens_b or 0.0)
    else:  # legacy store without split sub-counters
        denom = agg_metric(conn, baseline, "compress", "output_bytes", dataset)
    if denom is None or denom == 0:
        return 1.0 if baseline == "uncompressed" else None
    return ib / denom


def throughput_bps(conn: sqlite3.Connection, baseline: str, op: str,
                   dataset: str | None = None) -> float | None:
    """Derived throughput (bytes/s) = input_bytes / (t · 1e-9) for the op.

    `t` is the runner's INTERNAL codec timer (t_compress_ns / t_decode_ns) — the
    pure in-RAM compress/decode with the output buffer pre-allocated and file I/O
    EXCLUDED. (The old t_op_ns timed the whole subprocess op incl. reading the
    wire and writing the column, so throughput measured the disk, not the codec —
    e.g. uncompressed 'decode' showed ~1 GB/s instead of the ~20 GiB/s memcpy
    floor.)

    The work unit is `input_bytes` = the string PAYLOAD only (col.bytes.size()),
    NOT including the (N+1)*4 per-row string offsets. This is the deliberate
    payload-only counterpart of the payload-only `ratio()` above: this benchmark
    measures string-payload (de)compression and does not compress string offsets,
    so neither metric counts them. Decoders that still reconstruct the offset
    array do so OUTSIDE the work unit (and ideally the payload-only decode path is
    timed alone — see the runner decode ops). Same numerator for compress and
    decode so the two throughputs are directly comparable.
    Falls back to t_op_ns only for legacy stores that predate the internal timers."""
    work = agg_metric(conn, baseline, "compress", "input_bytes", dataset)
    timer = "t_compress_ns" if op == "compress" else "t_decode_ns"
    t_ns = agg_metric(conn, baseline, op, timer, dataset)
    if t_ns is None:  # legacy store without internal codec timers
        t_ns = agg_metric(conn, baseline, op, "t_op_ns", dataset)
    if work is None or t_ns is None or t_ns <= 0:
        return None
    return work / (t_ns * 1e-9)


def baselines_present(conn: sqlite3.Connection) -> list[str]:
    bs = [r[0] for r in conn.execute(
        "SELECT DISTINCT baseline FROM results_agg WHERE op='compress'")]
    return sorted(bs, key=baseline_key)


def datasets_present(conn: sqlite3.Connection) -> list[str]:
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT dataset FROM results_agg WHERE op='compress'"))


def color_for(baseline: str):
    cmap = plt.get_cmap("tab20")
    return cmap(baseline_key(baseline)[0] % 20)


def save(fig, out_dir: Path, name: str) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{name}.png"
    fig.tight_layout()
    fig.savefig(png, dpi=100)
    fig.savefig(out_dir / f"{name}.pdf")
    plt.close(fig)
    return png


def write_table(out_dir: Path, name: str, header: list[str],
                table_rows: list[list]) -> list[Path]:
    """Write a table as both CSV and GitHub-flavored Markdown."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{name}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(table_rows)
    md_path = out_dir / f"{name}.md"
    lines = ["| " + " | ".join(str(h) for h in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for r in table_rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    md_path.write_text("\n".join(lines) + "\n")
    return [csv_path, md_path]


def fmt(x, nd: int = 3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"
