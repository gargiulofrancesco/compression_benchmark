"""SQLite + CSV result store."""
from __future__ import annotations
import csv
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_PATH_DEFAULT = Path(__file__).resolve().parent.parent / "schema" / "results.sql"


@dataclass
class ResultRow:
    run_id: str
    cell_id: str
    baseline: str
    op: str
    metric_name: str
    metric_value: Optional[float]
    status: str
    prefix_search_mode: Optional[str]
    config_json: str
    dataset: str
    sample_idx: int
    machine_id: str
    baseline_build_flags_sha: str
    notes: str
    ts_utc: str


def apply_schema(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH_DEFAULT) -> None:
    conn.executescript(schema_path.read_text())
    conn.commit()


def open_store(db_path: Path, schema_path: Path = SCHEMA_PATH_DEFAULT) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    apply_schema(conn, schema_path)
    return conn


_RESULTS_COLS = [
    "run_id", "cell_id", "baseline", "op", "metric_name", "metric_value",
    "status", "prefix_search_mode", "config_json", "dataset", "sample_idx",
    "machine_id", "baseline_build_flags_sha", "notes", "ts_utc",
]


def insert_rows(conn: sqlite3.Connection, rows: Iterable[ResultRow]) -> int:
    cur = conn.cursor()
    rows = list(rows)
    # Validate statuses against the canonical set BEFORE the bulk insert. The
    # schema CHECK constraint would otherwise reject the whole executemany() with
    # an opaque IntegrityError — discarding every row in the run. Fail fast and
    # name the offending value(s) so a missing status (e.g. a newly introduced
    # 'lossy') is obvious instead of silently nuking hours of compute.
    from orchestrator.status import VALID as _VALID_STATUS
    bad = sorted({r.status for r in rows if r.status not in _VALID_STATUS})
    if bad:
        raise ValueError(
            f"insert_rows: {len(bad)} unknown status value(s) {bad!r} not in "
            f"status.VALID — add them to status.VALID AND every schema/*results.sql "
            f"CHECK(status IN ...) constraint before inserting.")
    payload = [tuple(asdict(r)[c] for c in _RESULTS_COLS) for r in rows]
    placeholders = ",".join("?" * len(_RESULTS_COLS))
    cur.executemany(
        f"INSERT OR REPLACE INTO results ({','.join(_RESULTS_COLS)}) VALUES ({placeholders})",
        payload,
    )
    conn.commit()
    return cur.rowcount


def export_csv(conn: sqlite3.Connection, table: str, out_path: Path) -> int:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    n = 0
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in cur:
            w.writerow(row)
            n += 1
    return n
