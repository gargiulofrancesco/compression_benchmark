"""A5 driver smoke test.

Invokes `python -m experiments.a5_driver --smoke` against the committed synthetic
fixture; asserts a5_results.sqlite contains the two expected rows — one
block-based baseline cell (zstd @ block=256) and one OnPair flat-line cell —
each with valid L_ra percentiles (p50 ≤ p95 ≤ p99, all positive). Verifies the
block-based row carries a concrete block size + block count while the OnPair row
carries NULL for both (block-independent flat reference). Skipped if the zstd or
OnPair runner binaries are not built.
"""
from __future__ import annotations
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD = REPO_ROOT / "build"
ZSTD = BUILD / "runners" / "zstd" / "zstd_runner"
ONPAIR = BUILD / "runners" / "onpair" / "onpair_runner"

# Shipping L_ra path is x86_64 rdtscp (ra_timer_tsc.h); refused on non-x86.
_X86 = platform.machine() in ("x86_64", "AMD64", "amd64")


@pytest.mark.skipif(not _X86,
                    reason="L_ra rdtscp measurement path is x86_64-only (refused on this arch)")
@pytest.mark.skipif(not (ZSTD.exists() and ONPAIR.exists()),
                    reason=f"runner binaries missing: {ZSTD} / {ONPAIR}")
def test_a5_smoke_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "a5.sqlite"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "experiments.a5_driver", "--smoke",
         "--db", str(db),
         "--build-root", str(BUILD)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"a5_driver --smoke failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "a5-smoke: OK" in result.stdout, result.stdout

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT baseline, status, block_size_strings, cache_state, "
        "query_distribution, n_samples, input_rows, compressed_bytes, n_blocks, "
        "l_ra_p50_ns, l_ra_p95_ns, l_ra_p99_ns, l_ra_mean_ns, l_ra_min_ns, "
        "l_ra_max_ns FROM a5_results ORDER BY baseline"
    ).fetchall()
    conn.close()

    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    by_baseline = {r[0]: r for r in rows}
    assert set(by_baseline) == {"zstd", "onpair"}

    def check_percentiles(r) -> None:
        (_baseline, status, _block, _cache, qdist, n_samples, in_rows,
         comp_bytes, _nblocks, p50, p95, p99, mean, lo, hi) = r
        assert status == "ok", f"expected status=ok, got {status}"
        assert qdist == "uniform"
        assert n_samples == 2000
        assert in_rows == 1000          # The fixture is 1000 rows, used whole.
        assert comp_bytes > 0
        # rdtscp per-access timing subtracts calibrated pair overhead, so the
        # fastest samples may floor at 0 (min >= 0); but NO baseline may report a
        # sub-nanosecond p50 — that was the old per-access 41ns/0ns artifact.
        assert lo >= 0.0, f"min must be >= 0, got {lo}"
        assert p50 >= 1.0, f"sub-ns p50 = latency-primitive failure, got {p50}"
        assert hi > 0.0 and p99 > 0.0 and mean > 0.0
        assert lo <= p50 <= p95 <= p99 <= hi, (
            f"percentile ordering broken: min={lo} p50={p50} p95={p95} "
            f"p99={p99} max={hi}")

    # Block-based baseline: concrete block size + block count.
    z = by_baseline["zstd"]
    check_percentiles(z)
    assert z[2] == 256, f"zstd block_size_strings should be 256, got {z[2]}"
    assert z[3] == "warm"
    assert z[8] is not None and z[8] > 0, "zstd n_blocks must be a positive count"

    # Flat reference: OnPair carries NULL block size + NULL block count.
    o = by_baseline["onpair"]
    check_percentiles(o)
    assert o[2] is None, "OnPair block_size_strings must be NULL (block-independent)"
    assert o[8] is None, "OnPair n_blocks must be NULL (no blocks)"
