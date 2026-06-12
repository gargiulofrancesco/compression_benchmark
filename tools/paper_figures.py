"""ICDE — paper-quality figures from the result stores.

  F1_pareto.pdf    6 small-multiple panels: ratio (y) vs decode GiB/s (x, log),
                   mark area ~ compression MiB/s. One panel per headline column.
  F4_threshold.pdf (left) A1 fixed-vs-dynamic paired held-out ratio;
                   (right) A3 held-out ratio vs budget f per column.
  F5_latency.pdf   access-latency CDF-ish percentile plot per baseline
                   (needs ra_headline_results.sqlite).
  F6_synthetic.pdf 3x3 heatmap of ratio winner / onpair position
                   (needs vd_results.sqlite).

Usage: PYTHONPATH=. .venv/bin/python -m tools.paper_figures \
         [--build-root build] [--out ../onpair_icde_overleaf/figures]
"""
from __future__ import annotations
import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

def _result_stores(build_root):
    """results.sqlite plus any supplementary results_*.sqlite stores, excluding
    the turbo-bridge and verification stores (different clock / quarantined)."""
    stores = [build_root / "results.sqlite"]
    for p in sorted(build_root.glob("results_*.sqlite")):
        if p.name.startswith(("results_turbo", "results_verify")):
            continue
        stores.append(p)
    return [p for p in stores if p.exists()]


REPO_ROOT = Path(__file__).resolve().parent.parent

COLUMNS = [
    ("dbpedia/abstract", "DBpedia"),
    ("msmarco_urls/url", "URLs"),
    ("msmarco_queries/query", "Query Logs"),
    ("amazon/titles", "Amazon Titles"),
    ("tpch/p_name", "TPC-H p_name"),
    ("tpch/c_comment", "TPC-H c_comment"),
]

STYLE = {
    # baseline: (label, marker, color)
    "lz4":        ("LZ4", "v", "#7f7f7f"),
    "zstd_l1":    ("Zstd-1", "s", "#9edae5"),
    "zstd":       ("Zstd-3", "s", "#1f9bc7"),
    "zstd_l9":    ("Zstd-9", "s", "#17527a"),
    "fsst":       ("FSST", "D", "#2ca02c"),
    "bpe_v65536": ("BPE-64K", "^", "#d62728"),
    "rlz":        ("RLZ", "P", "#9467bd"),
    "repair":     ("Re-Pair", "o", "#8c564b"),
    "onpair":     ("OnPair", "*", "#ff7f0e"),
}

plt.rcParams.update({
    "font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 8,
    "legend.fontsize": 7, "pdf.fonttype": 42, "ps.fonttype": 42,
})


def med(conn, ds, b, op, m):
    conns = conn if isinstance(conn, (list, tuple)) else [conn]
    rows = []
    for c in conns:
        rows += [r[0] for r in c.execute(
            "SELECT metric_value FROM results WHERE dataset=? AND baseline=? AND "
            "op=? AND metric_name=? AND metric_value IS NOT NULL",
            (ds, b, op, m))]
    return statistics.median(rows) if rows else None


def f1(build_root: Path, out: Path) -> None:
    conn = [sqlite3.connect(p) for p in _result_stores(build_root)]
    fig, axes = plt.subplots(2, 3, figsize=(7.0, 4.0), sharey=False)
    for ax, (ds, label) in zip(axes.flat, COLUMNS):
        for b, (blbl, mk, col) in STYLE.items():
            ib = med(conn, ds, b, "compress", "input_bytes")
            db_ = med(conn, ds, b, "compress", "dict_bytes") or 0
            tb = med(conn, ds, b, "compress", "tokens_bytes")
            td = med(conn, ds, b, "bulk_decode", "t_decode_ns")
            tc = med(conn, ds, b, "compress", "t_compress_ns")
            if not (ib and tb and td and tc):
                continue
            ratio = ib / (db_ + tb)
            td_gibs = ib / (td / 1e9) / 2**30
            tc_mibs = ib / (tc / 1e9) / 2**20
            size = 14 + 22 * (tc_mibs / 600) ** 0.5
            ax.scatter(td_gibs, ratio, s=size, marker=mk, color=col,
                       zorder=3 if b == "onpair" else 2,
                       edgecolors="black" if b == "onpair" else "none",
                       linewidths=0.5)
        # LINEAR x (user): the log axis visually compressed the
        # high end and undersold OnPair's 4.4-8.5 GiB/s; on a linear axis the
        # slow third (Re-Pair, zstd) sits honestly at the left edge.
        ax.set_xlim(left=0)
        ax.set_title(label, pad=2)
        ax.grid(alpha=0.25, linewidth=0.4)
        ax.tick_params(length=2)
    for ax in axes[1]:
        ax.set_xlabel("decompression GiB/s")
    for ax in axes[:, 0]:
        ax.set_ylabel("compression ratio")
    handles = [plt.Line2D([], [], linestyle="", marker=mk, color=col,
                          markersize=5, label=lbl)
               for b, (lbl, mk, col) in STYLE.items()]
    fig.legend(handles=handles, ncol=5, loc="lower center",
               bbox_to_anchor=(0.5, -0.04), frameon=False)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out / "F1_pareto.pdf", bbox_inches="tight")
    plt.close(fig)
    print("F1_pareto.pdf written")


