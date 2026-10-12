# LZ4 baseline — configuration and fairness notes

## Upstream

| Field | Value |
|---|---|
| Project | lz4/lz4 |
| Tag | `v1.10.0` (released 2024-07-22) |
| Commit SHA | `ebb370ca83af193212df4dcbadcc5d87bc0de2f0` |
| License | BSD-2-Clause (`lib/` subset only; upstream `programs/` is GPLv2 and is **not** vendored) |
| Vendored at | `third_party/lz4/` (snapshot; no submodule, no download-at-install by policy) |
| Vendoring scope | `lib/` subset only — `lz4.{h,c}`, `lz4hc.{h,c}`, `lz4file.{h,c}`, `lz4frame.{h,c}`, `lz4frame_static.h`, `xxhash.{h,c}`. Excludes `programs/`, `tests/`, `examples/`, `doc/`, `ossfuzz/`, `contrib/`, `dll/`. |
| Provenance file | `third_party/lz4/PROVENANCE.txt` + `SHA256SUMS` |

## Build

Sidesteps upstream's CMake. Local `third_party/lz4/CMakeLists.txt` builds
`liblz4_static.a` from the `lib/` subset above, with the repository-wide
flags (`-O3 -march=native`). No special defines
needed (LZ4 has no MT macro in `lib/`; MT lives only in `programs/lz4.c`
upstream, which we never vendor).

C standard for the LZ4 TUs: `-std=c11`. The runner binary linking these is
C++20. LZ4F (`lz4frame.c`) is linked into the static library but **never
called** (block API only).

zstd's `#ifdef ZSTD_MULTITHREAD` pitfall (see zstd.md) does not apply to
LZ4 — no equivalent macro in `lib/`.

## Ops surface

| Op | Supported | Mode |
|---|---|---|
| `compress`        | yes | `LZ4_compress_fast_extState` (Fast tier) / `LZ4_compress_HC_extStateHC` (HC tier) |
| `bulk_decode`     | yes | `LZ4_decompress_safe` per block |
| `random_decode`   | yes | block-level random access (binary search on `block_first_string`) |
| `prefix_search`   | yes | `prefix_search_mode() == DecompressThenScan` (through `runners/core/decompress_then_scan.{h,cpp}`) |

LZ4 has no native compressed-domain search; we explicitly use the harness's
`decompress_then_scan` helper, as zstd and Re-Pair do.

## Wire format

On-disk layout of the compressed payload (`out_path` of `compress`) —
**the shape of zstd's** (same two-level offset table), plus the decompressed
size of each block, which a raw LZ4 block does not record:

```
[N u32]                          row count
[B u32]                          block count
[block_offsets[B+1] u32]         byte offsets into compressed_blocks[]
[block_first_string[B+1] u32]    row index where each block begins; [B] = N
[block_raw_sizes[B] u32]         decompressed payload bytes per block
[within_offsets[N+B] u32]        per block, k+1 local row offsets (uncompressed)
[compressed_blocks[total] u8]    LZ4 blocks concatenated, one per block
```

Each block's uncompressed payload is its strings concatenated, with no
header: a compressed frame holds payload only, so a bulk decode writes every
block straight into the output column. The `k+1` local row offsets of each
block are stored uncompressed in `within_offsets` (block `b`'s slice starts at
index `block_first_string[b] + b`) and are billed to `boundaries_bytes`.

