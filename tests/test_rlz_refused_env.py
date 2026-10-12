"""RLZ refused-env Python smoke. Tiny column (5000 bytes) at R%=1% →
column_size * R% = 50 < block_size=16384 → K=0 → status='refused-env'
cleanly with NULL metrics + non-empty notes. This test exercises the
orchestrator-bound code path.
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


@pytest.mark.skipif(not RUNNER.exists(),
                    reason="rlz_runner not built (run `make build`)")
def test_rlz_refused_env_path(tmp_path: Path) -> None:
    sha = "test_" + "0" * 40
    env = {"BASELINE_BUILD_FLAGS_SHA": sha, "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )
    try:
        hello = json.loads(proc.stdout.readline())
        assert hello["name"] == "rlz"

        # 100 rows × 50 bytes = 5000 bytes; R%=1% → 50 < 16384 → K=0.
        rows = [(f"row_{i:03d}_payload_xxxxx_yyyyy_zzzzz").encode("ascii") for i in range(100)]
        col = tmp_path / "tiny.col"
        _write_column(col, rows)
        cz = tmp_path / "z.bin"
        cfg = json.dumps({
            "reference_size_fraction": 0.01,
            "block_size": 16384,
            "seed": 42,
            "threads": 1,
            "mem_cap_gb": 4,
        })
        op = {"type": "op", "op": "compress", "cell_id": "refused",
              "input_path": str(col), "output_path": str(cz),
              "config_json": cfg}
        proc.stdin.write(json.dumps(op) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["type"] == "result"
        assert resp["status"] == "refused-env", \
            f"expected refused-env, got: {resp}"
        assert "reference fraction too small" in resp.get("notes", "")
        # Metrics MUST be NULL/empty for refused-style statuses (the runner
        # protocol layer omits the metrics list; only t_op_ns may be present
        # depending on protocol details).
        for m in resp.get("metrics", []):
            assert m["name"] in {"t_op_ns"}, \
                f"unexpected metric in refused-env: {m}"

        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
