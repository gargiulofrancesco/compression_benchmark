"""step 2 — OnPair runner regression for FixedThreshold cfg knob.

Spawns the OnPair runner with `threshold_strategy="fixed"` + `threshold_value=10`
cfg_json; runs the standard compress op against a tiny synthetic column;
asserts that the runner accepts the FixedThreshold strategy AND produces a
valid bit-tight compress output.
"""
from __future__ import annotations
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.column_io import Column, write_column


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "build" / "runners" / "onpair" / "onpair_runner"


def _make_column(rows: list[bytes]) -> Column:
    offsets = [0]
    for r in rows:
        offsets.append(offsets[-1] + len(r))
    return Column(bytes_=b"".join(rows), offsets=offsets)


@pytest.mark.skipif(not RUNNER.exists(),
                    reason=f"OnPair runner binary missing: {RUNNER}")
def test_fixed_threshold_compress(tmp_path: Path) -> None:
    rows = [f"row_{i:05d}_alphabet".encode() * 4 for i in range(100)]
    in_col = tmp_path / "in.col"
    out_col = tmp_path / "out.wire"
    write_column(str(in_col), _make_column(rows))

    sub_env = {"BASELINE_BUILD_FLAGS_SHA": "probe", "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )
    try:
        hello = json.loads(proc.stdout.readline())
        assert hello["type"] == "hello"
        assert hello["name"] == "onpair"

        cfg = json.dumps({
            "bits": 16, "seed": 42, "mem_cap_gb": 4,
            "threshold_strategy": "fixed", "threshold_value": 10,
        })
        env = {
            "type": "op", "op": "compress", "cell_id": "fixed-test",
            "input_path": str(in_col), "output_path": str(out_col),
            "config_json": cfg,
        }
        proc.stdin.write(json.dumps(env) + "\n"); proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp.get("type") == "result"
        assert resp.get("status") == "ok", f"status={resp.get('status')} notes={resp.get('notes')}"

        metrics = {m["name"]: m["value"] for m in resp.get("metrics", [])}
        # Bit-tight identity: dict + tokens + boundaries = output.
        assert metrics["dict_bytes"] + metrics["tokens_bytes"] + metrics["boundaries_bytes"] \
            == metrics["output_bytes"]

        proc.stdin.write('{"type":"bye"}\n'); proc.stdin.flush()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)


@pytest.mark.skipif(not RUNNER.exists(),
                    reason=f"OnPair runner binary missing: {RUNNER}")
def test_fixed_threshold_rejects_out_of_range(tmp_path: Path) -> None:
    """threshold_value outside [2, 255] must be rejected at config validation."""
    rows = [b"hello"] * 10
    in_col = tmp_path / "in.col"
    write_column(str(in_col), _make_column(rows))

    sub_env = {"BASELINE_BUILD_FLAGS_SHA": "probe", "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )
    try:
        _ = json.loads(proc.stdout.readline())  # hello
        cfg = json.dumps({
            "bits": 16, "seed": 42, "mem_cap_gb": 4,
            "threshold_strategy": "fixed", "threshold_value": 300,  # invalid
        })
        env = {
            "type": "op", "op": "compress", "cell_id": "invalid-T",
            "input_path": str(in_col), "output_path": str(tmp_path / "x.bin"),
            "config_json": cfg,
        }
        proc.stdin.write(json.dumps(env) + "\n"); proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp.get("status") == "crash"
        assert "threshold_value" in resp.get("notes", "")

        proc.stdin.write('{"type":"bye"}\n'); proc.stdin.flush()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)


@pytest.mark.skipif(not RUNNER.exists(),
                    reason=f"OnPair runner binary missing: {RUNNER}")
def test_dynamic_default_preserved(tmp_path: Path) -> None:
    """Backward-compat: cfg_json WITHOUT threshold_strategy still works
    (defaults to 'dynamic' with the existing sample_fraction semantics)."""
    rows = [f"row_{i:05d}".encode() for i in range(50)]
    in_col = tmp_path / "in.col"
    write_column(str(in_col), _make_column(rows))

    sub_env = {"BASELINE_BUILD_FLAGS_SHA": "probe", "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )
    try:
        _ = json.loads(proc.stdout.readline())  # hello
        cfg = json.dumps({
            "bits": 16, "seed": 42, "mem_cap_gb": 4,
            "sample_fraction": 0.15,  # legacy field; no threshold_strategy
        })
        env = {
            "type": "op", "op": "compress", "cell_id": "legacy",
            "input_path": str(in_col), "output_path": str(tmp_path / "out.bin"),
            "config_json": cfg,
        }
        proc.stdin.write(json.dumps(env) + "\n"); proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp.get("status") == "ok"
        proc.stdin.write('{"type":"bye"}\n'); proc.stdin.flush()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)
