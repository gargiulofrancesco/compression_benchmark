"""step 7 — A6 ablation driver (query-distribution sweep vs random-access
latency).

Drives the A6 roster — block-based zstd/lz4 + per-string-native fsst/onpair — via
the standard NDJSON protocol. For each (baseline, dataset, column,
query_distribution, query_layout, cache_state) cell, the baseline compresses the
FULL column at the FIXED headline 1 MB block budget (block size is NOT swept in
A6 — that was A5), then the `ra_latency` op times n_samples single-string accesses
drawn from that distribution + layout (no file I/O in the timed loop) and reduces
them to L_ra p50/p95/p99 (+ mean/min/max). One row is written to
build/a6_results.sqlite per schema/a6_results.sql.

REUSE: A6 reuses the SAME `ra_latency` op as A5,
only varying its query_distribution + query_layout — harness C++ via the shared
runners/core/query_sampler.h, NOT an OnPair library change. The timed region is
byte-identical across every baseline; only the decode work differs.

ROSTER + LAYOUT BRACKET: A5's roster + one
per-string-native baseline besides OnPair (fsst) for fairness; each skewed
distribution run under BOTH identity (hot set contiguous; block baselines' best
case) and scramble (hot set scattered; YCSB-realistic). Uniform is
layout-invariant → identity only. See schema/a6_results.sql for the full note.

cache_state ∈ {warm, cold} maps to the op's `warmup` flag (warm → one in-process
decode pre-pass before timing; cold → none). True FS-cache-cold (drop OS page
cache) is a measurement-rig concern of the `make a6` wrapper, not the op.

Modes:
  --smoke   : zstd + fsst + onpair against the committed V-D fixture under
              Zipf(1.0) × {identity, scramble}, warm, n_samples=2000. <1 sec.
  default   : full grid against datasets/materialized/<dataset>/<col>.col
              (produced by `make datasets`). Measurement rig only.

A6 owns its own a6_results.sqlite store and never touches the headline
results.sqlite or a1..a5_results.sqlite.
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
DEFAULT_BUILD = REPO_ROOT / "build"
SCHEMA_PATH = REPO_ROOT / "schema" / "a6_results.sql"

# Block-based baselines decode the enclosing block per query (n_blocks
# meaningful); per-string baselines decode each row independently (n_blocks NULL).
BLOCK_BASELINES = {"zstd", "lz4"}
PER_STRING_BASELINES = {"fsst", "onpair"}

RUNNER_REL = {
    "zstd":   Path("runners") / "zstd" / "zstd_runner",
    "lz4":    Path("runners") / "lz4" / "lz4_runner",
    "fsst":   Path("runners") / "fsst" / "fsst_runner",
    "onpair": Path("runners") / "onpair" / "onpair_runner",
}


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


def _column_header(path: Path) -> tuple[int, int]:
    """(n_rows, total_bytes) from the 8-byte column header."""
    import struct
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return 0, 0
        n, total = struct.unpack("<II", head)
    return int(n), int(total)


def _runner_path(build_root: Path, baseline: str) -> Path:
    return build_root / RUNNER_REL[baseline]


def _spawn(runner_path: Path, flags_sha: str) -> subprocess.Popen:
    sub_env = {"BASELINE_BUILD_FLAGS_SHA": flags_sha, "PATH": "/usr/bin:/bin"}
    return subprocess.Popen(
        [str(runner_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )


def _handshake(proc: subprocess.Popen, expect_name: str) -> None:
    hello = _recv(proc)
    if hello.get("type") != "hello":
        raise RuntimeError(f"expected hello, got {hello}")
    if hello.get("name") != expect_name:
        raise RuntimeError(f"expected name={expect_name}, got {hello.get('name')}")


def _compress_config(baseline: str, block_budget_bytes: int, seed: int) -> dict:
    """Compress-op config. Block baselines pin the FIXED headline block budget
    (A6 does not sweep block size); per-string baselines use their headline
    encode config."""
    if baseline == "zstd":
        return {"level": 3, "block_budget_bytes": int(block_budget_bytes)}
    if baseline == "lz4":
        return {"tier": "fast", "accel": 1,
                "block_budget_bytes": int(block_budget_bytes)}
    if baseline == "fsst":
        return {"variant": "fsst", "training": "full_column"}
    if baseline == "onpair":
        return {"bits": 16, "sample_fraction": 0.15, "seed": int(seed)}
    raise ValueError(f"unknown baseline {baseline!r}")


def _drive_one_cell(build_root: Path, baseline: str, column_path: Path,
                    cell_id: str, block_budget_bytes: int, cache_state: str,
                    n_samples: int, seed: int, query_distribution: str,
                    query_layout: str, flags_sha: str) -> dict:
    """compress (fixed block budget) → ra_latency (n_samples accesses under the
    distribution + layout), in one runner process. Returns a flat dict with both
    ops' metrics + bookkeeping. Combined status is 'ok' only when both succeed."""
    runner_path = _runner_path(build_root, baseline)
    comp_cfg = _compress_config(baseline, block_budget_bytes, seed)
    warmup = 1 if cache_state == "warm" else 0
    ra_cfg = {
        "n_samples": int(n_samples),
        "seed": int(seed),
        "query_distribution": query_distribution,
        "query_layout": query_layout,
        "warmup": warmup,
    }
    config_json = json.dumps({
        "baseline": baseline, "compress": comp_cfg, "ra_latency": ra_cfg,
        "cache_state": cache_state,
    })
    out: dict = {"status": "crash", "notes": "", "config_json": config_json,
                 "metrics": {}}

    with tempfile.TemporaryDirectory() as tmp:
        wire = Path(tmp) / "a6.compressed.bin"
        discard = Path(tmp) / "a6.discard.bin"

        comp_status, comp_metrics, comp_notes = "crash", {}, ""
        ra_status, ra_metrics, ra_notes = "crash", {}, ""
        proc = _spawn(runner_path, flags_sha)
        try:
            _handshake(proc, baseline)
            # ── compress at the fixed headline block budget ────────────────────
            _send(proc, {
                "type": "op", "op": "compress", "cell_id": cell_id,
                "input_path": str(column_path), "output_path": str(wire),
                "config_json": json.dumps(comp_cfg),
            })
            r_comp = _recv(proc)
            if r_comp.get("type") != "result":
                raise RuntimeError(f"expected result, got {r_comp}")
            comp_status = r_comp.get("status", "crash")
            comp_metrics = {m["name"]: m["value"] for m in r_comp.get("metrics", [])}
            comp_notes = r_comp.get("notes", "")

            # ── ra_latency on the wire → L_ra percentiles ──────────────────────
            if comp_status == "ok":
                _send(proc, {
                    "type": "op", "op": "ra_latency", "cell_id": cell_id,
                    "input_path": str(wire), "output_path": str(discard),
                    "config_json": json.dumps(ra_cfg),
                })
                r_ra = _recv(proc)
                if r_ra.get("type") != "result":
                    raise RuntimeError(f"expected result, got {r_ra}")
                ra_status = r_ra.get("status", "crash")
                ra_metrics = {m["name"]: m["value"] for m in r_ra.get("metrics", [])}
                ra_notes = r_ra.get("notes", "")
            _send(proc, {"type": "bye"})
            proc.wait(timeout=10)
        except Exception as e:  # noqa: BLE001
            comp_notes = (comp_notes + f"; a6 driver error: {e}").strip("; ")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream and not stream.closed:
                        stream.close()
                except Exception:
                    pass

        metrics = dict(comp_metrics)
        for k, v in ra_metrics.items():
            metrics[k] = v
        out["metrics"] = metrics
        if comp_status == "ok" and ra_status == "ok":
            out["status"] = "ok"
            note = (f"ra_latency {query_distribution}/{query_layout} "
                    f"n={n_samples} cache={cache_state}")
            if "batch_k" in ra_metrics:
                note += f" batch_k={int(ra_metrics['batch_k'])}"
            if "clock_resolution_ns" in ra_metrics:
                note += f" clock_res={ra_metrics['clock_resolution_ns']:.1f}ns"
            out["notes"] = note
        elif comp_status != "ok":
            out["status"] = comp_status
            out["notes"] = (comp_notes + " (compress failed)").strip()
        else:
            out["status"] = ra_status
            out["notes"] = (ra_notes + " (ra_latency failed)").strip()
        return out


