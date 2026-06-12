"""ICDE — headline random-access latency grid (paper §8.4 / T2 Access
column + F5 CDF).

For every headline column × every ra_latency-capable baseline, compress at the
headline config and time uniform single-string accesses in-process via the shared
`ra_latency` op (runners/core/query_sampler.h + ra_timer_tsc.h), reporting
L_ra p50/p95/p99 (+ mean/min/max).

Roster = the 8 headline systems that implement ra_latency:
  per-string : uncompressed, fsst, bpe (V=2^14), bpe_v65536 (V=2^16), rlz, onpair
  block      : zstd (level 3, 1 MiB), lz4 (1 MiB)
`repair` has no random-access entry point (full-column decode); the paper derives
its access cost from bulk-decode time and footnotes it.

Query counts: 10^6 for per-string baselines; 10^5 for block baselines (each
access decodes a 1 MiB block, ~10^2 us — 10^5 keeps the cell under ~1 min while
leaving p99 well-resolved).

Writes one row per (column × baseline) to build/ra_headline_results.sqlite
(schema inline below). Warm cache (one decode pre-pass), uniform distribution,
identity layout, seed 42 — matching the Table-2 access protocol.

Usage:
  PYTHONPATH=. .venv/bin/python -m experiments.ra_headline_driver \
      [--columns-dir datasets/materialized] [--build-root build] \
      [--db build/ra_headline_results.sqlite]
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HEADLINE_COLUMNS = [
    ("dbpedia", "abstract.col"),
    ("msmarco_urls", "url.col"),
    ("msmarco_queries", "query.col"),
    ("amazon", "titles.col"),
    ("tpch", "p_name.col"),
    ("tpch", "c_comment.col"),
]

RUNNER_REL = {
    "uncompressed": Path("runners") / "uncompressed" / "uncompressed_runner",
    "zstd":         Path("runners") / "zstd" / "zstd_runner",
    "lz4":          Path("runners") / "lz4" / "lz4_runner",
    "fsst":         Path("runners") / "fsst" / "fsst_runner",
    "bpe":          Path("runners") / "bpe" / "bpe_runner",
    "bpe_v65536":   Path("runners") / "bpe" / "bpe_runner",
    "rlz":          Path("runners") / "rlz" / "rlz_runner",
    "onpair":       Path("runners") / "onpair" / "onpair_runner",
    # repair: per-string grammar-descent RA. Driven separately
    # (--only repair) because its in-driver compression is minutes per column.
    "repair":       Path("runners") / "repair" / "repair_runner",
}

HELLO_NAME = {"bpe_v65536": "bpe", "zstd_l1": "zstd", "zstd_l9": "zstd"}

COMPRESS_CFG = {
    "uncompressed": {},
    "zstd":   {"level": 3, "block_budget_bytes": 1048576},
    "lz4":    {"tier": "fast", "accel": 1, "block_budget_bytes": 1048576},
    "fsst":   {"variant": "fsst", "training": "full_column"},
    "bpe":    {"vocab_size": 16384, "sample_fraction": 0.15, "threads": 1,
               "mem_cap_gb": 64},
    "bpe_v65536": {"vocab_size": 65536, "sample_fraction": 0.15, "threads": 1,
                   "mem_cap_gb": 64},
    "rlz":    {"reference_size_fraction": 0.05, "block_size": 16384, "seed": 42,
               "threads": 1, "mem_cap_gb": 64},
    "onpair": {"bits": 16, "sample_fraction": 0.15, "seed": 42, "threads": 1,
               "mem_cap_gb": 64},
    "repair": {"mem_cap_gb": 64},
}

BLOCK_BASELINES = {"zstd", "lz4"}
N_PER_STRING = 1_000_000
N_BLOCK = 100_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS ra_headline_results (
  run_id TEXT, cell_id TEXT PRIMARY KEY, dataset TEXT, column_name TEXT,
  baseline TEXT, n_queries INTEGER, status TEXT,
  l_ra_mean_ns REAL, l_ra_p50_ns REAL, l_ra_p95_ns REAL, l_ra_p99_ns REAL,
  l_ra_min_ns REAL, l_ra_max_ns REAL,
  input_bytes INTEGER, compressed_bytes INTEGER,
  config_json TEXT, notes TEXT, ts_utc TEXT
);
"""


def _send(p, env):
    p.stdin.write(json.dumps(env) + "\n")
    p.stdin.flush()


def _recv(p):
    line = p.stdout.readline()
    if not line:
        raise EOFError("runner closed stdout")
    return json.loads(line)


