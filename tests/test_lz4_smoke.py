"""Spawn the lz4 runner binary, handshake, drive one compress op end-to-end.

Smoke-grade sanity: handshake fields match expectations; a compress op against
a ~250 KB temp column returns status='ok' with output_bytes < input_bytes (the
synthetic generator at fast/accel=1 produces ~1.1-1.3× ratio).
"""
from __future__ import annotations
import json
import os
import struct
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
RUNNER = BUILD / "runners" / "lz4" / "lz4_runner"


def _write_column(path: Path, rows: list[bytes]) -> None:
    n = len(rows)
    total = sum(len(r) for r in rows)
    offsets = [0]
    for r in rows:
        offsets.append(offsets[-1] + len(r))
    with open(path, "wb") as f:
        f.write(struct.pack("<II", n, total))
        f.write(struct.pack(f"<{n + 1}I", *offsets))
        for r in rows:
            f.write(r)


@pytest.mark.skipif(not RUNNER.exists(), reason="lz4_runner not built (run `make build`)")
def test_lz4_handshake_and_compress(tmp_path: Path) -> None:
    sha = "test_" + "0" * 40
    env = {"BASELINE_BUILD_FLAGS_SHA": sha, "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )
    try:
        hello = json.loads(proc.stdout.readline())
        assert hello["type"] == "hello"
        assert hello["name"] == "lz4"
        assert hello["prefix_search_mode"] == "DecompressThenScan"
        assert hello["baseline_build_flags_sha"] == sha
        assert hello["version"] == "1.10.0"

        col = tmp_path / "in.col"
        # ~250 KB column with repeated word substrings — LZ4 fast has no entropy
        # coder (unlike zstd), so it can only compress repeated byte runs. Real
        # string columns look like this: a small vocabulary recombined per row.
        import random as _rnd
        _rnd.seed(123)
        vocab = [
            b"GET", b"POST", b"PUT", b"DELETE", b"HTTP/1.1", b"200", b"404", b"500",
            b"/api/v1/", b"/users/", b"/orders/", b"/items/", b"/login", b"/logout",
            b"session=", b"token=", b"user_id=", b"order_id=", b"item_id=",
            b"created_at", b"updated_at", b"status:ok", b"status:err",
            b"INFO", b"WARN", b"ERROR", b"DEBUG",
            b"alice", b"bob", b"carol", b"dave", b"eve", b"frank",
            b" ", b" ", b" ", b"|", b":", b"-",
        ]
        rows = []
        for _ in range(2000):
            n_tokens = _rnd.randint(8, 24)
            row = b" ".join(_rnd.choice(vocab) for _ in range(n_tokens))
            rows.append(row)
        _write_column(col, rows)
        cz = tmp_path / "z.bin"
        op = {"type": "op", "op": "compress", "cell_id": "smoke",
              "input_path": str(col), "output_path": str(cz),
              "config_json": '{"tier":"fast","accel":1,"block_budget_bytes":1048576}'}
        proc.stdin.write(json.dumps(op) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["type"] == "result"
        assert resp["status"] == "ok", f"compress failed: {resp}"

        metrics = {m["name"]: m["value"] for m in resp["metrics"]}
        assert metrics["dict_bytes"] == 0
        assert metrics["tokens_bytes"] > 0
        assert metrics["boundaries_bytes"] > 0
        assert metrics["num_rows"] == 2000
        assert metrics["n_blocks"] >= 1
        assert metrics["output_bytes"] < metrics["input_bytes"], \
            f"expected lz4 to compress; got out={metrics['output_bytes']} in={metrics['input_bytes']}"

        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
