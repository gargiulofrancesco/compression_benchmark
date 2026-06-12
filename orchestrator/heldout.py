"""Deterministic last-fraction held-out splitter for real-dataset columns.

Held-out portion: the deterministic last 20% of each real-data column is
reserved for held-out compression-ratio measurements (threshold/budget
ablations). A shuffled split was rejected because it would require a
per-dataset seed audit; deterministic last-fraction is dataset-independent.

Input/output: binary column format from orchestrator.column_io
(`[N u32][total u32][offsets[N+1] u32][bytes[total] u8]`).

The split preserves row order in both partitions: train = first 80%,
heldout = last 20%, with bytewise concatenation `train ++ heldout == original`
when both columns share their internal row order with the source.
"""
from __future__ import annotations
import argparse
import math
from pathlib import Path

from .column_io import Column, read_column, write_column


_MIN_ROWS_TO_SPLIT = 5  # refuse to split too-small columns; heldout would be 0 or 1 row


def split_heldout(in_path: Path, train_out: Path, heldout_out: Path,
                  heldout_fraction: float = 0.20) -> tuple[int, int]:
    """Write (train_out, heldout_out) as the first 1-frac / last frac partition
    of in_path by row order. Returns (N_train, N_heldout). Idempotent.

    Refuses N < _MIN_ROWS_TO_SPLIT — heldout would be too small to be meaningful.
    Edge case n_rows == 0 raises ValueError (empty column has nothing to split).
    """
    if not 0.0 < heldout_fraction < 1.0:
        raise ValueError(f"heldout_fraction must be in (0, 1); got {heldout_fraction}")

    col = read_column(str(in_path))
    n = col.num_rows
    if n == 0:
        raise ValueError(f"split_heldout: empty column at {in_path}")
    if n < _MIN_ROWS_TO_SPLIT:
        raise ValueError(
            f"split_heldout: column at {in_path} has N={n} rows < "
            f"_MIN_ROWS_TO_SPLIT={_MIN_ROWS_TO_SPLIT}; heldout would be degenerate"
        )

    n_heldout = max(1, math.ceil(n * heldout_fraction))
    n_train = n - n_heldout
    if n_train < 1:
        raise ValueError(
            f"split_heldout: heldout_fraction={heldout_fraction} on N={n} "
            f"leaves N_train={n_train} < 1"
        )

    train_bytes = col.bytes_[: col.offsets[n_train]]
    train_offsets = col.offsets[: n_train + 1]
    train_col = Column(bytes_=train_bytes, offsets=list(train_offsets))

    heldout_byte_start = col.offsets[n_train]
    heldout_bytes = col.bytes_[heldout_byte_start:]
    heldout_offsets = [o - heldout_byte_start for o in col.offsets[n_train:]]
    heldout_col = Column(bytes_=heldout_bytes, offsets=heldout_offsets)

    write_column(str(train_out), train_col)
    write_column(str(heldout_out), heldout_col)

    return n_train, n_heldout


def main() -> int:
    p = argparse.ArgumentParser(
        description="Split a column file into a train (first 1-frac) + heldout (last frac) pair."
    )
    p.add_argument("input", type=Path, help="Input column file")
    p.add_argument("train_out", type=Path, help="Output column file (first 1-frac of rows)")
    p.add_argument("heldout_out", type=Path, help="Output column file (last frac of rows)")
    p.add_argument("--heldout-fraction", type=float, default=0.20)
    a = p.parse_args()

    n_train, n_heldout = split_heldout(a.input, a.train_out, a.heldout_out, a.heldout_fraction)
    print(f"split_heldout: N_train={n_train}, N_heldout={n_heldout} "
          f"({n_heldout / (n_train + n_heldout):.4f} of total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
