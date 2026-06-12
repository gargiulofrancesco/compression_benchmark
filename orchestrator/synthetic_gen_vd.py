"""Parametric V-D synthetic generator for §8.8 N-sweep (10^5 .. 10^9) and §8.11
F6 36-cell Pareto sweep.

Paper.3 (rev-2): the synthetic V-D generator is a parametric Markov
over a prefix tree, swept over `L ∈ {8, 32, 128}` × `|Σ| ∈ {4, 16, 256}`
× `ρ ∈ {0.0, 0.3, 0.7, 0.95}` (36 cells × 10M strings per cell).

ρ semantics (see experiments/datasets.yaml
"Synthetic V-D rho semantics"):
  - With probability `ρ`, the next byte is drawn from the empirical 1-gram
    distribution of the already-emitted bytes in the current row (preferential
    attachment — heavier weight on bytes already used in this row).
  - With probability `1−ρ`, the next byte is drawn uniformly from Σ.
  - ρ=0.0  → IID-uniform over Σ. Matches the simplest classical generator.
  - ρ=0.95 → strong intra-row repetition. Marginal entropy << log2(|Σ|).

L semantics: mean Gaussian string length (σ = L / 4, clamped at L_min = 1).
σ semantics: alphabet is a deterministic byte set of size `sigma_size`
  (lowercase ASCII a..z extended with digits + symbols when sigma_size > 26;
  see _alphabet() below).

Determinism: `(L, sigma_size, rho, n_rows, seed)` fully determines the column
bytes; no randomness from the platform leaks in (uses random.Random(seed) only).
"""
from __future__ import annotations
import argparse
import random
from collections import Counter
from pathlib import Path

from .column_io import Column, write_column


# Deterministic byte alphabet up to size 256. Order is fixed so different
# Python runs against the same `sigma_size` see the identical alphabet bytes.
_PRINTABLE = (
    b"abcdefghijklmnopqrstuvwxyz"
    b"0123456789"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    b"_-./:"
)


def _alphabet(sigma_size: int) -> bytes:
    if sigma_size < 1 or sigma_size > 256:
        raise ValueError(f"sigma_size must be in [1, 256]; got {sigma_size}")
    if sigma_size <= len(_PRINTABLE):
        return _PRINTABLE[:sigma_size]
    # Beyond _PRINTABLE: extend with remaining raw bytes 0x00..0xFF in order,
    # skipping any already in _PRINTABLE so the alphabet stays a set.
    used = set(_PRINTABLE)
    extra = bytes(b for b in range(256) if b not in used)
    return _PRINTABLE + extra[: sigma_size - len(_PRINTABLE)]


def generate_vd(L: int, sigma_size: int, rho: float, n_rows: int, seed: int) -> Column:
    """Produce a V-D column. Pure-Python; deterministic given the parameters.

    Memory: per-row counter resets each row, so peak RAM is O(L + |Σ|), and
    the total output column buffer is sized incrementally row-by-row.
    """
    if L < 1:
        raise ValueError(f"L must be >= 1; got {L}")
    if not 0.0 <= rho <= 1.0:
        raise ValueError(f"rho must be in [0, 1]; got {rho}")
    if n_rows < 0:
        raise ValueError(f"n_rows must be >= 0; got {n_rows}")

    alphabet = _alphabet(sigma_size)
    sigma_list = list(alphabet)
    rng = random.Random(seed)
    sigma_stddev = max(1.0, L / 4.0)

    parts: list[bytes] = []
    offsets: list[int] = [0]
    cur = 0

    for _ in range(n_rows):
        length = max(1, int(rng.gauss(L, sigma_stddev)))
        row_counts: Counter[int] = Counter()
        row_bytes = bytearray()
        for i in range(length):
            if i == 0 or rho == 0.0 or rng.random() >= rho:
                # Uniform draw from Σ.
                b = rng.choice(sigma_list)
            else:
                # Preferential attachment: weighted draw over already-emitted
                # 1-grams in this row.
                seen = list(row_counts.items())
                weights = [c for _, c in seen]
                b = rng.choices([sym for sym, _ in seen], weights=weights, k=1)[0]
            row_bytes.append(b)
            row_counts[b] += 1
        s = bytes(row_bytes)
        parts.append(s)
        cur += len(s)
        offsets.append(cur)

    return Column(bytes_=b"".join(parts), offsets=offsets)


def main() -> int:
    p = argparse.ArgumentParser(description="Parametric V-D synthetic column generator.")
    p.add_argument("--L",      type=int,   required=True, help="Mean Gaussian string length")
    p.add_argument("--sigma",  type=int,   required=True, help="Alphabet size |Σ| (1..256)")
    p.add_argument("--rho",    type=float, required=True, help="Repetition rate (0..1)")
    p.add_argument("--n-rows", type=int,   required=True)
    p.add_argument("--seed",   type=int,   required=True)
    p.add_argument("--out",    type=Path,  required=True)
    a = p.parse_args()

    col = generate_vd(a.L, a.sigma, a.rho, a.n_rows, a.seed)
    write_column(str(a.out), col)
    print(
        f"synthetic_gen_vd: wrote {col.num_rows} rows / {len(col.bytes_)} bytes "
        f"(L={a.L} σ={a.sigma} ρ={a.rho} seed={a.seed}) → {a.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
