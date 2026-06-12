# FSST baseline — configuration and fairness notes

## Upstream

| Field | Value |
|---|---|
| Project | cwida/fsst |
| Tag | (none — no upstream release tags exist) |
| Commit SHA | `e638d4cf8c26129d73c242a4127b42b975de5b63` (fetched) |
| License | MIT |
| Vendored at | `third_party/fsst/` (snapshot; no submodule, no download-at-install by policy) |
| Vendoring scope | library files only — `fsst.h`, `libfsst.hpp`, `libfsst.cpp`, `fsst_avx512.cpp`. Excludes the bundled CLI (upstream `fsst.cpp` with `int main`), the `fsst12/` variant (`fsst12.h`, `libfsst12.{hpp,cpp}`), tests/, examples/, paper/, doc/. |
| Provenance file | `third_party/fsst/PROVENANCE.txt` + `SHA256SUMS` |

**Reproducibility caveat.** cwida/fsst has no upstream release tags as of the
fetch date. The commit SHA above is the authoritative version designator;
reproducibility relies on the SHA pin + the SHA256SUMS manifest. fingerprint-strict mode reads SHA256SUMS as the third PROVENANCE entry (after
zstd, LZ4).

**Vendoring scope.** The required file set is
`{fsst.h, libfsst.hpp, libfsst.cpp, fsst_avx512.cpp}` because
`libfsst.cpp` references `fsst_compressAVX512` / `fsst_hasAVX512`
unconditionally inside `namespace libfsst {}`. The AVX-512 intrinsics inside
`fsst_avx512.cpp` are gated by `__AVX512F__`; on M1/ARM the file compiles to
the scalar no-op fallback and `fsst_hasAVX512()` returns false, so
`libfsst.cpp`'s runtime gate takes the scalar path. The PROVENANCE
records the full library file set explicitly.

## Build

Sidesteps upstream's CMake. Local `third_party/fsst/CMakeLists.txt` builds
`libfsst_static.a` from `libfsst.cpp` + `fsst_avx512.cpp` with the
repository-wide flags (`-O3 -march=native`); the AVX-512 TU compiles to a
scalar stub on CPUs without `__AVX512F__`.

C++ standard for the FSST TUs: `-std=c++17` (scoped via
`target_compile_features(fsst_static PRIVATE cxx_std_17)`). The runner code
linking these is C++20 (project default). Single-thread fairness is
structurally enforced: `fsst_duplicate` (the MT API) is **never called**
from `runners/fsst/`; FSST has no MT-gating macro in its sources, so the
enforcement is the absence of call sites.

## Ops surface

| Op | Supported | Mode |
|---|---|---|
| `compress`        | yes | `fsst_create` + `fsst_export` + `fsst_compress` (single batch call over column) |
| `bulk_decode`     | yes | `fsst_import` + per-row `fsst_decompress` |
| `random_decode`   | yes | `fsst_import` + single-row `fsst_decompress(idx)` |
| `prefix_search`   | yes | `prefix_search_mode() == DecompressThenScan` (third claimant of `runners/core/decompress_then_scan.{h,cpp}`) |

FSST has no native compressed-domain prefix search; we explicitly use the
harness's `decompress_then_scan` helper, identical to what zstd / LZ4 use
and what every other DecompressThenScan baseline will use.

## Wire format (native; NOT block-framed)

On-disk layout of the compressed payload (`out_path` of `compress`):

```
[H u32]                            row count for header (≤ FSST_MAXHEADER = 2066)
[header[H] u8]                     fsst_export() output (symbol table)
[N u32]                            row count
[compressed_offsets[N+1] u32]      Arrow offsets into codewords[]
[codewords[Σ Lc_i] u8]             contiguous fsst_compress output (no per-row framing)
```

**Why no block framing.** FSST has true string-level random access by
design — `compressed_offsets[i]` is sufficient to locate row `i`'s codewords.
Block framing would add complexity with no decode-cost benefit. The block-frame helpers in `lz4_runner.cpp` /
`zstd_runner.cpp` (`plan_blocks` / `build_block_raw` / `parse_block`) are
**not used** and **not copied** into the FSST runner.

`compressed_offsets[]` is the cumulative sum of `lenOut[]` (the per-row
compressed sizes returned by `fsst_compress`). No `col_to_sorted[]` (FSST is
per-string, order-preserving). No `block_first_string[]` (no blocks).

## Ratio sub-counters (natural split)

```
dict_bytes       = H                       # fsst_export header (symbol table); ≤ 2066 B
tokens_bytes     = Σ compressed_len_i      # the codeword stream
boundaries_bytes = 4 (H scalar)
                 + 4 (N scalar)
                 + 4·(N+1)                 # Arrow compressed_offsets[]
                 = 8 + 4·(N+1)
```

