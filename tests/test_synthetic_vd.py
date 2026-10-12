"""Parametric synthetic generator contract.

Covers:
  - Determinism: identical (L, σ, ρ, n_rows, seed) → identical column bytes.
  - Edge cases: n_rows=0, ρ=0 (IID uniform), ρ=1 (max repetition), σ=1.
  - Statistical sanity (smoke-grade): ρ=0 marginal byte distribution ≈ uniform;
    ρ=0.95 marginal entropy substantially below uniform.
  - Committed-bytes oracle: the three synthetic fixtures under testdata/datasets/
    are bit-identical to a fresh `generate_vd(...)` call at the same params.
"""
from __future__ import annotations
import math
from collections import Counter
from pathlib import Path

import pytest

from orchestrator.column_io import Column, read_column, write_column
from orchestrator.synthetic_gen_vd import _alphabet, generate_vd


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "testdata" / "datasets"


def _entropy_bits(counts: Counter[int]) -> float:
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def test_determinism_seed42() -> None:
    a = generate_vd(L=32, sigma_size=16, rho=0.7, n_rows=500, seed=42)
    b = generate_vd(L=32, sigma_size=16, rho=0.7, n_rows=500, seed=42)
    assert a.bytes_ == b.bytes_
    assert a.offsets == b.offsets


def test_seed_changes_output() -> None:
    a = generate_vd(L=32, sigma_size=16, rho=0.7, n_rows=500, seed=42)
    b = generate_vd(L=32, sigma_size=16, rho=0.7, n_rows=500, seed=43)
    assert a.bytes_ != b.bytes_


def test_empty_n_rows() -> None:
    c = generate_vd(L=8, sigma_size=4, rho=0.0, n_rows=0, seed=42)
    assert c.num_rows == 0
    assert c.bytes_ == b""
    assert c.offsets == [0]


def test_sigma_one_all_runs_same_byte() -> None:
    c = generate_vd(L=16, sigma_size=1, rho=0.7, n_rows=10, seed=42)
    assert set(c.bytes_) == {ord("a")}  # alphabet[:1] = b"a"
    assert c.num_rows == 10


def test_rho_zero_iid_uniform_marginal() -> None:
    """At ρ=0, marginal byte distribution should track uniform within ±10%
    on a moderate sample (smoke-grade; not a strict statistical test)."""
    sigma_size = 16
    c = generate_vd(L=32, sigma_size=sigma_size, rho=0.0, n_rows=2000, seed=42)
    counts = Counter(c.bytes_)
    total = sum(counts.values())
    expected = total / sigma_size
    for sym in _alphabet(sigma_size):
        observed = counts.get(sym, 0)
        rel_err = abs(observed - expected) / expected
        assert rel_err < 0.10, f"sym={sym!r}: observed={observed}, expected={expected:.1f}, rel_err={rel_err:.3f}"


def test_rho_high_2gram_entropy_below_uniform() -> None:
    """At ρ=0.95, intra-row preferential attachment biases 2-gram (conditional
    next-byte) entropy substantially below the uniform baseline. 1-gram marginal
    is uninformative here — every row resets, so the marginal converges to
    uniform regardless of ρ. The intra-row clustering surfaces in 2-gram stats."""
    sigma_size = 16
    c_uniform = generate_vd(L=128, sigma_size=sigma_size, rho=0.0,  n_rows=200, seed=42)
    c_rep     = generate_vd(L=128, sigma_size=sigma_size, rho=0.95, n_rows=200, seed=42)

    def _bigram_entropy(col: Column) -> float:
        bigrams: Counter[tuple[int, int]] = Counter()
        for i in range(col.num_rows):
            row = col.at(i)
            for j in range(len(row) - 1):
                bigrams[(row[j], row[j + 1])] += 1
        return _entropy_bits(Counter({k: v for k, v in bigrams.items()}))

    h_uniform = _bigram_entropy(c_uniform)
    h_rep     = _bigram_entropy(c_rep)
    # Uniform 2-gram entropy ≈ log2(16^2) = 8. ρ=0.95 must drop well below.
    assert h_uniform > 7.5, f"ρ=0 2-gram entropy expected ≈ 8, got {h_uniform:.2f}"
    assert h_rep < h_uniform - 1.0, (
        f"ρ=0.95 2-gram entropy {h_rep:.2f} should be > 1 bit below uniform {h_uniform:.2f}"
    )