def f4(build_root: Path, out: Path) -> None:
    a1 = sqlite3.connect(build_root / "a1_results.sqlite")
    a3 = sqlite3.connect(build_root / "a3_results.sqlite")
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(7.0, 2.3))
    colors = {ds: c for (ds, _), c in zip(
        COLUMNS, ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
                  "#9467bd", "#8c564b"])}
    # left: A1 paired — x = fixed T, y = (dyn - fixed)/fixed %
    for ds, lbl in COLUMNS:
        d, c = ds.split("/")
        rows = a1.execute(
            "SELECT fixed_threshold_T, heldout_ratio_fixed, heldout_ratio_dyn "
            "FROM a1_results WHERE dataset=? AND column_name=? ORDER BY 1",
            (d, c)).fetchall()
        if not rows:
            continue
        xs = [r[0] for r in rows]
        ys = [100.0 * (r[2] - r[1]) / r[1] for r in rows]
        axl.plot(xs, ys, "o-", ms=3, lw=1, color=colors[ds], label=lbl)
    axl.axhline(0, color="black", lw=0.6)
    axl.set_xlabel("fixed threshold $T$")
    axl.set_ylabel("adaptive vs fixed ratio (%)")
    axl.set_xticks([5, 10, 20])
    axl.grid(alpha=0.25, linewidth=0.4)
    # right: A3 budget sweep
    for ds, lbl in COLUMNS:
        d, c = ds.split("/")
        rows = a3.execute(
            "SELECT sample_fraction, heldout_ratio FROM a3_results "
            "WHERE dataset=? AND column_name=? ORDER BY 1", (d, c)).fetchall()
        if not rows:
            continue
        base = rows[0][1]
        axr.plot([r[0] for r in rows],
                 [r[1] / base for r in rows],
                 "o-", ms=3, lw=1, color=colors[ds], label=lbl)
    axr.set_xscale("log")
    axr.set_xticks([0.05, 0.15, 0.3, 0.6, 1.0])
    axr.set_xticklabels(["0.05", "0.15", "0.3", "0.6", "1.0"])
    axr.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    axr.set_xlabel("training budget $f$ (log)")
    axr.set_ylabel("held-out ratio (rel. to $f{=}0.05$)")
    axr.grid(alpha=0.25, linewidth=0.4)
    axr.legend(ncol=2, frameon=False, fontsize=6)
    fig.tight_layout()
    fig.savefig(out / "F4_threshold.pdf", bbox_inches="tight")
    plt.close(fig)
    print("F4_threshold.pdf written")


def f5(build_root: Path, out: Path) -> None:
    db = build_root / "ra_headline_results.sqlite"
    if not db.exists():
        print("F5 skipped (no ra_headline store)")
        return
    conn = sqlite3.connect(db)
    fig, ax = plt.subplots(figsize=(3.45, 2.3))
    # percentile profile per baseline, median across columns
    pcts = [("l_ra_p50_ns", 50), ("l_ra_p95_ns", 95), ("l_ra_p99_ns", 99)]
    for b, (lbl, mk, col) in STYLE.items():
        xs, ys = [], []
        for name, p in pcts:
            vals = [r[0] for r in conn.execute(
                f"SELECT {name} FROM ra_headline_results WHERE baseline=? "
                "AND status='ok' AND " + name + " IS NOT NULL", (b,))]
            if vals:
                xs.append(statistics.median(vals))
                ys.append(p)
        if xs:
            ax.plot(xs, ys, marker=mk, ms=3.5, lw=1, color=col, label=lbl)
    ax.set_xscale("log")
    ax.set_xlabel("access latency (ns, log)")
    ax.set_ylabel("percentile")
    ax.grid(alpha=0.25, linewidth=0.4)
    # legend above the axes: with curves spanning the full x-range there is
    # no interior spot that clears every line at column width
    ax.legend(frameon=False, fontsize=6, ncol=3, loc="lower center",
              bbox_to_anchor=(0.5, 1.0), borderaxespad=0.2,
              columnspacing=1.0, handletextpad=0.4)
    fig.tight_layout()
    fig.savefig(out / "F5_latency.pdf", bbox_inches="tight")
    plt.close(fig)
    print("F5_latency.pdf written")


