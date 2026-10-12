#pragma once
// RlzRunner — Relative Lempel-Ziv against uniform-sampled reference.
// Pure C++ reimplementation of Hoobin/Puglisi/Zobel
// VLDB 2011 + Kuruppu PhD thesis 2012 against libdivsufsort 2.0.1 (MIT).
//
// Why a reimplementation (see
// third_party/libdivsufsort/PROVENANCE.txt + docs/benchmark/fairness/rlz.md):
// skuruppu/RLZ has no in-process API + requires
//   libcds (GPL-3.0-or-later). The pure C++ reimplementation lives in
// runners/rlz/rlz_core.{h,cpp} and links no GPL code.
//
// Configuration:
//   - uniform random-sampled blocks (Hoobin 2011).
//   - reference_size_fraction in {0.01, 0.05, 0.25}; the main grid runs 0.05.
//   - natural split (dict=R_len, tokens=packed_phrases, boundaries=Arrow).
//   - each row factorized on its own against the shared reference.
//   - block_size fixed at 16384 (Hoobin 2011 canonical).
//   - RNG seed = 42; recorded in every result row.
//   - training (sampling + SA + factor-parse) inside compress timed region.
//   - mixed-width phrase packing: bits_per_pos + bits_per_len + 8 per
//            triple. bits_per_len is a
//            stored, configurable width (default 8). Motivation: this benchmark
//            targets SHORT strings each compressed INDEPENDENTLY, so per-factor
//            copy lengths are tiny (mean ~3.5–22 B, matches >255 negligible); a
//            16-bit len would waste ~8 bits/factor. 8-bit len => +16–19% ratio
//            at ~0 factor-count change. bits_per_len lives
//            on the wire so the decoder is self-describing.
// prefix_search_mode == DecompressThenScan (no `count`/`locate`/`extract`
// self-index in the runner).
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
    double      reference_size_fraction = 0.05;   // in {0.01, 0.05, 0.25}.
    uint32_t    block_size              = 16384;  // Must be 16384.
    uint32_t    seed                    = 42;     // Must be 42.
    uint32_t    threads                 = 1;
    uint64_t    mem_cap_gb              = 64;
    // Per-factor `len` field width, in bits. The
    // benchmark compresses SHORT strings INDEPENDENTLY (per-string parse, no
    // cross-row factors — see rlz_core), so copy lengths are tiny: measured mean
    // match is ~3.5–22 B and matches >255 are vanishingly rare. A 16-bit len field
    // would waste ~8 bits on EVERY factor; an 8-bit field (max match 255)
    // recovers ~16–19% ratio at negligible extra factor
    // count. Stored on the wire (`bits_per_len`) so decode is self-describing and
    // the width stays tunable. Default 8; range [4, 16].
    uint32_t    max_len_bits            = 8;
    // Off by default; main-grid cells never set these:
    //  - reference_size_bytes > 0 overrides the R% path with an ABSOLUTE dictionary
    //    size (K = ceil(bytes/block_size)); lifts the {1,5,25}% validation. Used by
    //    the reference-size sweep (experiments/rlz_sweep_driver.py, Fig. 7).
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
