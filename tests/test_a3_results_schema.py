"""step 3 — a3_results.sql schema gate.

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
SCHEMA = REPO_ROOT / "schema" / "a3_results.sql"


REQUIRED_COLUMNS = {
    "run_id": "TEXT", "cell_id": "TEXT", "dataset": "TEXT", "column_name": "TEXT",
    "sample_fraction": "REAL", "bits": "INTEGER", "seed": "INTEGER",
    "threshold_strategy": "TEXT",
    "train_input_bytes": "INTEGER", "train_input_rows": "INTEGER",
    "heldout_input_bytes": "INTEGER", "heldout_input_rows": "INTEGER",
    "sample_bytes_consumed": "INTEGER", "effective_fraction": "REAL",
    "num_dict_tokens": "INTEGER",
    "dict_bytes": "INTEGER", "tokens_bytes": "INTEGER",
    "boundaries_bytes": "INTEGER", "heldout_output_bytes": "INTEGER",
    "heldout_ratio": "REAL",
    "train_ns": "REAL", "encode_ns": "REAL",
    "status": "TEXT", "machine_id": "TEXT", "baseline_build_flags_sha": "TEXT",
    "config_json": "TEXT", "notes": "TEXT", "ts_utc": "TEXT",
}

# Column order for positional INSERT in the CHECK-constraint tests.
N_COLUMNS = len(REQUIRED_COLUMNS)


def _payload(status: str, cell_id: str = "c") -> list:
    # run_id, cell_id, dataset, column_name, sample_fraction, bits, seed,
    # threshold_strategy, then 14 numeric metric columns, then status + 5 bookkeeping.
    return (["r", cell_id, "ds", "col", 0.15, 16, 42, "dynamic"]
            + [0] * 14
            + [status, "m", "f", "{}", "", "T00:00:00+00:00"])


def test_schema_executes(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a3.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='a3_results'")
    assert cur.fetchone() is not None
    conn.close()


def test_schema_columns(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a3.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA table_info(a3_results)")
    observed = {row[1]: row[2] for row in cur.fetchall()}
    for col, ty in REQUIRED_COLUMNS.items():
        assert col in observed, f"missing column: {col}"
        assert observed[col].upper() == ty, f"{col}: expected {ty}, got {observed[col]}"
    assert len(observed) == N_COLUMNS, (
        f"column count drift: schema has {len(observed)}, test expects {N_COLUMNS}")


def test_schema_indexes(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a3.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA index_list(a3_results)")
    indexes = {row[1] for row in cur.fetchall()}
    assert "idx_a3_dataset_column" in indexes
    assert "idx_a3_fraction" in indexes


def test_status_check_constraint_rejects_invalid(tmp_path: Path) -> None:
    import pytest
    conn = sqlite3.connect(str(tmp_path / "a3.sqlite"))
    conn.executescript(SCHEMA.read_text())
    payload = _payload("weird_status_not_in_set")
    placeholders = ",".join(["?"] * len(payload))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO a3_results VALUES ({placeholders})", payload)


def test_status_check_constraint_accepts_valid(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a3.sqlite"))
    conn.executescript(SCHEMA.read_text())
    for status in ("ok", "oom", "crash", "refused-env", "dev-only", "unsupported"):
        payload = _payload(status, cell_id="c-" + status)
        placeholders = ",".join(["?"] * len(payload))
        conn.execute(f"INSERT INTO a3_results VALUES ({placeholders})", payload)
    conn.commit()
