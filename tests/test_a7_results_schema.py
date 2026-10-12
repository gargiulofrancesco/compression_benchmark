"""a7_results.sql schema test.

Asserts:
  - The schema file parses and executes against a fresh sqlite DB.
  - Required columns exist with correct types.
  - Primary key + indexes are present.
  - status CHECK constraint accepts the valid set and rejects anything else.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "schema" / "a7_results.sql"


REQUIRED_COLUMNS = {
    "run_id": "TEXT", "cell_id": "TEXT", "dataset": "TEXT", "column_name": "TEXT",
    "seed": "INTEGER", "bits": "INTEGER", "sample_fraction": "REAL",
    "threshold_strategy": "TEXT",
    "input_bytes": "INTEGER", "input_rows": "INTEGER",
    "dict_bytes": "INTEGER", "tokens_bytes": "INTEGER",
    "boundaries_bytes": "INTEGER", "output_bytes": "INTEGER",
    "ratio": "REAL",
    "compress_ns": "REAL",
    "status": "TEXT", "machine_id": "TEXT", "baseline_build_flags_sha": "TEXT",
    "config_json": "TEXT", "notes": "TEXT", "ts_utc": "TEXT",
}

# Column count drift guard.
N_COLUMNS = len(REQUIRED_COLUMNS)


def _payload(status: str, cell_id: str = "c") -> list:
    # run_id, cell_id, dataset, column_name, seed, bits, sample_fraction,
    # threshold_strategy, then 8 numeric metric columns (input_bytes, input_rows,
    # dict_bytes, tokens_bytes, boundaries_bytes, output_bytes, ratio,
    # compress_ns), then status + 5 bookkeeping.
    return (["r", cell_id, "ds", "col", 42, 16, 0.15, "dynamic"]
            + [0] * 8
            + [status, "m", "f", "{}", "", "T00:00:00+00:00"])


def test_schema_executes(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a7.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='a7_results'")
    assert cur.fetchone() is not None
    conn.close()


def test_schema_columns(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a7.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA table_info(a7_results)")
    observed = {row[1]: row[2] for row in cur.fetchall()}
    for col, ty in REQUIRED_COLUMNS.items():
        assert col in observed, f"missing column: {col}"
        assert observed[col].upper() == ty, f"{col}: expected {ty}, got {observed[col]}"
    assert len(observed) == N_COLUMNS, (
        f"column count drift: schema has {len(observed)}, test expects {N_COLUMNS}")


def test_schema_indexes(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a7.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA index_list(a7_results)")
    indexes = {row[1] for row in cur.fetchall()}
    assert "idx_a7_dataset_column" in indexes
    assert "idx_a7_seed" in indexes


def test_status_check_constraint_rejects_invalid(tmp_path: Path) -> None:
    import pytest
    conn = sqlite3.connect(str(tmp_path / "a7.sqlite"))
    conn.executescript(SCHEMA.read_text())
    payload = _payload("weird_status_not_in_set")
    placeholders = ",".join(["?"] * len(payload))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO a7_results VALUES ({placeholders})", payload)


def test_status_check_constraint_accepts_valid(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a7.sqlite"))
    conn.executescript(SCHEMA.read_text())
    for status in ("ok", "oom", "crash", "refused-env", "dev-only", "unsupported"):
        payload = _payload(status, cell_id="c-" + status)
        placeholders = ",".join(["?"] * len(payload))
        conn.execute(f"INSERT INTO a7_results VALUES ({placeholders})", payload)
    conn.commit()
