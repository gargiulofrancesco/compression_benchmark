# OnPair — configuration and fairness notes

## Upstream

- URL: `https://github.com/gargiulofrancesco/onpair_cpp.git`
- Pin: SHA `f8ecc64fa7786cfa6fc46d46564135ee99a8ba49` ( perf commit
  "perf: optimize hash maps and avoid reallocations").
- Fetch date:.
- See [external/onpair_cpp.cmake](../../../external/onpair_cpp.cmake) for the full
  audit-trail record.

## Licence

MIT. **First MIT runner binary in the lineup.** Transitive dep
Boost.Unordered ≥1.81 (BSL-1.0, compatible). No GPL surface area in the
runner; LGPL-2.1 already fenced (libCSD; static-link satisfied per LGPL §6).

## Build

`external/onpair_cpp.cmake` declares `FetchContent_Declare(onpair_cpp …
GIT_REPOSITORY=public-github-URL GIT_TAG=f8ecc64f)`; `FetchContent_MakeAvailable`
populates `_deps/onpair_cpp-src/` at configure time. The library target
`onpair` (alias `OnPair::onpair`) is statically linked into
`runners/onpair/onpair_runner`.

**audit-trail negative**: the library is consumed via FetchContent
*not* vendored under `third_party/onpair_cpp/`. The library is the artefact
under measurement — the asymmetry vs every other baseline is itself honest
signal.

## Ops surface

compress / bulk_decode / random_decode / prefix_search — all supported.

`prefix_search_mode() == NativeOnCompressed` — **only claimant in the
lineup; FLIPPED**. Eight prior baselines declare `DecompressThenScan`;
Uncompressed declares `Uncompressed`. OnPair serves prefix-search via
`OnPairColumnView::starts_with` (PrefixAutomaton scan over bit-packed token
stream, no decompression). G6 self-test verifies hit-set bit-equality vs
Uncompressed canonical scan on the 16 fixed needles.

## Wire format

Direct use of the library's own serialiser via `OnPairColumn::write_to` /
`OnPairColumn::read_from`. Per probe of upstream `src/onpair/column/column.cpp`
at SHA f8ecc64f, the wire layout is:

```
[MAGIC u8[8]]
[bits u8]
[dict_true_bytes u32][dict_bytes_arr[dict_true_bytes] u8]
[(num_tokens+1) u32][dict_offsets[(num_tokens+1)] u32]
[real_words u32][packed[real_words] u64]
[(N+1) u32][boundaries[(N+1)] u32]
```

**Departure from the block-framed baselines**: lex-sorted
*token dictionary*, NOT lex-sorted column rows. Row order preserved
natively via `boundaries[N+1]` (Arrow-style, identical to BPE/RLZ). No
`col_to_sorted[N]` permutation cost. The four non-inheritances now
in lineup, each distinct rationale:

| Baseline   | Rationale |
|------------|-----------|
| BigRePair  | single-text concatenation |
| BPE        | per-string Encode without sort |
| RLZ        | per-string against shared external reference |
| **OnPair** | **per-string against shared internal sorted-token-dict** |

## Sub-counter formula (NON-collapse natural split, fifth in lineup)

```
dict_content_bytes = DictionaryView::token_content_bytes() = dict offsets.back()
dict_offset_bytes  = DictionaryView::offset_bytes()        = (num_tokens+1)*4
dict_bytes         = dict_content_bytes + dict_offset_bytes = Dictionary::bytes_used()
tokens_bytes       = ceil(total_tokens * bits / 8)                  [packed payload bytes]
boundaries_bytes   = 25 + 4*(N+1)                                   [STRING per-row offsets]
                   = 8 MAGIC + 1 bits + 4 dict_true_bytes_scalar
                   + 4 dict_offsets_size + 4 real_words + 4 boundaries_size
                   + (N+1)*4
num_dict_entries   = num_tokens
```

