"""step 5 — A4 driver smoke gate.

Invokes `python -m experiments.a4_driver --smoke` against the committed V-D
fixture (used whole — A4 is a full-column experiment); asserts a4_results.sqlite
contains exactly one row with valid metrics, including a positive M_train
(peak RSS of the compress-only process). Skipped if the OnPair runner binary
is not built.
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
def test_a4_smoke_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "a4.sqlite"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "experiments.a4_driver", "--smoke",
         "--db", str(db),
         "--runner-path", str(RUNNER)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"a4_driver --smoke failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "a4-smoke: OK" in result.stdout, result.stdout

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT cell_id, status, bits, capacity, sample_fraction, "
        "threshold_strategy, input_rows, input_bytes, dict_bytes, tokens_bytes, "
        "boundaries_bytes, output_bytes, ratio, compress_ns, decode_ns, "
        "m_train_bytes FROM a4_results"
    ).fetchall()
    conn.close()

    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    (cell_id, status, bits, capacity, frac, strategy, n_rows, in_b, db_b, tb_b,
     bb_b, ob_b, ratio, compress_ns, decode_ns, m_train_bytes) = rows[0]

    assert status == "ok", f"expected status=ok, got {status}"
    assert bits == 12
    assert capacity == 1 << 12          # capacity ≡ 2^bits = 4096.
    assert frac == 0.15
    assert strategy == "dynamic"
    assert n_rows == 1000          # V-D fixture is 1000 rows, used whole.
    assert in_b > 0

    # Bit-tight identity for the full column.
    assert db_b + tb_b + bb_b == ob_b, (
        f"bit-tight identity broken: {db_b}+{tb_b}+{bb_b}={db_b+tb_b+bb_b} != {ob_b}"
    )

    # R = input / output.
    assert ratio > 0.0
    assert abs(ratio - in_b / ob_b) < 1e-6

    # T_c and T_d are wall-time positives.
    assert compress_ns > 0.0
    assert decode_ns > 0.0

    # M_train — peak RSS of the compress-only process — is a positive byte count.
    # (Even a tiny column loads the binary + onpair into RSS, so this is well
    # above zero; we only assert positivity + a sane lower bound.)
    assert m_train_bytes > 0, "M_train (peak RSS) must be positive"
    assert m_train_bytes > 1_000_000, (
        f"M_train implausibly small ({m_train_bytes} bytes) — unit/capture bug?"
    )