def drive(build_root: Path, columns_dir: Path, db: Path,
          only: str | None = None, cheap_first: bool = False) -> int:
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    run_id = uuid.uuid4().hex
    failures = 0
    cols = list(HEADLINE_COLUMNS)
    if cheap_first:
        # cheap->expensive so an interrupted run keeps the most cells
        order = {"tpch": 0, "msmarco_queries": 1, "msmarco_urls": 2,
                 "amazon": 3, "dbpedia": 4}
        cols.sort(key=lambda c: order.get(c[0], 9))
    for ds, fn in cols:
        col_path = columns_dir / ds / fn
        col_name = fn.removesuffix(".col")
        for baseline, rel in RUNNER_REL.items():
            if only is not None and baseline != only:
                continue
            n_q = N_BLOCK if baseline in BLOCK_BASELINES else N_PER_STRING
            cell_id = f"ra-{ds}-{col_name}-{baseline}"
            runner = build_root / rel
            comp_cfg = COMPRESS_CFG[baseline]
            ra_cfg = {"n_samples": n_q, "seed": 42,
                      "query_distribution": "uniform",
                      "query_layout": "identity", "warmup": 1}
            status, metrics, notes = "crash", {}, ""
            with tempfile.TemporaryDirectory() as tmp:
                wire = Path(tmp) / "wire.bin"
                discard = Path(tmp) / "discard.bin"
                proc = subprocess.Popen(
                    [str(runner)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1,
                    env={"BASELINE_BUILD_FLAGS_SHA": "headline",
                         "PATH": "/usr/bin:/bin"})
                try:
                    hello = _recv(proc)
                    assert hello.get("type") == "hello", hello
                    expect = HELLO_NAME.get(baseline, baseline)
                    assert hello.get("name") == expect, (hello, expect)
                    _send(proc, {"type": "op", "op": "compress",
                                 "cell_id": cell_id,
                                 "input_path": str(col_path),
                                 "output_path": str(wire),
                                 "config_json": json.dumps(comp_cfg)})
                    rc = _recv(proc)
                    cstat = rc.get("status", "crash")
                    cmet = {m["name"]: m["value"] for m in rc.get("metrics", [])}
                    if cstat == "ok":
                        _send(proc, {"type": "op", "op": "ra_latency",
                                     "cell_id": cell_id,
                                     "input_path": str(wire),
                                     "output_path": str(discard),
                                     "config_json": json.dumps(ra_cfg)})
                        rr = _recv(proc)
                        status = rr.get("status", "crash")
                        metrics = {m["name"]: m["value"]
                                   for m in rr.get("metrics", [])}
                        metrics.update({f"c_{k}": v for k, v in cmet.items()})
                        notes = rr.get("notes", "")
                    else:
                        status, notes = cstat, rc.get("notes", "")
                    _send(proc, {"type": "bye"})
                    proc.wait(timeout=10)
                except Exception as e:  # noqa: BLE001
                    notes = f"driver error: {e}"
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            if status != "ok":
                failures += 1
            conn.execute(
                "INSERT OR REPLACE INTO ra_headline_results VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, cell_id, ds, col_name, baseline, n_q, status,
                 metrics.get("l_ra_mean_ns"), metrics.get("l_ra_p50_ns"),
                 metrics.get("l_ra_p95_ns"), metrics.get("l_ra_p99_ns"),
                 metrics.get("l_ra_min_ns"), metrics.get("l_ra_max_ns"),
                 metrics.get("c_input_bytes"), metrics.get("c_output_bytes"),
                 json.dumps({"compress": comp_cfg, "ra_latency": ra_cfg}),
                 notes, _dt.datetime.now(_dt.timezone.utc).isoformat()))
            conn.commit()
            print(f"ra_headline: {cell_id} -> {status} "
                  f"p50={metrics.get('l_ra_p50_ns')}", flush=True)
    print(f"ra_headline: done ({failures} failures)")
    return 0 if failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--columns-dir", type=Path,
                    default=REPO_ROOT / "datasets" / "materialized")
    ap.add_argument("--build-root", type=Path, default=REPO_ROOT / "build")
    ap.add_argument("--db", type=Path,
                    default=REPO_ROOT / "build" / "ra_headline_results.sqlite")
    ap.add_argument("--only", default=None,
                    help="run a single baseline (e.g. repair)")
    ap.add_argument("--cheap-first", action="store_true")
    a = ap.parse_args()
    return drive(a.build_root, a.columns_dir, a.db, a.only, a.cheap_first)


if __name__ == "__main__":
    sys.exit(main())
