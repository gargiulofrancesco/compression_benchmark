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
called** in the headline path per (block API only).

**Footgun note:** zstd's
`#ifdef ZSTD_MULTITHREAD` footgun does not apply to LZ4 — no equivalent macro
in `lib/`.

## Ops surface

| Op | Supported | Mode |
|---|---|---|
| `compress`        | yes | `LZ4_compress_fast_extState` (Fast tier) / `LZ4_compress_HC_extStateHC` (HC tier) |
| `bulk_decode`     | yes | `LZ4_decompress_safe` per block |
| `random_decode`   | yes | block-level random access (binary search on `block_first_string`) |
| `prefix_search`   | yes | `prefix_search_mode() == DecompressThenScan` (second claimant of `runners/core/decompress_then_scan.{h,cpp}`) |

LZ4 has no native compressed-domain search; we explicitly use the harness's
`decompress_then_scan` helper, identical to what zstd uses and what every
other DecompressThenScan baseline will use.

## Wire format

On-disk layout of the compressed payload (`out_path` of `compress`) —
**identical shape to zstd's** (reuses the two-level offset table
verbatim; only the compression call differs):

```
[N u32]                          row count
[B u32]                          block count
[block_offsets[B+1] u32]         byte offsets into compressed_blocks[]
[block_first_string[B+1] u32]    row index where each block begins; [B] = N
[compressed_blocks[total] u8]    LZ4 frames concatenated, one per block
```

Each block's uncompressed payload =
`[k u32][within_offsets[k+1] u32][strings[total_block_bytes] u8]`
where `k` is the strings in the block. The within-block offsets are
LZ4-compressed alongside the strings; their **logical/pre-compression**
size is billed to `boundaries_bytes` (boundaries-conservative — never
over-credits LZ4).

