"""OnPair Python smoke: NDJSON handshake + compress + bit-tight identity
at bits=16. Library
consumed via FetchContent from public github at SHA f8ecc64f.

Bit-tight identity (natural dict/tokens split):
    dict_bytes + tokens_bytes + boundaries_bytes == output_bytes (exactly)
Boundaries derivation (see runners/onpair/onpair_runner.h):
    boundaries_bytes = 25 + 4 * (N + 1)
    (8 MAGIC + 1 bits + 4 true_bytes_scalar + 4 dict_offsets_size +
     4 real_words + 4 boundaries_size + (N+1)*4)

OnPair-only diagnostic sub-counter:
    metric_name='dict_offsets_bytes' = (num_tokens+1)*4   <=  dict_bytes
    (surfaces internal split of dict_bytes; not a separate term of the ratio.)
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
RUNNER = BUILD / "runners" / "onpair" / "onpair_runner"


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


def _make_repetitive_column(seed: int, n_rows: int) -> list[bytes]:
    import random as _rnd
    _rnd.seed(seed)
    pool = [
        b"GET /api/v1/users/", b"POST /api/v1/orders/", b"DELETE /api/v1/items/",
        b"HTTP/1.1 200 OK ", b"session=", b"token=", b"user_id=", b"order_id=",
        b"alice ", b"bob ", b"carol ", b"dave ",
        b"INFO ", b"WARN ", b"ERROR ", b"DEBUG ",
    ]
    rows: list[bytes] = []
    for i in range(n_rows):
        parts = []
        for _ in range(6 + _rnd.randint(0, 4)):
            parts.append(_rnd.choice(pool))
            parts.append(str(_rnd.randint(0, 999)).encode("ascii"))
        rows.append(b" ".join(parts))
    return rows


@pytest.mark.skipif(not RUNNER.exists(),
                    reason="onpair_runner not built (run `make build`)")
def test_onpair_handshake_and_compress(tmp_path: Path) -> None:
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
        assert hello["name"] == "onpair"
        # OnPair is the ONLY NativeOnCompressed claimant in the lineup.
        assert hello["prefix_search_mode"] == "NativeOnCompressed"
        assert hello["baseline_build_flags_sha"] == sha
        assert hello["version"].startswith("onpair-cpp+")

        N = 3000
        col = tmp_path / "in.col"
        rows = _make_repetitive_column(seed=123, n_rows=N)
        _write_column(col, rows)
        cz = tmp_path / "z.bin"
        cfg = json.dumps({
            "bits": 16,
            "sample_fraction": 0.15,
            "seed": 42,
            "threads": 1,
            "mem_cap_gb": 4,
        })
        op = {"type": "op", "op": "compress", "cell_id": "smoke",
              "input_path": str(col), "output_path": str(cz),
              "config_json": cfg}
        proc.stdin.write(json.dumps(op) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["type"] == "result"
        assert resp["status"] == "ok", f"compress failed: {resp}"
        metrics = {m["name"]: m["value"] for m in resp["metrics"]}
        assert metrics["dict_bytes"] > 0
        assert metrics["tokens_bytes"] > 0
        # boundaries_bytes is structurally derived: 25 + 4*(N+1).
        assert metrics["boundaries_bytes"] == 25 + 4 * (N + 1)
        assert metrics["num_rows"] == N

        # Bit-tight identity.
        assert (metrics["dict_bytes"]
                + metrics["tokens_bytes"]
                + metrics["boundaries_bytes"]) == metrics["output_bytes"], (
            f"NOT bit-tight: {metrics}")

        # OnPair-only diagnostic sub-counter row.
        assert "dict_offsets_bytes" in metrics, \
            "dict_offsets_bytes diagnostic MISSING"
        # dict_offsets_bytes is the offsets-table portion of dict_bytes; the
        # diagnostic is contained within dict_bytes (NOT subtracted, not a
        # separate term of the ratio).
        assert 0 < metrics["dict_offsets_bytes"] <= metrics["dict_bytes"]
        # Must be divisible by 4 (offsets are u32).
        assert metrics["dict_offsets_bytes"] % 4 == 0

        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
