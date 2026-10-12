# OnPair — configuration and fairness notes

## Upstream

- URL: `https://github.com/gargiulofrancesco/onpair_cpp.git`
- Pin: SHA `f8ecc64fa7786cfa6fc46d46564135ee99a8ba49` (commit
  "perf: optimize hash maps and avoid reallocations").
- See [external/onpair_cpp.cmake](../../../external/onpair_cpp.cmake) for the pin
  and [external/onpair_local_shims.patch](../../../external/onpair_local_shims.patch)
  for the local patch applied on top of it.

## Licence

MIT. Transitive dep
Boost.Unordered ≥1.81 (BSL-1.0, compatible). No GPL code in the runner.

## Build

`external/onpair_cpp.cmake` declares `FetchContent_Declare(onpair_cpp …
GIT_REPOSITORY=public-github-URL GIT_TAG=f8ecc64f)`; `FetchContent_MakeAvailable`
populates `_deps/onpair_cpp-src/` at configure time. The library target
`onpair` (alias `OnPair::onpair`) is statically linked into
`runners/onpair/onpair_runner`.

**Not vendored**: the library is consumed via FetchContent, not copied
under `third_party/`. It is the artefact under measurement, and is fetched
the way any user of the library would fetch it.

## Ops surface

compress / bulk_decode / random_decode / prefix_search — all supported.

`prefix_search_mode() == NativeOnCompressed` — **only claimant in the
lineup**. Every other compressed baseline declares `DecompressThenScan`;
Uncompressed declares `Uncompressed`. OnPair serves prefix search by building
a `PrefixAutomaton` from the needle and running `OnPairColumnView::scan` over
the bit-packed token stream, with no decompression.
`onpair_native_prefix_search_test` verifies hit-set equality with the
canonical scan on the 16 fixed needles.

## Wire format

Direct use of the library's own serialiser via `OnPairColumn::write_to` /
`OnPairColumn::read_from`. As written by upstream `src/onpair/column/column.cpp`
at SHA f8ecc64f, the wire layout is:

```
[MAGIC u8[8]]
[bits u8]
[dict_true_bytes u32][dict_bytes_arr[dict_true_bytes] u8]
[(num_tokens+1) u32][dict_offsets[(num_tokens+1)] u32]
[real_words u32][packed[real_words] u64]
[(N+1) u32][boundaries[(N+1)] u32]
```

The *token dictionary* is lex-sorted; the column's rows are not. Row order
is preserved through `boundaries[N+1]` (Arrow-style, as for BPE and RLZ), so
there is no permutation to store. Each row is parsed on its own against the
shared dictionary.

## Sub-counter formula (natural dict/tokens split)

```
dict_content_bytes = DictionaryView::token_content_bytes() = dict offsets.back()
dict_offset_bytes  = DictionaryView::offset_bytes()        = (num_tokens+1)*4
dict_bytes         = dict_content_bytes + dict_offset_bytes = Dictionary::bytes_used()
tokens_bytes       = ceil(total_tokens * bits / 64) * 8             [packed payload, whole 64-bit words]
boundaries_bytes   = 25 + 4*(N+1)                                   [STRING per-row offsets]
                   = 8 MAGIC + 1 bits + 4 dict_true_bytes_scalar
                   + 4 dict_offsets_size + 4 real_words + 4 boundaries_size
                   + (N+1)*4
num_dict_entries   = num_tokens
```