Block builder (per — canonical Parquet `data_page_size`,
byte-identical to zstd's):

```
budget = block_budget_bytes               # 1 MiB at headline
for s in column:
    if current_uncompressed_size + 4 + len(s) > budget and current is non-empty:
        flush current block
    append s to current block
flush trailing block
```

## Ratio sub-counters (per §12.6, boundaries-conservative — identical formula to zstd)

```
dict_bytes       = 0                                       # LZ4 is dictionary-free in headline
tokens_bytes     = Σ_blocks max(0, csize_b - 4 - (k_b+1)·4) # frame minus header+within-offsets logical
boundaries_bytes = 8                                       # N + B scalars
                 + 4·(B+1)                                 # block_offsets
                 + 4·(B+1)                                 # block_first_string
                 + Σ_blocks (k_b+1)·4                      # within-block offsets, PRE-COMPRESSION
```

`boundaries_bytes` over-counts the on-disk reality (within-block offsets
compress inside the frame, often substantially). Direction: under-credits
LZ4, never over-credits — the V-B reviewer-safety invariant.

The `max(0, …)` clamp matches the zstd accounting (locks-memory adjusted-
decision #2): on degenerate tiny inputs where `csize < within_offsets_logical`,
`tokens_bytes` lands at 0 with `dict_bytes + tokens_bytes + boundaries_bytes ≥ csize`
still holding (under-states LZ4 ratio).

`R = Σ|s_i| / (dict_bytes + tokens_bytes + boundaries_bytes)` per.6.

## Timing rule

Inside the `compress` timed region:
- column read (file I/O at boundary)
- block-builder loop
- per block (Fast): `LZ4_compress_fast_extState(state, src, dst, srcSize, dstCapacity, accel)`
- per block (HC):   `LZ4_compress_HC_extStateHC(stateHC, src, dst, srcSize, dstCapacity, level)`
- wire materialisation

Inside `bulk_decode` / `random_decode` / `prefix_search` timed region:
- wire read
- per block (or single-block for `random_decode`): `LZ4_decompress_safe(src, dst, csize, dstCapacity)`
- op-specific tail: write column / extract one string / canonical
  `scan_prefix` over the decoded column (via `decompress_then_scan`)

The Fast / HC state buffers (`LZ4_sizeofState()` / `LZ4_sizeofStateHC()`) are
member fields lazily sized once at first `compress` call — out of every
timed region's hot loop. The `_extState` API resets internally between
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
  G6 needles; for each needle assert LZ4 `prefix_search` hit-set ==
  `scan_prefix` reference (the G6 gate, evaluated locally before the
  orchestrator's higher-level G6).
- `tests/lz4_cross_tier_decode_test.cpp`: compress with fast / hc9 / hc12,
  decode each via the same `LZ4_decompress_safe` path; assert each
  recovers the original column bytewise. Validates the "decode
  unification" claim that justifies admitting HC level=12 (zstd's L19/L22
  were excluded because decode cost balloons; LZ4 HC decodes via the same
  path as fast).
- `tests/test_lz4_smoke.py`: end-to-end handshake + one compress op,
  asserts `output_bytes < input_bytes` on a synthetic compressible column.

The orchestrator's existing G6 / G7 / round-trip checks (`orchestrate.py`)
fire automatically at smoke for the `smoke-lz4-fast` cell — the
non-Uncompressed branch routes LZ4 through the `bulk_decode(compress(x))`
byte-equality assertion.

## What is NOT done at headline

| Item | reference | Phase | Rationale |
|---|---|---|---|
| LZ4F frame format | | excluded forever | ≥15 B/block header is column-store waste; block API is the fair byte-stream comparator |
| Trained-dict LZ4 appendix | | **excluded entirely** | neither zstd nor LZ4 carries a trained-dict cell; both reported in their as-deployed streaming configuration. zstd's dictionary builder was removed from the vendored tree entirely and LZ4 has no upstream-shipped trainer. |
| Multithreaded compression | by design | excluded forever | single-thread fairness; MT lives only in `programs/lz4.c` upstream, which we never vendor |
| GPLv2 CLI (`programs/`) | by design | not vendored | license boundary; CLI never linked |
| Block-size sweep `{16 KB, 64 KB, 256 KB, 1 MB, 4 MB}` | extends to LZ4 | appendix | joint ratio-vs-block-size plot with zstd at headline level set only |

## Canonical headline config

Three production cells (fanout; smoke only runs the first):

```json
{"tier":"fast","accel":1,"block_budget_bytes":1048576}   ← smoke
{"tier":"hc","level":9,"block_budget_bytes":1048576}
{"tier":"hc","level":12,"block_budget_bytes":1048576}
```

HC level=12 is admitted because — unlike zstd ultra-tier levels {19, 22} —
LZ4's decode side is unified (`LZ4_decompress_safe` for both fast and HC).
HC ratio gains are therefore "free" on the decode plot; excluding HC would
understate LZ4's ratio envelope and a reviewer familiar with LZ4 would name
the omission.

## Cross-baseline rules

| Rule | Status | Notes |
|---|---|---|
| (training inside compress timed region) | inherits | LZ4 has no training step at headline (excludes trained-dict). |
| (DecompressThenScan for non-OnPair compressed baselines) | inherits | declared via `prefix_search_mode == DecompressThenScan`; G7-asserted; second claimant of `runners/core/decompress_then_scan.{h,cpp}`. |
| (sorted-dict permutation framing) | n/a | LZ4 is sequence-preserving; no permutation; row order is `block_first_string` cumulative offsets in N space. |
| (collapsed sub-counter decomposition) | n/a | LZ4's payload is byte-stream; the dict/tokens/boundaries split here is structural to the wire format, not algorithmic. |
| (BPE/OnPair size-matched pairing) | non-participant | LZ4 is byte-stream, not part of that pairing. |
| (trained-dict appendix policy) | n/a (retired) | zstd's dictionary builder was removed from the vendored tree so there is no asymmetry to report — neither codec has a trained-dict capability in the build. |
| Single-thread fairness | enforced | no MT in `lib/`; `programs/` (which carries MT) never vendored. |
| Pessimistic-load timing | enforced | per-block `LZ4_decompress_safe` between blocks; no decoded-state cache (structurally). |
