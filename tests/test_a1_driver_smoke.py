"""A1 driver smoke test.

Invokes `python -m experiments.a1_driver --smoke` against the committed synthetic
fixture; asserts a1_results.sqlite contains exactly one row with valid metrics.
Skipped if the OnPair runner binary is not built (smoke runs `make build` first).
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
def test_a1_smoke_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "a1.sqlite"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "experiments.a1_driver", "--smoke",
         "--db", str(db),
         "--runner-path", str(RUNNER)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"a1_driver --smoke failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "a1-smoke: OK" in result.stdout, result.stdout

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT cell_id, status, fixed_threshold_T, train_input_rows, "
        "heldout_input_rows, sample_bytes_consumed_fixed, matched_fraction, "
        "dict_bytes_fixed, tokens_bytes_fixed, boundaries_bytes_fixed, "
        "heldout_output_bytes_fixed, heldout_ratio_fixed, "
        "dict_bytes_dyn, tokens_bytes_dyn, boundaries_bytes_dyn, "
        "heldout_output_bytes_dyn, heldout_ratio_dyn "
        "FROM a1_results"
    ).fetchall()
    conn.close()

    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    (cell_id, status, T, n_train, n_heldout, sample_bytes, matched_f,
     db_f, tb_f, bb_f, ob_f, ratio_f,
     db_d, tb_d, bb_d, ob_d, ratio_d) = rows[0]

    assert status == "ok", f"expected status=ok, got {status}"
    assert T == 10
    assert n_train == 800
    assert n_heldout == 200
    assert sample_bytes > 0
    assert 0.0 < matched_f <= 1.0

    # Bit-tight identity for both blocks.
    assert db_f + tb_f + bb_f == ob_f, (
        f"Fixed bit-tight identity broken: {db_f}+{tb_f}+{bb_f}={db_f+tb_f+bb_f}"
        f" != {ob_f}"
    )
    assert db_d + tb_d + bb_d == ob_d, (
        f"Dyn bit-tight identity broken: {db_d}+{tb_d}+{bb_d}={db_d+tb_d+bb_d}"
        f" != {ob_d}"
    )

    assert ratio_f > 0.0
    assert ratio_d > 0.0
