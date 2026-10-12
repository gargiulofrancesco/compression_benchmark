"""RLZ Python smoke: NDJSON handshake + compress + bit-tight identity at
R%=5%. Pure C++ + libdivsufsort implementation. Target bit-tight identity:
    R_len + ceil(P_total * (bits_per_pos + bits_per_len + 8) / 8) + (28 + 4*(N+1))
        == output_bytes
(bits_per_len, default 8 for the short-string len field, is
 stored on the wire; the scalar header is 28 bytes.)
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
RUNNER = BUILD / "runners" / "rlz" / "rlz_runner"


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
                    reason="rlz_runner not built (run `make build`)")
def test_rlz_handshake_and_compress(tmp_path: Path) -> None:
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
        assert hello["name"] == "rlz"
        assert hello["prefix_search_mode"] == "DecompressThenScan"
        assert hello["baseline_build_flags_sha"] == sha
        assert hello["version"].startswith("rlz-pureCpp")

        # Build a column large enough that R%=5% yields K >= 1 (column >=
        # ~327 KB so that 5% × col >= 16 KB block_size).
        N = 4000
        col = tmp_path / "in.col"
        rows = _make_repetitive_column(seed=123, n_rows=N)
        _write_column(col, rows)
        cz = tmp_path / "z.bin"
        cfg = json.dumps({
            "reference_size_fraction": 0.05,
            "block_size": 16384,
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
        # 28 = wire scalars R_len,N,P_total,bits_per_pos,bits_per_len (20) +
        # decoded_bytes u64 (8, lets bulk_decode pre-size its output outside the
        # timer); + (N+1)*4. (bits_per_len added in the revised.)
        assert metrics["boundaries_bytes"] == 28 + 4 * (N + 1)
        assert metrics["num_rows"] == N
        # Bit-tight identity (target eighth bit-tight baseline).
        assert (metrics["dict_bytes"]
                + metrics["tokens_bytes"]
                + metrics["boundaries_bytes"]) == metrics["output_bytes"]
        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
