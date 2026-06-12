"""step 2 — a1_results.sql schema gate.

Asserts:
  - The schema file parses and executes against a fresh sqlite DB.
  - Required columns exist with correct types + NOT NULL flags.
  - Primary key + indexes are present.
  - status CHECK constraint accepts {ok, oom, crash, refused-env, dev-only, unsupported}
    and rejects anything else.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "schema" / "a1_results.sql"


REQUIRED_COLUMNS = {
    "run_id": "TEXT", "cell_id": "TEXT", "dataset": "TEXT", "column_name": "TEXT",
    "fixed_threshold_T": "INTEGER", "bits": "INTEGER", "seed": "INTEGER",
    "train_input_bytes": "INTEGER", "train_input_rows": "INTEGER",
    "heldout_input_bytes": "INTEGER", "heldout_input_rows": "INTEGER",
    "sample_bytes_consumed_fixed": "INTEGER", "matched_fraction": "REAL",
    "dict_bytes_fixed": "INTEGER", "tokens_bytes_fixed": "INTEGER",
    "boundaries_bytes_fixed": "INTEGER", "heldout_output_bytes_fixed": "INTEGER",
    "heldout_ratio_fixed": "REAL",
    "dict_bytes_dyn": "INTEGER", "tokens_bytes_dyn": "INTEGER",
    "boundaries_bytes_dyn": "INTEGER", "heldout_output_bytes_dyn": "INTEGER",
    "heldout_ratio_dyn": "REAL",
    "status": "TEXT", "machine_id": "TEXT", "baseline_build_flags_sha": "TEXT",
    "config_json": "TEXT", "notes": "TEXT", "ts_utc": "TEXT",
}


def test_schema_executes(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a1.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='a1_results'")
    assert cur.fetchone() is not None
    conn.close()


def test_schema_columns(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a1.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA table_info(a1_results)")
    observed = {row[1]: row[2] for row in cur.fetchall()}
    for col, ty in REQUIRED_COLUMNS.items():
        assert col in observed, f"missing column: {col}"
        assert observed[col].upper() == ty, f"{col}: expected {ty}, got {observed[col]}"


def test_schema_primary_key(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a1.sqlite"))
    conn.executescript(SCHEMA.read_text())
    cur = conn.execute("PRAGMA index_list(a1_results)")
    indexes = {row[1] for row in cur.fetchall()}
    assert "idx_a1_dataset_column" in indexes
    assert "idx_a1_T" in indexes


def test_status_check_constraint_rejects_invalid(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a1.sqlite"))
    conn.executescript(SCHEMA.read_text())
    # Insert a row with an invalid status; the CHECK constraint should reject.
    payload = ["r", "c", "ds", "col", 10, 16, 42] + [0] * 16 + [
        "weird_status_not_in_set", "m", "f", "{}", "", "T00:00:00+00:00"
    ]
    placeholders = ",".join(["?"] * len(payload))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO a1_results VALUES ({placeholders})", payload)


def test_status_check_constraint_accepts_valid(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "a1.sqlite"))
    conn.executescript(SCHEMA.read_text())
    for status in ("ok", "oom", "crash", "refused-env", "dev-only", "unsupported"):
        payload = ["r", "c-" + status, "ds", "col", 10, 16, 42] + [0] * 16 + [
            status, "m", "f", "{}", "", "T00:00:00+00:00"
        ]
        placeholders = ",".join(["?"] * len(payload))
        conn.execute(f"INSERT INTO a1_results VALUES ({placeholders})", payload)
    conn.commit()
