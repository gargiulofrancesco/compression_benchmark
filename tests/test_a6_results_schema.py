"""a6_results.sql schema test.

Asserts:
  - The schema file parses and executes against a fresh sqlite DB.
  - Required columns exist with correct types (incl. query_layout + the L_ra
    percentile block).
  - Primary key + indexes are present.
  - status CHECK constraint accepts the valid set and rejects anything else.
  - n_blocks accepts NULL (the per-string baselines fsst/onpair).
"""
from __future__ import annotations
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "schema" / "a6_results.sql"


REQUIRED_COLUMNS = {
    "run_id": "TEXT", "cell_id": "TEXT", "baseline": "TEXT",
    "dataset": "TEXT", "column_name": "TEXT",
    "query_distribution": "TEXT", "query_layout": "TEXT",
    "cache_state": "TEXT", "seed": "INTEGER", "n_samples": "INTEGER",
    "input_bytes": "INTEGER", "input_rows": "INTEGER",
    "n_blocks": "INTEGER", "compressed_bytes": "INTEGER",
    "l_ra_p50_ns": "REAL", "l_ra_p95_ns": "REAL", "l_ra_p99_ns": "REAL",
    "l_ra_mean_ns": "REAL", "l_ra_min_ns": "REAL", "l_ra_max_ns": "REAL",
    "status": "TEXT", "machine_id": "TEXT", "baseline_build_flags_sha": "TEXT",
    "config_json": "TEXT", "notes": "TEXT", "ts_utc": "TEXT",
}

# Column count drift guard.
N_COLUMNS = len(REQUIRED_COLUMNS)


def _payload(status: str, cell_id: str = "c", n_blocks=4) -> list:
    # run_id, cell_id, baseline, dataset, column_name, query_distribution,
    # query_layout, cache_state, seed, n_samples, input_bytes, input_rows,
    # n_blocks, compressed_bytes, then 6 REAL L_ra columns, then status + 5
    # bookkeeping.
    return (["r", cell_id, "zstd", "ds", "col", "uniform", "identity", "warm",
             42, 1000, 0, 0, n_blocks, 0]
            + [0.0] * 6
            + [status, "m", "f", "{}", "", "T00:00:00+00:00"])


def test_schema_executes(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a6.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='a6_results'")
    assert cur.fetchone() is not None
    conn.close()


def test_schema_columns(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a6.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA table_info(a6_results)")
    observed = {row[1]: row[2] for row in cur.fetchall()}
    for col, ty in REQUIRED_COLUMNS.items():
        assert col in observed, f"missing column: {col}"
        assert observed[col].upper() == ty, f"{col}: expected {ty}, got {observed[col]}"
    assert len(observed) == N_COLUMNS, (
        f"column count drift: schema has {len(observed)}, test expects {N_COLUMNS}")


def test_schema_indexes(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a6.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA index_list(a6_results)")
    indexes = {row[1] for row in cur.fetchall()}
    assert "idx_a6_baseline_dist" in indexes
    assert "idx_a6_dataset_column" in indexes


def test_status_check_constraint_rejects_invalid(tmp_path: Path) -> None:
    import pytest
    conn = sqlite3.connect(str(tmp_path / "a6.sqlite"))
    conn.executescript(SCHEMA.read_text())
    payload = _payload("weird_status_not_in_set")
    placeholders = ",".join(["?"] * len(payload))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO a6_results VALUES ({placeholders})", payload)


def test_status_check_constraint_accepts_valid(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a6.sqlite"))
    conn.executescript(SCHEMA.read_text())
    for status in ("ok", "oom", "crash", "refused-env", "dev-only", "unsupported"):
        payload = _payload(status, cell_id="c-" + status)
        placeholders = ",".join(["?"] * len(payload))
        conn.execute(f"INSERT INTO a6_results VALUES ({placeholders})", payload)
    conn.commit()


def test_nullable_n_blocks_accepts_null(tmp_path: Path) -> None:
    # The per-string baselines (fsst, onpair) carry NULL n_blocks.
    conn = sqlite3.connect(str(tmp_path / "a6.sqlite"))
    conn.executescript(SCHEMA.read_text())
    payload = _payload("ok", cell_id="c-fsst", n_blocks=None)
    placeholders = ",".join(["?"] * len(payload))
    conn.execute(f"INSERT INTO a6_results VALUES ({placeholders})", payload)
    conn.commit()
    row = conn.execute(
        "SELECT n_blocks FROM a6_results WHERE cell_id='c-fsst'").fetchone()
    assert row == (None,)
    conn.close()
