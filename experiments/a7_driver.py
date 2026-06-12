"""step 8 — A7 ablation driver (shuffle-seed sensitivity sweep).

Drives the OnPair runner via the standard NDJSON protocol. For each
(dataset, column, seed) cell in experiments/a7_cells.yaml, compresses the FULL
materialised column with everything held at the headline (bits=16,
DynamicThreshold{0.15}) and ONLY the training shuffle seed varying; writes one
row to build/a7_results.sqlite per schema/a7_results.sql.

Like A2, A7 adds NO new runner op and NO upstream OnPair change: the `seed`
cfg knob already exists (parse_onpair_config, default 42), so the driver reuses
the existing `compress` op and reads its sub-counters for R. Unlike A2, A7 is
compress-only (no bulk_decode) because its single y-axis is R — there is no
T_d in the A7 plan row.

Modes:
  --smoke         : single cell (seed=7, a non-default) against the committed
                    V-D fixture, used whole (no split). <1 sec.
  default         : full grid against datasets/materialized/<dataset>/<col>.col
                    full-column files (produced by `make datasets`).

Mirrors experiments/a2_driver.py; A7 owns its own a7_results.sqlite store and
never touches the headline results.sqlite or a1..a6_results.sqlite.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNNER = REPO_ROOT / "build" / "runners" / "onpair" / "onpair_runner"
SCHEMA_PATH = REPO_ROOT / "schema" / "a7_results.sql"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def _send(proc: subprocess.Popen, env: dict) -> None:
    proc.stdin.write(json.dumps(env) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict:
    line = proc.stdout.readline()
    if not line:
        raise EOFError("runner closed stdout")
    return json.loads(line)


def _column_n_rows(path: Path) -> int:
    import struct
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return 0
        n, _total = struct.unpack("<II", head)
    return int(n)


def _column_n_bytes(path: Path) -> int:
    import struct
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return 0
        _n, total = struct.unpack("<II", head)
    return int(total)


def _drive_one_cell(runner_path: Path, column_path: Path, cell_id: str,
                    bits: int, sample_fraction: float, seed: int,
                    mem_cap_gb: int, flags_sha: str) -> dict:
    """Spawn the OnPair runner; compress the full column with the given training
    shuffle `seed`. Returns a flat dict with the compress metric values +
    bookkeeping."""
    sub_env = {"BASELINE_BUILD_FLAGS_SHA": flags_sha, "PATH": "/usr/bin:/bin"}
    config_json = json.dumps({
        "bits":               bits,
        "seed":               seed,
        "mem_cap_gb":         mem_cap_gb,
        "threshold_strategy": "dynamic",
        "sample_fraction":    sample_fraction,
    })
    proc = subprocess.Popen(
        [str(runner_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )
    out: dict = {"status": "crash", "notes": "", "config_json": config_json,
                 "metrics": {}}
    with tempfile.TemporaryDirectory() as tmp:
        compressed = Path(tmp) / "a7.compressed.bin"
        try:
            hello = _recv(proc)
            if hello.get("type") != "hello":
                raise RuntimeError(f"expected hello, got {hello}")
            if hello.get("name") != "onpair":
                raise RuntimeError(f"expected name=onpair, got {hello.get('name')}")

            # ── op: compress (full column) → R, sub-counters, T_c ───────────
            _send(proc, {
                "type": "op", "op": "compress", "cell_id": cell_id,
                "input_path": str(column_path), "output_path": str(compressed),
                "config_json": config_json,
            })
            r_comp = _recv(proc)
            if r_comp.get("type") != "result":
                raise RuntimeError(f"expected result, got {r_comp}")
            comp_status = r_comp.get("status", "crash")
            comp_metrics = {m["name"]: m["value"] for m in r_comp.get("metrics", [])}

            _send(proc, {"type": "bye"})
            proc.wait(timeout=5)

            metrics = dict(comp_metrics)
            metrics["compress_ns"] = float(comp_metrics.get("t_op_ns", 0.0))
            out["metrics"] = metrics
            out["status"] = comp_status
            if comp_status != "ok":
                out["notes"] = r_comp.get("notes", "")
            return out
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)


def _insert_row(conn: sqlite3.Connection, *, run_id: str, cell_id: str,
                dataset: str, column_name: str, seed: int, bits: int,
                sample_fraction: float, column_path: Path,
                runner_out: dict, machine_id: str, flags_sha: str) -> None:
    m = runner_out.get("metrics", {})
    status = runner_out.get("status", "crash")
    notes = runner_out.get("notes", "") or ""
    config_json = runner_out.get("config_json", "{}")

    def f(key: str, default: float = 0.0) -> float:
        return float(m.get(key, default))

    input_bytes = int(f("input_bytes")) or _column_n_bytes(column_path)
    output_bytes = int(f("output_bytes"))
    ratio = (input_bytes / output_bytes) if output_bytes > 0 else 0.0

    conn.execute(
        """INSERT INTO a7_results VALUES (
            :run_id, :cell_id, :dataset, :column_name, :seed, :bits,
            :sample_fraction, :threshold_strategy,
            :input_bytes, :input_rows,
            :dict_bytes, :tokens_bytes, :boundaries_bytes, :output_bytes, :ratio,
            :compress_ns,
            :status, :machine_id, :flags_sha, :config_json, :notes, :ts_utc
        )""",
        {
            "run_id":  run_id,
            "cell_id": cell_id,
            "dataset": dataset,
            "column_name": column_name,
            "seed":    seed,
            "bits":    bits,
            "sample_fraction": float(sample_fraction),
            "threshold_strategy": "dynamic",
            "input_bytes":  input_bytes,
            "input_rows":   _column_n_rows(column_path),
            "dict_bytes":       int(f("dict_bytes")),
            "tokens_bytes":     int(f("tokens_bytes")),
            "boundaries_bytes": int(f("boundaries_bytes")),
            "output_bytes":     output_bytes,
            "ratio":            ratio,
            "compress_ns":      f("compress_ns"),
            "status":     status,
            "machine_id": machine_id,
            "flags_sha":  flags_sha,
            "config_json": config_json,
            "notes":      notes,
            "ts_utc":     _now_iso(),
        },
    )
    conn.commit()


def _flags_sha(default: str) -> str:
    p = REPO_ROOT / "build" / "baseline_build_flags.sha"
    return p.read_text().strip() if p.exists() else default


def _run_smoke(runner_path: Path, db_path: Path) -> int:
    """Single-cell A7 smoke: seed=7 (non-default) × V-D fixture, used whole."""
    cells_yaml = REPO_ROOT / "experiments" / "a7_cells.yaml"
    cfg = yaml.safe_load(cells_yaml.read_text())
    smoke = cfg["smoke"]
    fixture = REPO_ROOT / smoke["fixture_path"]
    if not fixture.exists():
        print(f"a7-smoke: fixture not committed: {fixture}", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex
    machine_id = os.uname().nodename[:16]
    flags_sha = _flags_sha("smoke")
    conn = _open_db(db_path)

    seed = int(smoke["seed"])
    bits = int(cfg["bits"])
    cell_id = f"a7-smoke-{smoke['dataset']}-{smoke['column']}-s{seed}"
    out = _drive_one_cell(
        runner_path, fixture, cell_id=cell_id, bits=bits,
        sample_fraction=float(cfg["sample_fraction"]), seed=seed,
        mem_cap_gb=cfg["mem_cap_gb"], flags_sha=flags_sha,
    )
    _insert_row(conn, run_id=run_id, cell_id=cell_id, dataset=smoke["dataset"],
                column_name=smoke["column"], seed=seed, bits=bits,
                sample_fraction=float(cfg["sample_fraction"]),
                column_path=fixture, runner_out=out,
                machine_id=machine_id, flags_sha=flags_sha)
    print(f"a7-smoke: {cell_id} → status={out['status']}")
    conn.close()
    if out["status"] != "ok":
        print(f"a7-smoke: FAIL — notes: {out.get('notes')}", file=sys.stderr)
        return 1
    print("a7-smoke: OK")
    return 0


def _run_full(runner_path: Path, datasets_manifest: Path, a7_cells: Path,
              columns_dir: Path, db_path: Path) -> int:
    cells_cfg = yaml.safe_load(a7_cells.read_text())
    datasets_cfg = yaml.safe_load(datasets_manifest.read_text())
    by_name = {e["name"]: e for e in datasets_cfg["datasets"] if e.get("kind") == "real"}

    run_id = uuid.uuid4().hex
    machine_id = os.uname().nodename[:16]
    flags_sha = _flags_sha("headline")
    conn = _open_db(db_path)
    n_ok = 0
    n_total = 0

    for col_spec in cells_cfg["columns"]:
        ds_name = col_spec["dataset"]
        col_name = col_spec["column"]
        if ds_name not in by_name:
            print(f"a7: dataset {ds_name!r} not in manifest; skipping", file=sys.stderr)
            continue
        ds_entry = by_name[ds_name]
        col_entry = next((c for c in ds_entry["columns"] if c["name"] == col_name), None)
        if col_entry is None:
            print(f"a7: column {ds_name}/{col_name} not in manifest; skipping",
                  file=sys.stderr)
            continue
        out_fname = col_entry.get("output_filename", f"{col_name}.col")
        col_path = columns_dir / ds_name / out_fname
        if not col_path.exists():
            print(f"a7: {ds_name}/{col_name} not materialised ({col_path}); "
                  f"run `make datasets` first", file=sys.stderr)
            continue

        for seed in cells_cfg["seeds"]:
            n_total += 1
            cell_id = f"a7-{ds_name}-{col_name}-s{seed}"
            out = _drive_one_cell(
                runner_path, col_path, cell_id=cell_id, bits=int(cells_cfg["bits"]),
                sample_fraction=float(cells_cfg["sample_fraction"]),
                seed=int(seed), mem_cap_gb=cells_cfg["mem_cap_gb"],
                flags_sha=flags_sha,
            )
            _insert_row(conn, run_id=run_id, cell_id=cell_id, dataset=ds_name,
                        column_name=col_name, seed=int(seed),
                        bits=int(cells_cfg["bits"]),
                        sample_fraction=float(cells_cfg["sample_fraction"]),
                        column_path=col_path, runner_out=out,
                        machine_id=machine_id, flags_sha=flags_sha)
            print(f"a7: {cell_id} → status={out['status']}")
            if out["status"] == "ok":
                n_ok += 1

    conn.close()
    print(f"a7: {n_ok}/{n_total} cells ok")
    return 0 if n_ok == n_total else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runner-path", type=Path, default=DEFAULT_RUNNER)
    p.add_argument("--datasets-manifest", type=Path,
                   default=REPO_ROOT / "experiments" / "datasets.yaml")
    p.add_argument("--a7-cells", type=Path,
                   default=REPO_ROOT / "experiments" / "a7_cells.yaml")
    p.add_argument("--columns-dir", type=Path,
                   default=REPO_ROOT / "datasets" / "materialized")
    p.add_argument("--db", type=Path,
                   default=REPO_ROOT / "build" / "a7_results.sqlite")
    p.add_argument("--smoke", action="store_true")
    a = p.parse_args()

    if not a.runner_path.exists():
        print(f"a7: runner binary missing: {a.runner_path}; run `make cpp-build` first",
              file=sys.stderr)
        return 2

    if a.smoke:
        return _run_smoke(a.runner_path, a.db)
    return _run_full(a.runner_path, a.datasets_manifest, a.a7_cells,
                     a.columns_dir, a.db)


if __name__ == "__main__":
    raise SystemExit(main())
