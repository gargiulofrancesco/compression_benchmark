"""The paper's figures from the result stores. File names keep their own
numbering; the figure of the paper each one is follows the name.

  F1_pareto.pdf    Fig. 4. 6 small-multiple panels: ratio (y) vs decode GiB/s (x),
                   mark area ~ compression MiB/s. One panel per headline column.
                   BPE and OnPair get two marks, sorted (filled) and
                   frequency-ordered (hollow) ids, where
                   dictorder/results.json exists; OnPair's
                   filled mark stays the store's number.
  F4_threshold.pdf Fig. 6. (left) A1 fixed-vs-dynamic paired held-out ratio;
                   (right) A3 held-out ratio vs budget f per column.
  F5_latency.pdf   Fig. 5. Access-latency percentile plot per baseline
                   (needs ra_headline_results.sqlite).
  F6_synthetic.pdf Fig. 8. Ratio vs intra-row repetitiveness, one panel per
                   string length (needs vd_results.sqlite).
  F7_modelsize.pdf Fig. 7. Ratio vs model size: OnPair capacity curve vs RLZ
                   reference curve (needs a2_results.sqlite +
                   rlz_sweep2_results.sqlite).

Usage: PYTHONPATH=. .venv/bin/python -m tools.paper_figures \
         [--build-root build] [--out build/paper/figures]
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

def _result_stores(build_root):
    """results.sqlite plus any supplementary results_*.sqlite stores, excluding
    results_turbo* and results_verify* (runs under a different clock setting
    and verification runs, which are not pooled)."""
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
    ("msmarco_queries/query", "Queries"),
    ("amazon/titles", "Amazon"),
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
    "bpe_v65536": ("BPE", "^", "#d62728"),
    "rlz":        ("RLZ", "P", "#9467bd"),
    "repair":     ("Re-Pair", "o", "#8c564b"),
    "onpair":     ("OnPair", "*", "#ff7f0e"),
}

plt.rcParams.update({
    "font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8,
    "legend.fontsize": 8, "pdf.fonttype": 42, "ps.fonttype": 42,
})

# Every figure is drawn at the size it is printed and included at scale 1, so
# the font sizes above are the printed sizes. IEEEtran conference geometry:
COLUMN_WIDTH_IN = 252 / 72   # \linewidth
TEXT_WIDTH_IN = 516 / 72     # \textwidth


def row_major(handles, ncol):
    """Reorder legend handles so the entries read left to right, top to
    bottom (matplotlib fills a multi-column legend column by column)."""
    nrow = -(-len(handles) // ncol)
    order = [r * ncol + c for c in range(ncol) for r in range(nrow)
             if r * ncol + c < len(handles)]
    return [handles[i] for i in order]


def med(conn, ds, b, op, m):
    conns = conn if isinstance(conn, (list, tuple)) else [conn]
    rows = []
    for c in conns:
        rows += [r[0] for r in c.execute(
            "SELECT metric_value FROM results WHERE dataset=? AND baseline=? AND "
            "op=? AND metric_name=? AND metric_value IS NOT NULL",
            (ds, b, op, m))]
    return statistics.median(rows) if rows else None


# Baselines the dictorder stage decodes under both dictionary ID orders, with
# their variant prefix in its results.json.
DICTORDER_CODECS = {"bpe_v65536": "bpe", "onpair": "onpair"}
# Of those, the ones whose own format sorts its ids. Their store number is
# already the sorted-order one, measured like every other row, so it stays
# the first number; BPE's store number uses SentencePiece's own id order.
SORTED_BY_FORMAT = {"onpair"}


def dictorder_decode(build_root):
    """{(dataset, baseline): (sorted-id, frequency-id) decode GiB/s} from the
    dictorder stage; empty on a build without its results.json."""
    path = build_root / "dictorder" / "results.json"
    if not path.exists():
        return {}
    out = {}
    for ds, entry in json.loads(path.read_text())["datasets"].items():
        for b, codec in DICTORDER_CODECS.items():
            out[ds, b] = tuple(
                entry["summary"][f"{codec}_{order}"]["median_gib_per_second"]
                for order in ("lexicographic", "frequency"))
    return out


def f1(build_root: Path, out: Path) -> None:
    stores = _result_stores(build_root)
    if not stores:
        print("F1 skipped (no main-grid store)")
        return
    conn = [sqlite3.connect(p) for p in stores]
    dictorder = dictorder_decode(build_root)
    # included at 0.92\textwidth
    fig, axes = plt.subplots(2, 3, figsize=(0.92 * TEXT_WIDTH_IN, 3.97),
                             sharey=False, layout="constrained")
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
            if (ds, b) in dictorder:
                # BPE and OnPair: filled mark at sorted ids, joined to a
                # hollow mark at frequency-ordered ids
                td_sorted, td_freq = dictorder[ds, b]
                if b not in SORTED_BY_FORMAT:
                    td_gibs = td_sorted
                ax.plot([td_gibs, td_freq], [ratio, ratio], color=col,
                        linewidth=0.8, zorder=1)
                ax.scatter(td_freq, ratio, s=size, marker=mk,
                           facecolors="white", edgecolors=col,
                           linewidths=0.9, zorder=3)
            ax.scatter(td_gibs, ratio, s=size, marker=mk, color=col,
                       zorder=4 if b == "onpair" else 2,
                       edgecolors="black" if b == "onpair" else "none",
                       linewidths=0.5)
        # LINEAR x: a log axis visually compresses the
        # high end (OnPair's 4.4-8.5 GiB/s); on a linear axis the
        # slow third (Re-Pair, zstd) sits at the left edge.
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
    fig.legend(handles=row_major(handles, 5), ncol=5,
               loc="outside lower center", frameon=False)
    fig.savefig(out / "F1_pareto.pdf")
    plt.close(fig)
    print("F1_pareto.pdf written")


def f4(build_root: Path, out: Path) -> None:
    a1db = build_root / "a1_results.sqlite"
    a3db = build_root / "a3_results.sqlite"
    if not (a1db.exists() and a3db.exists()):
        print("F4 skipped (missing a1 or a3 store)")
        return
    a1 = sqlite3.connect(a1db)
    a3 = sqlite3.connect(a3db)
    # included at 0.95\textwidth
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(0.95 * TEXT_WIDTH_IN, 2.21),
                                   layout="constrained")
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
    axl.set_ylabel("adaptive vs. fixed ratio (%)")
    axl.set_xticks([2, 5, 10, 20, 32, 50])
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
    handles, labels = axr.get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=len(labels),
               frameon=False, handlelength=1.6, columnspacing=1.2,
               borderaxespad=0)
    fig.savefig(out / "F4_threshold.pdf")
    plt.close(fig)
    print("F4_threshold.pdf written")


def f5(build_root: Path, out: Path) -> None:
    db = build_root / "ra_headline_results.sqlite"
    if not db.exists():
        print("F5 skipped (no ra_headline store)")
        return
    conn = sqlite3.connect(db)
    fig, ax = plt.subplots(figsize=(COLUMN_WIDTH_IN, 2.29),
                           layout="constrained")
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
    handles, _ = ax.get_legend_handles_labels()
    ax.legend(handles=row_major(handles, 4), frameon=False, ncol=4,
              loc="lower center", bbox_to_anchor=(0.5, 1.0), borderaxespad=0.2,
              columnspacing=1.0, handletextpad=0.4)
    fig.savefig(out / "F5_latency.pdf")
    plt.close(fig)
    print("F5_latency.pdf written")


F6_RHOS = [0.0, 0.5, 0.95]


def draw_synthetic(series, out: Path) -> None:
    """series[L][baseline] = (rhos, ratios); one panel per string length L."""
    fig, axes = plt.subplots(1, len(series), figsize=(COLUMN_WIDTH_IN, 1.45),
                             sharey=True, layout="constrained")
    for ax, (L, by_baseline) in zip(axes, series.items()):
        for b, (xs, ys) in by_baseline.items():
            lbl, mk, col = STYLE[b]
            ax.plot(xs, ys, marker=mk, ms=3.5, lw=1, color=col, label=lbl)
        ax.text(0.06, 0.93, f"L = {L}", transform=ax.transAxes, va="top",
                fontsize=8)
        ax.set_xticks(F6_RHOS, [f"{rho:g}" for rho in F6_RHOS])
        ax.margins(y=0.1)
        ax.grid(alpha=0.25, linewidth=0.4)
    axes[0].yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
    axes[0].set_ylabel("compression ratio")
    fig.supxlabel(r"repetitiveness $\rho$", fontsize=8)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=len(labels),
               frameon=False, handlelength=1.6, columnspacing=1.2,
               borderaxespad=0)
    fig.savefig(out / "F6_synthetic.pdf")
    plt.close(fig)
    print("F6_synthetic.pdf written")


def f6(build_root: Path, out: Path) -> None:
    db = build_root / "vd_results.sqlite"
    if not db.exists():
        print("F6 skipped (no vd store)")
        return
    conn = sqlite3.connect(db)
    series = {}
    for L in [8, 32, 128]:
        series[L] = {}
        for b in STYLE:
            xs, ys = [], []
            for rho in F6_RHOS:
                ds = f"synthetic_vd/f6__L{L}_rho{rho}"
                ib = med(conn, ds, b, "compress", "input_bytes")
                db_ = med(conn, ds, b, "compress", "dict_bytes") or 0
                tb = med(conn, ds, b, "compress", "tokens_bytes")
                if ib and tb:
                    xs.append(rho)
                    ys.append(ib / (db_ + tb))
            if xs:
                series[L][b] = (xs, ys)
    draw_synthetic(series, out)


def draw_modelsize(panels, out: Path) -> None:
    """panels = [(label, (onpair_kib, onpair_ratio), (rlz_kib, rlz_ratio))]."""
    fig, axes = plt.subplots(1, len(panels), figsize=(COLUMN_WIDTH_IN, 1.5),
                             layout="constrained")
    for ax, (label, (xs, ys), (rxs, rys)) in zip(axes, panels):
        ax.plot(xs, ys, "*-", ms=6, lw=1.2, color="#ff7f0e", label="OnPair")
        ax.plot(rxs, rys, "P-", ms=4, lw=1.2, color="#9467bd", label="RLZ")
        if rys:
            ax.axhline(max(rys), color="#9467bd", lw=0.6, ls=":")
        ax.set_xscale("log")
        ax.margins(y=0.12)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 2.5, 5, 10]))
        ax.set_title(label, pad=2)
        ax.grid(alpha=0.25, linewidth=0.4)
    axes[0].set_ylabel("compression ratio")
    fig.supxlabel("model size (KiB, log)", fontsize=8)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=len(labels),
               frameon=False, handlelength=1.6, columnspacing=1.2,
               borderaxespad=0)
    fig.savefig(out / "F7_modelsize.pdf")
    plt.close(fig)
    print("F7_modelsize.pdf written")


def f7(build_root: Path, out: Path) -> None:
    """F7 (Fig. 7) — ratio vs MODEL size (payload-only, log x): OnPair capacity curve
    (a2_results; ratio recomputed payload-only = input/(dict+tokens)) vs RLZ
    reference curve (rlz_sweep2_results)."""
    a2db = build_root / "a2_results.sqlite"
    rlzdb = build_root / "rlz_sweep2_results.sqlite"
    if not (a2db.exists() and rlzdb.exists()):
        print("F7 skipped (missing a2 or rlz sweep store)")
        return
    a2 = sqlite3.connect(a2db)
    rlz = sqlite3.connect(rlzdb)
    panels = []
    for ds, col, label in [("msmarco_urls", "url", "URLs"),
                           ("tpch", "c_comment", "TPC-H c_comment")]:
        rows = a2.execute(
            "SELECT bits, dict_bytes, input_bytes, tokens_bytes FROM a2_results "
            "WHERE dataset=? AND column_name=? AND status IN ('ok','dev-only') "
            "ORDER BY bits", (ds, col)).fetchall()
        xs = [r[1] / 1024 for r in rows]
        ys = [r[2] / (r[1] + r[3]) for r in rows]
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
        panels.append((label, (xs, ys), (rxs, rys)))
    draw_modelsize(panels, out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-root", type=Path, default=REPO_ROOT / "build")
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT / "build" / "paper" / "figures")
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
