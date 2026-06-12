"""Spawn the fsst runner binary, handshake, drive one compress op end-to-end.

Asserts FSST-specific bit-tight identity (dict + tokens + boundaries ==
output_bytes) and FSST_MAXHEADER sanity (H ≤ 2066). Word-vocabulary column
gives FSST plenty of repeated substrings to harvest into the symbol table.
"""
from __future__ import annotations
import json
import struct
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
RUNNER = BUILD / "runners" / "fsst" / "fsst_runner"


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


@pytest.mark.skipif(not RUNNER.exists(), reason="fsst_runner not built (run `make build`)")
def test_fsst_handshake_and_compress(tmp_path: Path) -> None:
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
        assert hello["name"] == "fsst"
        assert hello["prefix_search_mode"] == "DecompressThenScan"
        assert hello["baseline_build_flags_sha"] == sha
        assert hello["version"] == "master-e638d4cf"

        col = tmp_path / "in.col"
        # Same word-vocab column as test_lz4_smoke.py — FSST trains very well
        # on this (small vocabulary, lots of repetition) and we want a column
        # that comfortably compresses to validate the headline-config path.
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
              "config_json": '{"variant":"fsst","training":"full_column"}'}
        proc.stdin.write(json.dumps(op) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["type"] == "result"
        assert resp["status"] == "ok", f"compress failed: {resp}"

        metrics = {m["name"]: m["value"] for m in resp["metrics"]}
        N = 2000

        # FSST_MAXHEADER = 8+1+8+2048+1 = 2066 per fsst.h
        assert metrics["dict_bytes"] > 0
        assert metrics["dict_bytes"] <= 2066
        assert metrics["fsst_header_bytes"] == metrics["dict_bytes"]
        assert metrics["tokens_bytes"] > 0
        # boundaries_bytes = 4 (H) + 4 (N) + TWO 4*(N+1) Arrow-offset arrays:
        # compressed_offsets (random access) + decoded_offsets (on-wire per-row
        # boundaries that let bulk_decode decode the whole column in ONE call and
        # rebuild the output offsets UNTIMED). Bit-tight formula.
        assert metrics["boundaries_bytes"] == 8 + 2 * (N + 1) * 4
        assert metrics["num_rows"] == N
        # BIT-TIGHT identity (no zstd-style slack — FSST wire has no epilogue).
        assert (metrics["dict_bytes"]
                + metrics["tokens_bytes"]
                + metrics["boundaries_bytes"]) == metrics["output_bytes"]
        # And FSST should easily compress the word-vocab column.
        assert metrics["output_bytes"] < metrics["input_bytes"], \
            f"expected fsst to compress; got out={metrics['output_bytes']} in={metrics['input_bytes']}"

        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
