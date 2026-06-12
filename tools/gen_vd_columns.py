"""ICDE — pre-generate synthetic V-D columns to disk, in parallel.

Two column sets, written under datasets/synthetic_vd/:

F6 Pareto sweep (§8.9): 3×3 cells, L ∈ {8, 32, 128} × rho ∈ {0.0, 0.5, 0.95}
at sigma=16, constant ~32 MiB of payload per cell (N scales with 1/L), so every
cell presents the same workload volume:
    f6__L{L}_rho{rho}.col

N-sweep (§8.8 / T4): L=32, sigma=16, rho=0.5, N ∈ {10^5, 10^6, 10^7, 10^8}:
    nsweep__n{N}.col

Sharded generation: rows are i.i.d. given the cell parameters (the
preferential-attachment state resets every row), so a column of N rows is the
concatenation of independent shards with derived seeds seed*1000+shard —
deterministic, parallel, and distribution-identical to single-shot generation.
"""
from __future__ import annotations
import multiprocessing as mp
import os
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.synthetic_gen_vd import generate_vd  # noqa: E402

OUT_DIR = REPO_ROOT / "datasets" / "synthetic_vd"
SIGMA = 16
SEED = 42
N_SHARDS = 16

F6_BYTES_TARGET = 32 * 2**20
F6_CELLS = [(L, rho) for L in (8, 32, 128) for rho in (0.0, 0.5, 0.95)]
NSWEEP = [10**5, 10**6, 10**7, 10**8]


def _gen_shard(args):
    L, rho, n_rows, seed = args
    col = generate_vd(L=L, sigma_size=SIGMA, rho=rho, n_rows=n_rows, seed=seed)
    return col.bytes_, col.offsets


def write_column_sharded(path: Path, L: int, rho: float, n_rows: int,
                         pool: mp.Pool) -> None:
    if path.exists():
        print(f"skip (exists): {path.name}", flush=True)
        return
    per = n_rows // N_SHARDS
    counts = [per + (1 if i < n_rows % N_SHARDS else 0) for i in range(N_SHARDS)]
    jobs = [(L, rho, c, SEED * 1000 + i) for i, c in enumerate(counts) if c > 0]
    results = pool.map(_gen_shard, jobs)
    total = sum(len(b) for b, _ in results)
    n = sum(len(o) - 1 for _, o in results)
    with open(path, "wb") as f:
        f.write(struct.pack("<II", n, total))
        base = 0
        f.write(struct.pack("<I", 0))
        for b, offs in results:
            f.write(struct.pack(f"<{len(offs)-1}I", *[base + o for o in offs[1:]]))
            base += len(b)
        for b, _ in results:
            f.write(b)
    print(f"wrote {path.name}: {n} rows, {total/2**20:.1f} MiB", flush=True)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with mp.Pool(min(N_SHARDS, os.cpu_count() or 4)) as pool:
        for L, rho in F6_CELLS:
            n_rows = max(1000, F6_BYTES_TARGET // L)
            write_column_sharded(OUT_DIR / f"f6__L{L}_rho{rho}.col",
                                 L, rho, n_rows, pool)
        for n in NSWEEP:
            write_column_sharded(OUT_DIR / f"nsweep__n{n}.col",
                                 32, 0.5, n, pool)
    print("gen_vd_columns: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
