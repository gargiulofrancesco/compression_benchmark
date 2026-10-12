# RLZ baseline — configuration and fairness notes

## Upstream

- **Algorithm**: Hoobin, Puglisi and Zobel, "Relative Lempel-Ziv Factorization
  for Efficient Storage and Retrieval of Web Collections", PVLDB 5(3), 2011;
  Kuruppu, Puglisi and Zobel, "Relative Lempel-Ziv Compression of Genomes for
  Large-Scale Storage and Retrieval", SPIRE 2010.
- **Static dep**: libdivsufsort 2.0.1 (Yuta Mori 2003-2008; MIT; tag `2.0.1`,
  HEAD SHA `82b01f76`); vendored at [third_party/libdivsufsort/](../../../third_party/libdivsufsort/);
  see [PROVENANCE.txt](../../../third_party/libdivsufsort/PROVENANCE.txt).
- **NOT linked**: skuruppu/RLZ (canonical academic implementation; GPL-3.0).
  The pure C++ implementation under [runners/rlz/rlz_core.{h,cpp}](../../../runners/rlz/)
  follows Hoobin et al.'s published algorithm verbatim; not a code fork.

## Why a reimplementation

The canonical implementation is skuruppu/RLZ (GPL-3.0; with libcds
GPL-3.0-or-later + libdivsufsort MIT transitive deps). Inspection showed:

1. **No in-process API.** `RLZCompress(char **argv, int argc, char encoding,
   bool isshort, bool isliss)` takes argv-style filename arrays; the
   constructor reads from disk and writes to a custom binary output file.
2. **Custom binary output format.** Remarshalling into our wire shape would
   require re-implementing skuruppu/RLZ's bit-encoder state-machine.
3. **Heavy transitive GPL surface area.** libcds 1.0.8 vendoring brings in
   ~10 KLOC of 2011-era C++ (succinct data structures runtime).
4. **Wrapper-layer ceremony exceeds algorithm cost.** A wrapper
   would in practice be a fork+exec
   + tempfile-materialisation + binary-format-reparse layer substantially
   larger than the algorithm itself (~200 lines greedy LZ77 + SA-based
   longest-match).

For these reasons the harness adopts a pure C++ implementation under
[runners/rlz/rlz_core.{h,cpp}](../../../runners/rlz/), whose sole external
dependency is libdivsufsort 2.0.1 (MIT); no GPL code is linked. Provenance
is preserved via the published academic reference (Hoobin et al. VLDB
2011), which the implementation follows directly.

**Configuration:**
- uniform random-sampled blocks at `seed=42`.
- reference size R% ∈ {1%, 5%, 25%} of the column; the main grid runs 5%.
- natural dict/tokens/boundaries split.
- each row factorized on its own against the shared reference.
- block_size = 16 KB.
- RNG seed = 42 (the runner rejects any other).
- training inside compress timed region.
- mixed-width packing (bits_per_pos + bits_per_len + 8); `bits_per_len`
  is a stored, configurable width (default 8)
  — see "Why an 8-bit `len`" below.
- `DecompressThenScan` prefix search.

## Build configuration

- flags: `-O3 -DNDEBUG -march=native -fvisibility=hidden -fno-omit-frame-pointer`.
  C99 for vendored libdivsufsort TUs; C++20 for runner +
  bitpack_mixed.
- libdivsufsort built via [third_party/libdivsufsort/CMakeLists.txt](../../../third_party/libdivsufsort/CMakeLists.txt)
  thin shim (upstream's `cmake_minimum_required(VERSION 2.4)` rejected by
  modern CMake; we bypass with pre-baked `config.h` + `divsufsort.h` under
  [onpair_bench_shim/](../../../third_party/libdivsufsort/onpair_bench_shim/)).
- `BUILD_DIVSUFSORT64=OFF` — 32-bit SA only. Reference R length capped at
  2 GiB; the largest reference in the paper's runs is 32 MiB.

## Ops surface

All four ops supported: `compress` / `bulk_decode` / `random_decode` /
`prefix_search`, plus `ra_latency`. `prefix_search_mode == DecompressThenScan`:
each row is decoded up to the needle's length and compared
(`partial_prefix_scan` in
[runners/core/scan_prefix.h](../../../runners/core/scan_prefix.h)).

## Wire format

