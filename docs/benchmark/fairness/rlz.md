# RLZ baseline — configuration and fairness notes

## Upstream

- **Algorithm**: Hoobin/Puglisi/Zobel, "Relative Lempel-Ziv Compression of
  Genomes for Large-Scale Storage and Retrieval", VLDB 2011; Kuruppu PhD
  thesis 2012.
- **Static dep**: libdivsufsort 2.0.1 (Yuta Mori 2003-2008; MIT; tag `2.0.1`,
  HEAD SHA `82b01f76`); vendored at [third_party/libdivsufsort/](../../../third_party/libdivsufsort/);
  see [PROVENANCE.txt](../../../third_party/libdivsufsort/PROVENANCE.txt).
- **NOT linked**: skuruppu/RLZ (canonical academic implementation; GPL-3.0).
  The pure C++ implementation under [runners/rlz/rlz_core.{h,cpp}](../../../runners/rlz/)
  follows Hoobin et al.'s published algorithm verbatim; not a code fork.

## 
selected skuruppu/RLZ (GPL-3.0; with libcds GPL-3.0-or-later
+ libdivsufsort MIT transitive deps). Empirical inspection at integration
time revealed:

1. **No in-process API.** `RLZCompress(char **argv, int argc, char encoding,
   bool isshort, bool isliss)` takes argv-style filename arrays; the
   constructor reads from disk and writes to a custom binary output file.
2. **Custom binary output format.** Remarshalling into our wire shape would
   require re-implementing skuruppu/RLZ's bit-encoder state-machine.
3. **Heavy transitive GPL surface area.** libcds 1.0.8 vendoring brings in
   ~10 KLOC of 2011-era C++ (succinct data structures runtime) that requires
   Apple clang patches.
4. **Wrapper-layer ceremony exceeds algorithm cost.** The "thin wrapper"
   anticipated by would in practice be a BigRePair-style fork+exec
   + tempfile-materialisation + binary-format-reparse layer substantially
   larger than the algorithm itself (~200 lines greedy LZ77 + SA-based
   longest-match).

For these reasons the harness adopts a pure C++ implementation under
[runners/rlz/rlz_core.{h,cpp}](../../../runners/rlz/), whose sole external
dependency is libdivsufsort 2.0.1 (MIT); no GPL code is linked. Provenance
is preserved via the published academic reference (Hoobin et al. VLDB
2011), which the implementation follows directly.

**Configuration decisions that remain binding:**
- uniform random-sampled blocks at `seed=42`.
- three headline R% cells {1%, 5%, 25%}.
- natural dict/tokens/boundaries split.
- per-string against shared reference (third NON-inheritance,
  third distinct rationale).
- block_size = 16 KB.
- RNG seed = 42 reproducibility binding.
- training inside compress timed region (sixth inheritance).
- mixed-width packing (bits_per_pos + bits_per_len + 8); `bits_per_len`
  revised from a fixed 16 to a stored, configurable width (default 8)
  — see "Why an 8-bit `len`" below.
- `DecompressThenScan` inheritance (ninth consecutive).

## Build configuration

- dev flags: `-O3 -DNDEBUG -fvisibility=hidden -fno-omit-frame-pointer`.
  C99 for vendored libdivsufsort TUs (`-std=c99`); C++20 for runner +
  bitpack_mixed.
- libdivsufsort built via [third_party/libdivsufsort/CMakeLists.txt](../../../third_party/libdivsufsort/CMakeLists.txt)
  thin shim (upstream's `cmake_minimum_required(VERSION 2.4)` rejected by
  modern CMake; we bypass with pre-baked `config.h` + `divsufsort.h` under
  [onpair_bench_shim/](../../../third_party/libdivsufsort/onpair_bench_shim/)).
- `BUILD_DIVSUFSORT64=OFF` — 32-bit SA only. Reference R length capped at
  4 GB; sufficient for smoke + measurement (R never exceeds
  1 GB even at R%=25% on the largest planned column).

## Ops surface

All four ops supported: `compress` / `bulk_decode` / `random_decode` /
`prefix_search`. `prefix_search_mode == DecompressThenScan` (ninth claimant
of the runner-core helper at
[runners/core/decompress_then_scan.{h,cpp}](../../../runners/core/decompress_then_scan.h)).

## Wire format (non-inheritance, third rationale)

