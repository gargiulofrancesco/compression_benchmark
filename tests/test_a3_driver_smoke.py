"""step 3 — A3 driver smoke gate.

Invokes `python -m experiments.a3_driver --smoke` against the committed V-D
fixture; asserts a3_results.sqlite contains exactly one row with valid metrics.
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
def test_a3_smoke_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "a3.sqlite"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "experiments.a3_driver", "--smoke",
         "--db", str(db),
         "--runner-path", str(RUNNER)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"a3_driver --smoke failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "a3-smoke: OK" in result.stdout, result.stdout

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT cell_id, status, sample_fraction, threshold_strategy, "
        "train_input_rows, heldout_input_rows, sample_bytes_consumed, "
        "effective_fraction, num_dict_tokens, "
        "dict_bytes, tokens_bytes, boundaries_bytes, heldout_output_bytes, "
        "heldout_ratio, train_ns, encode_ns "
        "FROM a3_results"
    ).fetchall()
    conn.close()

    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    (cell_id, status, frac, strategy, n_train, n_heldout, sample_bytes,
     eff_frac, n_dict, db_b, tb_b, bb_b, ob_b, ratio, train_ns, encode_ns) = rows[0]

    assert status == "ok", f"expected status=ok, got {status}"
    assert frac == 0.30
    assert strategy == "dynamic"
    assert n_train == 800
    assert n_heldout == 200
    assert sample_bytes > 0
    assert 0.0 < eff_frac <= 1.0
    assert n_dict > 0

    # Bit-tight identity for the single block.
    assert db_b + tb_b + bb_b == ob_b, (
        f"bit-tight identity broken: {db_b}+{tb_b}+{bb_b}={db_b+tb_b+bb_b} != {ob_b}"
    )

    assert ratio > 0.0
    assert train_ns >= 0.0
    assert encode_ns >= 0.0