Header billed to `dict_bytes` (NOT boundaries) for symmetry with OnPair's
`Dictionary::bytes_used()` — BPE/OnPair size-matched pairing policy. **First natural
dict/tokens split** in the lineup (zstd + LZ4 use the boundaries-conservative
collapsed shape; FSST through OnPair will have natural splits, except
RPDAC/PFC/HTFC which collapse per ).

`dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes` exactly to
the byte. This is **bit-tight** like LZ4 (FSST wire has no epilogue / no
trailing magic), not slack-tolerant like zstd. The Python smoke test
asserts the identity strictly.

`R = Σ|s_i| / (dict_bytes + tokens_bytes + boundaries_bytes)` per.6.

## Timing rule (load-bearing for cross-baseline symmetry)

Inside the `compress` timed region (training is billed — lock):
- column read (file I/O at boundary)
- `fsst_create(N, lenIn[], strIn[], 0)` — train encoder on the full column (sample = entire column per )
- `fsst_export(encoder, header_buf)` — serialise symbol table
- `fsst_compress(encoder, N, lenIn[], strIn[], outsize, output, lenOut[], strOut[])` — single batch call
- cumulative-sum `lenOut[]` → `compressed_offsets[]`
- wire materialisation
- `fsst_destroy(encoder)` (one-time tear-down)

Inside `bulk_decode` / `random_decode` / `prefix_search` timed region:
- wire read
- `fsst_decoder_t decoder; fsst_import(&decoder, header_buf)` — reconstruct decoder
- per-row (or single-row for `random_decode`): `fsst_decompress(&decoder, Lc_i, codeword_ptr, dst_capacity, dst_ptr)`
- op-specific tail: write column / extract one string / canonical `scan_prefix` over the decoded column (via `decompress_then_scan`)

**No state-buffer member fields** carried across calls. Unlike LZ4's
`LZ4_sizeofState()` buffer (per-runner-lifetime allocation), FSST's encoder
is a per-`compress`-call object (its size depends on the trained vocabulary)
and the decoder is a stack-local `fsst_decoder_t` (~8 KB) constructed
inside every decode-side timed region. **Structurally enforced no-cache:**
the runner holds no mutable cache field; each invocation rebuilds.

**Same pessimistic-load rule as zstd / LZ4.** Six subsequent baselines
(RPDAC, PFC, HTFC, BigRePair, BPE, RLZ) and OnPair must put their
decoder-construction step inside every decode timed region — is the
load-bearing inheritance.

## Round-trip self-tests

- `tests/fsst_roundtrip_test.cpp`: three variants of a synthetic column
  round-tripped through `compress → bulk_decode`:
  - vanilla 5K-row token-vocabulary column;
  - **empty-string stress**: 4 zero-length rows interleaved (FSST API
    contract supports `lenIn[i] == 0`);
  - **escape-code stress**: 1500-row column mixing vocab strings with
    high-entropy random bytes — forces FSST_ESC code path.
- `tests/fsst_random_decode_test.cpp`: 5K-row column at headline config;
  10+ `random_decode` probes including index 0 and N-1; assert each emits
  `col.at(idx)` bytewise.
- `tests/fsst_prefix_search_test.cpp`: 3K-row column seeded with the 16
  G6 needles; for each needle assert FSST `prefix_search` hit-set ==
  `scan_prefix` reference (G6 evaluated locally).
- `tests/fsst_decoder_symmetry_test.cpp` (FSST-specific): exercises the
  two decoder-construction paths — path A `fsst_decoder(encoder)` direct
  from live encoder vs path B `fsst_export → fsst_import` through the
  serialised header. Asserts struct memcmp == 0 AND bit-identical per-row
  decoded output. Validates that the runner's chosen path (B, since A
  would require holding the encoder across calls and violate the
  no-state-cache rule) is byte-equivalent to A.
- `tests/test_fsst_smoke.py`: end-to-end handshake + one compress op,
  asserts `dict_bytes + tokens_bytes + boundaries_bytes == output_bytes`
  bit-tight, `dict_bytes ≤ FSST_MAXHEADER`, and `output_bytes <
  input_bytes` on the word-vocabulary smoke column.

The orchestrator's existing G6 / G7 / round-trip checks (`orchestrate.py`)
fire automatically at smoke for the `smoke-fsst` cell — the non-Uncompressed
branch routes FSST through the `bulk_decode(compress(x))` byte-equality
assertion.

## What is NOT done at headline

