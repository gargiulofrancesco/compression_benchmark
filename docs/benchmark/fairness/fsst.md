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
reproducibility relies on the SHA pin + the SHA256SUMS manifest.

**Vendoring scope.** The required file set is
`{fsst.h, libfsst.hpp, libfsst.cpp, fsst_avx512.cpp}` because
`libfsst.cpp` references `fsst_compressAVX512` / `fsst_hasAVX512`
unconditionally inside `namespace libfsst {}`. The AVX-512 intrinsics inside
`fsst_avx512.cpp` are gated by `__AVX512F__`; on a CPU without AVX-512
(ARM, or x86 up to AVX2) the file compiles to
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
| `bulk_decode`     | yes | `fsst_import` + one `fsst_decompress` over the whole codeword stream |
| `random_decode`   | yes | `fsst_import` + single-row `fsst_decompress(idx)` |
| `prefix_search`   | yes | `prefix_search_mode() == DecompressThenScan`: each row is decoded up to the needle's length and compared (`partial_prefix_scan` in `runners/core/scan_prefix.h`) |

FSST has no native compressed-domain prefix search. Being a per-string
codec it does not need to decode the whole column to scan it: the runner
decodes only the first bytes of every row, as the BPE and RLZ runners do.

## Wire format (native; NOT block-framed)

On-disk layout of the compressed payload (`out_path` of `compress`):

```
[H u32]                            byte count of the header (≤ FSST_MAXHEADER = 2066)
[header[H] u8]                     fsst_export() output (symbol table)
[N u32]                            row count
[compressed_offsets[N+1] u32]      Arrow offsets into codewords[]
[decoded_offsets[N+1] u32]         the original row offsets
[codewords[Σ Lc_i] u8]             contiguous fsst_compress output (no per-row framing)
```

**Why no block framing.** FSST has true string-level random access by
design — `compressed_offsets[i]` is sufficient to locate row `i`'s codewords.
Block framing would add complexity with no decode-cost benefit.

`compressed_offsets[]` is the cumulative sum of `lenOut[]` (the per-row
compressed sizes returned by `fsst_compress`). `decoded_offsets[]` lets a
bulk decode size its output exactly and rebuild the output column's row
offsets outside the timed region. Rows keep their order (FSST is
per-string); there are no blocks.

## Ratio sub-counters (natural split)

```
dict_bytes       = H                       # fsst_export header (symbol table); ≤ 2066 B
tokens_bytes     = Σ compressed_len_i      # the codeword stream
boundaries_bytes = 4 (H scalar)
                 + 4 (N scalar)
                 + 4·(N+1)                 # compressed_offsets[]
                 + 4·(N+1)                 # decoded_offsets[]
                 = 8 + 8·(N+1)
```

Header billed to `dict_bytes` (NOT boundaries) for symmetry with OnPair's
`Dictionary::bytes_used()`: the symbol table is FSST's model, as the token
dictionary is OnPair's.

`dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes` exactly to
the byte (the FSST wire has no epilogue / no trailing magic). The Python
smoke test asserts the identity strictly.

`R = Σ|s_i| / (dict_bytes + tokens_bytes)`: like every baseline's, the ratio
counts the payload and leaves the row offsets out.

## Timing rule

`t_compress_ns` (behind the compression throughput) covers, with training
included:
- `fsst_create(N, lenIn[], strIn[], 0)` — train the encoder on the full column
- `fsst_export(encoder, header_buf)` — serialise symbol table
- `fsst_compress(encoder, N, lenIn[], strIn[], outsize, output, lenOut[], strOut[])` — single batch call
- `fsst_destroy(encoder)`
- cumulative-sum `lenOut[]` → `compressed_offsets[]`

Reading the input column, allocating the buffers and writing the wire are
outside it.

`t_decode_ns` (behind the decompression throughput) covers:
- `fsst_decoder_t decoder; fsst_import(&decoder, header_buf)` — reconstruct decoder
- one `fsst_decompress` over the whole codeword stream into a pre-allocated
  buffer. FSST has no decode state across rows, so this yields exactly the
  concatenation of the per-row decodes.

Reading the wire, allocating the output and writing the column are outside
it. `random_decode` imports the decoder and decodes one row. `ra_latency`
and `prefix_search` import the decoder once, before their timed loops, and
then time one row decode per access, or the capped decode of every row.

