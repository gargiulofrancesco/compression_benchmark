#pragma once
// ZstdRunner — block-framed two-level column store see docs/benchmark/fairness/zstd.md.
// Public class so unit tests can drive it without going through the NDJSON protocol.

#include "runner_protocol.h"

#include <cstdint>
#include <string>
#include <vector>

struct ZSTD_CCtx_s;
struct ZSTD_DCtx_s;

namespace onpair_bench {

struct ZstdConfig {
    int level                  = 3;
    uint32_t block_budget_bytes = 1u << 20;  // 1 MiB
    // step 6 (A5): when > 0, each block holds exactly this many strings
    // (the A5 block-size-in-strings sweep) and block_budget_bytes is ignored.
    // 0 (default) keeps the headline byte-budget planning untouched.
    uint32_t block_strings      = 0;
};

ZstdConfig parse_zstd_config(const std::string& cfg_json);

struct ZstdWire {
    uint32_t N = 0, B = 0;
    std::vector<uint32_t> block_offsets;       // size B+1
    std::vector<uint32_t> block_first_string;  // size B+1
    // Per-block LOCAL row offsets (each block contributes k_b+1 entries, 0..payload
    // size), concatenated; total = N+B entries. Stored UNCOMPRESSED on the wire so
    // each compressed frame is PAYLOAD-ONLY — decode writes each block straight into
    // the output column in one pass (no second payload-concat copy), and the per-row
    // offsets are rebuilt UNTIMED straight from here (no re-decompress). Block b's
    // slice starts at index block_first_string[b]+b. Billed as boundaries (excluded
    // from the payload-only ratio), so moving them out of the frame is accounting-
    // neutral; it only makes tokens_bytes the exact compressed-payload size.
    std::vector<uint32_t> within_offsets;
    std::vector<uint8_t>  compressed_blocks;
};

class ZstdRunner final : public Runner {
public:
    ZstdRunner();
    ~ZstdRunner() override;

    const char* name()    const override { return "zstd"; }
    const char* version() const override { return "1.5.6"; }
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
    // step 6 (A5): times n_samples block-decode random accesses.
    OpResult ra_latency(const std::string& in_path,
                        const std::string& out_path,
                        const std::string& config_json) override;

private:
    ZSTD_CCtx_s* cctx_ = nullptr;
    ZSTD_DCtx_s* dctx_ = nullptr;
};

} // namespace onpair_bench
