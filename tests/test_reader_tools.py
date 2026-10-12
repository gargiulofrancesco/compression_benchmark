"""The tools a reader runs first: the published measurements, the dataset
table, and the check that the machine is ready for timing."""
from __future__ import annotations
import sqlite3
import struct
from pathlib import Path

from tools import check_env, dataset_stats, paper_tables, published_results

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_published_measurements_unpack_and_combine(tmp_path: Path) -> None:
    combined = published_results.unpack(results=tmp_path)
    assert combined == tmp_path / "combined"
    # three passes of the main grid, pooled by the table generator
    stores = paper_tables._result_stores(combined)
    assert [p.name for p in stores] == ["results.sqlite", "results_pass2.sqlite",
                                        "results_pass3.sqlite"]
    conns = [sqlite3.connect(p) for p in stores]
    n = sum(c.execute("SELECT COUNT(*) FROM results WHERE baseline='onpair' AND "
                      "dataset='tpch/p_name' AND op='compress' AND "
                      "metric_name='t_compress_ns'").fetchone()[0] for c in conns)
    assert n == 33                                    # 11 repetitions x 3 passes
    # point access: the eight schemes of Table III and a BPE with a 2^14
    # vocabulary that the paper does not report, on six columns, all measured
    access = sqlite3.connect(combined / "ra_headline_results.sqlite")
    assert access.execute("SELECT COUNT(*), COUNT(DISTINCT baseline) FROM "
                          "ra_headline_results WHERE status='ok'").fetchone() == (54, 9)
    # nothing of the measurement machine's paths is published
    for path in tmp_path.rglob("*.json"):
        assert "/home/" not in path.read_text()


def _column(path: Path, rows: list[bytes]) -> None:
    offsets, data = [0], b"".join(rows)
    for r in rows:
        offsets.append(offsets[-1] + len(r))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<II", len(rows), len(data))
                     + struct.pack(f"<{len(offsets)}I", *offsets) + data)


def test_dataset_statistics(tmp_path: Path) -> None:
    col = tmp_path / "c.col"
    _column(col, [b"abc", b"abd", b"abc", b"x"])
    s = dataset_stats.stats(col)
    assert (s["n"], s["bytes"]) == (4, 10)
    assert s["unique_pct"] == 75.0 and s["l_avg"] == 2.5
    # sorted: abc, abc, abd, x -> common prefixes 3, 2, 0
    assert abs(s["lcp_avg"] - 5 / 3) < 1e-12
    tex = dataset_stats.table([("Col", s)])
    assert "Col & 0.00M & 0 & 75.0 & 2 & 2 \\\\" in tex


def test_a_core_the_process_cannot_use_is_not_ready(monkeypatch, capsys) -> None:
    import os
    if not hasattr(os, "sched_getaffinity"):
        return
    absent = max(os.sched_getaffinity(0)) + 4096
    monkeypatch.setattr("sys.argv", ["check_env", "--cpu", str(absent)])
    assert check_env.main() == 1
    out = capsys.readouterr().out
    assert f"measurement CPU {absent}   <- not a CPU this process may run on" in out
    assert "NOT READY" in out and "choose another with CPU=<n>" in out


def test_a_machine_as_it_is_can_measure_unless_strict(monkeypatch, capsys) -> None:
    """Governor and turbo are the paper's setup, not a requirement: a machine
    without them is ready, is told how it differs, and is refused only when
    strictness is asked for."""
    monkeypatch.setattr(check_env, "env_control", lambda: (
        False, "the frequency governor is 'powersave', not 'performance' on every CPU",
        "powersave", "on"))
    monkeypatch.setattr(check_env, "cpuinfo_flags", lambda: {"constant_tsc", "nonstop_tsc"})
    monkeypatch.delenv("STRICT_ENV", raising=False)

    monkeypatch.setattr("sys.argv", ["check_env"])
    assert check_env.main() == 0
    out = capsys.readouterr().out
    assert "governor       powersave   (the paper: performance on every CPU)" in out
    assert "READY: the timing stages can run. This is not the paper's setup:" in out
    assert "sudo cpupower frequency-set -g performance" in out

    monkeypatch.setattr("sys.argv", ["check_env", "--strict"])
    assert check_env.main() == 1
    assert "NOT READY" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["check_env", "--note"])
    assert check_env.main() == 0
    assert capsys.readouterr().out.startswith("not the paper's setup (the frequency governor")

    # without an invariant TSC the latency timer cannot work, strict or not
    monkeypatch.setattr(check_env, "cpuinfo_flags", lambda: set())
    monkeypatch.setattr("sys.argv", ["check_env"])
    assert check_env.main() == 1
    assert "invariant TSC, which the latency timer needs" in capsys.readouterr().out


def test_main_grid_records_anywhere_and_refuses_only_when_strict(monkeypatch, capsys) -> None:
    import types
    import pytest
    from orchestrator import orchestrate
    laptop = types.SimpleNamespace(env_controlled=False, env_control_reason="turbo is on, not off")
    paper = types.SimpleNamespace(env_controlled=True, env_control_reason="")
    grid, smoke = {"require_controlled_env": True}, {}
    monkeypatch.delenv("STRICT_ENV", raising=False)
    orchestrate.check_setup(grid, laptop)
    assert "rows are labelled dev-only" in capsys.readouterr().out
    for doc, env in ((grid, paper), (smoke, laptop)):
        orchestrate.check_setup(doc, env)
        assert capsys.readouterr().out == ""
    monkeypatch.setenv("STRICT_ENV", "1")
    with pytest.raises(SystemExit, match="not set up as for the paper: turbo is on"):
        orchestrate.check_setup(grid, laptop)
    orchestrate.check_setup(grid, paper)
    orchestrate.check_setup(smoke, laptop)


def test_measurement_cpu_is_an_allowed_cpu() -> None:
    import os
    cpu = check_env.measurement_cpu()
    if hasattr(os, "sched_getaffinity") and cpu is not None:
        assert cpu in os.sched_getaffinity(0)
