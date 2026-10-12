"""A2 ablation driver (bit-width sweep; Section VIII-D, Fig. 7).

Drives the OnPair runner via the standard NDJSON protocol. For each
(dataset, column, bits) cell in experiments/a2_cells.yaml, compresses the FULL
materialised column at bit-width b (DynamicThreshold{0.15}, seed=42 held
constant) and then bulk-decodes it; writes one row to build/a2_results.sqlite
per schema/a2_results.sql.

Unlike A1/A3 this needs NO dedicated runner op and NO upstream OnPair change: A2's
three quantities (R, T_d, |D|) all have direct headline-op equivalents, so the
driver reuses the existing `compress` + `bulk_decode` ops and reads their
dispatch-loop `t_op_ns` as T_c / T_d. Note that t_op_ns covers the whole op,
including I/O, unlike the codec-only timers behind Table II. See the
schema/a2_results.sql header for the full rationale.

Modes:
  --smoke         : single cell (b=12) against the committed synthetic fixture, used
                    whole (no split — A2 is a full-column experiment). <1 sec.
  default         : full grid against datasets/materialized/<dataset>/<col>.col
                    full-column files (produced by `make datasets`).

Mirrors experiments/a3_driver.py; A2 owns its own a2_results.sqlite store and
never touches the headline results.sqlite or a1/a3_results.sqlite.
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
SCHEMA_PATH = REPO_ROOT / "schema" / "a2_results.sql"


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
    """Spawn the OnPair runner; compress the full column at bit-width `bits`,
    then bulk-decode it. Returns a flat dict with the metric values from both
    ops + bookkeeping. The combined status is 'ok' only when both ops succeed."""
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
        compressed = Path(tmp) / "a2.compressed.bin"
        decoded = Path(tmp) / "a2.decoded.col"
        try:
            hello = _recv(proc)
            if hello.get("type") != "hello":
                raise RuntimeError(f"expected hello, got {hello}")
            if hello.get("name") != "onpair":
                raise RuntimeError(f"expected name=onpair, got {hello.get('name')}")

            # ── op 1: compress (full column) → R, |D|, T_c ──────────────────
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

            # ── op 2: bulk_decode (compressed payload) → T_d ────────────────
            dec_status = "crash"
            dec_metrics: dict = {}
            if comp_status == "ok":
                _send(proc, {
                    "type": "op", "op": "bulk_decode", "cell_id": cell_id,
                    "input_path": str(compressed), "output_path": str(decoded),
                    "config_json": config_json,
                })
                r_dec = _recv(proc)
                if r_dec.get("type") != "result":
                    raise RuntimeError(f"expected result, got {r_dec}")
                dec_status = r_dec.get("status", "crash")
                dec_metrics = {m["name"]: m["value"] for m in r_dec.get("metrics", [])}

            _send(proc, {"type": "bye"})
            proc.wait(timeout=5)

            # Merge: compress sub-counters + compress_ns (its t_op_ns) and
            # decode_ns (bulk_decode's t_op_ns).
            metrics = dict(comp_metrics)
            metrics["compress_ns"] = float(comp_metrics.get("t_op_ns", 0.0))
            metrics["decode_ns"] = float(dec_metrics.get("t_op_ns", 0.0))
            out["metrics"] = metrics
            # Combined status: ok iff both ok; else surface the failing one.
            if comp_status == "ok" and dec_status == "ok":
                out["status"] = "ok"
            elif comp_status != "ok":
                out["status"] = comp_status
                out["notes"] = r_comp.get("notes", "")
            else:
                out["status"] = dec_status
                out["notes"] = r_dec.get("notes", "") if comp_status == "ok" else ""
            return out
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)


def _insert_row(conn: sqlite3.Connection, *, run_id: str, cell_id: str,
                dataset: str, column_name: str, bits: int,
                sample_fraction: float, seed: int, column_path: Path,
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
        """INSERT INTO a2_results VALUES (
            :run_id, :cell_id, :dataset, :column_name, :bits,
            :sample_fraction, :seed, :threshold_strategy,
            :input_bytes, :input_rows,
            :dict_bytes, :tokens_bytes, :boundaries_bytes, :output_bytes, :ratio,
            :compress_ns, :decode_ns,
            :status, :machine_id, :flags_sha, :config_json, :notes, :ts_utc
        )""",
        {
            "run_id":  run_id,
            "cell_id": cell_id,
            "dataset": dataset,
            "column_name": column_name,
            "bits":    bits,
            "sample_fraction": float(sample_fraction),
            "seed":    seed,
            "threshold_strategy": "dynamic",
            "input_bytes":  input_bytes,
            "input_rows":   _column_n_rows(column_path),
            "dict_bytes":       int(f("dict_bytes")),
            "tokens_bytes":     int(f("tokens_bytes")),
            "boundaries_bytes": int(f("boundaries_bytes")),
            "output_bytes":     output_bytes,
            "ratio":            ratio,
            "compress_ns":      f("compress_ns"),
            "decode_ns":        f("decode_ns"),
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
    """Single-cell A2 smoke: b=12 × synthetic fixture, used whole (no split)."""
    cells_yaml = REPO_ROOT / "experiments" / "a2_cells.yaml"
    cfg = yaml.safe_load(cells_yaml.read_text())
    smoke = cfg["smoke"]
    fixture = REPO_ROOT / smoke["fixture_path"]
    if not fixture.exists():
        print(f"a2-smoke: fixture not committed: {fixture}", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex
    machine_id = os.uname().nodename[:16]
    flags_sha = _flags_sha("smoke")
    conn = _open_db(db_path)

    bits = int(smoke["bits"])
    cell_id = f"a2-smoke-{smoke['dataset']}-{smoke['column']}-b{bits}"
    out = _drive_one_cell(
        runner_path, fixture, cell_id=cell_id, bits=bits,
        sample_fraction=float(cfg["sample_fraction"]), seed=cfg["seed"],
        mem_cap_gb=cfg["mem_cap_gb"], flags_sha=flags_sha,
    )
    _insert_row(conn, run_id=run_id, cell_id=cell_id, dataset=smoke["dataset"],
                column_name=smoke["column"], bits=bits,
                sample_fraction=float(cfg["sample_fraction"]), seed=cfg["seed"],
                column_path=fixture, runner_out=out,
                machine_id=machine_id, flags_sha=flags_sha)
    print(f"a2-smoke: {cell_id} → status={out['status']}")
    conn.close()
    if out["status"] != "ok":
        print(f"a2-smoke: FAIL — notes: {out.get('notes')}", file=sys.stderr)
        return 1
    print("a2-smoke: OK")
    return 0


def _run_full(runner_path: Path, datasets_manifest: Path, a2_cells: Path,
              columns_dir: Path, db_path: Path) -> int:
    cells_cfg = yaml.safe_load(a2_cells.read_text())
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
            print(f"a2: dataset {ds_name!r} not in manifest; skipping", file=sys.stderr)
            continue
        ds_entry = by_name[ds_name]
        col_entry = next((c for c in ds_entry["columns"] if c["name"] == col_name), None)
        if col_entry is None:
            print(f"a2: column {ds_name}/{col_name} not in manifest; skipping",
                  file=sys.stderr)
            continue
        out_fname = col_entry.get("output_filename", f"{col_name}.col")
        col_path = columns_dir / ds_name / out_fname
        if not col_path.exists():
            print(f"a2: {ds_name}/{col_name} not materialised ({col_path}); "
                  f"run `make datasets` first", file=sys.stderr)
            continue

        for bits in cells_cfg["bit_widths"]:
            n_total += 1
            cell_id = f"a2-{ds_name}-{col_name}-b{bits}"
            out = _drive_one_cell(
                runner_path, col_path, cell_id=cell_id, bits=int(bits),
                sample_fraction=float(cells_cfg["sample_fraction"]),
                seed=cells_cfg["seed"], mem_cap_gb=cells_cfg["mem_cap_gb"],
                flags_sha=flags_sha,
            )
            _insert_row(conn, run_id=run_id, cell_id=cell_id, dataset=ds_name,
                        column_name=col_name, bits=int(bits),
                        sample_fraction=float(cells_cfg["sample_fraction"]),
                        seed=cells_cfg["seed"], column_path=col_path,
                        runner_out=out, machine_id=machine_id, flags_sha=flags_sha)
            print(f"a2: {cell_id} → status={out['status']}")
            if out["status"] == "ok":
                n_ok += 1

    conn.close()
    print(f"a2: {n_ok}/{n_total} cells ok")
    return 0 if n_ok == n_total else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runner-path", type=Path, default=DEFAULT_RUNNER)
    p.add_argument("--datasets-manifest", type=Path,
                   default=REPO_ROOT / "experiments" / "datasets.yaml")
    p.add_argument("--a2-cells", type=Path,
                   default=REPO_ROOT / "experiments" / "a2_cells.yaml")
    p.add_argument("--columns-dir", type=Path,
                   default=REPO_ROOT / "datasets" / "materialized")
    p.add_argument("--db", type=Path,
                   default=REPO_ROOT / "build" / "a2_results.sqlite")
    p.add_argument("--smoke", action="store_true")
    a = p.parse_args()

    if not a.runner_path.exists():
        print(f"a2: runner binary missing: {a.runner_path}; run `make cpp-build` first",
              file=sys.stderr)
        return 2

    if a.smoke:
        return _run_smoke(a.runner_path, a.db)
    return _run_full(a.runner_path, a.datasets_manifest, a.a2_cells,
                     a.columns_dir, a.db)


if __name__ == "__main__":
    raise SystemExit(main())
