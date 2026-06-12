"""Minimal V-D synthetic generator for smoke.

Produces a Column with `n_rows` strings of approximately `mean_len` bytes,
salted with prefixes from the G6 needle list so prefix_search returns
non-zero hits. Deterministic given `seed`.
"""
from __future__ import annotations
import argparse
import random
import string
from pathlib import Path

from .column_io import Column, write_column


def generate(n_rows: int, mean_len: int, seed: int, g6_needles: list[bytes]) -> Column:
    rng = random.Random(seed)
    alphabet = (string.ascii_letters + string.digits + "_/-.:").encode()
    parts: list[bytes] = []
    offsets: list[int] = [0]
    cur = 0
    for i in range(n_rows):
        # ~30% of rows are prefixed by one of the G6 needles, round-robin —
        # guarantees every needle gets at least a few hits for self-consistency.
        if rng.random() < 0.30 and g6_needles:
            prefix = g6_needles[i % len(g6_needles)]
        else:
            prefix = b""
        length = max(1, int(rng.gauss(mean_len, mean_len / 4)))
        body_len = max(0, length - len(prefix))
        body = bytes(rng.choices(alphabet, k=body_len))
        s = prefix + body
        parts.append(s)
        cur += len(s)
        offsets.append(cur)
    return Column(bytes_=b"".join(parts), offsets=offsets)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-rows",   type=int, required=True)
    p.add_argument("--mean-len", type=int, required=True)
    p.add_argument("--seed",     type=int, required=True)
    p.add_argument("--needles",  type=Path, required=True)
    p.add_argument("--out",      type=Path, required=True)
    a = p.parse_args()

    needles = [ln.encode() for ln in a.needles.read_text().splitlines() if ln]
    col = generate(a.n_rows, a.mean_len, a.seed, needles)
    write_column(str(a.out), col)
    print(f"synthetic_gen: wrote {col.num_rows} rows / {len(col.bytes_)} bytes → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
