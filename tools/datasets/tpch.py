"""TPC-H extractor via DuckDB's tpch extension — TPC academic.

Phase 5 step 1 caveat (locked at plan time): DuckDB's `dbgen(sf=N)` produces
pipeline-reproducible TPC-H tables but is NOT separately verified bit-identical
to the reference TPC dbgen tool. The duckdb version + SF + post-extraction
SHA-256 are pinned in experiments/datasets.yaml; bit-identity-to-reference is
a Phase 7 legal-check gate (plan R7).

`SF=10` chosen so the load-bearing l_comment column (~1.6 GB) sits 5–10× above
L3-cache capacity — throughput numbers reflect main-memory behaviour, not
cache-resident artefacts.
"""
from __future__ import annotations
import csv
from pathlib import Path

from ._common import write_rows


# Maps column-name (manifest) → (table, column) for the DuckDB SELECT.
_COLUMN_TO_DUCKDB = {
    "l_comment": ("lineitem", "l_comment"),
    "p_name":    ("part",     "p_name"),
    "c_address": ("customer", "c_address"),
    "c_comment": ("customer", "c_comment"),
    "s_comment": ("supplier", "s_comment"),
}


def download(target_dir: Path, manifest_entry: dict) -> Path:
    """In-process: DuckDB generates SF=N from scratch each invocation. We
    materialise per-column CSV dumps under target_dir/ so extract_columns()
    can stream them into the harness column format independently."""
    try:
        import duckdb  # type: ignore[import-untyped]
    except ImportError as e:
        raise RuntimeError(
            "duckdb is required to materialise TPC-H; install with `pip install duckdb`"
        ) from e

    sf = (manifest_entry.get("subset") or {}).get("sf", 10)
    target_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("INSTALL tpch")
    con.execute("LOAD tpch")
    con.execute(f"CALL dbgen(sf={sf})")
    for col_name, (table, column) in _COLUMN_TO_DUCKDB.items():
        out = target_dir / f"{col_name}.tsv"
        con.execute(
            f"COPY (SELECT {column} FROM {table}) TO '{out}' "
            f"(FORMAT 'CSV', HEADER FALSE, DELIMITER '\\t', QUOTE '')"
        )
    con.close()
    return target_dir


def _iter_tsv_rows(path: Path):
    """Yield one row per line, single-column TSV (no quoting in the COPY above)."""
    with open(path, "rb") as f:
        for line in f:
            yield line.rstrip(b"\r\n")


def extract_columns(raw_path: Path, output_dir: Path, manifest_entry: dict,
                    subset_params: dict | None = None) -> dict[str, Path]:
    """raw_path is the directory produced by download() — one .tsv per column."""
    out: dict[str, Path] = {}
    for col in manifest_entry.get("columns", []):
        col_name = col["name"]
        src = raw_path / f"{col_name}.tsv" if raw_path.is_dir() else raw_path
        if not src.exists():
            continue
        rows = list(_iter_tsv_rows(src))
        out[col_name] = write_rows(output_dir / f"{col_name}.col", rows)
    return out
