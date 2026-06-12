"""Spawn the zstd runner binary, handshake, drive one compress op end-to-end.

Smoke-grade sanity: handshake fields match expectations; a compress op against
a tiny temp column returns status='ok' with output_bytes < input_bytes (the
synthetic generator is mildly compressible by zstd at L3).
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
RUNNER = BUILD / "runners" / "zstd" / "zstd_runner"


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


@pytest.mark.skipif(not RUNNER.exists(), reason="zstd_runner not built (run `make build`)")
def test_zstd_handshake_and_compress(tmp_path: Path) -> None:
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
        assert hello["name"] == "zstd"
        assert hello["prefix_search_mode"] == "DecompressThenScan"
        assert hello["baseline_build_flags_sha"] == sha
        assert hello["version"] == "1.5.6"

        col = tmp_path / "in.col"
        # ~200 KB column — large enough that the within-offsets boundary share
        # is small relative to the frame, so tokens_bytes is meaningfully > 0
        # under the conservative accounting formula in zstd_runner.cpp.
        import random as _rnd
        _rnd.seed(123)
        rows = []
        alphabet = b"abcdefghijklmnopqrstuvwxyz0123456789 "
        for _ in range(2000):
            n = _rnd.randint(64, 192)
            rows.append(bytes(_rnd.choice(alphabet) for _ in range(n)))
        _write_column(col, rows)
        cz = tmp_path / "z.bin"
        op = {"type": "op", "op": "compress", "cell_id": "smoke",
              "input_path": str(col), "output_path": str(cz),
              "config_json": '{"level":3,"block_budget_bytes":1048576}'}
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
            f"expected zstd to compress; got out={metrics['output_bytes']} in={metrics['input_bytes']}"

        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
