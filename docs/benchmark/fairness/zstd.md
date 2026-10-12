# Zstandard baseline — configuration and fairness notes

## Upstream

| Field | Value |
|---|---|
| Project | facebook/zstd |
| Tag | `v1.5.6` (released 2024-03-30) |
| Commit SHA | `794ea1b0afca0f020f4e57b6732332231fb23c70` |
| License | BSD-3-Clause OR GPL-2.0-only (dual) |
| Vendored at | `third_party/zstd/` (snapshot; no submodule, no download-at-install by policy) |
| Vendoring scope | `lib/` subset only — `common/` + `compress/` + `decompress/` + two public headers (`zstd.h`, `zstd_errors.h`). Excludes `programs/`, `tests/`, `doc/`, `examples/`, `contrib/`, `lib/deprecated/`, `lib/legacy/`, `lib/dll/`, and `lib/dictBuilder/` + `lib/zdict.h` (the trained-dictionary builder). `lib/compress/zstdmt_compress.c` is in the tree but excluded from the build (single-thread fairness). |
| Provenance file | `third_party/zstd/PROVENANCE.txt` + per-file `SHA256SUMS` |

## Build

Sidesteps upstream's CMake. Local `third_party/zstd/CMakeLists.txt` builds
`libzstd_static.a` from the `lib/` subset above with the repository-wide
flags (`-O3 -march=native`). Defines:

```
ZSTD_LEGACY_SUPPORT=0
HUF_DISABLE_ASM=1      # only on non-x86_64 hosts; x86_64 assembles huf_decompress_amd64.S
```

`ZSTD_MULTITHREAD` is left undefined: zstd tests it with `#ifdef`, so defining
it at all, even to 0, would enable the multithreaded path.

C standard for the zstd TUs: `-std=c11`. The runner binary linking these is
C++20.

## Ops surface

| Op | Supported | Mode |
|---|---|---|
| `compress`        | yes | — |
| `bulk_decode`     | yes | — |
| `random_decode`   | yes | block-level random access |
| `prefix_search`   | yes | `prefix_search_mode() == DecompressThenScan` |

The column is wrapped in block-framed two-level offsets, each block one
zstd frame, and random_decode is paid at
block granularity. Native compressed-domain search is N/A
(zstd has none); we explicitly use the harness's `decompress_then_scan`
helper, as LZ4 and Re-Pair do.

## Wire format

On-disk layout of the compressed payload (`out_path` of `compress`):

```
[N u32]                          row count
[B u32]                          block count
[block_offsets[B+1] u32]         byte offsets into compressed_blocks[]
[block_first_string[B+1] u32]    row index where each block begins; [B] = N
[within_offsets[N+B] u32]        per block, k+1 local row offsets (uncompressed)
[compressed_blocks[total] u8]    zstd frames concatenated, one per block
```

Each block's uncompressed payload is its strings concatenated, with no
header: a compressed frame holds payload only, so a bulk decode writes every
block straight into the output column. The `k+1` local row offsets of each
block are stored uncompressed in `within_offsets` (block `b`'s slice starts at
index `block_first_string[b] + b`) and are billed to `boundaries_bytes`.

