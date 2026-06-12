"""— headline figures/tables (§8.2): F1 Pareto + T1 ratio + T2 throughput.

Reads the headline store (build/results.sqlite, produced by `make bench`). All
three honor the lossless guard (orchestrator.modes.assert_lossless_only) before
any cross-baseline rendering — any baseline flagged is_lossless: false must never
appear in a headline artifact. Throughput and ratio are DERIVED here (see
tools.plot._figbase): ratio from input_bytes / (dict_bytes + tokens_bytes), and
throughput from input_bytes / the runner's codec-only t_compress_ns / t_decode_ns
timers (I/O excluded; t_op_ns is only a legacy fallback). Runners emit the timers
and byte sub-counters; the derived ratio/throughput are computed here.

Each renderer returns the list of files it wrote, or [] if the store / table is
absent (so render_all can skip cleanly on a partial run).
"""
from __future__ import annotations
import statistics
from pathlib import Path

from . import _figbase as fb

try:  # package vs script import
    from orchestrator.modes import lossless_baselines, assert_lossless_only
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from orchestrator.modes import lossless_baselines, assert_lossless_only


REPO_ROOT = Path(__file__).resolve().parents[2]
MODES = REPO_ROOT / "experiments" / "baseline_modes.yaml"


def _guard(conn) -> list[str]:
    baselines = fb.baselines_present(conn)
    assert_lossless_only(baselines, lossless_baselines(MODES), "headline figures")
    return baselines


def render_f1(db_path: Path, out_dir: Path) -> list[Path]:
    """F1 — ratio vs decode throughput Pareto. One faint point per
    (baseline, column); one bold labelled point at the per-baseline median."""
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "results_agg"):
        return []
    baselines = _guard(conn)
    datasets = fb.datasets_present(conn)
    if not baselines or not datasets:
        return []

    fig, ax = fb.plt.subplots()
    for b in baselines:
        rs, tps = [], []
        for ds in datasets:
            r = fb.ratio(conn, b, ds)
            tp = fb.throughput_bps(conn, b, "bulk_decode", ds)
            if r is not None and tp is not None:
                rs.append(r); tps.append(tp / 1e9)  # GB/s
        if not rs:
            continue
        c = fb.color_for(b)
        ax.scatter(rs, tps, color=c, alpha=0.30, s=18)
        mr, mt = statistics.median(rs), statistics.median(tps)
        ax.scatter([mr], [mt], color=c, s=70, edgecolor="black", zorder=3, label=b)
        ax.annotate(b, (mr, mt), fontsize=7, xytext=(3, 3),
                    textcoords="offset points")
    ax.set_xlabel("compression ratio  R = input / output  (higher = smaller)")
    ax.set_ylabel("decode throughput (GB/s)")
    ax.set_title("F1 — headline ratio vs decode-throughput Pareto")
    ax.grid(True, alpha=0.3)
    conn.close()
    return [fb.save(fig, out_dir, "F1_pareto")]


def _pivot_table(conn, baselines, datasets, value_fn, name, out_dir):
    header = ["baseline"] + datasets + ["median"]
    table_rows = []
    for b in baselines:
        vals = [value_fn(conn, b, ds) for ds in datasets]
        present = [v for v in vals if v is not None]
        med = statistics.median(present) if present else None
        table_rows.append([b] + [fb.fmt(v) for v in vals] + [fb.fmt(med)])
    return fb.write_table(out_dir, name, header, table_rows)


def render_t1(db_path: Path, out_dir: Path) -> list[Path]:
    """T1 — compression ratio R per (baseline × column)."""
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "results_agg"):
        return []
    baselines, datasets = _guard(conn), fb.datasets_present(conn)
    if not baselines or not datasets:
        return []
    out = _pivot_table(conn, baselines, datasets, fb.ratio, "T1_ratio", out_dir)
    conn.close()
    return out


def render_t2(db_path: Path, out_dir: Path) -> list[Path]:
    """T2 — compress + decode throughput (GB/s) per (baseline × column)."""
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "results_agg"):
        return []
    baselines, datasets = _guard(conn), fb.datasets_present(conn)
    if not baselines or not datasets:
        return []

    def comp_tp(c, b, ds):
        v = fb.throughput_bps(c, b, "compress", ds)
        return v / 1e9 if v is not None else None

    def dec_tp(c, b, ds):
        v = fb.throughput_bps(c, b, "bulk_decode", ds)
        return v / 1e9 if v is not None else None

    out = _pivot_table(conn, baselines, datasets, comp_tp, "T2_compress_throughput_gbps", out_dir)
    out += _pivot_table(conn, baselines, datasets, dec_tp, "T2_decode_throughput_gbps", out_dir)
    conn.close()
    return out


def render_all(db_path: Path, out_dir: Path) -> list[Path]:
    return (render_f1(db_path, out_dir) + render_t1(db_path, out_dir)
            + render_t2(db_path, out_dir))
