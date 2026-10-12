from __future__ import annotations

from pathlib import Path

import pytest

from experiments.prefix_driver import (
    PrefixQuery,
    _observation,
    discover_queries,
    summarise,
)
from orchestrator.column_io import Column, write_column


def _write(path: Path, rows: list[bytes]) -> None:
    offsets = [0]
    for row in rows:
        offsets.append(offsets[-1] + len(row))
    write_column(str(path), Column(bytes_=b"".join(rows), offsets=offsets))


def test_discover_queries_reports_exact_selectivity(tmp_path: Path) -> None:
    rows = [
        b"alpha-one", b"alpha-two", b"alpha-three",
        b"beta-one", b"beta-two", b"gamma",
    ]
    path = tmp_path / "input.col"
    _write(path, rows)

    queries, metadata = discover_queries(path, [1, 4], sample_cap=len(rows))
    assert metadata["num_rows"] == len(rows)
    assert metadata["sample_size"] == len(rows)
    assert {query.query_class for query in queries} == {
        "common", "sample-rare", "absent"}
    for query in queries:
        expected = sum(row.startswith(query.needle) for row in rows)
        assert query.n_hits == expected
        assert query.selectivity == expected / len(rows)
    assert any(query.query_class == "absent" and query.n_hits == 0
               for query in queries)


def test_summary_speedup_uses_raw_median(tmp_path: Path) -> None:
    query = PrefixQuery(
        query_id="common-L4", query_class="common", needle=b"alph",
        n_hits=3, n_rows=6)
    observations = []
    for sample_idx, elapsed in enumerate((100.0, 110.0, 90.0)):
        observations.append(_observation(
            "fixture", query, "uncompressed_scan", sample_idx,
            elapsed, payload_bytes=60))
    for sample_idx, elapsed in enumerate((50.0, 55.0, 45.0)):
        observations.append(_observation(
            "fixture", query, "onpair_native", sample_idx,
            elapsed, payload_bytes=60))

    summaries = summarise(observations)
    by_method = {row["method"]: row for row in summaries}
    assert by_method["uncompressed_scan"]["median_ns"] == 100.0
    assert by_method["uncompressed_scan"]["speedup_vs_uncompressed"] == 1.0
    assert by_method["onpair_native"]["median_ns"] == 50.0
    assert by_method["onpair_native"]["speedup_vs_uncompressed"] == 2.0


def test_machine_state_is_recorded_and_insisted_on_only_when_strict(monkeypatch) -> None:
    """The diagnostic stages run on a machine as it is and record its state.
    STRICT_ENV=1 insists on the paper's setup, and verifies the frequency cap
    where the kernel exposes the base frequency (intel_pstate)."""
    import os
    import types
    from experiments import prefix_driver as P
    cpu = min(os.sched_getaffinity(0))
    files = {"scaling_governor": "powersave", "scaling_max_freq": "4500000"}
    controlled = [False]
    monkeypatch.setattr(P, "_sysfs_text", lambda path: files.get(path.rsplit("/", 1)[1], ""))
    monkeypatch.setattr(P, "probe", lambda build_root=None: types.SimpleNamespace(
        env_controlled=controlled[0], env_control_reason="turbo is on, not off"))
    monkeypatch.setattr(P.dataclasses, "asdict", lambda env: {})
    monkeypatch.setattr(P, "_thermal_snapshot", lambda: {})
    expected = []
    monkeypatch.setattr(P, "_clock_load_validation",
                        lambda c, khz: expected.append(khz) or {"steady_median_khz": 4400000})

    # a laptop as it is: nothing refused, everything recorded
    monkeypatch.delenv("STRICT_ENV", raising=False)
    machine = P._machine_preflight(Path("build"), cpu)
    assert machine["cpu_governor"] == "powersave" and machine["cpu_base_khz"] is None
    assert machine["cpu_scaling_max_khz"] == 4500000 and expected == [None]

    # strict: not the paper's setup, refused
    monkeypatch.setenv("STRICT_ENV", "1")
    with pytest.raises(RuntimeError, match="not set up as for the paper: turbo is on"):
        P._machine_preflight(Path("build"), cpu)

    # strict, paper's setup, no base frequency exposed: runs, cap not verifiable
    controlled[0] = True
    files["scaling_governor"] = "performance"
    P._machine_preflight(Path("build"), cpu)
    assert expected == [None, None]

    # strict, base frequency exposed: the limit must equal it, and the clock is held to it
    files["base_frequency"] = "3900000"
    with pytest.raises(RuntimeError, match="not capped at base frequency"):
        P._machine_preflight(Path("build"), cpu)
    files["scaling_max_freq"] = "3900000"
    P._machine_preflight(Path("build"), cpu)
    assert expected == [None, None, 3900000]

    # not strict: the same machine is never held to the base frequency
    monkeypatch.delenv("STRICT_ENV")
    P._machine_preflight(Path("build"), cpu)
    assert expected[-1] is None
