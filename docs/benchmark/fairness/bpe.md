# BPE baseline — configuration and fairness notes

The single, canonical **BPE** row of the paper (named by class, like
`zstd` / `fsst` / Re-Pair — not by implementation library).
`is_lossless: true`.

## Implementation: SentencePiece BPE + GPT-2 byte↔unicode bijection

The measured baseline is **Google SentencePiece v0.2.2** in BPE mode
(`--model_type=bpe`), a mature C++ implementation — *not* a hand-rolled
reimplementation. The paper names the row by algorithm class, but its
training/encoding-time results are implementation-specific. The measured
path is pure C++20, without a Python or Rust runtime.

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
`split_by_whitespace=false`, `max_sentencepiece_length=16`, `num_threads=1`,
deterministic (no BPE-dropout).

### History

This row supersedes two earlier artifacts, both retired:
- `bpe-sentencepiece` — SentencePiece on *raw* bytes; quarantined `is_lossless:
  false` for the U+FFFD failure. Fixed here by the byte↔unicode mapping.
- `bpe-byte-level-hf` — a hand-rolled C++ port of minbpe's naive O(vocab×corpus)
  algorithm. Deleted: a production implementation strictly supersedes a
  reimplementation, and the naive trainer was ~0.014 MiB/s (infeasible at scale).

## Train-on-sample (OnPair `sample_fraction` parity)

Training runs on seed-42-shuffled rows until they total `sample_fraction` of the
column bytes (headline `sample_fraction=0.15`, matching OnPair); **encoding is
always over the full column**. This is how BPE is deployed in practice
(SentencePiece subsamples its training set by default) and keeps the comparison
with OnPair symmetric, since OnPair also trains on a sample.

**Compression time is end to end.** `T_c` (`t_compress_ns`) covers the whole
codec path from raw column to packed tokens: sample preparation, training,
model load, full-column encoding and packing, as for every other learned codec
in the roster (FSST, RLZ, Re-Pair, OnPair). The phases are also reported
separately (`t_train_ns`, `t_encode_ns`, ...), and `t_reuse_compress_ns` is the
cost of encoding with an already trained model.

## Correctness (hierarchy)

1. **PRIMARY — exhaustive 0x00..0xFF bytewise round-trip fuzzer**
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

```
[M u32][model_bytes[M]][N u32][V u32][T u32][decoded_bytes u64]
[tok_offsets[N+1] u32][packed_tokens[ceil(T*B/8)] u8]
```
`model_bytes` is the SentencePiece ModelProto blob (`M`). `decoded_bytes` is the
exact decoded payload size, so a bulk decode sizes its output outside the
timer. `B = ceil(log2(V))` per column (shared `runners/core/bitpack`).

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

The runner accepts `vocab_size ∈ {4096, 16384, 65536, 262144}`; the main grid
runs 16384 (`bpe`, = OnPair `bits=14`) and 65536 (`bpe_v65536`, = OnPair
`bits=16`), with `sample_fraction=0.15`, `threads=1`, `mem_cap_gb=64`.
Single-thread fairness (runner rejects `threads != 1`). OOM-as-data
(`setrlimit(RLIMIT_AS)` + `std::bad_alloc` → `status='oom'`). `prefix_search_mode
== DecompressThenScan`: each row is decoded up to the needle's length and
compared. Size-matched pairing: `bpe` V=16384 ↔ OnPair
`bits=14` (`tests/onpair_cluster_b_symmetry_test.cpp`).

For the paper's BPE row (`bpe_v65536`), `vocab_size=65536` is both the ID-space
capacity and the realized model size on all six paper columns: `GetPieceSize()` is
65,536 and the packed stream uses 16-bit IDs. The measured maximum raw piece
length is 16 bytes in every model, with no >16-byte vocabulary entry or token
occurrence. This is SentencePiece's default `max_sentencepiece_length=16`, now
set explicitly in the training arguments. OnPair `bits=16` has the same
65,536-ID capacity and 16-byte token cap, but its learned dictionary may
underfill. The comparison is therefore capacity/ID-width matched, not
necessarily equal in realized entry count.

## Decode architecture: normalized flat kernel vs. SentencePiece API

SentencePiece v0.2.2 supplies vocabulary training and tokenization. The headline
`bulk_decode` result does **not** time `SentencePieceProcessor::Decode`; as stated
in the paper, it expands packed IDs through a decode-minimal flat, pre-expanded
id-to-raw-bytes table, matching OnPair's decode architecture. This intentionally
isolates tokenization, token frequency/length, and dictionary/ID layout from a
general-purpose text detokenizer's semantic and protobuf overhead. Consequently:

