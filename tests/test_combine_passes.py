"""tools/combine_passes.py: several passes of the benchmark merged into one
build root (pooled main grid, per-cell medians, medians of the JSON timings)."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

from tools import combine_passes, paper_tables

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = paper_tables.COLUMNS[0][0]


def _pass(root: Path, decode_ns: float, p50: float, repair_status: str = "ok") -> Path:
    """One pass: a main-grid store with a single sample, an access store with
    two cells, and a dictionary-order JSON, all carrying the given timings."""
    root.mkdir(parents=True)
    conn = sqlite3.connect(root / "results.sqlite")
    conn.executescript((REPO_ROOT / "schema" / "results.sql").read_text())
    for op, name, value in (("compress", "input_bytes", 2**30), ("bulk_decode", "t_decode_ns", decode_ns)):
        conn.execute(
            "INSERT INTO results (run_id, cell_id, baseline, op, metric_name, metric_value, "
            "status, config_json, dataset, sample_idx, machine_id, baseline_build_flags_sha, "
            "ts_utc) VALUES (?, 'c', 'onpair', ?, ?, ?, 'ok', '{}', ?, 0, 'm', 's', 't')",
            (root.name, op, name, value, DATASET))
    conn.commit()
    conn.close()
    conn = sqlite3.connect(root / "ra_headline_results.sqlite")
    conn.execute("CREATE TABLE ra_headline_results (run_id TEXT, cell_id TEXT PRIMARY KEY, "
                 "baseline TEXT, status TEXT, l_ra_p50_ns REAL, compressed_bytes INTEGER)")
    conn.execute("INSERT INTO ra_headline_results VALUES (?, 'a', 'onpair', 'ok', ?, 7)",
                 (root.name, p50))
    conn.execute("INSERT INTO ra_headline_results VALUES (?, 'b', 'repair', ?, ?, 7)",
                 (root.name, repair_status, p50))
    conn.commit()
    conn.close()
    path = root / "dictorder" / "results.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"datasets": {DATASET: {
        "input_bytes": 10,
        "summary": {"onpair_lexicographic": {"median_decode_ns": decode_ns,
                                             "median_gib_per_second": 1e9 / decode_ns,
                                             "n_samples": 11}}}}}))
    return root


def test_combined_build_root(tmp_path: Path) -> None:
    passes = [_pass(tmp_path / "p1", 3e8, 100.0),
              _pass(tmp_path / "p2", 1e8, 300.0, repair_status="timeout"),
              _pass(tmp_path / "p3", 2e8, 200.0, repair_status="timeout")]
    out = tmp_path / "combined"
    combine_passes.combine(passes, out)

    # main grid: the tools see every pass's rows, so their median is over all three
    stores = paper_tables._result_stores(out)
    assert [p.name for p in stores] == ["results.sqlite", "results_pass2.sqlite",
                                        "results_pass3.sqlite"]
    conns = [sqlite3.connect(p) for p in stores]
    assert paper_tables.med(conns, DATASET, "onpair", "bulk_decode", "t_decode_ns") == 2e8

    # one row per cell: median over the passes in which the cell is ok
    rows = dict(sqlite3.connect(out / "ra_headline_results.sqlite").execute(
        "SELECT cell_id, l_ra_p50_ns FROM ra_headline_results"))
    assert rows == {"a": 200.0, "b": 100.0}       # b is ok in the first pass only
    assert sqlite3.connect(out / "ra_headline_results.sqlite").execute(
        "SELECT DISTINCT compressed_bytes, run_id FROM ra_headline_results").fetchall() == [(7, "p1")]

    # JSON: timings and rates combined, everything else from the first pass
    entry = json.loads((out / "dictorder" / "results.json").read_text())
    stats = entry["datasets"][DATASET]["summary"]["onpair_lexicographic"]
    assert stats == {"median_decode_ns": 2e8, "median_gib_per_second": 5.0, "n_samples": 11}
    assert entry["combined_from_passes"] == 3


def test_single_pass_is_carried_over(tmp_path: Path) -> None:
    only = _pass(tmp_path / "p1", 3e8, 100.0)
    out = tmp_path / "combined"
    combine_passes.combine([only], out)
    assert [p.name for p in paper_tables._result_stores(out)] == ["results.sqlite"]
    assert dict(sqlite3.connect(out / "ra_headline_results.sqlite").execute(
        "SELECT cell_id, l_ra_p50_ns FROM ra_headline_results")) == {"a": 100.0, "b": 100.0}
