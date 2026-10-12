#pragma once
// BpeRunner — SentencePiece BPE compressor with bit-packed token stream.
// Configuration rationale: docs/benchmark/fairness/bpe.md.
//
//   - SentencePiece v0.2.2, Apache-2.0.
//   - vocab_size in {4096, 16384, 65536, 262144}; the main grid runs 16384
//     and 65536.
//   - Trains on a sample of the column (sample_fraction), encodes the full
//     column; OOM-as-data (setrlimit + bad_alloc).
//   - Natural dict/tokens split.
//   - byte<->unicode bijection (GPT-2 bytes_to_unicode) for byte-exact
//           losslessness; normalization=identity; NLP transforms off. (A
//           byte_fallback model would decode lone high bytes as U+FFFD — see
//           docs/benchmark/fairness/bpe.md, "Implementation".)
//   - No sampling at encode time; deterministic compression.
//   - Uniform-width tokens per column, B = ceil(log2(V)) in {12, 14, 16, 18}.
// As for every baseline, training is inside the compress timed region.
// prefix_search_mode == DecompressThenScan: each row's prefix is decoded
//         and compared; nothing is searched in compressed form.
// Rows are encoded one by one, in column order (no sort).
//
// Wire = [M u32][model_bytes[M]][N u32][V u32][T u32][decoded_bytes u64]
//        [tok_offsets[N+1] u32][packed_tokens[ceil(T*B/8)] u8].
// Accounted size: dict_bytes (the decode table: raw piece bytes + 4*(V+1),
// not the M-byte model proto) + tokens_bytes(ceil(T*B/8)) + boundaries_bytes
// (24 + 4*(N+1)) == output_bytes, exactly.

#include "runner_protocol.h"

#include <cstdint>
#include <string>

namespace onpair_bench {

struct BpeConfig {
    uint32_t    vocab_size      = 16384;       // Must be in {4096, 16384, 65536, 262144}.
    std::string normalization   = "identity";  // Only "identity" is accepted.
    double      sample_fraction = 0.15;        // train on this fraction of input
                                               // bytes (OnPair sample_fraction parity);
                                               // encode is always over the full column.
    uint32_t    threads         = 1;           // Single-thread fairness; must be 1.
    uint64_t    mem_cap_gb      = 64;          // Address-space cap (OOM-as-data).
};

BpeConfig parse_bpe_config(const std::string& cfg_json);

class BpeRunner final : public Runner {
public:
    BpeRunner();
    ~BpeRunner() override;

    const char* name()    const override { return "bpe"; }
    const char* version() const override { return "sentencepiece-v0.2.2+bytemap"; }
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