def _insert_row(conn: sqlite3.Connection, *, run_id: str, cell_id: str,
                baseline: str, dataset: str, column_name: str,
                query_distribution: str, query_layout: str, cache_state: str,
                seed: int, n_samples: int, column_path: Path,
                runner_out: dict, machine_id: str, flags_sha: str) -> None:
    m = runner_out.get("metrics", {})
    status = runner_out.get("status", "crash")
    notes = runner_out.get("notes", "") or ""
    config_json = runner_out.get("config_json", "{}")

    def f(key: str, default: float = 0.0) -> float:
        return float(m.get(key, default))

    n_rows_hdr, n_bytes_hdr = _column_header(column_path)
    input_bytes = int(f("input_bytes")) or n_bytes_hdr
    input_rows = int(f("num_rows")) or n_rows_hdr
    compressed_bytes = int(f("output_bytes"))
    # n_blocks is meaningful only for the block baselines; NULL for per-string.
    n_blocks = int(f("n_blocks")) if baseline in BLOCK_BASELINES else None

    conn.execute(
        """INSERT INTO a6_results VALUES (
            :run_id, :cell_id, :baseline, :dataset, :column_name,
            :query_distribution, :query_layout, :cache_state, :seed,
            :n_samples, :input_bytes, :input_rows, :n_blocks, :compressed_bytes,
            :l_ra_p50_ns, :l_ra_p95_ns, :l_ra_p99_ns,
            :l_ra_mean_ns, :l_ra_min_ns, :l_ra_max_ns,
            :status, :machine_id, :flags_sha, :config_json, :notes, :ts_utc
        )""",
        {
            "run_id": run_id,
            "cell_id": cell_id,
            "baseline": baseline,
            "dataset": dataset,
            "column_name": column_name,
            "query_distribution": query_distribution,
            "query_layout": query_layout,
            "cache_state": cache_state,
            "seed": seed,
            "n_samples": int(n_samples),
            "input_bytes": input_bytes,
            "input_rows": input_rows,
            "n_blocks": n_blocks,
            "compressed_bytes": compressed_bytes,
            "l_ra_p50_ns":  f("l_ra_p50_ns"),
            "l_ra_p95_ns":  f("l_ra_p95_ns"),
            "l_ra_p99_ns":  f("l_ra_p99_ns"),
            "l_ra_mean_ns": f("l_ra_mean_ns"),
            "l_ra_min_ns":  f("l_ra_min_ns"),
            "l_ra_max_ns":  f("l_ra_max_ns"),
            "status": status,
            "machine_id": machine_id,
            "flags_sha": flags_sha,
            "config_json": config_json,
            "notes": notes,
            "ts_utc": _now_iso(),
        },
    )
    conn.commit()


