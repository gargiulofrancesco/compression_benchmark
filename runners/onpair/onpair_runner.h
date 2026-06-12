#pragma once
// OnPairRunner — the artefact under measurement.
// OnPair runner. The library under measurement is consumed through the same
// public API as any downstream user (no privileged path).
//   - bits=16 library default at headline; bits=14 is the size-matched
//     BPE-pairing ablation cell.
//   - seed=42, DynamicThreshold{0.15}: library defaults.
//: NON-inheritance (fourth in lineup, fourth rationale —
//          lex-sorted *token dictionary*, NOT lex-sorted column rows).
//: dict_offsets_bytes diagnostic sub-counter  —
//          emitted as an additional metric_name row at every compress;
//          NOT a schema column; NOT in §12.6 ratio formula.
//: BitWidth ceiling [9..16]; honest BPE V=256K asymmetry.
//: FetchContent + commit SHA; NOT vendored under third_party/
//          (the library is the artefact under measurement).
// Inheritances:
// (seventh consecutive): training inside compress timed region.
// FLIPPED: prefix_search_mode == NativeOnCompressed (only claimant).
// Size-matched pairing: BPE V=2^b is always compared against OnPair bits=b.
// /fixed-width packing reused via Store's uniform B.
// Scope-fence: contains/equals/KMP/AhoCorasick/EQ
// out of harness measurement path; CMake-include + CI-grep enforce via
// tools/lint/onpair_scope_audit.sh.
//
// Wire format: OnPairColumn::write_to / read_from from the library
// directly. Per integration probe of upstream src/column.cpp
// at SHA f8ecc64f, the wire is:
//   [MAGIC u8[8]]
//   [bits u8]
//   [dict_true_bytes u32][dict_bytes_arr[dict_true_bytes] u8]
//   [num_tokens+1 u32][dict_offsets[num_tokens+1] u32]
//   [real_words u32][packed[real_words] u64]
//   [N+1 u32][boundaries[N+1] u32]
//
// Sub-counter mapping (NON-collapse; natural split; fifth in lineup):
//   dict_bytes        = Dictionary::bytes_used() = dict_true_bytes + (num_tokens+1)*4
//   dict_offsets_bytes= (num_tokens+1)*4                                  [diagnostic]
//   tokens_bytes      = real_words * 8                                    [packed payload]
//   boundaries_bytes  = 25 + (N+1)*4                                      [framing scalars + boundaries[]]
//                       (8 MAGIC + 1 bits + 4 true_bytes scalar +
//                        4 dict_offsets size + 4 real_words +
//                        4 boundaries size + (N+1)*4)
// Bit-tight identity (ninth in lineup):
//   dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes (exactly).

#include "runner_protocol.h"

#include <cstdint>
#include <string>

namespace onpair_bench {

struct OnPairConfig {
    uint32_t bits             = 16;    // default; the size-matched BPE pairing uses 14.
    double   sample_fraction  = 0.15;  // DynamicThreshold{0.15}; used when strategy=="dynamic".
    uint64_t seed             = 42;    // LOCKED at headline.
    uint32_t threads          = 1;     // Single-thread fairness lock.
    uint64_t mem_cap_gb       = 64;    // Inherits BigRePair/BPE/RLZ value.
    // step 2 (A1 ablation) — threshold-strategy knob.
    // "dynamic"              → DynamicThreshold{sample_fraction} (headline default)
    //   "fixed"                → FixedThreshold{threshold_value}; value ∈ [2, 255]
    //   "fixed_dynamic_paired" → consumed only by held_out_compress op (A1 protocol)
    std::string threshold_strategy = "dynamic";
    uint32_t    threshold_value    = 0;
};

OnPairConfig parse_onpair_config(const std::string& cfg_json);

class OnPairRunner final : public Runner {
public:
    const char* name()    const override { return "onpair"; }
    const char* version() const override;  // "onpair-cpp+<sha[:8]>"
    PrefixSearchMode prefix_search_mode() const override {
        return PrefixSearchMode::NativeOnCompressed;   // only claimant
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
    // step 6 (A5): times n_samples per-string random accesses; OnPair's
    // L_ra is block-independent (the flat reference line of figure A5).
    OpResult ra_latency(const std::string& in_path,
                        const std::string& out_path,
                        const std::string& config_json) override;
    // step 2 — A1 ablation. Trains both Fixed{T} and matched-Dynamic{f}
    // on `train_in_path` with seed=42; encodes `heldout_in_path` with each
    // trained dictionary. Emits 12 paired metric rows + sample_bytes_consumed_fixed
    // + matched_fraction in result.metrics.
    OpResult held_out_compress(const std::string& train_in_path,
                               const std::string& heldout_in_path,
                               const std::string& out_path,
                               const std::string& config_json) override;
};

} // namespace onpair_bench
