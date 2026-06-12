import sqlite3
import tempfile
from pathlib import Path

from orchestrator.store import open_store, insert_rows, export_csv, ResultRow


def _row(**kw):
    base = dict(
        run_id="r", cell_id="c", baseline="uncompressed", op="compress",
        metric_name="dict_bytes", metric_value=0.0, status="ok",
        prefix_search_mode=None, config_json="{}", dataset="d",
        sample_idx=0, machine_id="m", baseline_build_flags_sha="s",
        notes="", ts_utc="T00:00:00+00:00",
    )
    base.update(kw)
    return ResultRow(**base)


def test_schema_applies_clean(tmp_path: Path):
    db = tmp_path / "results.sqlite"
    conn = open_store(db)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = sorted([r[0] for r in cur])
    assert "results" in tables
    assert "results_agg" in tables


def test_insert_and_csv_export(tmp_path: Path):
    db = tmp_path / "results.sqlite"
    conn = open_store(db)
    rows = [_row(sample_idx=i, metric_value=float(i)) for i in range(3)]
    insert_rows(conn, rows)
    out = tmp_path / "results.csv"
    n = export_csv(conn, "results", out)
    assert n == 3
    assert "metric_value" in out.read_text().splitlines()[0]


def test_op_check_rejects_garbage(tmp_path: Path):
    db = tmp_path / "results.sqlite"
    conn = open_store(db)
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        insert_rows(conn, [_row(op="UNKNOWN_OP")])