def _flags_sha(default: str) -> str:
    p = REPO_ROOT / "build" / "baseline_build_flags.sha"
    return p.read_text().strip() if p.exists() else default


def _dist_layout_combos(distributions, layouts):
    """Uniform is layout-invariant → identity only; skewed dists → every layout."""
    combos = []
    for d in distributions:
        if d == "uniform":
            combos.append((d, "identity"))
        else:
            for lay in layouts:
                combos.append((d, lay))
    return combos


def _run_smoke(build_root: Path, db_path: Path) -> int:
    """zstd + fsst + onpair against the V-D fixture, Zipf(1.0) × {identity,
    scramble}, warm. Block budget tiny so zstd splits into many blocks."""
    cells_yaml = REPO_ROOT / "experiments" / "a6_cells.yaml"
    cfg = yaml.safe_load(cells_yaml.read_text())
    smoke = cfg["smoke"]
    fixture = REPO_ROOT / smoke["fixture_path"]
    if not fixture.exists():
        print(f"a6-smoke: fixture not committed: {fixture}", file=sys.stderr)
        return 2

    for baseline in smoke["baselines"]:
        rp = _runner_path(build_root, baseline)
        if not rp.exists():
            print(f"a6-smoke: runner binary missing: {rp}; run `make cpp-build`",
                  file=sys.stderr)
            return 2

    run_id = uuid.uuid4().hex
    machine_id = os.uname().nodename[:16]
    flags_sha = _flags_sha("smoke")
    seed = int(cfg["seed"])
    n_samples = int(smoke["n_samples"])
    cache_state = smoke["cache_state"]
    block_budget = int(smoke["block_budget_bytes"])
    dist = smoke["distribution"]
    conn = _open_db(db_path)

    ok = True
    for baseline in smoke["baselines"]:
        for layout in smoke["layouts"]:
            cell_id = (f"a6-smoke-{baseline}-{smoke['dataset']}-{smoke['column']}"
                       f"-{dist}-{layout}-{cache_state}")
            out = _drive_one_cell(
                build_root, baseline, fixture, cell_id=cell_id,
                block_budget_bytes=block_budget, cache_state=cache_state,
                n_samples=n_samples, seed=seed, query_distribution=dist,
                query_layout=layout, flags_sha=flags_sha)
            _insert_row(conn, run_id=run_id, cell_id=cell_id, baseline=baseline,
                        dataset=smoke["dataset"], column_name=smoke["column"],
                        query_distribution=dist, query_layout=layout,
                        cache_state=cache_state, seed=seed, n_samples=n_samples,
                        column_path=fixture, runner_out=out,
                        machine_id=machine_id, flags_sha=flags_sha)
            print(f"a6-smoke: {cell_id} → status={out['status']} "
                  f"p50={out['metrics'].get('l_ra_p50_ns')}")
            ok = ok and out["status"] == "ok"

    conn.close()
    if not ok:
        print("a6-smoke: FAIL", file=sys.stderr)
        return 1
    print("a6-smoke: OK")
    return 0