Block builder (the 1 MiB budget is Parquet's default `data_page_size`;
identical to zstd's):

```
budget = block_budget_bytes               # 1 MiB at headline
for s in column:
    if current_uncompressed_size + 4 + len(s) > budget and current is non-empty:
        flush current block
    append s to current block
flush trailing block
```

## Ratio sub-counters (same formula as zstd, plus `block_raw_sizes`)

```
dict_bytes       = 0                                       # LZ4 is dictionary-free in headline
tokens_bytes     = Σ_blocks csize_b                        # the blocks hold payload only
boundaries_bytes = 8                                       # N + B scalars
                 + 4·(B+1)                                 # block_offsets
                 + 4·(B+1)                                 # block_first_string
                 + 4·B                                     # block_raw_sizes
                 + Σ_blocks (k_b+1)·4                      # within_offsets
```

`dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes` exactly.

`R = Σ|s_i| / (dict_bytes + tokens_bytes)`: like every baseline's, the ratio
counts the payload and leaves the row and block offsets out.

## Timing rule

`t_compress_ns` (behind the compression throughput) covers, for every block:
- building the block's payload from the column
- (Fast) `LZ4_compress_fast_extState(state, src, dst, srcSize, dstCapacity, accel)`
- (HC)   `LZ4_compress_HC_extStateHC(stateHC, src, dst, srcSize, dstCapacity, level)`

Reading the input column and writing the wire are outside it.

`t_decode_ns` (behind the decompression throughput) covers, for every block,
`LZ4_decompress_safe(src, dst, csize, dstCapacity)` straight into the
pre-allocated output. Reading the wire, allocating the output, rebuilding the
row offsets and writing the column are outside it.

`bulk_decode` times the decompression of every block into a pre-allocated
output buffer, nothing else; the row offsets of the output column are rebuilt
from `within_offsets` after the timer stops. `random_decode` and each access of
`ra_latency` decode the one enclosing block. `prefix_search` decodes the whole
column and runs the canonical `scan_prefix` over it (via
`decompress_then_scan`).

The Fast / HC state buffers (`LZ4_sizeofState()` / `LZ4_sizeofStateHC()`) are
member fields lazily sized once at first `compress` call — out of every
timed region. The `_extState` API resets internally between
blocks, so no explicit reset call is needed (per-frame setup cost is still
inside the timed region; it's just internal to LZ4).

No decoded-state caching across calls. Structurally enforced — the runner
holds no mutable cache field; each `bulk_decode` / `random_decode` /
`prefix_search` invocation re-reads the wire file and decodes from scratch.

## Round-trip self-tests

- `tests/lz4_roundtrip_test.cpp`: synthetic 5K-row column, 3 tiers
  (fast/accel=1, hc/level=9, hc/level=12) × `block_budget ∈ {32 KB, 1 MB}`
  → 6 round-trips; assert `bulk_decode` returns the original column bytewise.
- `tests/lz4_random_decode_test.cpp`: 5K-row column at 32 KB budget
  (forces multi-block); `random_decode` at 10+ probes across block
  boundaries; assert each emits `col.at(idx)` bytewise.
- `tests/lz4_prefix_search_test.cpp`: 3K-row column seeded with the 16
  needles; for each needle assert LZ4 `prefix_search` hit-set ==
  `scan_prefix` reference.
- `tests/lz4_cross_tier_decode_test.cpp`: compress with fast / hc9 / hc12,
  decode each via the same `LZ4_decompress_safe` path; assert each
  recovers the original column bytewise (LZ4 HC decodes via the same
  path as fast).
- `tests/test_lz4_smoke.py`: end-to-end handshake + one compress op,
  asserts `output_bytes < input_bytes` on a synthetic compressible column.

The orchestrator's round-trip, mode and prefix-search checks
(`orchestrate.py`) run at smoke for the `smoke-lz4-fast` cell — the
non-Uncompressed branch routes LZ4 through the `bulk_decode(compress(x))`
byte-equality assertion.

## What is NOT done at headline

| Item | Status | Rationale |
|---|---|---|
| LZ4F frame format | not used | ≥15 B/block header is column-store waste; block API is the fair byte-stream comparator |
| Trained-dict LZ4 | not run | neither zstd nor LZ4 carries a trained-dict cell; both reported in their as-deployed streaming configuration. zstd's dictionary builder is not in the vendored tree and LZ4 has no upstream-shipped trainer. |
| HC tier | supported, not in the main grid | the runner and its tests cover `hc` at levels 9 and 12; the paper reports the fast tier |
| Multithreaded compression | excluded by design | single-thread fairness; MT lives only in `programs/lz4.c` upstream, which we never vendor |
| GPLv2 CLI (`programs/`) | not vendored | license boundary; CLI never linked |
| Block-size sensitivity | ablation A5 | block sizes of 64 to 65,536 strings, with zstd (Section VIII-E) |

## Canonical headline config

One cell in the main grid, the one smoke runs too:

```json
{"tier":"fast","accel":1,"block_budget_bytes":1048576}
```

The runner also accepts the HC tier, which decodes through the same
`LZ4_decompress_safe` as the fast tier:

```json
{"tier":"hc","level":9,"block_budget_bytes":1048576}
{"tier":"hc","level":12,"block_budget_bytes":1048576}
```

## Cross-baseline rules

| Rule | Status | Notes |
|---|---|---|
| Training inside the compress timed region | applies | LZ4 has no training step at headline (no trained dictionary). |
| DecompressThenScan for non-OnPair compressed baselines | applies | declared via `prefix_search_mode == DecompressThenScan`; checked against `baseline_modes.yaml` at the handshake; uses `runners/core/decompress_then_scan.{h,cpp}`. |
| Row order | preserved | LZ4 is sequence-preserving; no permutation; row order is `block_first_string` cumulative offsets in N space. |
| Sub-counter split | structural | LZ4's payload is byte-stream; the dict/tokens/boundaries split here is structural to the wire format, not algorithmic. |
| BPE/OnPair size-matched pairing | non-participant | LZ4 is byte-stream, not part of that pairing. |
| Trained dictionaries | none | neither zstd nor LZ4 has a trained-dict capability in the build. |
| Single-thread fairness | enforced | no MT in `lib/`; `programs/` (which carries MT) never vendored. |
| Pessimistic-load timing | enforced | per-block `LZ4_decompress_safe` between blocks; no decoded-state cache (structurally). |