| Item | reference | Phase | Rationale |
|---|---|---|---|
| `fsst12` variant (4095 codes) | | appendix | canonical citation form is plain `fsst` (255 codes); fsst12 reserved for optional appendix only |
| Sub-sample training | | appendix | full-column training matches FSST-as-deployed and preserves OnPair BPE/OnPair size-matched pairing; sub-sample variants are diagnostic only |
| `fsst_duplicate` (MT) | by design | excluded forever | single-thread fairness; never called from `runners/fsst/` |
| Bundled CLI (`fsst.cpp` upstream) | by design | not vendored | not linked into the measurement path; never vendored |
| Native compressed-domain prefix search | | excluded forever | breaks NativeOnCompressed=OnPair-only harness rule; invites symmetry complaint from libCSD reviewers |

## Canonical headline config

FSST has no level dimension, but the headline carries **two same-machine operating
points** to settle the AVX-512 fairness objection (audit W3 / V-A):

```json
{"variant":"fsst","training":"full_column"}                 ← fsst        (AVX-512 encoder, default)
{"variant":"fsst","training":"full_column","simd":false}    ← fsst_scalar (AVX-512 forced off)
```

`simd:false` forces the scalar compression path *even on AVX-512 hardware*, via a
documented local patch to the vendored `fsst_hasAVX512()` (honours the
`FSST_DISABLE_AVX512` env var — see `third_party/fsst/PROVENANCE.txt` `local_patches`).
The runner sets that env around the timed `compress` under an RAII guard, so a process
serving multiple cells never leaks it.

**Why same machine, not two machines.** AVX-512 accelerates FSST's *compression* only —
`fsst_decompress` is scalar on every ISA. So `fsst` and `fsst_scalar` produce
byte-identical output (the SIMD encoder is byte-compatible, libfsst.cpp `chunk=511`
note) ⇒ **identical ratio**, and both decode through the same scalar path ⇒ **identical
decode `T_d`**. The pair differs *only* in compression `T_c`, isolating AVX-512's effect
to the one axis it touches. Each `compress` result row carries `fsst_avx512` (1.0 =
AVX-512 path active this op, 0.0 = scalar) so the headline can label the two points;
both read 0.0 on a non-AVX-512 box (e.g. the Arrow Lake dev rig). The `fsst_scalar`
baseline is served by the same `fsst` runner binary via `RUNNER_ALIASES` in
`orchestrator/modes.py`.

## Cross-baseline rules

| Rule | Status | Notes |
|---|---|---|
| (training inside compress timed region) | **load-bearing source** | every baseline follows this rule. Excluding training would silently subsidise FSST's `compress_throughput_bytes_per_s` vs OnPair. |
| (DecompressThenScan for non-OnPair compressed baselines) | inherits | declared via `prefix_search_mode == DecompressThenScan`; G7-asserted; third claimant of `runners/core/decompress_then_scan.{h,cpp}`. |
| (sorted-dict permutation framing) | n/a | FSST is per-string, order-preserving; no `col_to_sorted`. |
| (collapsed sub-counter decomposition) | n/a | natural split (`dict_bytes = H`, `tokens_bytes = Σ Lc_i`); not collapsed. |
| (upstream pin) | enforced | SHA `e638d4cf…de5b63` pinned (no tags exist). |
| (full-column training) | enforced | `fsst_create` receives every column row. |
| (training inside compress timed region) | enforced | `fsst_create + fsst_export + fsst_compress` all inside the timed region. |
| (DecompressThenScan) | enforced | uniform with zstd / LZ4 / future libCSD. |
| (`fsst` only, 255 codes) | enforced | `fsst12` not built into the runner. |
| Single-thread fairness | enforced | `fsst_duplicate` never called; no MT macro in sources. |
| Pessimistic-load timing | enforced | `fsst_import` inside every decode timed region; no decoded-state cache (structurally). |

**Template inheritance note.** Sections §3 (Wire format), §4 (Sub-counters),
§5 (Timing rule), §6 (Round-trip self-tests structure) and §8 (Inheritance
ledger shape) of this document serve as the template for the other baselines
inherited by RPDAC, PFC/HTFC, BigRePair, BPE, RLZ, and OnPair. Each
each baseline substitutes its own native data structures (RPDAC's
DAC-encoded grammar, PFC/HTFC's buckets, BigRePair's grammar+start, BPE's
SentencePiece proto+tokens, RLZ's reference+phrases, OnPair's
dict+packed-tokens) for FSST's symbol-table + codeword-stream, but the
shape — timing rule, header → `dict_bytes`, no decoded-state cache,
no `col_to_sorted` for per-string baselines — is locked here.
