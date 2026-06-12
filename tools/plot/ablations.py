"""— ablation figures/tables: F4 (A1/A3/A7), A2, A4, A5, A6, F5, T3.

Each renderer reads its own ablation store (a*_results.sqlite) and returns the
files it wrote, or [] if the store / its rows are absent — so render_all runs
after any partial set of `make a1..a7` runs. L_ra figures (A5/A6/F5) consider
only status='ok' rows: on a non-x86 box every L_ra cell is 'refused-env' (the
rdtscp path is rig-only), so those figures correctly produce nothing until the
rig run.
"""
from __future__ import annotations
import statistics
from pathlib import Path

from . import _figbase as fb


# ─────────────────────────────── A2 (bit-width) ─────────────────────────────
def render_a2(db_path: Path, out_dir: Path) -> list[Path]:
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "a2_results"):
        return []
    data = fb.rows(conn, "SELECT dataset, column_name, bits, ratio, decode_ns, "
                         "dict_bytes FROM a2_results WHERE status='ok' ORDER BY bits")
    conn.close()
    if not data:
        return []
    fig, (axR, axD) = fb.plt.subplots(1, 2, figsize=(9.0, 3.6))
    keys = sorted({(d["dataset"], d["column_name"]) for d in data})
    for ds, col in keys:
        pts = [d for d in data if d["dataset"] == ds and d["column_name"] == col]
        bits = [p["bits"] for p in pts]
        axR.plot(bits, [p["ratio"] for p in pts], marker="o", label=f"{ds}/{col}")
        axD.plot(bits, [(p["dict_bytes"] or 0) / 1e6 for p in pts], marker="o")
    axR.set_xlabel("bit-width b"); axR.set_ylabel("compression ratio R")
    axD.set_xlabel("bit-width b"); axD.set_ylabel("dictionary |D| (MB)")
    axR.set_title("A2 — ratio vs bit-width"); axD.set_title("A2 — |D| vs bit-width")
    for ax in (axR, axD):
        ax.grid(True, alpha=0.3)
    if len(keys) <= 8:
        axR.legend(fontsize=6)
    return [fb.save(fig, out_dir, "A2_bitwidth")]


# ─────────────────────────────── A4 (capacity) ──────────────────────────────
def render_a4(db_path: Path, out_dir: Path) -> list[Path]:
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "a4_results"):
        return []
    data = fb.rows(conn, "SELECT dataset, column_name, capacity, ratio, decode_ns, "
                         "m_train_bytes FROM a4_results WHERE status='ok' ORDER BY capacity")
    conn.close()
    if not data:
        return []
    fig, (axR, axM) = fb.plt.subplots(1, 2, figsize=(9.0, 3.6))
    keys = sorted({(d["dataset"], d["column_name"]) for d in data})
    for ds, col in keys:
        pts = [d for d in data if d["dataset"] == ds and d["column_name"] == col]
        cap = [p["capacity"] for p in pts]
        axR.plot(cap, [p["ratio"] for p in pts], marker="o", label=f"{ds}/{col}")
        axM.plot(cap, [(p["m_train_bytes"] or 0) / 1e6 for p in pts], marker="o")
    for ax in (axR, axM):
        ax.set_xscale("log", base=2); ax.set_xlabel("capacity (dict entries)"); ax.grid(True, alpha=0.3)
    axR.set_ylabel("compression ratio R"); axM.set_ylabel("M_train peak (MB)")
    axR.set_title("A4 — ratio vs capacity"); axM.set_title("A4 — train memory vs capacity")
    if len(keys) <= 8:
        axR.legend(fontsize=6)
    return [fb.save(fig, out_dir, "A4_capacity")]


# ─────────────────────────── A5 (block size vs L_ra) ────────────────────────
def render_a5(db_path: Path, out_dir: Path) -> list[Path]:
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "a5_results"):
        return []
    data = fb.rows(conn, "SELECT baseline, block_size_strings, cache_state, "
                         "l_ra_p50_ns, l_ra_p95_ns, l_ra_p99_ns FROM a5_results "
                         "WHERE status='ok' ORDER BY block_size_strings")
    conn.close()
    if not data:
        return []
    states = sorted({d["cache_state"] for d in data})
    fig, axes = fb.plt.subplots(1, len(states), figsize=(4.6 * len(states), 3.6),
                                squeeze=False)
    for ax, state in zip(axes[0], states):
        for b in sorted({d["baseline"] for d in data}, key=fb.baseline_key):
            pts = [d for d in data if d["baseline"] == b and d["cache_state"] == state]
            if not pts:
                continue
            xs = [p["block_size_strings"] or 0 for p in pts]
            ax.plot(xs, [p["l_ra_p50_ns"] for p in pts], marker="o",
                    color=fb.color_for(b), label=b)
            ax.fill_between(xs, [p["l_ra_p50_ns"] for p in pts],
                            [p["l_ra_p99_ns"] for p in pts],
                            color=fb.color_for(b), alpha=0.12)
        ax.set_xlabel("block size (strings)"); ax.set_ylabel("L_ra (ns)")
        ax.set_title(f"A5 — L_ra vs block size ({state})")
        ax.set_xscale("log", base=2); ax.grid(True, alpha=0.3); ax.legend(fontsize=6)
    return [fb.save(fig, out_dir, "A5_blocksize")]


