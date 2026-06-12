"""step 2 — end-to-end NDJSON probe of the new held_out_compress op.

Confirms:
  - The OnPair runner accepts op="held_out_compress".
  - The metrics array contains all 14 expected names.
  - Other runners cleanly respond status="unsupported" for the same op
    (regression for the default ABC fallback).
"""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from orchestrator.column_io import Column, write_column
from orchestrator.heldout import split_heldout


REPO_ROOT = Path(__file__).resolve().parent.parent
ONPAIR_RUNNER = REPO_ROOT / "build" / "runners" / "onpair" / "onpair_runner"
UNCOMPRESSED_RUNNER = REPO_ROOT / "build" / "runners" / "uncompressed" / "uncompressed_runner"


EXPECTED_METRIC_NAMES = {
    "sample_bytes_consumed_fixed",
    "matched_fraction",
    "train_input_bytes",
    "heldout_input_bytes",
    "dict_bytes_fixed", "tokens_bytes_fixed", "boundaries_bytes_fixed",
    "heldout_output_bytes_fixed", "heldout_ratio_fixed",
    "dict_bytes_dyn", "tokens_bytes_dyn", "boundaries_bytes_dyn",
    "heldout_output_bytes_dyn", "heldout_ratio_dyn",
    "t_op_ns",  # auto-added by the protocol loop
}


def _make_synthetic_column(n_rows: int = 200) -> Column:
    import random
    rng = random.Random(20260531)
    alphabet = b"abcdefghijklmnopqrstuvwxyz"
    parts: list[bytes] = []
    offsets = [0]
    for _ in range(n_rows):
        row = bytes(rng.choice(alphabet) for _ in range(rng.randint(10, 25)))
        parts.append(row)
        offsets.append(offsets[-1] + len(row))
    return Column(bytes_=b"".join(parts), offsets=offsets)


@pytest.mark.skipif(not ONPAIR_RUNNER.exists(),
                    reason=f"OnPair runner binary missing: {ONPAIR_RUNNER}")
def test_held_out_compress_metrics_complete(tmp_path: Path) -> None:
    col = _make_synthetic_column()
    full = tmp_path / "full.col"
    train = tmp_path / "train.col"
    heldout = tmp_path / "heldout.col"
    write_column(str(full), col)
    split_heldout(full, train, heldout)

    sub_env = {"BASELINE_BUILD_FLAGS_SHA": "probe", "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(ONPAIR_RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )
    try:
        _ = json.loads(proc.stdout.readline())
        cfg = json.dumps({
            "bits": 16, "seed": 42, "mem_cap_gb": 4,
            "threshold_strategy": "fixed_dynamic_paired", "threshold_value": 10,
        })
        env = {
            "type": "op", "op": "held_out_compress", "cell_id": "probe",
            "train_input_path": str(train), "heldout_input_path": str(heldout),
            "output_path": "/dev/null",
            "config_json": cfg,
        }
        proc.stdin.write(json.dumps(env) + "\n"); proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp.get("status") == "ok", (
            f"status={resp.get('status')} notes={resp.get('notes')}"
        )
        observed = {m["name"] for m in resp.get("metrics", [])}
        missing = EXPECTED_METRIC_NAMES - observed
        assert not missing, f"missing metrics: {missing}"

        proc.stdin.write('{"type":"bye"}\n'); proc.stdin.flush()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)


@pytest.mark.skipif(not UNCOMPRESSED_RUNNER.exists(),
                    reason=f"uncompressed runner binary missing: {UNCOMPRESSED_RUNNER}")
def test_other_runner_returns_unsupported(tmp_path: Path) -> None:
    """The default Runner ABC implementation must return status='unsupported'
    for op='held_out_compress' on any runner that hasn't overridden it."""
    col = _make_synthetic_column()
    full = tmp_path / "full.col"
    train = tmp_path / "train.col"
    heldout = tmp_path / "heldout.col"
    write_column(str(full), col)
    split_heldout(full, train, heldout)

    sub_env = {"BASELINE_BUILD_FLAGS_SHA": "probe", "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(UNCOMPRESSED_RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
    )
    try:
        _ = json.loads(proc.stdout.readline())
        env = {
            "type": "op", "op": "held_out_compress", "cell_id": "probe",
            "train_input_path": str(train), "heldout_input_path": str(heldout),
            "output_path": "/dev/null",
            "config_json": "{}",
        }
        proc.stdin.write(json.dumps(env) + "\n"); proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp.get("status") == "unsupported"
        assert "not implemented" in resp.get("notes", "")

        proc.stdin.write('{"type":"bye"}\n'); proc.stdin.flush()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)
