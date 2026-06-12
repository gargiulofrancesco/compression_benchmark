"""step 7 — A6 driver smoke gate.

Invokes `python -m experiments.a6_driver --smoke` against the committed V-D
fixture; asserts a6_results.sqlite contains the six expected rows — zstd (block),
fsst + onpair (per-string), each under Zipf(1.0) × {identity, scramble} — with
valid L_ra percentiles (p50 ≤ p95 ≤ p99, tail positive). Verifies the block
baseline carries a positive block count while the per-string baselines carry NULL
n_blocks, and that both layouts are present. Skipped if any of the three runner
binaries are not built.
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
FSST = BUILD / "runners" / "fsst" / "fsst_runner"
ONPAIR = BUILD / "runners" / "onpair" / "onpair_runner"

# The shipping L_ra path is the x86_64 rdtscp timer (ra_timer_tsc.h); on any
# non-x86 build the ra_latency op returns refused-env by design, so this
# end-to-end smoke is meaningful only on x86_64. The portable batched-chrono
# correctness oracle is exercised by the C++ ra_measure_test instead.
_X86 = platform.machine() in ("x86_64", "AMD64", "amd64")


def _controlled_env() -> bool:
    # Timing drivers refuse unless the environment is controlled: 'performance'
    # governor on all CPUs AND turbo off (see orchestrator/env_probe.py). An
    # uncontrolled box returns status=refused-env, not a result.
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from orchestrator.env_probe import env_control
        return env_control()[0]
    except Exception:
        return False


@pytest.mark.skipif(not _X86,
                    reason="L_ra rdtscp measurement path is x86_64-only (refused on this arch)")
@pytest.mark.skipif(not _controlled_env(),
                    reason="requires a controlled environment: performance governor on all CPUs "
                           "and turbo off (see README 'Environment control')")
@pytest.mark.skipif(not (ZSTD.exists() and FSST.exists() and ONPAIR.exists()),
                    reason=f"runner binaries missing: {ZSTD} / {FSST} / {ONPAIR}")
def test_a6_smoke_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "a6.sqlite"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "experiments.a6_driver", "--smoke",
         "--db", str(db),
         "--build-root", str(BUILD)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"a6_driver --smoke failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "a6-smoke: OK" in result.stdout, result.stdout

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT baseline, status, query_distribution, query_layout, cache_state, "
        "n_samples, input_rows, compressed_bytes, n_blocks, "
        "l_ra_p50_ns, l_ra_p95_ns, l_ra_p99_ns, l_ra_mean_ns, l_ra_min_ns, "
        "l_ra_max_ns FROM a6_results ORDER BY baseline, query_layout"
    ).fetchall()
    conn.close()

    # 3 baselines × 2 layouts = 6 rows.
    assert len(rows) == 6, f"expected 6 rows, got {len(rows)}"
    assert {r[0] for r in rows} == {"zstd", "fsst", "onpair"}
    assert {r[3] for r in rows} == {"identity", "scramble"}

    def check_percentiles(r) -> None:
        (_baseline, status, qdist, qlayout, _cache, n_samples, in_rows,
         comp_bytes, _nblocks, p50, p95, p99, mean, lo, hi) = r
        assert status == "ok", f"expected status=ok, got {status} ({_baseline}/{qlayout})"
        assert qdist == "zipf1.0"
        assert qlayout in ("identity", "scramble")
        assert n_samples == 2000
        assert in_rows == 1000          # V-D fixture is 1000 rows, used whole.
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

    by = {}
    for r in rows:
        check_percentiles(r)
        by[(r[0], r[3])] = r

    # Block baseline: positive block count under both layouts.
    for layout in ("identity", "scramble"):
        z = by[("zstd", layout)]
        assert z[8] is not None and z[8] > 0, (
            f"zstd n_blocks must be a positive count ({layout})")

    # Per-string baselines: NULL n_blocks (no blocks) under both layouts.
    for baseline in ("fsst", "onpair"):
        for layout in ("identity", "scramble"):
            assert by[(baseline, layout)][8] is None, (
                f"{baseline} n_blocks must be NULL ({layout})")