# ──────────────────────── A6 (query distribution vs L_ra) ───────────────────
def render_a6(db_path: Path, out_dir: Path) -> list[Path]:
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "a6_results"):
        return []
    data = fb.rows(conn, "SELECT baseline, query_distribution, query_layout, "
                         "cache_state, l_ra_p50_ns, l_ra_p99_ns FROM a6_results "
                         "WHERE status='ok'")
    conn.close()
    if not data:
        return []
    layouts = sorted({d["query_layout"] for d in data})
    dists = sorted({d["query_distribution"] for d in data})
    fig, axes = fb.plt.subplots(1, len(layouts), figsize=(4.8 * len(layouts), 3.6),
                                squeeze=False)
    for ax, layout in zip(axes[0], layouts):
        baselines = sorted({d["baseline"] for d in data}, key=fb.baseline_key)
        width = 0.8 / max(len(baselines), 1)
        for i, b in enumerate(baselines):
            ys = []
            for dist in dists:
                vals = [d["l_ra_p50_ns"] for d in data if d["baseline"] == b
                        and d["query_layout"] == layout and d["query_distribution"] == dist]
                ys.append(statistics.median(vals) if vals else 0.0)
            xs = [j + i * width for j in range(len(dists))]
            ax.bar(xs, ys, width=width, color=fb.color_for(b), label=b)
        ax.set_xticks([j + 0.4 for j in range(len(dists))])
        ax.set_xticklabels(dists, rotation=20, fontsize=7)
        ax.set_ylabel("L_ra p50 (ns)"); ax.set_title(f"A6 — L_ra vs distribution ({layout})")
        ax.grid(True, axis="y", alpha=0.3); ax.legend(fontsize=6)
    return [fb.save(fig, out_dir, "A6_distribution")]


# ─────────────────────────────── F5 (L_ra CDF) ──────────────────────────────
def render_f5(db_path: Path, out_dir: Path) -> list[Path]:
    """F5 — per-baseline L_ra CDF, interpolated through the stored percentiles
    (min, p50, p95, p99, max). Built from the A6 uniform/identity/warm slice."""
    conn = fb.open_db(db_path)
    if conn is None or not fb.has_table(conn, "a6_results"):
        return []
    data = fb.rows(conn, "SELECT baseline, l_ra_min_ns, l_ra_p50_ns, l_ra_p95_ns, "
                         "l_ra_p99_ns, l_ra_max_ns FROM a6_results WHERE status='ok' "
                         "AND query_distribution='uniform' AND query_layout='identity' "
                         "AND cache_state='warm'")
    conn.close()
    if not data:
        return []
    fig, ax = fb.plt.subplots()
    probs = [0.0, 0.5, 0.95, 0.99, 1.0]
    for d in sorted(data, key=lambda r: fb.baseline_key(r["baseline"])):
        xs = [d["l_ra_min_ns"], d["l_ra_p50_ns"], d["l_ra_p95_ns"],
              d["l_ra_p99_ns"], d["l_ra_max_ns"]]
        if any(v is None for v in xs):
            continue
        ax.plot(xs, probs, marker="o", color=fb.color_for(d["baseline"]),
                label=d["baseline"])
    ax.set_xlabel("L_ra (ns)"); ax.set_ylabel("cumulative probability")
    ax.set_title("F5 — random-access latency CDF (percentile-interpolated)")
    ax.grid(True, alpha=0.3); ax.legend(fontsize=6)
    return [fb.save(fig, out_dir, "F5_latency_cdf")]