```
[R_len u32]                       reference R byte count = K · block_size
[R[R_len] u8]                     raw uniform-sampled reference bytes
[N u32]                           column row count
[P_total u32]                     total phrase-triple count = Σ phrases_i
[bits_per_pos u32]                = max(10, ⌈log2(R_len + 1)⌉); ∈ [10, 32]
[bits_per_len u32]                len field width; default 8
[decoded_bytes u64]               exact decoded payload size (decode pre-sizes
                                  its output buffer OUTSIDE the timed region)
[phrase_offsets[N+1] u32]         per-row phrase-count cumsum
[packed_phrases[ceil(P_total·(bits_per_pos+bits_per_len+8)/8)] u8]
                                  heterogeneous triples (pos, len, literal)
                                  at bits_per_pos + bits_per_len + 8 bits resp.;
                                  LSB-first within each byte.
```

Rows keep their order (Arrow-style `phrase_offsets[N+1]`); each row is
factorized on its own against the shared reference R.

**Tokens cap**: `bits_per_pos` capped at 32 (the 32-bit suffix array limits
R_len to 2 GiB). `len` capped at `(1 << bits_per_len) − 1` — **255 with the
8-bit default**. If a natural match would exceed the cap, the algorithm
advances `len + 1` bytes and emits subsequent factors covering the remainder.

**Why an 8-bit `len`.** This benchmark compresses
**short strings, each parsed INDEPENDENTLY** against the shared reference (no
cross-row factors — a literal always terminates a factor inside its row). Copy
lengths are therefore tiny: measured mean match is **~3.5–22 bytes**, and
matches > 255 are vanishingly rare. A 16-bit
`len` would spend 8 bits per factor encoding a range the workload never uses; an
8-bit field recovers **~16–19 % of ratio** on the columns it was measured on,
with **no
measurable change in factor count** (the rare > 255 match simply splits). The
field width is stored on the wire (`bits_per_len`) so the decoder is
self-describing and the width stays tunable (`max_len_bits` ∈ [4, 16], default
8) for non-short-string corpora.

**Endianness**: LSB-first within each byte, triples in increasing row +
position order; identical convention to existing
[runners/core/bitpack.h](../../../runners/core/bitpack.h).

## Sub-counter mapping (natural split)

```
dict_bytes       = R_len
tokens_bytes     = ceil(P_total · (bits_per_pos + bits_per_len + 8) / 8)
boundaries_bytes = 4 (R_len) + 4 (N) + 4 (P_total) + 4 (bits_per_pos)
                 + 4 (bits_per_len) + 8 (decoded_bytes)
                 + 4·(N+1) (phrase_offsets[])
                 = 28 + 4·(N+1)
```

**Bit-tight identity** structurally guaranteed by the wire layout:
`dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes` exactly at
every R%.

**Reference-size tradeoff**: across R% ∈ {1%, 5%, 25%} on a fixed column,
`dict_bytes` is strictly monotonic-increasing (R_len = K·block_size grows
linearly in R%) and `tokens_bytes` is strictly monotonic-decreasing (more R
→ longer matches → fewer factors). Asserted in
`tests/rlz_reference_size_disjointness_test.cpp`.

## Timing rule

`t_compress_ns` (behind the compression throughput) covers:
1. Sample K = ⌈R% · L / block_size⌉ uniform blocks via `std::mt19937(seed)`
   into reference R, straight from the column's payload.
2. Build 32-bit SA over R via `divsufsort(R, SA, R_len)`.
3. For each row, greedy LZ77 factor-parse against R: at position p, find
   longest prefix of `row[p:]` appearing in R via SA binary search (capped
   at `(1 << bits_per_len) − 1` bytes), emit `(pos, len, literal = row[p+len])`,
   advance by `len + 1`. Literal-only case (no R-match): emit `(0, 0, row[p])`,
   advance by 1.
4. Bit-pack the (pos, len, literal) stream via
   [runners/core/bitpack_mixed.h](../../../runners/core/bitpack_mixed.h).

Reading the input column and writing the wire are outside it.

`t_decode_ns` (behind the decompression throughput) covers one fused pass
that unpacks each triple and copies it from R into a pre-allocated output.
Loading the wire (the reference included), allocating the output and writing
the column are outside it. `random_decode` and each access of `ra_latency`
decode only the phrases of the requested row; `prefix_search` times the
capped decode of every row.

RLZ is not part of the BPE/OnPair size-matched pairing: its training
(sampling + SA + factor-parse) is structurally different from OnPair's
pair-merge.

## Stochastic training

Uniform random-sampled blocks (Hoobin 2011 canonical method). Seed fixed at
`42` and recorded in every result row's `config_json`. Self-test
`rlz_seed_determinism_test.cpp`:
- `seed=42` × 2 compress invocations → bytewise-identical wire output.
- the sampler, called directly with seeds 42 and 43, yields different
  references (the runner itself rejects any seed but 42).

