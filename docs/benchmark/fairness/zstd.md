# Zstandard baseline — configuration and fairness notes

## Upstream

| Field | Value |
|---|---|
| Project | facebook/zstd |
| Tag | `v1.5.6` (released 2024-03-30) |
| Commit SHA | `794ea1b0afca0f020f4e57b6732332231fb23c70` |
| License | BSD-3-Clause OR GPL-2.0-only (dual) |
| Vendored at | `third_party/zstd/` (snapshot; no submodule, no download-at-install by policy) |
| Vendoring scope | `lib/` subset only — `common/` + `compress/` + `decompress/` + two public headers (`zstd.h`, `zstd_errors.h`). Excludes `programs/`, `tests/`, `doc/`, `examples/`, `contrib/`, `lib/deprecated/`, `lib/legacy/`, `lib/dll/`, `lib/compress/zstdmt_compress.c` (single-thread fairness), and **`lib/dictBuilder/` + `lib/zdict.h` (trained-dictionary builder, removed)**. |
| Provenance file | `third_party/zstd/PROVENANCE.txt` + per-file `SHA256SUMS` |

## Build

Sidesteps upstream's CMake. Local `third_party/zstd/CMakeLists.txt` builds
`libzstd_static.a` from the `lib/` subset above. dev flag profile
applies; publish profile adds `-march=icelake-server -mtune=icelake-server
-fno-plt` per §"canonical M1 publish flags". Defines:

```
ZSTD_MULTITHREAD=0     # single-thread fairness
ZSTD_LEGACY_SUPPORT=0
HUF_DISABLE_ASM=1      # only on non-x86_64 hosts; x86_64 assembles huf_decompress_amd64.S
```

C standard for the zstd TUs: `-std=c11`. The runner binary linking these is
C++20.

## Ops surface

| Op | Supported | Mode |
|---|---|---|
| `compress`        | yes | — |
| `bulk_decode`     | yes | — |
| `random_decode`   | yes | block-level random access |
| `prefix_search`   | yes | `prefix_search_mode() == DecompressThenScan` |

zstd's native `streaming` decompression is the underlying primitive; we wrap
the column in block-framed two-level offsets and pay random_decode at
block granularity per. Native compressed-domain search is N/A
(zstd has none); we explicitly use the harness's `decompress_then_scan`
helper, identical to what every other DecompressThenScan baseline will use.

## Wire format

On-disk layout of the compressed payload (`out_path` of `compress`):

```
[N u32]                          row count
[B u32]                          block count
[block_offsets[B+1] u32]         byte offsets into compressed_blocks[]
[block_first_string[B+1] u32]    row index where each block begins; [B] = N
[compressed_blocks[total] u8]    zstd frames concatenated, one per block
```

Each block's uncompressed payload =
`[k u32][within_offsets[k+1] u32][strings[total_block_bytes] u8]`
where `k` is the strings in the block. The within-block offsets are
zstd-compressed alongside the strings; their **logical/pre-compression**
size is billed to `boundaries_bytes` (boundaries-conservative — never
over-credits zstd).

Block builder (per — canonical Parquet `data_page_size`):

```
budget = block_budget_bytes               # 1 MiB at headline
for s in column:
    if current_uncompressed_size + 4 + len(s) > budget and current is non-empty:
        flush current block
    append s to current block
flush trailing block
```

## Ratio sub-counters (per §12.6, boundaries-conservative)

```
dict_bytes       = 0                                       # zstd is dictionary-free (streaming default; no trained dict)
tokens_bytes     = Σ_blocks (frame_size_b - 4 - (k_b+1)*4) # frame minus header+within-offsets logical
boundaries_bytes = 8                                       # N + B scalars
                 + 4·(B+1)                                 # block_offsets
                 + 4·(B+1)                                 # block_first_string
                 + Σ_blocks (k_b+1)·4                      # within-block offsets, PRE-COMPRESSION
```

`boundaries_bytes` over-counts the on-disk reality (within-block offsets
compress inside the frame, often substantially). Direction: under-credits
zstd, never over-credits — the V-B reviewer-safety invariant.

`R = Σ|s_i| / (dict_bytes + tokens_bytes + boundaries_bytes)` per.6.

## Timing rule (inheritance)

Inside the `compress` timed region:
- column read (file I/O at boundary)
- block-builder loop
- per block: `ZSTD_CCtx_reset(cctx, ZSTD_reset_session_and_parameters)`,
  `ZSTD_CCtx_setParameter(cctx, ZSTD_c_compressionLevel, level)`,
  `ZSTD_compress2(cctx, ...)`