# ───────────────────────── F4 (A1 / A3 / A7 threshold) ──────────────────────
def render_f4(a1_db: Path, a3_db: Path, a7_db: Path, out_dir: Path) -> list[Path]:
    a1 = fb.open_db(a1_db); a3 = fb.open_db(a3_db); a7 = fb.open_db(a7_db)
    have = []
    if a1 and fb.has_table(a1, "a1_results"):
        have.append("a1")
    if a3 and fb.has_table(a3, "a3_results"):
        have.append("a3")
    if a7 and fb.has_table(a7, "a7_results"):
        have.append("a7")
    if not have:
        return []
    fig, axes = fb.plt.subplots(1, 3, figsize=(12.0, 3.6))
    axA1, axA3, axA7 = axes

    if a1 and "a1" in have:
        rows = fb.rows(a1, "SELECT column_name, heldout_ratio_fixed, heldout_ratio_dyn "
                           "FROM a1_results WHERE status='ok'")
        if rows:
            xs = range(len(rows))
            axA1.bar([x - 0.2 for x in xs], [r["heldout_ratio_fixed"] for r in rows],
                     width=0.4, label="Fixed{T}")
            axA1.bar([x + 0.2 for x in xs], [r["heldout_ratio_dyn"] for r in rows],
                     width=0.4, label="matched-Dynamic")
            axA1.set_xticks(list(xs))
            axA1.set_xticklabels([r["column_name"] for r in rows], rotation=30, fontsize=6)
            axA1.legend(fontsize=7)
    axA1.set_ylabel("held-out ratio R"); axA1.set_title("A1 — Fixed vs matched-Dynamic")
    axA1.grid(True, axis="y", alpha=0.3)

    if a3 and "a3" in have:
        rows = fb.rows(a3, "SELECT column_name, sample_fraction, heldout_ratio "
                           "FROM a3_results WHERE status='ok' ORDER BY sample_fraction")
        for col in sorted({r["column_name"] for r in rows}):
            pts = [r for r in rows if r["column_name"] == col]
            axA3.plot([p["sample_fraction"] for p in pts],
                      [p["heldout_ratio"] for p in pts], marker="o", label=col)
        if rows and len({r["column_name"] for r in rows}) <= 8:
            axA3.legend(fontsize=6)
    axA3.set_xlabel("sample_fraction"); axA3.set_ylabel("held-out ratio R")
    axA3.set_title("A3 — ratio vs sample_fraction"); axA3.grid(True, alpha=0.3)

    if a7 and "a7" in have:
        rows = fb.rows(a7, "SELECT dataset, column_name, seed, ratio FROM a7_results "
                           "WHERE status='ok' ORDER BY seed")
        for ds, col in sorted({(r["dataset"], r["column_name"]) for r in rows}):
            pts = [r for r in rows if r["dataset"] == ds and r["column_name"] == col]
            axA7.plot([str(p["seed"]) for p in pts], [p["ratio"] for p in pts],
                      marker="o", label=f"{ds}/{col}")
        if rows:
            axA7.legend(fontsize=6)
    axA7.set_xlabel("shuffle seed"); axA7.set_ylabel("ratio R")
    axA7.set_title("A7 — seed sensitivity"); axA7.grid(True, alpha=0.3)
    fb.plt.setp(axA7.get_xticklabels(), rotation=20, fontsize=6)

    for c in (a1, a3, a7):
        if c:
            c.close()
    return [fb.save(fig, out_dir, "F4_threshold")]


# ──────────────────────── T3 (ablation summary cross-tab) ───────────────────
def render_t3(a2_db: Path, a4_db: Path, a5_db: Path, a7_db: Path,
              out_dir: Path) -> list[Path]:
    """T3 — per-column summary of A2 + A4 + A5 + A7 (plan T3 cross-tab). Pulls
    one headline number from each ablation per column; '—' where a store/row is
    absent so the table renders on partial runs."""
    def col_map(db, table, sql):
        conn = fb.open_db(db)
        if conn is None or not fb.has_table(conn, table):
            return {}
        out = {r["column_name"]: r for r in fb.rows(conn, sql)}
        conn.close()
        return out

    a2 = col_map(a2_db, "a2_results",
                 "SELECT column_name, MAX(ratio) AS best_ratio FROM a2_results "
                 "WHERE status='ok' GROUP BY column_name")
    a4 = col_map(a4_db, "a4_results",
                 "SELECT column_name, MAX(ratio) AS best_ratio FROM a4_results "
                 "WHERE status='ok' GROUP BY column_name")
    a7 = col_map(a7_db, "a7_results",
                 "SELECT column_name, MAX(ratio)-MIN(ratio) AS seed_spread "
                 "FROM a7_results WHERE status='ok' GROUP BY column_name")
    # A5 has no column_name pivot identical to others (it carries baseline);
    # summarise OnPair p50 at the largest block per column instead.
    a5 = {}
    conn = fb.open_db(a5_db)
    if conn is not None and fb.has_table(conn, "a5_results"):
        a5 = {r["column_name"]: r for r in fb.rows(
            conn, "SELECT column_name, MIN(l_ra_p50_ns) AS onpair_p50 FROM a5_results "
                  "WHERE status='ok' AND baseline='onpair' GROUP BY column_name")}
        conn.close()

    cols = sorted(set(a2) | set(a4) | set(a5) | set(a7))
    if not cols:
        return []
    header = ["column", "A2 best R", "A4 best R", "A5 onpair L_ra p50 (ns)", "A7 seed ΔR"]
    table_rows = []
    for c in cols:
        table_rows.append([
            c,
            fb.fmt(a2.get(c, {}).get("best_ratio")),
            fb.fmt(a4.get(c, {}).get("best_ratio")),
            fb.fmt(a5.get(c, {}).get("onpair_p50"), 1),
            fb.fmt(a7.get(c, {}).get("seed_spread"), 4),
        ])
    return fb.write_table(out_dir, "T3_ablation_crosstab", header, table_rows)
