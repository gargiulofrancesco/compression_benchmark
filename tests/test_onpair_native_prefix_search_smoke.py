"""OnPair NativeOnCompressed prefix-search smoke. Drives the runner via
NDJSON; G6 bit-equality of OnPair's `starts_with` (PrefixAutomaton over
bit-packed tokens, no decompression) vs Python's canonical scan.

§8.10 reviewer-disclosure surface in Python smoke form. First and only
NativeOnCompressed claimant in the lineup.
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


def _make_column(seed: int, n_rows: int, needles: list[bytes]) -> list[bytes]:
    import random as _rnd
    _rnd.seed(seed)
    rows: list[bytes] = []
    for _ in range(n_rows):
        s = b""
        if _rnd.randint(0, 99) < 30:
            s = needles[_rnd.randint(0, len(needles) - 1)]
        L = max(1, int(_rnd.gauss(48, 12)))
        s += bytes(_rnd.randint(ord('a'), ord('z')) for _ in range(L))
        rows.append(s)
    return rows


@pytest.mark.skipif(not RUNNER.exists(),
                    reason="onpair_runner not built (run `make build`)")
def test_onpair_native_prefix_search(tmp_path: Path) -> None:
    sha = "test_" + "0" * 40
    env = {"BASELINE_BUILD_FLAGS_SHA": sha, "PATH": "/usr/bin:/bin"}
    needles = [b"GET ", b"POST ", b"DELETE ", b"http", b"user_", b"a"]
    rows = _make_column(seed=2027, n_rows=2000, needles=needles)
    col = tmp_path / "in.col"
    cz = tmp_path / "z.bin"
    hits_path = tmp_path / "hits.bin"
    _write_column(col, rows)

    proc = subprocess.Popen(
        [str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )
    try:
        hello = json.loads(proc.stdout.readline())
        assert hello["name"] == "onpair"
        assert hello["prefix_search_mode"] == "NativeOnCompressed"
        cfg = json.dumps({"bits": 16, "sample_fraction": 0.15, "seed": 42,
                          "threads": 1, "mem_cap_gb": 4})
        op = {"type": "op", "op": "compress", "cell_id": "psmoke",
              "input_path": str(col), "output_path": str(cz), "config_json": cfg}
        proc.stdin.write(json.dumps(op) + "\n"); proc.stdin.flush()
        cres = json.loads(proc.stdout.readline())
        assert cres["status"] == "ok", cres

        for needle in needles:
            op = {"type": "op", "op": "prefix_search", "cell_id": "psmoke",
                  "input_path": str(cz), "output_path": str(hits_path),
                  "needle": needle.decode("latin-1"), "config_json": "{}"}
            proc.stdin.write(json.dumps(op) + "\n"); proc.stdin.flush()
            pres = json.loads(proc.stdout.readline())
            assert pres["status"] == "ok", pres
            with open(hits_path, "rb") as f:
                nh = struct.unpack("<I", f.read(4))[0]
                got = list(struct.unpack(f"<{nh}I", f.read(nh * 4))) if nh else []
            expected = [i for i, r in enumerate(rows) if r.startswith(needle)]
            assert got == expected, (
                f"G6 mismatch needle={needle!r}: native={got[:5]}... "
                f"(n={len(got)}) expected={expected[:5]}... (n={len(expected)})")

        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush(); proc.stdin.close(); proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