## OOM-as-data

`setrlimit(RLIMIT_AS, mem_cap_gb << 30)` + `std::bad_alloc` →
`OpResult{.status="oom"}`. SA-build is the dominant memory hit at large
R% × large column (~5·R_len peak).

## Refused-env path

When `column_size · R% < block_size` (i.e., `K=0` after `ceil`), sampling
can't produce a block. Runner emits:

```
OpResult{
  .status = "refused-env",
  .metric_value = NULL,
  .notes = "reference fraction too small for block_size; "
           "column_size · R% = X < block_size = Y"
}
```

Plumbed via the existing
[orchestrator/status.py](../../../orchestrator/status.py) machinery
(`"refused-env"` in `VALID` set). The smoke cell at R%=5% on the 1.27 MB
smoke column has `K=4` (sampling succeeds), so smoke-rlz-R5
exercises the OK path; refused-env path exercised in
`tests/rlz_refused_env_test.cpp` + `tests/test_rlz_refused_env.py`.

## Reference sizes

The runner accepts three fractional sizes, `{0.01, 0.05, 0.25}` of the
column. The main grid (Table II) and the smoke run use `0.05`; the three
together are covered by the C++ test
`rlz_reference_size_disjointness_test`.

The model-size study (Fig. 7, Section VIII-D) sweeps absolute reference
sizes instead, from 256 KiB to 32 MiB, through the `reference_size_bytes`
option (`experiments/rlz_sweep_driver.py`).

## Single-thread fairness

Single-thread by construction (mt19937 is single-threaded; divsufsort built
without OpenMP; factor-parse is sequential).

## NOT done at headline

- skuruppu/RLZ's `count` / `locate` self-index APIs.
- BWT-based reference construction (Hoobin 2011 §4 explores it; this runner
  uses the uniform random-sample method).
- RLZAP / RLZ-Graph variants.
- Elias-gamma packing (fixed mixed-width packing instead).
- Hierarchical RLZ (recursive R construction).
- Multi-seed variance sweep.

## Round-trip self-tests (10 C++ + 3 Python)

C++ tests in [tests/](../../../tests/):

| Test | Checks |
|---|---|
| `rlz_roundtrip_test.cpp` | six column variants: all bytes, random bytes, non-UTF-8, NUL bytes, heavy repetition, empty rows mixed in |
| `rlz_random_decode_test.cpp` | stride 313 + boundary probes at R%=5% |
| `rlz_prefix_search_test.cpp` | hit-set equality vs the canonical scan on 16 fixed needles |
| `rlz_load_save_identity_test.cpp` | two compress runs at R%=5%, seed=42 → identical wire |
| `rlz_empty_column_test.cpp` | N=0 + all-empty → `status='unsupported'` |
| `rlz_refused_env_test.cpp` | tiny column × R%=1% → `status='refused-env'` cleanly |
| `rlz_reference_size_disjointness_test.cpp` | dict↑ + tokens↓ across R%  |
| `rlz_bit_width_test.cpp` | `packed_phrases` size matches formula; bit-tight identity  |
| `rlz_seed_determinism_test.cpp` | seed=42 → identical wire; the sampler differs between seeds 42 and 43  |
| `bitpack_mixed_unit_test.cpp` | shared packing helper unit tests |

Python tests:

| Test | Checks |
|---|---|
| `test_rlz_smoke.py` | hello envelope + bit-tight identity at R%=5% |
| `test_rlz_refused_env.py` | the runner answers `status='refused-env'` cleanly + NULL metrics |
| `test_modes_g7.py` | mode row for `rlz`; `is_lossless: true` in lossless set |

## Canonical headline config

```
{"reference_size_fraction":0.05,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":64}
```

The runner also accepts `reference_size_fraction` 0.01 and 0.25.

## Configuration summary

| Decision | Value |
|---|---|
| Training cost accounting | reference sampling + SA build inside the compress timed region (uniform with every baseline) |
| Prefix-search mode | `DecompressThenScan` |
| Random-access model | per-string, against the shared resident reference |
| Implementation | pure C++ reimplementation (Hoobin et al. 2011) + libdivsufsort only |
| Reference construction | uniform random-sampled blocks, `block_size = 16384`, `seed = 42` |
| Reference size (headline) | 5% of the column |
| Space accounting | natural dict/tokens/boundaries split; mixed-width factor packing |
| License posture | no GPL dependencies linked |
