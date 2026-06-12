"""step 8 — A7 driver smoke gate.

Invokes `python -m experiments.a7_driver --smoke` against the committed V-D
fixture (used whole — A7 is a full-column experiment); asserts a7_results.sqlite
contains exactly one row with valid metrics and the non-default smoke seed
plumbed through. Skipped if the OnPair runner binary is not built.
"""
from __future__ import annotations
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "build" / "runners" / "onpair" / "onpair_runner"


@pytest.mark.skipif(not RUNNER.exists(),
                    reason=f"OnPair runner binary missing: {RUNNER}")
def test_a7_smoke_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "a7.sqlite"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "experiments.a7_driver", "--smoke",
         "--db", str(db),
         "--runner-path", str(RUNNER)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"a7_driver --smoke failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "a7-smoke: OK" in result.stdout, result.stdout

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT cell_id, status, seed, bits, sample_fraction, threshold_strategy, "
        "input_rows, input_bytes, dict_bytes, tokens_bytes, boundaries_bytes, "
        "output_bytes, ratio, compress_ns "
        "FROM a7_results"
    ).fetchall()
    conn.close()

    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    (cell_id, status, seed, bits, frac, strategy, n_rows, in_b, db_b, tb_b, bb_b,
     ob_b, ratio, compress_ns) = rows[0]

    assert status == "ok", f"expected status=ok, got {status}"
    assert seed == 7              # non-default smoke seed → seed knob is wired.
    assert bits == 16             # held at headline default.
    assert frac == 0.15
    assert strategy == "dynamic"
    assert n_rows == 1000         # V-D fixture is 1000 rows, used whole.
    assert in_b > 0

    # Bit-tight identity for the full column.
    assert db_b + tb_b + bb_b == ob_b, (
        f"bit-tight identity broken: {db_b}+{tb_b}+{bb_b}={db_b+tb_b+bb_b} != {ob_b}"
    )

    # R = input / output.
    assert ratio > 0.0
    assert abs(ratio - in_b / ob_b) < 1e-6

    # T_c is a wall-time positive.
    assert compress_ns > 0.0
