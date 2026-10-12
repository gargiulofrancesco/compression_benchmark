#pragma once
// RepairRunner — plain Re-Pair grammar compression of the WHOLE column text.
//
// Just Re-Pair, no prefix coding. Unlike the
// libCSD string dictionaries (RPDAC = Re-Pair+DAC, RPFC = Re-Pair+FrontCoding),
// this is the canonical Larsson-Moffat Re-Pair grammar over the FULL column
// concatenation (duplicates included — they are captured as grammar rules, NOT
// deduplicated). There is no string dictionary, no id-sequence, and no DAC/
// front-coding random-access structure: it is the bare grammar (rules) + the
// top-level symbol sequence, the Re-Pair ratio ceiling.
//
// Grammar is built via libCSD's RePair class (we reuse its IRePair compressor),
// then we serialise the grammar + sequence ourselves (bit-packed, no DAC).
//
// Wire = [terminals u32][rules u32][n_seq u32][bits u32][N u32][decoded_bytes u64]
//        [row_offsets[N+1] u32]          # per-row byte boundaries (Arrow); boundaries
//        [packed_grammar]                # 2*rules symbols at `bits` bits (dict)
//        [packed_seq]                    # n_seq symbols at `bits` bits (tokens)
// Accounting: dict_bytes = |packed_grammar|, tokens_bytes = |packed_seq|,
// boundaries_bytes = scalars + row_offsets. ratio = input/(dict+tokens) — the
// id-sequence accounting issue of RPDAC/PFC does NOT apply (no id array).
//
// Random access descends the grammar over an index built at load time (see
// ra_latency below); prefix_search is DecompressThenScan.

#include "runner_protocol.h"

#include <cstdint>
#include <string>

namespace onpair_bench {

struct RepairConfig {
    uint64_t mem_cap_gb = 64;
};

RepairConfig parse_repair_config(const std::string& cfg_json);

class RepairRunner final : public Runner {
public:
    RepairRunner();
    ~RepairRunner() override;
    const char* name()    const override { return "repair"; }
    const char* version() const override { return "libcsd-RePair-plain"; }
    PrefixSearchMode prefix_search_mode() const override {
        return PrefixSearchMode::DecompressThenScan;
    }
    OpResult compress(const std::string& in_path, const std::string& out_path,
                      const std::string& config_json) override;
    OpResult bulk_decode(const std::string& in_path, const std::string& out_path,
                         const std::string& config_json) override;
    OpResult random_decode(const std::string& in_path, size_t idx,
                           const std::string& out_path,
                           const std::string& config_json) override;
    // Per-string grammar-descent access: O(log n_seq + depth +
    // |s_i|) per query over an untimed load-time index (rule lengths +
    // top-level prefix sums).
    OpResult ra_latency(const std::string& in_path,
                        const std::string& out_path,
                        const std::string& config_json) override;
    OpResult prefix_search(const std::string& in_path, const std::string& needle,
                           const std::string& out_path,
                           const std::string& config_json) override;
};

} // namespace onpair_bench