```
[R_len u32]                       reference R byte count = K · block_size
[R[R_len] u8]                     raw uniform-sampled reference bytes
[N u32]                           column row count
[P_total u32]                     total phrase-triple count = Σ phrases_i
[bits_per_pos u32]                = max(10, ⌈log2(R_len + 1)⌉); ∈ [10, 32]
[bits_per_len u32]                len field width; default 8 (revised )
[decoded_bytes u64]               exact decoded payload size (decode pre-sizes
                                  its output buffer OUTSIDE the timed region)
[phrase_offsets[N+1] u32]         per-row phrase-count cumsum
[packed_phrases[ceil(P_total·(bits_per_pos+bits_per_len+8)/8)] u8]
                                  heterogeneous triples (pos, len, literal)
                                  at bits_per_pos + bits_per_len + 8 bits resp.;
                                  LSB-first within each byte.
```

**Three distinct NON-inheritances now in lineup:**
- BigRePair: single-text concatenation.
- BPE: per-string Encode without sort.
- **RLZ: per-string against shared external reference R.**

No `col_to_sorted[N]` permutation (column row order preserved natively via
Arrow `phrase_offsets[N+1]`).

**Tokens cap**: `bits_per_pos` capped at 32 (R_len ≤ 4 GB; libdivsufsort
32-bit SA limit). `len` capped at `(1 << bits_per_len) − 1` — **255 with the
revised 8-bit default**. If a natural match would exceed the cap, the algorithm
advances `len + 1` bytes and emits subsequent factors covering the remainder.

**Why an 8-bit `len`.** This benchmark compresses
**short strings, each parsed INDEPENDENTLY** against the shared reference (no
cross-row factors — a literal always terminates a factor inside its row). Copy
lengths are therefore tiny: measured mean match is **~3.5–22 bytes** across the
headline columns, and matches > 255 are vanishingly rare. The previous 16-bit
`len` spent 8 bits per factor encoding a range the workload never uses; dropping
to 8 bits recovers **~16–19 % ratio on every headline column** (e.g. URL
3.34 → 3.89, geonames/name 1.02 → 1.22, l_comment 2.56 → 3.01) with **no
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

**multi-cell tradeoff**: across R% ∈ {1%, 5%, 25%} on a fixed column,
`dict_bytes` is strictly monotonic-increasing (R_len = K·block_size grows
linearly in R%) and `tokens_bytes` is strictly monotonic-decreasing (more R
→ longer matches → fewer factors). Asserted in
`tests/rlz_reference_size_disjointness_test.cpp`.

## Timing rule (inheritance — sixth consecutive)

Inside the `compress` timed region:
1. Concatenate corpus from `Column`.
2. Sample K = ⌈R% · L / block_size⌉ uniform blocks via `std::mt19937(seed)`
   into reference R.
3. Build 32-bit SA over R via `divsufsort(R, SA, R_len)`.
4. For each row, greedy LZ77 factor-parse against R: at position p, find
   longest prefix of `row[p:]` appearing in R via SA binary search (capped
   at 65535 bytes), emit `(pos, len, literal = row[p+len])`, advance by
   `len + 1`. Literal-only case (no R-match): emit `(0, 0, row[p])`,
   advance by 1.
5. Bit-pack the (pos, len, literal) stream via
   [runners/core/bitpack_mixed.h](../../../runners/core/bitpack_mixed.h).
6. Wire materialisation.

Inside every `bulk_decode` / `random_decode` / `prefix_search` timed region:
- Read wire from disk; reconstruct R, phrase offsets, unpacked triples;
  reconstruct row(s).

**the BPE/OnPair size-matched pairing explicit NON-participant** (+ ): RLZ's training
(sampling + SA + factor-parse) is structurally different from OnPair's
pair-merge. Self-test #13 documents informationally with no assertion;
the binding is **BPE↔OnPair only** (V=16K ↔ bits=14).

## Stochastic training (+ )

Uniform random-sampled blocks (Hoobin 2011 canonical method). Seed locked at
`42`; recorded in every result row's `config_json`; fingerprint-
strict-mode rerun manifest binds the seed. Self-test
`rlz_seed_determinism_test.cpp`:
- `seed=42` × 2 compress invocations → bytewise-identical wire output.
- `seed=43` → different wire bytes but identical decoded column (bytewise
  round-trip is the load-bearing correctness invariant; seed-dependent R
  selection affects only ratio, not correctness).

## OOM-as-data (third participant after BigRePair + BPE)

`setrlimit(RLIMIT_AS, mem_cap_gb << 30)` + `std::bad_alloc` →
`OpResult{.status="oom"}`. SA-build is the dominant memory hit at large
R% × large column (~5·R_len peak). `tests/rlz_oom_test.cpp` exercises the
plumbing at intentionally-tiny `mem_cap_gb=1` + R%=25%.