**No state-buffer member fields** carried across calls. Unlike LZ4's
`LZ4_sizeofState()` buffer (per-runner-lifetime allocation), FSST's encoder
is a per-`compress`-call object (its size depends on the trained vocabulary)
and the decoder is a stack-local `fsst_decoder_t` (~8 KB) constructed
in every op. **Structurally enforced no-cache:**
the runner holds no mutable cache field; each invocation rebuilds.

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
  needles; for each needle assert FSST `prefix_search` hit-set ==
  `scan_prefix` reference.
- `tests/fsst_decoder_symmetry_test.cpp` (FSST-specific): exercises the
  two decoder-construction paths — path A `fsst_decoder(encoder)` direct
  from live encoder vs path B `fsst_export → fsst_import` through the
  serialised header. Asserts bit-identical per-row
  decoded output. Validates that the runner's chosen path (B, since A
  would require holding the encoder across calls and violate the
  no-state-cache rule) is byte-equivalent to A.
- `tests/test_fsst_smoke.py`: end-to-end handshake + one compress op,
  asserts `dict_bytes + tokens_bytes + boundaries_bytes == output_bytes`
  bit-tight, `dict_bytes ≤ FSST_MAXHEADER`, and `output_bytes <
  input_bytes` on the word-vocabulary smoke column.

The orchestrator's round-trip, mode and prefix-search checks
(`orchestrate.py`) run at smoke for the `smoke-fsst` cell — the non-Uncompressed
branch routes FSST through the `bulk_decode(compress(x))` byte-equality
assertion.

## What is NOT done at headline

| Item | Status | Rationale |
|---|---|---|
| `fsst12` variant (4095 codes) | not vendored | canonical citation form is plain `fsst` (255 codes) |
| Sub-sample training | not run | full-column training matches FSST-as-deployed; the runner accepts only `training: full_column` |
| `fsst_duplicate` (MT) | excluded by design | single-thread fairness; never called from `runners/fsst/` |
| Bundled CLI (`fsst.cpp` upstream) | not vendored | not linked into the measurement path |
| Native compressed-domain prefix search | none | FSST does not provide one |

## Canonical headline config

FSST has no level dimension. The main grid runs one configuration, `fsst`; a
second one, `fsst_scalar`, exists for machines with AVX-512, where the pair
isolates what AVX-512 contributes:

```json
{"variant":"fsst","training":"full_column"}                 ← fsst        (AVX-512 encoder where available)
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
AVX-512 path active this op, 0.0 = scalar) so the two points can be told apart;
both read 0.0 on a machine without AVX-512. That is the case of the paper's
machine (Arrow Lake, AVX2 only), where the two configurations are identical;
this is why `fsst_scalar` is not in the main grid, and why Section VIII-A notes
that FSST's compression throughput would be higher with AVX-512. The `fsst_scalar`
baseline is served by the same `fsst` runner binary via `RUNNER_ALIASES` in
`orchestrator/modes.py`.

## Cross-baseline rules

| Rule | Status | Notes |
|---|---|---|
| Training inside the compress timed region | enforced | `fsst_create + fsst_export + fsst_compress` all inside the timed region, as for every baseline with a training step. Excluding training would subsidise FSST's compression throughput against OnPair's. |
| DecompressThenScan for non-OnPair compressed baselines | applies | declared via `prefix_search_mode == DecompressThenScan`; checked against `baseline_modes.yaml` at the handshake. |
| Row order | preserved | FSST is per-string, order-preserving. |
| Sub-counter split | natural | `dict_bytes = H`, `tokens_bytes = Σ Lc_i`. |
| Upstream pin | enforced | SHA `e638d4cf…de5b63` pinned (no tags exist). |
| Full-column training | enforced | `fsst_create` receives every column row. |
| `fsst` only, 255 codes | enforced | `fsst12` not built into the runner. |
| Single-thread fairness | enforced | `fsst_duplicate` never called; no MT macro in sources. |
| Pessimistic-load timing | enforced | `fsst_import` inside the bulk-decode timed region; no decoded-state cache (structurally). |

The other per-string baselines (BPE, RLZ, OnPair) follow the same shape with
their own structures in place of FSST's symbol table and codeword stream:
the model goes to `dict_bytes`, training is timed, no decoded state is
cached, and rows keep their order.
