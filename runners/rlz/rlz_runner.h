#pragma once
// RlzRunner — Relative Lempel-Ziv against uniform-sampled reference.
//. Pure C++ reimplementation of Hoobin/Puglisi/Zobel
// VLDB 2011 + Kuruppu PhD thesis 2012 against libdivsufsort 2.0.1 (MIT).
//
// (logged in
// third_party/libdivsufsort/PROVENANCE.txt + docs/benchmark/fairness/rlz.md):
// skuruppu/RLZ has no in-process API + requires
//   libcds (GPL-3.0-or-later). Pure C++ reimplementation under runners/rlz/
// rlz_core.{h,cpp} per-runner core split precedent.
// (GPL-3.0 escalation) voided by this.
//
// All other Q-RLZs from remain binding:
//:  uniform random-sampled blocks (Hoobin 2011).
//:  reference_size_fraction in {0.01, 0.05, 0.25}; all three headline.
//:  natural split (dict=R_len, tokens=packed_phrases, boundaries=Arrow).
//:  per-string against shared reference (NON-inheritance, third rationale).
//:  block_size locked at 16384 (Hoobin 2011 canonical).
//:  RNG seed = 42; recorded in every result row.
//:  training (sampling + SA + factor-parse) inside compress timed region
// Not part of the BPE/OnPair size-matched pairing.
//:  mixed-width phrase packing: bits_per_pos + bits_per_len + 8 per
// triple. REVISED bits_per_len dropped from a fixed 16 to a
//            stored, configurable width (default 8). Motivation: this benchmark
//            targets SHORT strings each compressed INDEPENDENTLY, so per-factor
//            copy lengths are tiny (mean ~3.5–22 B, matches >255 negligible); the
//            old 16-bit len wasted ~8 bits/factor. 8-bit len => +16–19% ratio on
//            every headline column, ~0 factor-count change. bits_per_len now lives
//            on the wire so the decoder is self-describing.
// inheritance: prefix_search_mode == DecompressThenScan (ninth consecutive;
// no `count`/`locate`/`extract` in runner; ).
//   Refused-env path: column_size · R% < block_size -> status='refused-env'.
//
// Wire = [R_len u32][R[R_len]][N u32][P_total u32][bits_per_pos u32]
//        [bits_per_len u32][decoded_bytes u64][phrase_offsets[N+1] u32]
//        [packed_phrases[ceil(P_total*(bits_per_pos+bits_per_len+8)/8)] u8].

#include "runner_protocol.h"

#include <cstdint>
#include <string>

namespace onpair_bench {

struct RlzConfig {
    double      reference_size_fraction = 0.05;   //: in {0.01, 0.05, 0.25}.
    uint32_t    block_size              = 16384;  // LOCKED.
    uint32_t    seed                    = 42;     // LOCKED.
    uint32_t    threads                 = 1;
    uint64_t    mem_cap_gb              = 64;
    //: per-factor `len` field width, in bits. The
    // benchmark compresses SHORT strings INDEPENDENTLY (per-string parse, no
    // cross-row factors — see rlz_core), so copy lengths are tiny: measured mean
    // match is ~3.5–22 B and matches >255 are vanishingly rare. A 16-bit len field
    // therefore wasted ~8 bits on EVERY factor; an 8-bit field (max match 255)
    // recovers ~16–19% ratio across all headline columns at negligible extra factor
    // count. Stored on the wire (`bits_per_len`) so decode is self-describing and
    // the width stays tunable. Default 8; range [4, 16].
    uint32_t    max_len_bits            = 8;
    // EXPERIMENTAL (off by default; headline cells never set these):
    //  - reference_size_bytes > 0 overrides the R% path with an ABSOLUTE dictionary
    //    size (K = ceil(bytes/block_size)); lifts the {1,5,25}% validation. Lets us
    //    sweep cache-resident dictionaries (64 KiB .. 2 MiB).
    //  - wildcopy_bytes selects the decode over-copy width (8/16/32/64); default 32.
    uint64_t    reference_size_bytes    = 0;
    uint32_t    wildcopy_bytes          = 32;
};

RlzConfig parse_rlz_config(const std::string& cfg_json);

class RlzRunner final : public Runner {
public:
    RlzRunner();
    ~RlzRunner() override;
    const char* name()    const override { return "rlz"; }
    const char* version() const override;
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