## Refused-env path (first participant; load-bearing)

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
(`REFUSED_ENV_EXIT=78`; `"refused-env"` in `VALID` set). RLZ is the load-
bearing exerciser of this status path. Smoke cell at R%=5% on the 1.27 MB
synthetic-V-D-smoke column has `K=4` (sampling succeeds), so smoke-rlz-R5
exercises the OK path; refused-env path exercised in
`tests/rlz_refused_env_test.cpp` + `tests/test_rlz_refused_env.py`.

## Multi-cell headline

Three R% cells at: `{0.01, 0.05, 0.25}` of column size. Fractional
sizing for cross-dataset comparability. §8.5 ratio plot renders RLZ as a
connected curve across R%; §8.6 sub-counter plot shows three RLZ bars per
column with the monotonic tradeoff above.

**Smoke pruning** (per BPE precedent): single canonical middle R%=5% smoke
cell. Multi-R% structural coverage in C++ ctest
(`rlz_reference_size_disjointness_test`). measurement runs all three
headline R% on real datasets.

## Single-thread fairness

Single-thread by construction (mt19937 is single-threaded; divsufsort built
with `USE_OPENMP=OFF`; factor-parse is sequential). CI grep audit at
[tools/lint/rlz_scope_audit.sh](../../../tools/lint/rlz_scope_audit.sh)
prohibits `pthread_create` / `std::thread` / `omp_set_num_threads` in
`runners/rlz/`.

## NOT done at headline

- skuruppu/RLZ's `count` / `locate` self-index APIs (inheritance +
.sh`).
- BWT-based reference construction (Hoobin 2011 §4 explores; we lock the
  uniform random-sample method per ).
- RLZAP / RLZ-Graph variants.
- Elias-gamma packing (locked mixed-width-uniform instead).
- Hierarchical RLZ (recursive R construction).
- Multi-seed variance sweep (rejected at headline per reviewer-
  requestable appendix).

## Round-trip self-tests (10 C++ + 2 Python)

C++ tests in [tests/](../../../tests/):

| Test | Gate |
|---|---|
| `rlz_roundtrip_test.cpp` | bulk + random + heavy_repetition + NUL-byte + whitespace-edge + non-UTF-8 + empty-string-mixed |
| `rlz_random_decode_test.cpp` | stride 313 + boundary probes at R%=5% |
| `rlz_prefix_search_test.cpp` | G6 bit-equality vs Uncompressed on 16 fixed needles |
| `rlz_load_save_identity_test.cpp` | `load(save())` round-trip at R%=5% |
| `rlz_empty_column_test.cpp` | N=0 + all-empty → `status='unsupported'` |
| `rlz_oom_test.cpp` | `mem_cap_gb=1` + R%=25% → `status='oom'` cleanly |
| `rlz_refused_env_test.cpp` | tiny column × R%=1% → `status='refused-env'` cleanly |
| `rlz_reference_size_disjointness_test.cpp` | dict↑ + tokens↓ across R%  |
| `rlz_bit_width_test.cpp` | `packed_phrases` size matches formula; mixed-width round-trip  |
| `rlz_seed_determinism_test.cpp` | seed=42 → identical wire; seed=43 → different wire / identical decoded  |
| `bitpack_mixed_unit_test.cpp` | shared helper unit tests (load-bearing for OnPair reuse candidate) |

Python tests:

| Test | Gate |
|---|---|
| `test_rlz_smoke.py` | hello envelope + bit-tight identity at R%=5% |
| `test_rlz_refused_env.py` | orchestrator records `status='refused-env'` cleanly + NULL metrics |
| `test_modes_g7.py` (extended) | G7 row for `rlz`; `is_lossless: true` in lossless set |

## Canonical headline configs

```
{"reference_size_fraction":0.01,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":64}
{"reference_size_fraction":0.05,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":64}
{"reference_size_fraction":0.25,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":64}
```

Smoke uses the middle (`0.05`) only.

## Configuration summary

| Decision | Value |
|---|---|
| Training cost accounting | reference sampling + SA build inside the compress timed region (uniform with every baseline) |
| Prefix-search mode | `DecompressThenScan` |
| Random-access model | per-string, against the shared resident reference |
| Implementation | pure C++ reimplementation (Hoobin et al. 2011) + libdivsufsort only |
| Reference construction | uniform random-sampled blocks, `block_size = 16384`, `seed = 42` |
| Reference sizes (headline) | {1%, 5%, 25%} of the column |
| Space accounting | natural dict/tokens/boundaries split; mixed-width factor packing |
| License posture | no GPL dependencies linked |
