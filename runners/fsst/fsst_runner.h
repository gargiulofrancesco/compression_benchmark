#pragma once
// FsstRunner — native-structure column store; see docs/benchmark/fairness/fsst.md.
// Wire = [H u32][header[H]][N u32][compressed_offsets[N+1] u32]
//        [decoded_offsets[N+1] u32][codewords[Σ Lc_i] u8].
// Public class so unit tests can drive it without going through the NDJSON protocol.

#include "runner_protocol.h"

#include <cstdint>
#include <string>
#include <vector>

namespace onpair_bench {

struct FsstConfig {
    // FSST has no level dimension. Headline is the single canonical row.
    // Any other variant or training value is rejected.
    std::string variant  = "fsst";          // only "fsst" is accepted
    std::string training = "full_column";   // only "full_column" is accepted
    // simd=false forces the scalar compression path even on AVX-512 hardware
    // (baseline `fsst_scalar`), so the with/without-AVX-512 points
    // can be measured on the same machine. AVX-512 accelerates compression only — the
    // decoder is scalar regardless — so the two points share ratio + decode T_d.
    bool simd = true;
};

FsstConfig parse_fsst_config(const std::string& cfg_json);

struct FsstWire {
    uint32_t H = 0, N = 0;
    std::vector<uint8_t>  header;              // size H (the fsst_export blob)
    std::vector<uint32_t> compressed_offsets;  // size N+1; per-row CODEWORD boundaries (offsets[N]=codewords.size()); needed for random access
    std::vector<uint32_t> decoded_offsets;     // size N+1; per-row DECODED boundaries (= original string offsets); rebuilds the output column UNTIMED
    std::vector<uint8_t>  codewords;
};

class FsstRunner final : public Runner {
public:
    FsstRunner();
    ~FsstRunner() override;

    const char* name()    const override { return "fsst"; }
    const char* version() const override { return "master-e638d4cf"; }
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
    // Random-access latency under a query distribution.
    // Per-string FSST decode (O(|s_i|), position-independent);
    // the second per-string codec, next to OnPair, in the A6 roster.
    OpResult ra_latency(const std::string& in_path,
                        const std::string& out_path,
                        const std::string& config_json) override;
    OpResult prefix_search(const std::string& in_path,
                           const std::string& needle,
                           const std::string& out_path,
                           const std::string& config_json) override;
};

} // namespace onpair_bench
