#pragma once
// Runner ABC + NDJSON dispatch loop.
// Every baseline (zstd, LZ4, FSST,..., OnPair) implements this ABC.
// The dispatch loop owns file I/O, column serialisation, and steady_clock timing —
// baselines see typed Columns and streams only.

#include <cstdint>
#include <iosfwd>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace onpair_bench {

struct Column {
    std::vector<uint8_t>  bytes;     // flat payload
    std::vector<uint32_t> offsets;   // Arrow-style; size = N+1; offsets[0] = 0, offsets[N] = bytes.size()

    size_t num_rows() const noexcept { return offsets.empty() ? 0 : offsets.size() - 1; }
    std::string_view at(size_t i) const noexcept {
        return {reinterpret_cast<const char*>(bytes.data() + offsets[i]),
                offsets[i + 1] - offsets[i]};
    }
};

// Disk format for Columns (used by both the orchestrator's synthetic generator
// and the runner's input/output paths):
//   [N u32][total_bytes u32][offsets[N+1] u32][bytes[total_bytes] u8]
Column read_column(const std::string& path);
void   write_column(const std::string& path, const Column& c);

enum class PrefixSearchMode { NativeOnCompressed, DecompressThenScan, Uncompressed };
const char* psm_str(PrefixSearchMode m) noexcept;

struct OpResult {
    std::string status = "ok";
    std::vector<std::pair<std::string, double>> metrics;
    std::string notes;
};

class Runner {
public:
    virtual ~Runner() = default;
    virtual const char*       name()               const = 0;
    virtual const char*       version()            const = 0;
    virtual PrefixSearchMode  prefix_search_mode() const = 0;

    // Read input column from `in_path`; write compressed payload to `out_path`.
    // Report dict_bytes / tokens_bytes / boundaries_bytes (and any natural-split
    // sub-counters) in result.metrics.
    virtual OpResult compress(const std::string& in_path,
                              const std::string& out_path,
                              const std::string& config_json) = 0;

    // Read compressed payload from `in_path`; write reconstructed Column to `out_path`.
    virtual OpResult bulk_decode(const std::string& in_path,
                                 const std::string& out_path,
                                 const std::string& config_json) = 0;

    // Read compressed payload from `in_path`; write single decoded string to `out_path`
    // (as raw bytes — no length prefix; orchestrator validates via size).
    virtual OpResult random_decode(const std::string& in_path,
                                   size_t idx,
                                   const std::string& out_path,
                                   const std::string& config_json) = 0;

    // Read compressed payload from `in_path`; write hit list to `out_path`
    // as [n_hits u32][hit_indices[n_hits] u32].
    virtual OpResult prefix_search(const std::string& in_path,
                                   const std::string& needle,
                                   const std::string& out_path,
                                   const std::string& config_json) = 0;

    // step 2 A1 ablation — OnPair only. Reads train and heldout
    // columns, trains on `train_in_path` using both Fixed{T} and matched
    // Dynamic{f}, then encodes `heldout_in_path` with each trained dictionary.
    // Default implementation returns status='unsupported'; the OnPair runner
    // overrides it. `out_path` is reserved for the future (currently unused).
    virtual OpResult held_out_compress(const std::string& /*train_in_path*/,
                                       const std::string& /*heldout_in_path*/,
                                       const std::string& /*out_path*/,
                                       const std::string& /*config_json*/) {
        return OpResult{.status = "unsupported",
                        .notes  = "held_out_compress not implemented by this runner"};
    }

    // step 6 (A5) — random-access latency primitive. Reads a compressed
    // column from `in_path`, then times `n_samples` single-string random
    // accesses drawn from a query distribution (config_json: n_samples, seed,
    // query_distribution, warmup) with NO file I/O inside the timed loop, and
    // reports l_ra_{p50,p95,p99,mean,min,max}_ns. For block-based baselines
    // (zstd, lz4) each access decodes the enclosing block, so L_ra grows with
    // block size; OnPair seeks per-string, so its L_ra is block-independent
    // (the flat reference line of figure A5). Default returns 'unsupported';
    // the zstd / lz4 / onpair runners override it. `out_path` is unused.
    virtual OpResult ra_latency(const std::string& /*in_path*/,
                                const std::string& /*out_path*/,
                                const std::string& /*config_json*/) {
        return OpResult{.status = "unsupported",
                        .notes  = "ra_latency not implemented by this runner"};
    }
};

// NDJSON dispatch loop. Sends `hello` once on entry, then processes ops until
// {"type":"bye"} or EOF. Returns process exit code (0 = clean shutdown).
int run_protocol_loop(Runner& r, const std::string& build_flags_sha);

} // namespace onpair_bench