- wire materialisation

Inside `bulk_decode` / `random_decode` / `prefix_search` timed region:
- wire read
- per block (or single-block for `random_decode`):
  `ZSTD_DCtx_reset(dctx, ZSTD_reset_session_and_parameters)`,
  `ZSTD_decompressDCtx(dctx, ...)`
- op-specific tail: write column / extract one string / canonical
  `scan_prefix` over the decoded column

`ZSTD_CCtx*` and `ZSTD_DCtx*` are member fields constructed once at runner
boot — out of every timed region (one-time setup is not what the paper
measures). Reset between blocks is inside the timed region (per-frame
setup is what real systems pay).

No decoded-state caching across calls. Structurally enforced — the runner
holds no mutable cache field; each `bulk_decode` / `random_decode` /
`prefix_search` invocation re-reads the wire file and decodes from scratch.

## Round-trip self-tests

- `tests/zstd_roundtrip_test.cpp`: synthetic 5K-row column,
  L ∈ {1, 3, 9} × block_budget ∈ {32 KB, 1 MB} → 6 round-trips; assert
  `bulk_decode` returns the original column bytewise.
- `tests/zstd_random_decode_test.cpp`: 5K-row column at 32 KB budget
  (forces multi-block); `random_decode` at 10+ probes across block
  boundaries; assert each emits `col.at(idx)` bytewise.
- `tests/zstd_prefix_search_test.cpp`: 3K-row column seeded with the 16
  G6 needles; for each needle assert zstd `prefix_search` hit-set ==
  `scan_prefix` reference (the G6 gate, evaluated locally before the
  orchestrator's higher-level G6).
- `tests/decompress_then_scan_test.cpp`: helper unit test; runs a stub
  decoder over a fixed column and asserts the helper output file matches
  `scan_prefix` directly.
- `tests/test_zstd_smoke.py`: end-to-end handshake + one compress op,
  asserts `output_bytes < input_bytes` on a synthetic compressible column.

The orchestrator's existing G6 / G7 / round-trip checks (`orchestrate.py`)
fire automatically at smoke for the `smoke-zstd-L3` cell.

## What is NOT done at headline

| Item | reference | Phase | Rationale |
|---|---|---|---|
| Ultra-tier levels {19, 22} | | excluded forever | decoder is level-decoupled; ultra-tier ratio dividend not present once L9 reached; compress cost balloons 10-50× |
| Trained-dict (`ZDICT`) | | **removed from repo** | zstd is reported in its as-deployed **streaming** configuration only (levels 1/3/9). The trained-dictionary builder (`lib/dictBuilder/` + `lib/zdict.h`) was never linked by the runner and has been deleted from the vendored tree — the ZDICT capability is no longer present in the build at all. |
| Multithreaded compression | by design | excluded forever | single-thread fairness; uniform with every other baseline |
| Legacy decoder support | by design | excluded forever | `ZSTD_LEGACY_SUPPORT=0`; not v1.5.x scope |
| Block-size sweep `{16 KB, 64 KB, 256 KB, 1 MB, 4 MB}` | | appendix (extends.7 A5) | sensitivity grid; headline pinned at 1 MB Parquet `data_page_size` default |

## Canonical headline config

Three production cells (fanout; smoke only runs the middle):

```json
{"level":1,"block_budget_bytes":1048576}
{"level":3,"block_budget_bytes":1048576}   ← smoke
{"level":9,"block_budget_bytes":1048576}
```

## Cross-baseline rules

| Rule | Status | Notes |
|---|---|---|
| (training inside compress timed region) | inherits | zstd has no training step at headline config; DCtx reset is inside the timed region. |
| (DecompressThenScan for non-OnPair compressed baselines) | inherits | declared via `prefix_search_mode == DecompressThenScan`; G7-asserted. |
| (sorted-dict permutation framing) | n/a | zstd is sequence-preserving; no permutation; row order is `block_first_string` cumulative offsets in N space. |
| (collapsed sub-counter decomposition) | n/a | zstd's payload is byte-stream; the dict/tokens/boundaries split here is structural to the wire format, not algorithmic. |
| (BPE/OnPair size-matched pairing) | non-participant | zstd is byte-stream, not part of that pairing. |
| Single-thread fairness | enforced | `ZSTD_MULTITHREAD=0`; `zstdmt_compress.c` not vendored. |
| Pessimistic-load timing | enforced | DCtx reset between blocks; no decoded-state cache. |
