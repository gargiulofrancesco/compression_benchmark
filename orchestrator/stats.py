"""Stat engine: median ± MAD primary; mean + 95%-CI secondary.

Aggregates `results` rows into `results_agg` per (run_id, cell_id, baseline, op, metric_name).
"""
from __future__ import annotations
import argparse
import math
import sqlite3
import statistics
from pathlib import Path

try:
    from scipy.stats import t as _t  # type: ignore
    _HAVE_SCIPY = True
except Exception:
    _HAVE_SCIPY = False


def median_mad(values: list[float]) -> tuple[float, float]:
    if not values:
        return (math.nan, math.nan)
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    return (med, mad)


def mean_ci95(values: list[float]) -> tuple[float, float, float]:
    n = len(values)
    if n == 0:
        return (math.nan, math.nan, math.nan)
    if n == 1:
        return (values[0], values[0], values[0])
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    se = sd / math.sqrt(n)
    if _HAVE_SCIPY:
        lo, hi = _t.interval(0.95, df=n - 1, loc=mean, scale=se)
    else:
        # Approximation: t ≈ 1.96 for df ≥ 30; for small n use a table.
        t_val = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
                 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}.get(n - 1, 1.96)
        lo = mean - t_val * se
        hi = mean + t_val * se
    return (mean, lo, hi)


def aggregate(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM results_agg")
    cur = conn.execute("""
        SELECT run_id, cell_id, baseline, op, metric_name, status,
               prefix_search_mode, config_json, dataset, machine_id,
               baseline_build_flags_sha, ts_utc
        FROM results
        GROUP BY run_id, cell_id, baseline, op, metric_name
    """)
    groups = cur.fetchall()
    n_written = 0
    for g in groups:
        (run_id, cell_id, baseline, op, metric_name, status_any,
         psm, cfg, dataset, machine_id, flags_sha, ts) = g
        vals_cur = conn.execute("""
            SELECT metric_value, status FROM results
            WHERE run_id=? AND cell_id=? AND baseline=? AND op=? AND metric_name=?
            ORDER BY sample_idx ASC
        """, (run_id, cell_id, baseline, op, metric_name))
        # dev-only rows carry valid metric values but a "not citable" annotation.
        # We aggregate them so plots render on dev hardware; the agg row inherits
        # the dev-only label so downstream scripts can filter for publication.
        usable = {"ok", "dev-only"}
        ok_values: list[float] = []
        statuses: list[str] = []
        for v, st in vals_cur:
            statuses.append(st)
            if st in usable and v is not None:
                ok_values.append(float(v))
        n_samples = len(statuses)
        med, mad = median_mad(ok_values)
        mean, lo, hi = mean_ci95(ok_values)
        if all(s == "ok" for s in statuses):
            agg_status = "ok"
        elif all(s in usable for s in statuses):
            agg_status = "dev-only"
        else:
            agg_status = next((s for s in statuses if s not in usable), statuses[0])
        conn.execute("""
            INSERT INTO results_agg
              (run_id, cell_id, baseline, op, metric_name, n_samples,
               median, mad, mean, ci95_lo, ci95_hi, status,
               prefix_search_mode, config_json, dataset, machine_id,
               baseline_build_flags_sha, ts_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (run_id, cell_id, baseline, op, metric_name, n_samples,
              med if not math.isnan(med) else None,
              mad if not math.isnan(mad) else None,
              mean if not math.isnan(mean) else None,
              lo if not math.isnan(lo) else None,
              hi if not math.isnan(hi) else None,
              agg_status, psm, cfg, dataset, machine_id, flags_sha, ts))
        n_written += 1
    conn.commit()
    return n_written


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    a = p.parse_args()
    conn = sqlite3.connect(a.db)
    n = aggregate(conn)
    print(f"stats: aggregated {n} rows into results_agg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
