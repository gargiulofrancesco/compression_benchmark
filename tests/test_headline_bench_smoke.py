"""— headline bench-smoke end-to-end gate.

Generates the headline smoke cells doc and drives it through the canonical
orchestrator (the same code path `make bench` uses at scale), then asserts the
headline results.sqlite holds the expected rows with valid compress sub-counters
+ a sane ratio for each baseline. Skipped if the required runner binaries are
not built. This is the dev-box proof that the headline wiring works before the
real `make bench` runs on the rig.
"""
from __future__ import annotations
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD = REPO_ROOT / "build"
NEEDED = {
    "uncompressed": BUILD / "runners" / "uncompressed" / "uncompressed_runner",
    "zstd": BUILD / "runners" / "zstd" / "zstd_runner",
    "onpair": BUILD / "runners" / "onpair" / "onpair_runner",
}


@pytest.mark.skipif(not all(p.exists() for p in NEEDED.values()),
                    reason=f"headline smoke runner binaries missing: {NEEDED}")
def test_bench_smoke_end_to_end(tmp_path: Path) -> None:
    cells = tmp_path / "headline_cells.smoke.yaml"
    db = tmp_path / "headline_smoke.sqlite"
    csv = tmp_path / "headline_smoke.csv"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONHASHSEED": "0"}

    gen = subprocess.run(
        [sys.executable, "-m", "experiments.gen_headline_cells",
         "--smoke", "--out", str(cells)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert gen.returncode == 0, gen.stderr
    assert cells.exists()

    orch = subprocess.run(
        [sys.executable, "-m", "orchestrator.orchestrate",
         "--build-root", str(BUILD),
         "--cells", str(cells),
         "--modes", str(REPO_ROOT / "experiments" / "baseline_modes.yaml"),
         "--needles", str(REPO_ROOT / "testdata" / "g6_needles.txt"),
         "--db", str(db), "--csv", str(csv),
         "--workdir", str(tmp_path / "wd")],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180)
    # exit 0 ⇒ G6 self-consistency passed for every cell.
    assert orch.returncode == 0, (
        f"orchestrate failed: stdout={orch.stdout[-2000:]!r} stderr={orch.stderr[-2000:]!r}")

    conn = sqlite3.connect(str(db))
    baselines = {r[0] for r in conn.execute("SELECT DISTINCT baseline FROM results")}
    assert baselines == {"uncompressed", "zstd", "onpair"}, baselines

    # Every baseline produced rows for all four headline ops.
    for b in baselines:
        ops = {r[0] for r in conn.execute(
            "SELECT DISTINCT op FROM results WHERE baseline=?", (b,))}
        assert {"compress", "bulk_decode", "random_decode", "prefix_search"} <= ops, (b, ops)

    # G6 passed (recorded as a metric row) for each cell.
    g6 = conn.execute(
        "SELECT baseline, metric_value FROM results WHERE metric_name='g6_passed'").fetchall()
    assert g6 and all(v == 1.0 for _b, v in g6), g6

    # Compress ratio is sane for the genuinely-compressing baseline (zstd) on
    # this fixture; onpair/uncompressed may inflate on synthetic-random text.
    def med(baseline, metric):
        row = conn.execute(
            "SELECT median FROM results_agg WHERE baseline=? AND op='compress' "
            "AND metric_name=?", (baseline, metric)).fetchone()
        return row[0] if row else None

    z_in, z_out = med("zstd", "input_bytes"), med("zstd", "output_bytes")
    assert z_in and z_out and z_in / z_out > 1.0, (z_in, z_out)
    conn.close()
