# BPE baseline — configuration and fairness notes

The single, canonical **BPE** row of the paper (named by class, like
`zstd` / `fsst` / Re-Pair — not by implementation library).
`is_lossless: true`.

## Implementation: SentencePiece BPE + GPT-2 byte↔unicode bijection

The measured baseline is **Google SentencePiece v0.2.0** in BPE mode
(`--model_type=bpe`), the production implementation in the GPT-2 / RoBERTa
lineage — *not* a hand-rolled reimplementation. Phase-0 lock preserved: pure
C++20 in the measured path, no Python interpreter, no Rust toolchain.

SentencePiece on raw bytes is **not** byte-lossless (even with `byte_fallback`
it decodes lone high-bit non-UTF-8 bytes as U+FFFD). We make it byte-exact the
same way GPT-2/RoBERTa byte-level BPE does: the **`bytes_to_unicode` bijection**
maps each of the 256 byte values to a distinct printable Unicode codepoint, so
the text handed to SentencePiece is always valid UTF-8 and control/whitespace
bytes can never perturb tokenisation. The mapping is inverted on decode. A single
**coverage line** carrying all 256 codepoints plus `--character_coverage=1.0`
makes every byte an atomic symbol, so a vocab trained on a *sample* still
round-trips any byte present in the full column.

Settings (see `runners/bpe/bpe_runner.cpp`): `normalization_rule_name=identity`,
`add_dummy_prefix=false`, `remove_extra_whitespaces=false`,
`split_by_whitespace=false`, `num_threads=1`, deterministic (no BPE-dropout).

### History (consolidation,)

This row supersedes two earlier artifacts, both retired:
- `bpe-sentencepiece` — SentencePiece on *raw* bytes; quarantined `is_lossless:
  false` for the U+FFFD failure. Fixed here by the byte↔unicode mapping.
- `bpe-byte-level-hf` — a hand-rolled C++ port of minbpe's naive O(vocab×corpus)
  algorithm. Deleted: a production implementation strictly supersedes a
  reimplementation, and the naive trainer was ~0.014 MiB/s (infeasible at scale).

## Train-on-sample (OnPair `sample_fraction` parity)

Training runs on a deterministic prefix of rows totalling `sample_fraction` of the
column bytes (headline `sample_fraction=0.15`, matching OnPair); **encoding is
always over the full column**. This is how BPE is deployed in practice
(SentencePiece subsamples its training set by default) and restores Cluster-B
symmetry with OnPair, which also trains on a sample.

**Training vs. encoding time are reported separately** (`t_train_ns`,
`t_encode_ns`), per the fairness-audit requirement for baselines with a training
phase (cf. Re-Pair build time). `T_c` derives from `t_encode_ns` over the full
column; training time is the appendix build-time row.

## Correctness (hierarchy)

1. **PRIMARY gate — exhaustive 0x00..0xFF bytewise round-trip fuzzer**
   (`tests/bpe_roundtrip_test.cpp`): compress → bulk_decode must reproduce the
   input byte-for-byte over every byte value, lone high bytes, NUL, newline, and
   empty rows. This is the load-bearing `is_lossless: true` assertion.
2. **SECONDARY — minbpe independent round-trip cross-check**
   (`tests/test_bpe_minbpe_crosscheck.py`, runs on a tiny slice in the test suite
   only). minbpe (Karpathy 2024, MIT; vendored `third_party/minbpe/`) is **not a
   baseline and not on any measurement path** — it is an independent BPE that
   operates on raw bytes and never touches the byte↔unicode mapping. Driving it
   and the `bpe` runner on the same slice and asserting both round-trip to the
   exact input catches a class of bug a SentencePiece *self*-round-trip cannot
   (e.g. the mapping applied inconsistently between encode and decode, which could
   self-cancel). It asserts **bytewise round-trip equivalence only** — explicitly
   **not** token identity: SentencePiece's vocab and tie-breaks legitimately
   differ from minbpe's.

## Wire format / bit-tight identity

Unchanged from the consolidated lineup:
```
[M u32][model_bytes[M]][N u32][V u32][T u32]
[tok_offsets[N+1] u32][packed_tokens[ceil(T*B/8)] u8]
```
`model_bytes` is the SentencePiece ModelProto blob (`M`). `B = ceil(log2(V))` per
column (shared `runners/core/bitpack`).

### Dictionary accounting — DECODE-MINIMAL model

The compression-ratio model cost is the **decode-minimal id→bytes table**, symmetric
with OnPair's `dict_content_bytes + dict_offset_bytes` and FSST's symbol-table
header — *not* the full serialized ModelProto:

```
dict_content_bytes = Σ_id |raw bytes of piece id|     (byte<->unicode mapping inverted)
dict_offset_bytes  = (V+1)·4                           (one u32 offset per id, like OnPair)
dict_bytes         = dict_content_bytes + dict_offset_bytes      ← charged to the ratio
tokens_bytes       = ceil(T·B/8)
boundaries_bytes   = 24 + 4·(N+1)                      (string offsets + wire scalars; excluded)
output_bytes       = dict_bytes + tokens_bytes + boundaries_bytes      (accounted/fair size; bit-tight)
model_proto_bytes  = M                                 (full proto, reported for transparency)
wire_bytes         = M + 24 + 4·(N+1) + ceil(T·B/8)    (literal on-disk size)
```

**Rationale.** The benchmark compares *compression algorithms*, not serialization
formats. The full `M` additionally carries per-piece float scores + protobuf framing
the decoder never reads, and stores pieces in 2-byte-per-original-byte GPT-2-mapped
form — encoder/serialization overhead that would *unfairly inflate* BPE's compressed
size against OnPair (whose `dict_bytes` is already decode-minimal). The runner already
builds this exact `id→bytes` table (`PieceDict`) for decoding, so the decode-minimal
model is a real, trivially-serialisable artifact, not a hypothetical. `M` is still
what the wire stores and is reported (`model_proto_bytes`/`wire_bytes`) so nothing is
hidden. Measured effect: ~+6.5% BPE ratio on an ASCII synthetic column (more on
non-ASCII columns, where the 2× mapping bites). Bit-tight identity holds on the
accounted size; `wire_bytes ≥ output_bytes`.

## Configuration

Headline configs: `vocab_size ∈ {4096, 16384, 65536, 262144}` (V=16384 canonical
middle = OnPair `bits=14`), `sample_fraction=0.15`, `threads=1`, `mem_cap_gb=64`.
Single-thread fairness lock (runner rejects `threads != 1`). OOM-as-data
(`setrlimit(RLIMIT_AS)` + `std::bad_alloc` → `status='oom'`). `prefix_search_mode
== DecompressThenScan`. Cluster-B symmetry: `bpe` V=16384 ↔ OnPair
`bits=14` (`tests/onpair_cluster_b_symmetry_test.cpp`).

## License

SentencePiece v0.2.0 — Apache-2.0 (vendored `third_party/sentencepiece/`).
minbpe — MIT (vendored `third_party/minbpe/`, test-only).
