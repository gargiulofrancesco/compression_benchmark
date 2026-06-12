#pragma once
// BpeRunner — SentencePiece BPE compressor with bit-packed token stream.
// Configuration rationale: docs/benchmark/fairness/bpe.md.
//
//: SentencePiece v0.2.0 Apache-2.0; no license escalation.
//: vocab_size in {4096, 16384, 65536, 262144}; all four headline.
//: full column, OOM-as-data (setrlimit + bad_alloc).
//: natural dict/tokens split (structurally guaranteed; third in lineup).
//: byte<->unicode bijection (GPT-2 bytes_to_unicode) for byte-exact
//           losslessness; normalization=identity; NLP transforms off. (Replaces
//           the former byte_fallback approach, which decoded lone high bytes as
//           U+FFFD — see docs/benchmark/fairness/bpe.md, "Why the byte<->unicode mapping".)
//: enable_sampling=false; deterministic compression.
//: uniform-width per column, B = ceil(log2(V)) in {12, 14, 16, 18}.
// inheritance: training inside compress timed region (seventh
// consecutive). the BPE/OnPair size-matched pairing primer for OnPair.
// inheritance: prefix_search_mode == DecompressThenScan
//         (PieceToId / IdToPiece NOT invoked; plan section 17 scope-fence;
//         CI-grep-audited by tools/lint/bpe_scope_audit.sh).
// NON-inheritance (second in lineup; different rationale than
//         BigRePair): per-string Encode without sort.
//
// Wire = [M u32][model_bytes[M]][N u32][V u32][T u32]
//        [tok_offsets[N+1] u32][packed_tokens[ceil(T*B/8)] u8].
// Bit-tight identity (sixth bit-tight baseline after FSST/RPDAC/PFC/HTFC/
// BigRePair): dict_bytes(M) + tokens_bytes(ceil(T*B/8)) + boundaries_bytes
// (16 + 4*(N+1)) == output_bytes, exactly.

#include "runner_protocol.h"

#include <cstdint>
#include <string>

namespace onpair_bench {

struct BpeConfig {
    uint32_t    vocab_size      = 16384;       // LOCKED: in {4096, 16384, 65536, 262144}.
    std::string normalization   = "identity";  // LOCKED.
    double      sample_fraction = 0.15;        // train on this fraction of input
                                               // bytes (OnPair sample_fraction parity);
                                               // encode is always over the full column.
    uint32_t    threads         = 1;           // Single-thread fairness lock.
    uint64_t    mem_cap_gb      = 64;          // OOM-as-data anchor.
};

BpeConfig parse_bpe_config(const std::string& cfg_json);

class BpeRunner final : public Runner {
public:
    BpeRunner();
    ~BpeRunner() override;

    const char* name()    const override { return "bpe"; }
    const char* version() const override { return "sentencepiece-v0.2.0+bytemap"; }
    PrefixSearchMode prefix_search_mode() const override {
        return PrefixSearchMode::DecompressThenScan;
    }

    OpResult compress(const std::string& in_path,
                      const std::string& out_path,
                      const std::string& config_json) override;
    OpResult bulk_decode(const std::string& in_path,
                         const std::string& out_path,
                         const std::string& config_json) override;
    OpResult random_decode(const std::string& in_path,
                           size_t idx,
                           const std::string& out_path,
                           const std::string& config_json) override;
    OpResult prefix_search(const std::string& in_path,
                           const std::string& needle,
                           const std::string& out_path,
                           const std::string& config_json) override;
    OpResult ra_latency(const std::string& in_path,
                        const std::string& out_path,
                        const std::string& config_json) override;
};

} // namespace onpair_bench
