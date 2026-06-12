"""step 1 — deterministic-last-fraction held-out splitter contract.

Plan §12.3 (rev-2): heldout = deterministic last 20% by row order.

The 5 invariants (per ):
  (a) N_train + N_heldout == N_total
  (b) bytewise concatenation: train ++ heldout == original column bytes
  (c) idempotent: same input → same output bytes across re-runs
  (d) refuses N < 5 (degenerate split)
  (e) heldout count within 1 row of fraction * N
"""
from __future__ import annotations
import pytest
from pathlib import Path

from orchestrator.column_io import Column, read_column, write_column
from orchestrator.heldout import split_heldout


def _make_column(rows: list[bytes]) -> Column:
    offsets = [0]
    for r in rows:
        offsets.append(offsets[-1] + len(r))
    return Column(bytes_=b"".join(rows), offsets=offsets)


def _write(tmp: Path, name: str, rows: list[bytes]) -> Path:
    p = tmp / name
    write_column(str(p), _make_column(rows))
    return p


def test_invariant_a_count_partition(tmp_path: Path) -> None:
    rows = [f"row{i:03d}".encode() for i in range(100)]
    in_col = _write(tmp_path, "in.col", rows)
    n_train, n_heldout = split_heldout(in_col, tmp_path / "train.col", tmp_path / "heldout.col")
    assert n_train + n_heldout == 100
    assert n_heldout == 20  # ceil(100 * 0.20)
    assert n_train == 80


def test_invariant_b_bytewise_concat(tmp_path: Path) -> None:
    rows = [f"row_{i:05d}".encode() for i in range(73)]  # nondivisible by 5
    in_col = _write(tmp_path, "in.col", rows)
    split_heldout(in_col, tmp_path / "train.col", tmp_path / "heldout.col")
    train = read_column(str(tmp_path / "train.col"))
    heldout = read_column(str(tmp_path / "heldout.col"))
    assert train.bytes_ + heldout.bytes_ == _make_column(rows).bytes_
    assert train.num_rows + heldout.num_rows == 73


def test_invariant_c_idempotent(tmp_path: Path) -> None:
    rows = [f"row_{i}".encode() for i in range(50)]
    in_col = _write(tmp_path, "in.col", rows)
    split_heldout(in_col, tmp_path / "train_1.col", tmp_path / "heldout_1.col")
    split_heldout(in_col, tmp_path / "train_2.col", tmp_path / "heldout_2.col")
    assert (tmp_path / "train_1.col").read_bytes() == (tmp_path / "train_2.col").read_bytes()
    assert (tmp_path / "heldout_1.col").read_bytes() == (tmp_path / "heldout_2.col").read_bytes()


def test_invariant_d_refuses_too_small(tmp_path: Path) -> None:
    for n in (1, 2, 3, 4):
        rows = [f"r{i}".encode() for i in range(n)]
        in_col = _write(tmp_path, f"in_{n}.col", rows)
        with pytest.raises(ValueError, match="_MIN_ROWS_TO_SPLIT"):
            split_heldout(in_col, tmp_path / "t.col", tmp_path / "h.col")


def test_invariant_e_ratio_within_one_row(tmp_path: Path) -> None:
    for n in (5, 11, 100, 999, 12345):
        rows = [f"r{i}".encode() for i in range(n)]
        in_col = _write(tmp_path, f"in_{n}.col", rows)
        n_train, n_heldout = split_heldout(in_col, tmp_path / "t.col", tmp_path / "h.col")
        expected = n * 0.20
        assert abs(n_heldout - expected) <= 1.0, (
            f"n={n}: heldout={n_heldout}, expected≈{expected}"
        )


def test_row_content_preserved(tmp_path: Path) -> None:
    """Each row in train + heldout matches the corresponding source row bytewise."""
    rows = [f"distinct_row_payload_{i:04d}_xyz".encode() for i in range(40)]
    in_col = _write(tmp_path, "in.col", rows)
    split_heldout(in_col, tmp_path / "train.col", tmp_path / "heldout.col")
    train = read_column(str(tmp_path / "train.col"))
    heldout = read_column(str(tmp_path / "heldout.col"))
    n_train = train.num_rows
    for i in range(n_train):
        assert train.at(i) == rows[i]
    for j in range(heldout.num_rows):
        assert heldout.at(j) == rows[n_train + j]


def test_empty_column_raises(tmp_path: Path) -> None:
    in_col = _write(tmp_path, "in.col", [])
    with pytest.raises(ValueError, match="empty column"):
        split_heldout(in_col, tmp_path / "t.col", tmp_path / "h.col")


def test_custom_fraction(tmp_path: Path) -> None:
    """Splitter accepts an explicit heldout_fraction (default 0.20 is the headline)."""
    rows = [f"r{i}".encode() for i in range(100)]
    in_col = _write(tmp_path, "in.col", rows)
    n_train, n_heldout = split_heldout(
        in_col, tmp_path / "t.col", tmp_path / "h.col", heldout_fraction=0.10
    )
    assert n_heldout == 10
    assert n_train == 90


def test_invalid_fraction_raises(tmp_path: Path) -> None:
    rows = [f"r{i}".encode() for i in range(10)]
    in_col = _write(tmp_path, "in.col", rows)
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="heldout_fraction"):
            split_heldout(in_col, tmp_path / "t.col", tmp_path / "h.col", heldout_fraction=bad)