The dictionary (the trained model) is now reported as TWO separated sub-counters,
`dict_content_bytes` (concatenated token byte sequences) and `dict_offset_bytes`
(the dictionary's OWN offset array, which indexes that content). Their sum is the
unchanged `dict_bytes = Dictionary::bytes_used()`. onpair_cpp exposes both via the
local-shim accessors `Dictionary::{token_content_bytes,offset_bytes}` (+ the
DictionaryView mirrors) — see `external/onpair_local_shims.patch`. Reporting them
separately lets the consumer decide what to include; the runner keeps BOTH in
`dict_bytes` because the dictionary offsets are part of the model.

**Bit-tight identity (target ninth bit-tight baseline)**:
`dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes` exactly.
`boundaries_bytes` over-bills by the 25 framing-scalar bytes
(conservativeness direction correct; never over-credits OnPair).

### Compression-ratio accounting — PAYLOAD-ONLY

This benchmark measures STRING-PAYLOAD (de)compression and does NOT compress the
per-row STRING offsets (`boundaries_bytes` — the `(N+1)*4` row-boundary array that
every baseline carries verbatim). The headline compression ratio therefore
EXCLUDES string offsets:

```
R = input_bytes / (dict_bytes + tokens_bytes)        # string offsets dropped
```

Crucial distinction (your dict-vs-string rule):
- **Dictionary offsets** (`dict_offset_bytes`, inside `dict_bytes`) — KEPT. They
  are part of the trained model, not per-row string boundaries.
- **String offsets** (`boundaries_bytes`) — EXCLUDED. They are the uncompressed
  per-row boundaries this benchmark does not compress.

Decode throughput is the symmetric payload-only counterpart: its work unit is
`input_bytes` (payload only), so neither metric counts string offsets. (Previously
the ratio used `output_bytes = dict + tokens + boundaries`, charging the string
offsets as a cost — the stricter old number.) Implemented in
`tools/plot/_figbase.py::ratio` / `throughput_bps`.

The decode *timer* is payload-only to match: `bulk_decode` times only the
no-offsets bulk decoder — `OnPairColumnView::decompress_all(buf)` →
`decoding::decompress_all(sv,dv,buf)` → `decode_all<Bits>(...)`, which writes just
the concatenated token bytes and never builds the Arrow-style per-row offset array
(that array is the string offsets we exclude). No library change was needed — this
overload already ships in `onpair/decoding/detail/decode_all.h`; the runner simply
stopped calling the offset-aware variant inside the timed region. The per-row
offsets needed to materialise the round-trip output are rebuilt in an UNTIMED pass
(the offset-aware decode into scratch), whose payload is cross-checked byte-for-byte
against the timed decode (mismatch ⇒ status `lossy`). Measured payload-only decode
throughput on the capped columns (P-core pinned, turbo off): URL 8.3, Title 10.1,
wiki/titles 6.1, tpch/l_comment 7.4, geonames/name 5.3 GiB/s — 1.0–3.5× faster than
the offset-aware path, the gain scaling with how many short rows pay offset
bookkeeping.

### OnPair dictionary-offset diagnostic

`metric_name='dict_offset_bytes'` (legacy alias `dict_offsets_bytes`, identical
value) is emitted at every OnPair compress alongside the headline metrics. It is:

- **Contained within** `dict_bytes` (not subtracted; not deducted).
- **NOT** a new schema column.
- The DICTIONARY's offset array — kept in the payload-only ratio denominator
  (part of the model), unlike the string `boundaries_bytes`.
- Strictly informational — surfaces the internal `dict_bytes` split for
  V-B reviewer transparency.

Verified by `onpair_dict_offsets_diagnostic_test.cpp` (load-bearing).

## BPE/OnPair size-matched pairing binding (+ )

bpe V=16384 ↔ OnPair `bits=14` (= 2^14 = 16384). *"Structurally identical layout to OnPair's
`StoreView::packed_data()` — enables BPE/OnPair size-matched pairing assertions."*

`onpair_cluster_b_symmetry_test.cpp` drives both runners on the same column
+ measures compress wall via `steady_clock`. Bound:

- **Hardware-strict rig (reference machine)**: ±25% proximity required.
- **M1 dev rig (smoke + ctest default)**: ±50% soft-warn; both runners
  status='ok' required. Hardware-strict bound documented but not enforced
  (Apple Silicon variance + non-Icelake + non-AVX-512).

dry run validates the cross-baseline symmetry.

## Timing rule — payload-only codec timers (: training is timed)

The reported throughput timers measure the CODEC only; file I/O, wire
(de)serialisation, and buffer allocation are EXCLUDED — the uniform compress/
decode-timer contract shared by every baseline. The runner reports
`t_compress_ns` / `t_decode_ns`; the figures derive `T_c` / `T_d` from those (NOT
the dispatch-loop `t_op_ns`, which includes disk I/O).

`t_compress_ns` wraps ONLY (`onpair_runner.cpp` compress):
- `OnPairColumn::compress(views, tc)` — pair-merge training over input +
  dictionary construction + bit-packed token-stream emission (: training is
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
mutable cache field in `OnPairRunner`); each op re-reads the wire from disk. Same
pessimistic-load rule as FSST `fsst_import`, BPE `LoadFromSerializedProto`, RLZ
R-memcpy + SA-build, and `repair` grammar unpack.

## OOM-as-data

`mem_cap_gb=64` rlimit AS (inherits BigRePair/BPE/RLZ value). `std::bad_alloc`
caught and converted to `status='oom'` with NULL metrics. `onpair_oom_test.cpp`
exercises at `mem_cap_gb=1` × 50 MB column; soft-warn on macOS RLIMIT_AS
variance accepted (mirrors `rlz_oom_test.cpp` precedent).

Fourth OOM-as-data participant in the lineup (after BigRePair, BPE, RLZ).

## §8.10 prefix-search satellite — load-bearing reviewer-disclosure surface

OnPair is the only `NativeOnCompressed` claimant. §8.10 quantifies OnPair's
prefix-search latency advantage vs Uncompressed floor + every
DecompressThenScan baseline. The G6 bit-equality gate is the structural
correctness check; latency is the headline number.

Plot legend ordering (locked at close): Uncompressed → zstd L3 →
LZ4 fast → FSST → RPDAC/PFC/HTFC → BigRePair → BPE V=16K → RLZ R%=5% →
**OnPair bits=16 + bits=14** (NativeOnCompressed; connected curve, rendered
last in foreground).

## NOT done at headline (scope-fence)

- KMP `contains` / EQ `equals` / Aho-Corasick scan / regex / boolean
  composition — all out-of-scope per.
- `bits ∈ {9, 10, 11, 12, 13, 15}` — reviewer-requestable /6
  appendix sweep only.
- `FixedThreshold{5, 10, 20}` — reviewer-requestable; not default mode.

Enforcement:
- `runners/onpair/onpair_runner.cpp` only includes the allowed OnPair
  headers (`api.h`-equivalent narrow subset).
- `tools/lint/onpair_scope_audit.sh` grep prohibits
  `kmp_automaton|aho_corasick|eq_automaton|eq_search|contains\(|equals\(`
  in `runners/onpair/`.
- `onpair_scope_fence_test.cpp` is the belt-and-suspenders C++ unit.

## Round-trip self-tests

12 C++ + 3 Python:

| Test | Purpose |
|------|---------|
| `onpair_roundtrip_test` | Bytewise round-trip at `bits=16` across 6 sub-variants (all_bytes / random_bytes / nul_byte / whitespace / non_utf8 / mixed_empty). Byte-completeness gate. |
| `onpair_random_decode_test` | Boundary + stride-313 probes; bytewise per-row. |
| `onpair_native_prefix_search_test` | **G6 bit-equality** vs Uncompressed canonical scan on 16 fixed needles. **First NativeOnCompressed G6 row in lineup.** |
| `onpair_load_save_identity_test` | determinism: `seed=42` × 2 → identical wire. |
| `onpair_seed_determinism_test` | `seed=42` × 2 identical; both seeds bytewise round-trip. |
| `onpair_empty_column_test` | N=0 + all-empty → `status='unsupported'`. |
| `onpair_oom_test` | OOM-as-data smoke (soft-warn on macOS). |
| `onpair_bit_width_disjointness_test` | `dict_bytes`↑ + `tokens_bytes`↓ across `bits ∈ {12, 14, 16}`. |
| `onpair_dict_offsets_diagnostic_test` | diagnostic metric row emitted, contained within `dict_bytes`, divisible by 4. |
| `onpair_bit_tight_identity_test` | Bit-tight identity at every `bits ∈ {9, 12, 14, 16}`. **Target ninth bit-tight baseline.** |
| `onpair_cluster_b_symmetry_test` | bpe V=16K ↔ OnPair `bits=14` binding. |
| `onpair_scope_fence_test` | Belt-and-suspenders for scope-audit shell script. |
| `test_onpair_smoke.py` | NDJSON handshake; bit-tight; diagnostic; lossless. |
| `test_onpair_native_prefix_search_smoke.py` | Native prefix-search vs Python canonical scan. |
| `test_modes_g7.py::test_g7_pass_onpair` | First `NativeOnCompressed` G7 row. |

## Canonical headline configs

```json
{"bits":16, "sample_fraction":0.15, "seed":42, "threads":1, "mem_cap_gb":64}
{"bits":14, "sample_fraction":0.15, "seed":42, "threads":1, "mem_cap_gb":64}   // the BPE/OnPair size-matched pairing ablation
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