- describe the headline result as **BPE tokens + normalized flat decoder**, not
  as SentencePiece detokenization throughput;
- scope training/encoding results to SentencePiece v0.2.2;
- never infer an intrinsic BPE decode limit from SentencePiece's public API.

A semantics-preserving diagnostic remaps only the BPE dictionary IDs and packed
IDs into lexicographic piece order, and the matching OnPair diagnostic remaps
its dictionary by token frequency
(`experiments/dictionary_order_diagnostic_driver.py`). At matched ID order the
two flat decoders run at comparable speed, so learned ID/dictionary locality
explains most of the difference between BPE in its native order and OnPair;
lexicographic ordering is a format trade-off that enables OnPair's compressed
prefix predicates but reduces locality.

For implementation context only, public `Decode(ids, string*)` can be measured
from already-unpacked IDs, row-wise and in one whole-column call
(`experiments/bpe_decode_diagnostic_driver.py`; runner variants
`flat_lexicographic`, `sentencepiece_rows`, `sentencepiece_column`). It is a
general-purpose detokenizer, far slower than the flat kernel, and is not used
as an algorithmic BPE baseline.

## License

SentencePiece v0.2.2 — Apache-2.0 (vendored `third_party/sentencepiece/`).
minbpe — MIT (vendored `third_party/minbpe/`, test-only).

## Decode codegen erratum (identified 2026-06-12; completed 2026-08-15)

**Symptom.** `bulk_decode` throughput for `bpe`/`bpe_v65536` was understated
roughly 2-2.5x in every store measured before this date (including the numbers
in the ICDE 2027 submission). The first correction removed the outlined call
but retained an unnecessary per-token `len > 16` test and generic fallback,
even though the actual models all obey SentencePiece's default 16-byte piece
cap. The complete correction specializes that known-common case once per
loaded model. Its final measurements supersede both the submitted and
intermediate corrected numbers.

**Root cause.** `decode_payload`'s per-token `emit` lambda was used twice:
written inline in the dedicated byte-aligned B==16/B==8 loops AND passed as the
callback to `bitpack::for_each_token` (sub-byte widths). GCC kept a single
outlined copy of the lambda and emitted a `call` per token in the dedicated
loops too; the by-reference captures then lived in memory across iterations,
putting a store/load round-trip plus call/ret on the loop-carried output-cursor
chain (~12 cycles/token vs ~5 inlined). The hazard was known -- the dedicated
loops exist precisely to avoid the generic callback path -- but the shared
lambda re-introduced it through codegen, invisibly at the source level.

**Evidence.** `objdump` of the shipped binary shows
`call <decode_payload::{lambda}>` inside the B==16 loop. A standalone
microbenchmark on identical synthetic data reproduces both regimes: the inlined
kernel runs at 5.7 GiB/s, a forced-outlined call-per-token variant at 2.5 GiB/s,
matching the recorded store values.

**Complete fix.** `BPE_EMIT` (the same pattern as OnPair's
`ONPAIR_EMIT_NO_OFF`) is expanded directly in the byte-aligned loops. A
one-time dispatch on `PieceDict::max_piece_bytes` selects a compiled
`<=16`-byte specialization without the impossible per-token branch, while a
general path remains correct for non-default models containing longer pieces.
The rebuilt B==16 hot loop has neither a per-token call nor a length branch.
Byte-exact round-trip remains unchanged.

**Status.** The submitted manuscript reports the pre-fix values; they were
measured under the uniform timing contract and understate a baseline, not the
paper's system. With the complete fix, BPE and OnPair bulk decode are within
about 20% of each other, with BPE ahead on most columns in its native ID
order, so OnPair is not faster than every compressed baseline on every column.
The camera-ready reports the corrected values, produced by the headline grid
and by `experiments/decode_driver.py` (paired run). Only the custom
BPE `bulk_decode` adapter was affected; compression ratios and BPE random
access (`decode_row_into`, a separate loop) are untouched, and no other runner
shares the pattern.

**Lesson.** When a closure is both used in a hot loop and passed as a callback
elsewhere, the compiler may outline it and silently route the hot loop through
a call -- verify the codegen (objdump) or use a macro/template functor so the
hot path cannot degrade. A data-independent flat per-token rate is the red flag
that a decode loop has become instruction-bound rather than memory-bound.