The dictionary (the trained model) is reported as TWO separated sub-counters,
`dict_content_bytes` (concatenated token byte sequences) and `dict_offset_bytes`
(the dictionary's OWN offset array, which indexes that content). Their sum is the
unchanged `dict_bytes = Dictionary::bytes_used()`. onpair_cpp exposes both via the
local-shim accessors `Dictionary::{token_content_bytes,offset_bytes}` (+ the
DictionaryView mirrors) — see `external/onpair_local_shims.patch`. Reporting them
separately lets the consumer decide what to include; the runner keeps BOTH in
`dict_bytes` because the dictionary offsets are part of the model.

**Bit-tight identity**:
`dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes` exactly.
`boundaries_bytes` includes the 25 bytes of framing scalars.

### Compression-ratio accounting — PAYLOAD-ONLY

This benchmark measures STRING-PAYLOAD (de)compression and does NOT compress the
per-row STRING offsets (`boundaries_bytes` — the `(N+1)*4` row-boundary array that
every baseline carries verbatim). The headline compression ratio therefore
EXCLUDES string offsets:

```
R = input_bytes / (dict_bytes + tokens_bytes)        # string offsets dropped
```

The distinction between the two kinds of offsets:
- **Dictionary offsets** (`dict_offset_bytes`, inside `dict_bytes`) — KEPT. They
  are part of the trained model, not per-row string boundaries.
- **String offsets** (`boundaries_bytes`) — EXCLUDED. They are the uncompressed
  per-row boundaries this benchmark does not compress.

Decode throughput is the symmetric payload-only counterpart: its work unit is
`input_bytes` (payload only), so neither metric counts string offsets.
(`T5_complete.tex` gives the ratio over `output_bytes = dict + tokens +
boundaries`, with the string offsets charged.) Implemented in
`tools/paper_tables.py`, and in `tools/plot/_figbase.py::ratio` /
`throughput_bps` for the diagnostic plots.

The decode *timer* is payload-only to match: `bulk_decode` times only the
no-offsets bulk decoder — `OnPairColumnView::decompress_all(buf)` →
`decoding::decompress_all(sv,dv,buf)` → `decode_all<Bits>(...)`, which writes just
the concatenated token bytes and never builds the Arrow-style per-row offset array
(that array is the string offsets we exclude). No library change is needed — this
overload ships in `onpair/decoding/detail/decode_all.h`; the runner does not
call the offset-aware variant inside the timed region. The per-row
offsets needed to materialise the round-trip output are rebuilt in an UNTIMED pass
(the offset-aware decode into scratch), whose payload is cross-checked byte-for-byte
against the timed decode (mismatch ⇒ status `lossy`). The resulting decode
throughput on the paper's columns is the D column of Table II.

### OnPair dictionary-offset diagnostic

`metric_name='dict_offset_bytes'` (legacy alias `dict_offsets_bytes`, identical
value) is emitted at every OnPair compress alongside the headline metrics. It is:

- **Contained within** `dict_bytes` (not subtracted; not deducted).
- **NOT** a new schema column.
- The DICTIONARY's offset array — kept in the payload-only ratio denominator
  (part of the model), unlike the string `boundaries_bytes`.
- Strictly informational — it shows how `dict_bytes` splits.

Verified by `onpair_dict_offsets_diagnostic_test.cpp`.

## BPE/OnPair size-matched pairing

BPE is compared with OnPair at the same token-id width: `bpe` V=16384 ↔
OnPair `bits=14` (= 2^14), and `bpe_v65536` V=65536 ↔ OnPair `bits=16`, the
pair that Table II reports. Both pack fixed-width token ids.

`onpair_cluster_b_symmetry_test.cpp` drives both runners (V=16384, `bits=14`)
on the same column, times each compress call with `steady_clock` and prints
the ratio. It requires both runners to return status='ok'; it asserts no
bound on the ratio.

## Timing rule — payload-only codec timers (training is timed)

The reported throughput timers measure the CODEC only; file I/O, wire
(de)serialisation, and buffer allocation are EXCLUDED — the uniform compress/
decode-timer contract shared by every baseline. The runner reports
`t_compress_ns` / `t_decode_ns`; the figures derive `T_c` / `T_d` from those (NOT
the dispatch-loop `t_op_ns`, which includes disk I/O).

`t_compress_ns` wraps ONLY (`onpair_runner.cpp` compress):
- `OnPairColumn::compress(views, tc)` — pair-merge training over input +
  dictionary construction + bit-packed token-stream emission (training is
  inside the timed region).

Excluded (UNTIMED): input read; input materialisation (column →
`std::vector<std::string_view>`, built before the timer starts); and
`OnPairColumn::write_to(ofstream)` — the wire write happens AFTER the timer stops.

`t_decode_ns` wraps ONLY (`onpair_runner.cpp` bulk_decode):
- the payload-only bulk decompress `OnPairColumnView::decompress_all(buf)` into a
  PRE-ALLOCATED, value-initialised (pre-faulted) buffer — no per-row offset array
  produced.

Excluded (UNTIMED): wire load (`OnPairColumn::read_from`); output-buffer
allocation; the offset-aware verification pass that rebuilds per-row offsets and
cross-checks the payload; and the column write.

**No caching of deserialised state across calls** — structurally enforced (no
mutable cache field in `OnPairRunner`); each op re-reads the wire from disk,
as in every other runner.

## OOM-as-data

`mem_cap_gb=64` sets `RLIMIT_AS` (the value BPE, RLZ and Re-Pair use).
`std::bad_alloc` is caught and converted to `status='oom'` with no metrics.

## Prefix search

OnPair is the only `NativeOnCompressed` claimant. The `prefix` stage
(`experiments/prefix_driver.py`) measures its compressed prefix filter
against a raw scan of the uncompressed column and against decode-then-scan
lower bounds for FSST, LZ4 and Zstandard; the paper does not report these
numbers. Hit-set equality with the canonical scan is the correctness check.

## NOT done at headline (scope-fence)

- KMP `contains` / EQ `equals` / Aho-Corasick scan / regex / boolean
  composition — all out of scope.
- `bits` other than 16 — not in the main grid; swept by the capacity
  ablations (A2: 9, 10, 12, 14, 16; A4: 9, 12, 14, 16).
- `FixedThreshold` — only in the threshold ablation (A1); not the default mode.

Enforcement:
- `runners/onpair/onpair_runner.cpp` only includes the OnPair headers it
  needs, none of them from the excluded search APIs.
- `tools/lint/onpair_scope_audit.sh` grep prohibits
  `kmp_automaton|aho_corasick|eq_automaton|eq_search|contains\(|equals\(`
  in `runners/onpair/`.
- `onpair_scope_fence_test.cpp` is the belt-and-suspenders C++ unit.

## Round-trip self-tests

C++ and Python tests:

| Test | Purpose |
|------|---------|
| `onpair_roundtrip_test` | Bytewise round-trip at `bits=16` across 6 sub-variants (all_bytes / random_bytes / nul_byte / whitespace / non_utf8 / mixed_empty). |
| `onpair_random_decode_test` | Boundary + stride-313 probes; bytewise per-row. |
| `onpair_native_prefix_search_test` | Hit-set equality vs the canonical scan on 16 fixed needles. |
| `onpair_load_save_identity_test` | determinism: `seed=42` × 2 → identical wire. |
| `onpair_seed_determinism_test` | `seed=42` × 2 identical; both seeds bytewise round-trip. |
| `onpair_empty_column_test` | N=0 + all-empty → `status='unsupported'`. |
| `onpair_bit_width_disjointness_test` | `dict_bytes`↑ or `tokens_bytes`↓ across `bits ∈ {12, 14, 16}`. |
| `onpair_dict_offsets_diagnostic_test` | diagnostic metric row emitted, contained within `dict_bytes`, divisible by 4. |
| `onpair_bit_tight_identity_test` | Bit-tight identity at every `bits ∈ {9, 12, 14, 16}`. |
| `onpair_cluster_b_symmetry_test` | bpe V=16K and OnPair `bits=14` both succeed on the same column; prints the compress-time ratio. |
| `onpair_scope_fence_test` | Belt-and-suspenders for scope-audit shell script. |
| `onpair_held_out_compress_test` | `held_out_compress`, paired Fixed/Dynamic protocol (A1). |
| `onpair_held_out_single_test` | `held_out_compress`, single strategy (A3). |
| `test_onpair_smoke.py` | NDJSON handshake; bit-tight; diagnostic; lossless. |
| `test_onpair_native_prefix_search_smoke.py` | Native prefix-search vs Python canonical scan. |
| `test_onpair_runner_fixed_threshold.py` | `threshold_strategy="fixed"` accepted by the compress op. |
| `test_onpair_runner_held_out_op.py` | `held_out_compress` over NDJSON; other runners answer `unsupported`. |
| `test_modes_g7.py::test_g7_pass_onpair` | Mode row for `onpair` (`NativeOnCompressed`). |

## Canonical headline configs

```json
{"bits":16, "sample_fraction":0.15, "seed":42, "threads":1, "mem_cap_gb":64}
{"bits":14, "sample_fraction":0.15, "seed":42, "threads":1, "mem_cap_gb":64}   // the smoke cell paired with bpe V=2^14; not in the main grid
```

## Configuration summary

| Decision | Value |
|---|---|
| Training cost accounting | training inside the compress timed region (uniform with every baseline) |
| Prefix-search mode | `NativeOnCompressed` (the only baseline with a native compressed prefix filter) |
| Random-access model | per-string, against the resident sorted token dictionary |
| Space accounting | dict/tokens split structurally guaranteed by the format |
| Capacity pairing with BPE | BPE V=65536 paired with OnPair capacity 2^16 (size-matched) |
| Token-id packing | fixed-width via `Store::packed`, uniform across the column |
| Diagnostic sub-counters | `dict_offsets_bytes` reported separately |
| Library consumption | FetchContent + commit SHA pin (not vendored) |
