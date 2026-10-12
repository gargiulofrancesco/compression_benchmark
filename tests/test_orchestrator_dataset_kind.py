"""Orchestrator dataset_kind routing contract.

The orchestrator's `_materialise_input_column` is the single entry point for
producing the input column file for a cell. Three paths:
  - synthetic_simple (default, used by the smoke cells)
  - synthetic_vd     (parametric synthetic generator)
  - real             (copy a previously-materialised .col from disk)
"""
from __future__ import annotations
from pathlib import Path

import pytest

from orchestrator.column_io import read_column
from orchestrator.orchestrate import _materialise_input_column


def test_default_synthetic_simple(tmp_path: Path) -> None:
    cell = {"cell_id": "test_default", "n_rows": 50, "mean_len": 16, "seed": 42}
    out = tmp_path / "out.col"
    _materialise_input_column(cell, out, needles=[b"al"])
    col = read_column(str(out))
    assert col.num_rows == 50


def test_explicit_synthetic_simple_matches_default(tmp_path: Path) -> None:
    cell_default = {"cell_id": "a", "n_rows": 30, "mean_len": 16, "seed": 7}
    cell_explicit = {**cell_default, "dataset_kind": "synthetic_simple", "cell_id": "b"}
    out_a = tmp_path / "a.col"
    out_b = tmp_path / "b.col"
    _materialise_input_column(cell_default,  out_a, needles=[b"al"])
    _materialise_input_column(cell_explicit, out_b, needles=[b"al"])
    assert out_a.read_bytes() == out_b.read_bytes()


def test_synthetic_vd_path(tmp_path: Path) -> None:
    cell = {
        "cell_id": "vd_test", "dataset_kind": "synthetic_vd",
        "L": 32, "sigma": 16, "rho": 0.7, "n_rows": 100, "seed": 42,
    }
    out = tmp_path / "vd.col"
    _materialise_input_column(cell, out, needles=[])
    col = read_column(str(out))
    assert col.num_rows == 100


def test_real_path(tmp_path: Path) -> None:
    fixture = (Path(__file__).resolve().parent.parent
               / "testdata" / "datasets" / "dblp__titles.fixture.col")
    cell = {
        "cell_id": "real_test", "dataset_kind": "real",
        "column_path": str(fixture),
    }
    out = tmp_path / "real.col"
    _materialise_input_column(cell, out, needles=[])
    assert out.read_bytes() == fixture.read_bytes()


def test_real_missing_path_raises(tmp_path: Path) -> None:
    cell = {
        "cell_id": "broken", "dataset_kind": "real",
        "column_path": "/nonexistent/path.col",
    }
    out = tmp_path / "x.col"
    with pytest.raises(FileNotFoundError, match="run `make datasets`"):
        _materialise_input_column(cell, out, needles=[])


def test_real_missing_column_path_raises(tmp_path: Path) -> None:
    cell = {"cell_id": "broken", "dataset_kind": "real"}  # no column_path
    out = tmp_path / "x.col"
    with pytest.raises(ValueError, match="column_path"):
        _materialise_input_column(cell, out, needles=[])


def test_unknown_dataset_kind_raises(tmp_path: Path) -> None:
    cell = {"cell_id": "weird", "dataset_kind": "telepathic"}
    out = tmp_path / "x.col"
    with pytest.raises(ValueError, match="unknown dataset_kind"):
        _materialise_input_column(cell, out, needles=[])


def test_synthetic_vd_determinism_through_orchestrator(tmp_path: Path) -> None:
    """Same synthetic_vd cell config → identical column bytes; second run is idempotent."""
    cell = {
        "cell_id": "vd_det", "dataset_kind": "synthetic_vd",
        "L": 8, "sigma": 4, "rho": 0.0, "n_rows": 250, "seed": 123,
    }
    out_a = tmp_path / "a.col"
    out_b = tmp_path / "b.col"
    _materialise_input_column(cell, out_a, needles=[])
    _materialise_input_column(cell, out_b, needles=[])
    assert out_a.read_bytes() == out_b.read_bytes()