def f6(build_root: Path, out: Path) -> None:
    db = build_root / "vd_results.sqlite"
    if not db.exists():
        print("F6 skipped (no vd store)")
        return
    conn = sqlite3.connect(db)
    Ls, rhos = [8, 32, 128], [0.0, 0.5, 0.95]
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.1), sharey=True)
    for ax, L in zip(axes, Ls):
        for b, (lbl, mk, col) in STYLE.items():
            xs, ys = [], []
            for rho in rhos:
                ds = f"synthetic_vd/f6__L{L}_rho{rho}"
                ib = med(conn, ds, b, "compress", "input_bytes")
                db_ = med(conn, ds, b, "compress", "dict_bytes") or 0
                tb = med(conn, ds, b, "compress", "tokens_bytes")
                if ib and tb:
                    xs.append(rho)
                    ys.append(ib / (db_ + tb))
            if xs:
                ax.plot(xs, ys, marker=mk, ms=3.5, lw=1, color=col, label=lbl)
        ax.set_title(f"L = {L}")
        ax.set_xlabel(r"repetitiveness $\rho$")
        ax.grid(alpha=0.25, linewidth=0.4)
    axes[0].set_ylabel("compression ratio")
    axes[-1].legend(frameon=False, fontsize=6)
    fig.tight_layout()
    fig.savefig(out / "F6_synthetic.pdf", bbox_inches="tight")
    plt.close(fig)
    print("F6_synthetic.pdf written")


def f7(build_root: Path, out: Path) -> None:
    """F7 — ratio vs MODEL size (payload-only, log x): OnPair capacity curve
    (a2_results; ratio recomputed payload-only = input/(dict+tokens)) vs RLZ
    reference curve (rlz_sweep_results)."""
    a2db = build_root / "a2_results.sqlite"
    rlzdb = build_root / "rlz_sweep2_results.sqlite"
    if not (a2db.exists() and rlzdb.exists()):
        print("F7 skipped (missing a2 or rlz sweep store)")
        return
    a2 = sqlite3.connect(a2db)
    rlz = sqlite3.connect(rlzdb)
    panels = [("msmarco_urls", "url", "URLs"),
              ("tpch", "c_comment", "TPC-H c_comment")]
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.2))
    for ax, (ds, col, label) in zip(axes, panels):
        rows = a2.execute(
            "SELECT bits, dict_bytes, input_bytes, tokens_bytes FROM a2_results "
            "WHERE dataset=? AND column_name=? AND status IN ('ok','dev-only') "
            "ORDER BY bits", (ds, col)).fetchall()
        xs = [r[1] / 1024 for r in rows]
        ys = [r[2] / (r[1] + r[3]) for r in rows]
        ax.plot(xs, ys, "*-", ms=7, lw=1.2, color="#ff7f0e", label="OnPair")
        rxs, rys = [], []
        kibs = ([512, 1024, 2048, 4096, 8192, 16384, 32768]
                if ds == "msmarco_urls"
                else [256, 512, 1024, 2048, 4096, 8192, 16384])
        for kib in kibs:
            cell = f"rlzsweep-{ds}-{col}-r{kib}k"
            def m(op, name, cell=cell):
                v = [r[0] for r in rlz.execute(
                    "SELECT metric_value FROM results WHERE cell_id=? AND op=? "
                    "AND metric_name=? AND metric_value IS NOT NULL",
                    (cell, op, name))]
                return statistics.median(v) if v else None
            ib, db_, tb = (m("compress", "input_bytes"),
                           m("compress", "dict_bytes"),
                           m("compress", "tokens_bytes"))
            if ib and tb:
                rxs.append((db_ or 0) / 1024)
                rys.append(ib / ((db_ or 0) + tb))
        ax.plot(rxs, rys, "P-", ms=5, lw=1.2, color="#9467bd", label="RLZ")
        if rys:
            ax.axhline(max(rys), color="#9467bd", lw=0.6, ls=":")
        ax.set_xscale("log")
        ax.set_title(label, pad=2)
        ax.set_xlabel("model size (KiB, log)")
        ax.grid(alpha=0.25, linewidth=0.4)
    axes[0].set_ylabel("compression ratio")
    axes[0].legend(frameon=False)
    fig.tight_layout()
    fig.savefig(out / "F7_modelsize.pdf", bbox_inches="tight")
    plt.close(fig)
    print("F7_modelsize.pdf written")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-root", type=Path, default=REPO_ROOT / "build")
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT.parent / "onpair_icde_overleaf" / "figures")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    f1(a.build_root, a.out)
    f4(a.build_root, a.out)
    f5(a.build_root, a.out)
    f6(a.build_root, a.out)
    f7(a.build_root, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