def _resolve_column(by_name: dict, columns_dir: Path, ds_name: str,
                    col_name: str) -> Path | None:
    if ds_name not in by_name:
        print(f"a6: dataset {ds_name!r} not in manifest; skipping", file=sys.stderr)
        return None
    ds_entry = by_name[ds_name]
    col_entry = next((c for c in ds_entry["columns"] if c["name"] == col_name), None)
    if col_entry is None:
        print(f"a6: column {ds_name}/{col_name} not in manifest; skipping",
              file=sys.stderr)
        return None
    out_fname = col_entry.get("output_filename", f"{col_name}.col")
    col_path = columns_dir / ds_name / out_fname
    if not col_path.exists():
        print(f"a6: {ds_name}/{col_name} not materialised ({col_path}); "
              f"run `make datasets` first", file=sys.stderr)
        return None
    return col_path


def _run_full(build_root: Path, datasets_manifest: Path, a6_cells: Path,
              columns_dir: Path, db_path: Path) -> int:
    cells_cfg = yaml.safe_load(a6_cells.read_text())
    datasets_cfg = yaml.safe_load(datasets_manifest.read_text())
    by_name = {e["name"]: e for e in datasets_cfg["datasets"] if e.get("kind") == "real"}

    run_id = uuid.uuid4().hex
    machine_id = os.uname().nodename[:16]
    flags_sha = _flags_sha("headline")
    seed = int(cells_cfg["seed"])
    n_samples = int(cells_cfg["n_samples"])
    block_budget = int(cells_cfg["block_budget_bytes"])
    combos = _dist_layout_combos(cells_cfg["distributions"], cells_cfg["layouts"])
    conn = _open_db(db_path)
    n_ok = n_total = 0

    for col_spec in cells_cfg["columns"]:
        ds_name, col_name = col_spec["dataset"], col_spec["column"]
        col_path = _resolve_column(by_name, columns_dir, ds_name, col_name)
        if col_path is None:
            continue

        for cache_state in cells_cfg["cache_states"]:
            for baseline in cells_cfg["baselines"]:
                for dist, layout in combos:
                    n_total += 1
                    cell_id = (f"a6-{baseline}-{ds_name}-{col_name}"
                               f"-{dist}-{layout}-{cache_state}")
                    out = _drive_one_cell(
                        build_root, baseline, col_path, cell_id=cell_id,
                        block_budget_bytes=block_budget, cache_state=cache_state,
                        n_samples=n_samples, seed=seed, query_distribution=dist,
                        query_layout=layout, flags_sha=flags_sha)
                    _insert_row(conn, run_id=run_id, cell_id=cell_id,
                                baseline=baseline, dataset=ds_name,
                                column_name=col_name, query_distribution=dist,
                                query_layout=layout, cache_state=cache_state,
                                seed=seed, n_samples=n_samples,
                                column_path=col_path, runner_out=out,
                                machine_id=machine_id, flags_sha=flags_sha)
                    print(f"a6: {cell_id} → status={out['status']} "
                          f"p50={out['metrics'].get('l_ra_p50_ns')}")
                    n_ok += out["status"] == "ok"

    conn.close()
    print(f"a6: {n_ok}/{n_total} cells ok")
    return 0 if n_ok == n_total else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--build-root", type=Path, default=DEFAULT_BUILD)
    p.add_argument("--datasets-manifest", type=Path,
                   default=REPO_ROOT / "experiments" / "datasets.yaml")
    p.add_argument("--a6-cells", type=Path,
                   default=REPO_ROOT / "experiments" / "a6_cells.yaml")
    p.add_argument("--columns-dir", type=Path,
                   default=REPO_ROOT / "datasets" / "materialized")
    p.add_argument("--db", type=Path,
                   default=REPO_ROOT / "build" / "a6_results.sqlite")
    p.add_argument("--smoke", action="store_true")
    a = p.parse_args()

    if a.smoke:
        return _run_smoke(a.build_root, a.db)
    return _run_full(a.build_root, a.datasets_manifest, a.a6_cells,
                     a.columns_dir, a.db)


if __name__ == "__main__":
    raise SystemExit(main())
