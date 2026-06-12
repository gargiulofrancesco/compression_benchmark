#pragma once
// Lz4Runner — block-framed two-level column store see docs/benchmark/fairness/lz4.md.
// Wire layout matches zstd; only the compression call differs.
// Public class so unit tests can drive it without going through the NDJSON protocol.

#include "runner_protocol.h"

#include <cstdint>
#include <string>
#include <vector>

namespace onpair_bench {

struct Lz4Config {
    enum class Tier { Fast, HC };
    Tier tier                   = Tier::Fast;
    int accel                   = 1;          // Fast tier acceleration; ignored for HC
    int level                   = 9;          // HC compressionLevel (1..12); ignored for Fast
    uint32_t block_budget_bytes = 1u << 20;   // 1 MiB
    // step 6 (A5): when > 0, each block holds exactly this many strings
    // (the A5 block-size-in-strings sweep) and block_budget_bytes is ignored.
    // 0 (default) keeps the headline byte-budget planning untouched.
    uint32_t block_strings      = 0;
};

Lz4Config parse_lz4_config(const std::string& cfg_json);

struct Lz4Wire {
    uint32_t N = 0, B = 0;
    std::vector<uint32_t> block_offsets;       // size B+1
    std::vector<uint32_t> block_first_string;  // size B+1
    std::vector<uint32_t> block_raw_sizes;     // size B — decompressed PAYLOAD bytes
                                               // per block; LZ4 frames don't self-
                                               // describe size (unlike zstd), so we
                                               // store it to size the decode buffer
                                               // EXACTLY. Frames are payload-only now.
    // Per-block LOCAL row offsets (k_b+1 entries each, 0..payload size), concatenated;
    // total = N+B. Uncompressed on the wire so each frame is PAYLOAD-ONLY and decode
    // is single-pass straight into the column. Block b's slice begins at index
    // block_first_string[b]+b. Billed as boundaries (excluded from the payload-only
    // ratio), so this is accounting-neutral. See zstd_runner.h for the rationale.
    std::vector<uint32_t> within_offsets;
    std::vector<uint8_t>  compressed_blocks;
};

class Lz4Runner final : public Runner {
public:
    Lz4Runner();
    ~Lz4Runner() override;

    const char* name()    const override { return "lz4"; }
    const char* version() const override { return "1.10.0"; }
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
    // Allocated outside the timed region at boot; reused across blocks via
    // LZ4_compress_{fast,HC}_extState (per LZ4 card timing rule).
    std::vector<char> state_;
    std::vector<char> state_hc_;
};

} // namespace onpair_bench
