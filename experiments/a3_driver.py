"""A3 ablation driver (sample_fraction sweep; Fig. 6, right).

Drives the OnPair runner via the standard NDJSON protocol with the
`op="held_out_compress"` envelope in its single-strategy mode
(threshold_strategy="dynamic"). For each (dataset, column, sample_fraction)
cell in experiments/a3_cells.yaml, trains a single DynamicThreshold{f}
dictionary on the train split and encodes the held-out split with it; writes
one row to build/a3_results.sqlite per schema/a3_results.sql.

Modes:
  --smoke         : single cell against the committed synthetic fixture; splits
                    inline via orchestrator.heldout.split_heldout. <1 sec.
  default         : full grid against datasets/materialized/<dataset>/
                    train/heldout column files (produced by `make datasets`).

Mirrors experiments/a1_driver.py; A3 owns its own a3_results.sqlite store and
never touches the headline results.sqlite or a1_results.sqlite.
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
SCHEMA_PATH = REPO_ROOT / "schema" / "a3_results.sql"


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


def _drive_one_cell(runner_path: Path, train_path: Path, heldout_path: Path,
                    cell_id: str, sample_fraction: float, bits: int, seed: int,
                    mem_cap_gb: int, flags_sha: str) -> dict:
    """Spawn the OnPair runner; run one single-strategy held_out_compress probe
    (DynamicThreshold{sample_fraction}); return a flat dict with the metric
    values + bookkeeping."""
    sub_env = {"BASELINE_BUILD_FLAGS_SHA": flags_sha, "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(runner_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )
    try:
        hello = _recv(proc)
        if hello.get("type") != "hello":
            raise RuntimeError(f"expected hello, got {hello}")
        if hello.get("name") != "onpair":
            raise RuntimeError(f"expected name=onpair, got {hello.get('name')}")

        config_json = json.dumps({
            "bits":               bits,
            "seed":               seed,
            "mem_cap_gb":         mem_cap_gb,
            "threshold_strategy": "dynamic",
            "sample_fraction":    sample_fraction,
        })
        env = {
            "type":               "op",
            "op":                 "held_out_compress",
            "cell_id":            cell_id,
            "train_input_path":   str(train_path),
            "heldout_input_path": str(heldout_path),
            "output_path":        "/dev/null",
            "config_json":        config_json,
        }
        _send(proc, env)
        resp = _recv(proc)
        if resp.get("type") != "result":
            raise RuntimeError(f"expected result, got {resp}")

        out: dict = {
            "status":      resp.get("status", "crash"),
            "notes":       resp.get("notes", ""),
            "config_json": config_json,
        }
        out["metrics"] = {m["name"]: m["value"] for m in resp.get("metrics", [])}

        _send(proc, {"type": "bye"})
        proc.wait(timeout=5)
        return out
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)


def _insert_row(conn: sqlite3.Connection, *, run_id: str, cell_id: str,
                dataset: str, column_name: str, sample_fraction: float,
                bits: int, seed: int, train_path: Path, heldout_path: Path,
                runner_out: dict, machine_id: str, flags_sha: str) -> None:
    m = runner_out.get("metrics", {})
    status = runner_out.get("status", "crash")
    notes = runner_out.get("notes", "") or ""
    config_json = runner_out.get("config_json", "{}")

    def f(key: str, default: float = 0.0) -> float:
        return float(m.get(key, default))

    conn.execute(
        """INSERT INTO a3_results VALUES (
            :run_id, :cell_id, :dataset, :column_name, :sample_fraction,
            :bits, :seed, :threshold_strategy,
            :train_input_bytes, :train_input_rows,
            :heldout_input_bytes, :heldout_input_rows,
            :sample_bytes_consumed, :effective_fraction, :num_dict_tokens,
            :dict_bytes, :tokens_bytes, :boundaries_bytes,
            :heldout_output_bytes, :heldout_ratio,
            :train_ns, :encode_ns,
            :status, :machine_id, :flags_sha, :config_json, :notes, :ts_utc
        )""",
        {
            "run_id":  run_id,
            "cell_id": cell_id,
            "dataset": dataset,
            "column_name": column_name,
            "sample_fraction": float(sample_fraction),
            "bits":     bits,
            "seed":     seed,
            "threshold_strategy": "dynamic",
            "train_input_bytes":   _column_n_bytes(train_path),
            "train_input_rows":    _column_n_rows(train_path),
            "heldout_input_bytes": _column_n_bytes(heldout_path),
            "heldout_input_rows":  _column_n_rows(heldout_path),
            "sample_bytes_consumed": int(f("sample_bytes_consumed")),
            "effective_fraction":    f("effective_fraction"),
            "num_dict_tokens":       int(f("num_dict_tokens")),
            "dict_bytes":            int(f("dict_bytes")),
            "tokens_bytes":          int(f("tokens_bytes")),
            "boundaries_bytes":      int(f("boundaries_bytes")),
            "heldout_output_bytes":  int(f("heldout_output_bytes")),
            "heldout_ratio":         f("heldout_ratio"),
            "train_ns":              f("train_ns"),
            "encode_ns":             f("encode_ns"),
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
    """Single-cell A3 smoke: f=0.30 × synthetic fixture; inline 800/200 split."""
    from orchestrator.heldout import split_heldout

    cells_yaml = REPO_ROOT / "experiments" / "a3_cells.yaml"
    cfg = yaml.safe_load(cells_yaml.read_text())
    smoke = cfg["smoke"]
    fixture = REPO_ROOT / smoke["fixture_path"]
    if not fixture.exists():
        print(f"a3-smoke: fixture not committed: {fixture}", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex
    machine_id = os.uname().nodename[:16]
    flags_sha = _flags_sha("smoke")
    conn = _open_db(db_path)

    with tempfile.TemporaryDirectory() as tmp:
        train_p   = Path(tmp) / "smoke_train.col"
        heldout_p = Path(tmp) / "smoke_heldout.col"
        n_train, n_held = split_heldout(fixture, train_p, heldout_p)
        print(f"a3-smoke: split fixture {fixture.name} → train={n_train} heldout={n_held}")

        frac = float(smoke["sample_fraction"])
        cell_id = f"a3-smoke-{smoke['dataset']}-{smoke['column']}-f{frac}"
        out = _drive_one_cell(
            runner_path, train_p, heldout_p,
            cell_id=cell_id, sample_fraction=frac, bits=cfg["bits"],
            seed=cfg["seed"], mem_cap_gb=cfg["mem_cap_gb"], flags_sha=flags_sha,
        )
        _insert_row(conn, run_id=run_id, cell_id=cell_id, dataset=smoke["dataset"],
                    column_name=smoke["column"], sample_fraction=frac,
                    bits=cfg["bits"], seed=cfg["seed"], train_path=train_p,
                    heldout_path=heldout_p, runner_out=out,
                    machine_id=machine_id, flags_sha=flags_sha)
        print(f"a3-smoke: {cell_id} → status={out['status']}")
        if out["status"] != "ok":
            print(f"a3-smoke: FAIL — notes: {out.get('notes')}", file=sys.stderr)
            conn.close()
            return 1

    conn.close()
    print("a3-smoke: OK")
    return 0


def _run_full(runner_path: Path, datasets_manifest: Path, a3_cells: Path,
              columns_dir: Path, db_path: Path) -> int:
    cells_cfg = yaml.safe_load(a3_cells.read_text())
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
            print(f"a3: dataset {ds_name!r} not in manifest; skipping", file=sys.stderr)
            continue
        ds_entry = by_name[ds_name]
        col_entry = next((c for c in ds_entry["columns"] if c["name"] == col_name), None)
        if col_entry is None:
            print(f"a3: column {ds_name}/{col_name} not in manifest; skipping",
                  file=sys.stderr)
            continue
        out_fname = col_entry.get("output_filename", f"{col_name}.col")
        col_path = columns_dir / ds_name / out_fname
        train_path   = col_path.with_suffix(".train.col")
        heldout_path = col_path.with_suffix(".heldout.col")
        if not train_path.exists() or not heldout_path.exists():
            print(f"a3: {ds_name}/{col_name} not materialised "
                  f"({train_path} / {heldout_path}); run `make datasets` first",
                  file=sys.stderr)
            continue

        for frac in cells_cfg["sample_fractions"]:
            n_total += 1
            cell_id = f"a3-{ds_name}-{col_name}-f{frac}"
            out = _drive_one_cell(
                runner_path, train_path, heldout_path,
                cell_id=cell_id, sample_fraction=float(frac), bits=cells_cfg["bits"],
                seed=cells_cfg["seed"], mem_cap_gb=cells_cfg["mem_cap_gb"],
                flags_sha=flags_sha,
            )
            _insert_row(conn, run_id=run_id, cell_id=cell_id, dataset=ds_name,
                        column_name=col_name, sample_fraction=float(frac),
                        bits=cells_cfg["bits"], seed=cells_cfg["seed"],
                        train_path=train_path, heldout_path=heldout_path,
                        runner_out=out, machine_id=machine_id, flags_sha=flags_sha)
            print(f"a3: {cell_id} → status={out['status']}")
            if out["status"] == "ok":
                n_ok += 1

    conn.close()
    print(f"a3: {n_ok}/{n_total} cells ok")
    return 0 if n_ok == n_total else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runner-path", type=Path, default=DEFAULT_RUNNER)
    p.add_argument("--datasets-manifest", type=Path,
                   default=REPO_ROOT / "experiments" / "datasets.yaml")
    p.add_argument("--a3-cells", type=Path,
                   default=REPO_ROOT / "experiments" / "a3_cells.yaml")
    p.add_argument("--columns-dir", type=Path,
                   default=REPO_ROOT / "datasets" / "materialized")
    p.add_argument("--db", type=Path,
                   default=REPO_ROOT / "build" / "a3_results.sqlite")
    p.add_argument("--smoke", action="store_true")
    a = p.parse_args()

    if not a.runner_path.exists():
        print(f"a3: runner binary missing: {a.runner_path}; run `make cpp-build` first",
              file=sys.stderr)
        return 2

    if a.smoke:
        return _run_smoke(a.runner_path, a.db)
    return _run_full(a.runner_path, a.datasets_manifest, a.a3_cells,
                     a.columns_dir, a.db)


if __name__ == "__main__":
    raise SystemExit(main())