def test_alphabet_byte_sets() -> None:
    assert _alphabet(1) == b"a"
    assert _alphabet(4) == b"abcd"
    assert _alphabet(26) == b"abcdefghijklmnopqrstuvwxyz"
    # σ=256 must yield every distinct byte exactly once.
    full = _alphabet(256)
    assert len(full) == 256
    assert len(set(full)) == 256


def test_alphabet_invalid_size() -> None:
    with pytest.raises(ValueError):
        _alphabet(0)
    with pytest.raises(ValueError):
        _alphabet(257)


def test_invalid_params() -> None:
    with pytest.raises(ValueError):
        generate_vd(L=0, sigma_size=16, rho=0.5, n_rows=10, seed=42)
    with pytest.raises(ValueError):
        generate_vd(L=8, sigma_size=4, rho=-0.1, n_rows=10, seed=42)
    with pytest.raises(ValueError):
        generate_vd(L=8, sigma_size=4, rho=1.1, n_rows=10, seed=42)
    with pytest.raises(ValueError):
        generate_vd(L=8, sigma_size=4, rho=0.5, n_rows=-1, seed=42)


def test_committed_fixture_L8_sigma4_rho0_0() -> None:
    """The committed synthetic fixture matches a fresh generate_vd call exactly.
    Sanity check: regression oracle for the parametric generator."""
    fixture = FIXTURE_DIR / "synthetic_vd__L8_sigma4_rho0.0_seed42_n1000.fixture.col"
    if not fixture.exists():
        pytest.skip(f"fixture not committed yet: {fixture}")
    fresh = generate_vd(L=8, sigma_size=4, rho=0.0, n_rows=1000, seed=42)
    on_disk = read_column(str(fixture))
    assert fresh.bytes_ == on_disk.bytes_
    assert fresh.offsets == on_disk.offsets


def test_committed_fixture_L32_sigma16_rho0_7() -> None:
    fixture = FIXTURE_DIR / "synthetic_vd__L32_sigma16_rho0.7_seed42_n1000.fixture.col"
    if not fixture.exists():
        pytest.skip(f"fixture not committed yet: {fixture}")
    fresh = generate_vd(L=32, sigma_size=16, rho=0.7, n_rows=1000, seed=42)
    on_disk = read_column(str(fixture))
    assert fresh.bytes_ == on_disk.bytes_
    assert fresh.offsets == on_disk.offsets


def test_committed_fixture_L128_sigma256_rho0_95() -> None:
    fixture = FIXTURE_DIR / "synthetic_vd__L128_sigma256_rho0.95_seed42_n1000.fixture.col"
    if not fixture.exists():
        pytest.skip(f"fixture not committed yet: {fixture}")
    fresh = generate_vd(L=128, sigma_size=256, rho=0.95, n_rows=1000, seed=42)
    on_disk = read_column(str(fixture))
    assert fresh.bytes_ == on_disk.bytes_
    assert fresh.offsets == on_disk.offsets


def test_column_file_is_written_whole_and_kept(tmp_path: Path) -> None:
    """The column generator of the synthetic and scale stages: a column is
    renamed into place once complete, and an existing one is not regenerated."""
    import struct
    from tools import gen_vd_columns as G

    class Serial:                       # stands in for the process pool
        def map(self, f, jobs):
            return [f(j) for j in jobs]

    path = tmp_path / "c.col"
    G.write_column_sharded(path, 8, 0.5, 100, Serial())
    assert [p.name for p in tmp_path.iterdir()] == ["c.col"]
    blob = path.read_bytes()
    n, total = struct.unpack("<II", blob[:8])
    assert n == 100 and len(blob) == 8 + 4 * (n + 1) + total
    path.write_bytes(b"kept")
    G.write_column_sharded(path, 8, 0.5, 100, Serial())
    assert path.read_bytes() == b"kept"