Block builder (the 1 MiB budget is Parquet's default `data_page_size`):

```
budget = block_budget_bytes               # 1 MiB at headline
for s in column:
    if current_uncompressed_size + 4 + len(s) > budget and current is non-empty:
        flush current block
    append s to current block
flush trailing block
```

## Ratio sub-counters

```
dict_bytes       = 0                                       # zstd is dictionary-free (streaming default; no trained dict)
tokens_bytes     = Σ_blocks frame_size_b                   # the frames hold payload only
boundaries_bytes = 8                                       # N + B scalars
                 + 4·(B+1)                                 # block_offsets
                 + 4·(B+1)                                 # block_first_string
                 + Σ_blocks (k_b+1)·4                      # within_offsets
```

`dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes` exactly.

`R = Σ|s_i| / (dict_bytes + tokens_bytes)`: like every baseline's, the ratio
counts the payload and leaves the row and block offsets out.

## Timing rule

`t_compress_ns` (behind the compression throughput) covers, for every block:
- building the block's payload from the column
- `ZSTD_CCtx_reset(cctx, ZSTD_reset_session_and_parameters)`,
  `ZSTD_CCtx_setParameter(cctx, ZSTD_c_compressionLevel, level)`,
  `ZSTD_compress2(cctx, ...)`

Reading the input column and writing the wire are outside it.

`t_decode_ns` (behind the decompression throughput) covers, for every block:
- `ZSTD_DCtx_reset(dctx, ZSTD_reset_session_and_parameters)`,
  `ZSTD_decompressDCtx(dctx, ...)` straight into the pre-allocated output

Reading the wire, allocating the output, rebuilding the row offsets and
writing the column are outside it.

`bulk_decode` times the decompression of every block into a pre-allocated
output buffer, nothing else; the row offsets of the output column are rebuilt
from `within_offsets` after the timer stops. `random_decode` and each access of
`ra_latency` decode the one enclosing block. `prefix_search` decodes the whole
column and runs the canonical `scan_prefix` over it (via
`decompress_then_scan`).

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
  needles; for each needle assert zstd `prefix_search` hit-set ==
  `scan_prefix` reference.
- `tests/decompress_then_scan_test.cpp`: helper unit test; runs a stub
  decoder over a fixed column and asserts the helper output file matches
  `scan_prefix` directly.
- `tests/test_zstd_smoke.py`: end-to-end handshake + one compress op,
  asserts `output_bytes < input_bytes` on a synthetic compressible column.

The orchestrator's round-trip, mode and prefix-search checks
(`orchestrate.py`) run at smoke for the `smoke-zstd-L3` cell.

## What is NOT done at headline

| Item | Status | Rationale |
|---|---|---|
| Ultra-tier levels {19, 22} | not run | decoder is level-decoupled; ultra-tier ratio dividend not present once L9 reached; compress cost balloons 10-50× |
| Trained-dict (`ZDICT`) | not in the build | zstd is reported in its as-deployed **streaming** configuration only (levels 1/3/9). The trained-dictionary builder (`lib/dictBuilder/` + `lib/zdict.h`) is not part of the vendored tree. |
| Multithreaded compression | excluded by design | single-thread fairness; uniform with every other baseline |
| Legacy decoder support | excluded by design | `ZSTD_LEGACY_SUPPORT=0`; not v1.5.x scope |
| Block-size sensitivity | ablation A5 | block sizes of 64 to 65,536 strings (Section VIII-E); the main grid uses a 1 MiB block budget, Parquet's default `data_page_size` |

## Canonical headline config

Three cells in the main grid (`zstd_l1`, `zstd`, `zstd_l9`; smoke runs only
the middle one):

```json
{"level":1,"block_budget_bytes":1048576}
{"level":3,"block_budget_bytes":1048576}   ← smoke
{"level":9,"block_budget_bytes":1048576}
```

## Cross-baseline rules

| Rule | Status | Notes |
|---|---|---|
| Training inside the compress timed region | applies | zstd has no training step at headline config; CCtx and DCtx resets are inside the timed regions. |
| DecompressThenScan for non-OnPair compressed baselines | applies | declared via `prefix_search_mode == DecompressThenScan`; checked against `baseline_modes.yaml` at the handshake. |
| Row order | preserved | zstd is sequence-preserving; no permutation; row order is `block_first_string` cumulative offsets in N space. |
| Sub-counter split | structural | zstd's payload is byte-stream; the dict/tokens/boundaries split here is structural to the wire format, not algorithmic. |
| BPE/OnPair size-matched pairing | non-participant | zstd is byte-stream, not part of that pairing. |
| Single-thread fairness | enforced | `ZSTD_MULTITHREAD` undefined; `zstdmt_compress.c` not compiled. |
| Pessimistic-load timing | enforced | DCtx reset between blocks; no decoded-state cache. |
