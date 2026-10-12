// Lz4Runner implementation. See lz4_runner.h for class shape.
// Wire format on disk (two-level layout, as zstd):
//   [N u32][B u32][block_offsets[B+1] u32][block_first_string[B+1] u32]
//   [block_raw_sizes[B] u32][within_offsets[N+B] u32][compressed_blocks[total] u8]
// Each block's uncompressed payload is its strings concatenated; the k+1 local
// row offsets of every block are stored uncompressed in within_offsets.
// block_raw_sizes[b] is the uncompressed byte length of block b: LZ4 raw frames
// (unlike zstd) do not self-describe their decompressed size, so we store it to
// size the decode buffer exactly — the analogue of ZSTD_getFrameContentSize.
// Both arrays are billed to boundaries_bytes.

#include "lz4_runner.h"
#include "decompress_then_scan.h"
#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include <lz4.h>
#include <lz4hc.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <fstream>
#include <random>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace onpair_bench {

namespace {

long long extract_int(const std::string& cfg, const std::string& key, long long def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    auto end = pos;
    while (end < cfg.size() && (cfg[end] == '-' || (cfg[end] >= '0' && cfg[end] <= '9'))) ++end;
    if (end == pos) return def;
    return std::stoll(cfg.substr(pos, end - pos));
}

std::string extract_str(const std::string& cfg, const std::string& key, const std::string& def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    if (pos >= cfg.size() || cfg[pos] != '"') return def;
    auto start = ++pos;
    while (pos < cfg.size() && cfg[pos] != '"') ++pos;
    if (pos > cfg.size()) return def;
    return cfg.substr(start, pos - start);
}

struct BlockPlan {
    std::vector<uint32_t> block_first_string;
};

BlockPlan plan_blocks(const Column& col, uint32_t block_budget_bytes) {
    BlockPlan plan;
    plan.block_first_string.push_back(0);
    const size_t N = col.num_rows();
    uint64_t cur = 4;
    uint32_t k_in_block = 0;
    for (uint32_t i = 0; i < N; ++i) {
        const auto sv = col.at(i);
        uint64_t add = 4 + sv.size();
        if (k_in_block > 0 && cur + add > block_budget_bytes) {
            plan.block_first_string.push_back(i);
            k_in_block = 0;
            cur        = 4;
        }
        cur += add;
        ++k_in_block;
    }
    plan.block_first_string.push_back(static_cast<uint32_t>(N));
    return plan;
}

// A5 sweep: block boundaries every `block_strings` rows. Mirrors
// plan_blocks' [0, ..., N] shape but partitions by string count, not bytes.
BlockPlan plan_blocks_by_strings(const Column& col, uint32_t block_strings) {
    BlockPlan plan;
    plan.block_first_string.push_back(0);
    const size_t N = col.num_rows();
    for (uint32_t i = block_strings; i < N; i += block_strings)
        plan.block_first_string.push_back(i);
    plan.block_first_string.push_back(static_cast<uint32_t>(N));
    return plan;
}

// Build one block's PAYLOAD (strings concatenated, no header) and append its k+1
// LOCAL row offsets (0..payload_size) to woff. Offsets travel uncompressed on
// the wire so the frame is payload-only and decode is single-pass. See zstd_runner.
void build_block_payload(const Column& col, uint32_t lo, uint32_t hi,
                         std::vector<uint8_t>& payload,
                         std::vector<uint32_t>& woff) {
    const uint32_t k = hi - lo;
    uint32_t cum = 0;
    woff.push_back(0);
    for (uint32_t i = 0; i < k; ++i) cum += static_cast<uint32_t>(col.at(lo + i).size());
    payload.clear();
    payload.resize(cum);
    cum = 0;
    for (uint32_t i = 0; i < k; ++i) {
        auto sv = col.at(lo + i);
        if (!sv.empty()) {
            std::memcpy(payload.data() + cum, sv.data(), sv.size());
            cum += static_cast<uint32_t>(sv.size());
        }
        woff.push_back(cum);
    }
}

// Index into Lz4Wire::within_offsets where block b's k_b+1 local offsets begin.
inline size_t woff_base(const Lz4Wire& w, uint32_t b) {
    return static_cast<size_t>(w.block_first_string[b]) + b;
}

Lz4Wire read_wire(const std::string& in_path) {
    std::ifstream f(in_path, std::ios::binary);
    if (!f) throw std::runtime_error("lz4: cannot open in_path " + in_path);
    Lz4Wire w;
    f.read(reinterpret_cast<char*>(&w.N), 4);
    f.read(reinterpret_cast<char*>(&w.B), 4);
    w.block_offsets.resize(w.B + 1);
    w.block_first_string.resize(w.B + 1);
    w.block_raw_sizes.resize(w.B);
    f.read(reinterpret_cast<char*>(w.block_offsets.data()), (w.B + 1) * 4);
    f.read(reinterpret_cast<char*>(w.block_first_string.data()), (w.B + 1) * 4);
    if (w.B > 0) f.read(reinterpret_cast<char*>(w.block_raw_sizes.data()), w.B * 4);
    w.within_offsets.resize(static_cast<size_t>(w.N) + w.B);  // sum_b (k_b+1)
    if (!w.within_offsets.empty())
        f.read(reinterpret_cast<char*>(w.within_offsets.data()),
               static_cast<std::streamsize>(w.within_offsets.size()) * 4);
    uint32_t total = w.block_offsets[w.B];
    w.compressed_blocks.resize(total);
    if (total > 0) f.read(reinterpret_cast<char*>(w.compressed_blocks.data()), total);
    if (!f) throw std::runtime_error("lz4: short read on wire " + in_path);
    return w;
}

// Decode one block's PAYLOAD into out (sized to block_raw_sizes[b], the exact
// decompressed payload size). Frames are payload-only, so no header stripping.
void decode_block(const Lz4Wire& w, uint32_t b, std::vector<uint8_t>& out) {
    const uint8_t* src = w.compressed_blocks.data() + w.block_offsets[b];
    int src_size       = static_cast<int>(w.block_offsets[b + 1] - w.block_offsets[b]);
    int raw_size       = static_cast<int>(w.block_raw_sizes[b]);
    out.resize(static_cast<size_t>(raw_size));
    int got = LZ4_decompress_safe(reinterpret_cast<const char*>(src),
                                  reinterpret_cast<char*>(out.data()),
                                  src_size, raw_size);
    if (got < 0 || got != raw_size)
        throw std::runtime_error("lz4: LZ4_decompress_safe failed or size mismatch");
}

// Pre-allocate the output column buffer OUTSIDE the timed region. block_raw_sizes
// (each == the block's decompressed payload size) sum to out.bytes; offsets come
// from the wire, so no per-block scratch is needed.
void lz4_prealloc(const Lz4Wire& w, Column& out) {
    out.offsets.assign(w.N + 1, 0);
    uint64_t total = 0;
    for (uint32_t b = 0; b < w.B; ++b) total += w.block_raw_sizes[b];
    out.bytes.resize(static_cast<size_t>(total));
}

// PAYLOAD-ONLY decode into the PRE-ALLOCATED out.bytes — the timed codec region.
// Each frame IS the block's payload, so LZ4_decompress_safe writes straight into the
// column at the running payload offset: ONE pass, no scratch, no second payload-
// concat copy (such a copy would cost ~27% that onpair/bpe/rlz don't pay).
void decode_all_payload(const Lz4Wire& w, Column& out) {
    uint64_t cum = 0;
    for (uint32_t b = 0; b < w.B; ++b) {
        const uint8_t* src = w.compressed_blocks.data() + w.block_offsets[b];
        int src_size       = static_cast<int>(w.block_offsets[b + 1] - w.block_offsets[b]);
        int raw_size       = static_cast<int>(w.block_raw_sizes[b]);
        int got = LZ4_decompress_safe(reinterpret_cast<const char*>(src),
                                      reinterpret_cast<char*>(out.bytes.data() + cum),
                                      src_size, raw_size);
        if (got < 0 || got != raw_size)
            throw std::runtime_error("lz4: LZ4_decompress_safe failed or size mismatch");
        cum += static_cast<uint64_t>(raw_size);
    }
}

// UNTIMED: rebuild the per-row string offsets straight from the wire's uncompressed
// within_offsets (no re-decompress). Round-trip verification output only; NOT measured.
void build_offsets(const Lz4Wire& w, Column& out) {
    uint32_t row = 0;
    uint64_t cum = 0;
    for (uint32_t b = 0; b < w.B; ++b) {
        const size_t base = woff_base(w, b);
        const uint32_t k  = w.block_first_string[b + 1] - w.block_first_string[b];
        for (uint32_t i = 0; i < k; ++i) {
            cum += w.within_offsets[base + i + 1] - w.within_offsets[base + i];
            out.offsets[++row] = static_cast<uint32_t>(cum);
        }
    }
    if (row != w.N) throw std::runtime_error("lz4: row count mismatch on decode");
}

// Convenience wrapper for non-timed callers (prefix_search / ra_latency warmup).
Column decode_all(const Lz4Wire& w) {
    Column out;
    lz4_prealloc(w, out);
    decode_all_payload(w, out);
    build_offsets(w, out);
    return out;
}

} // namespace

Lz4Config parse_lz4_config(const std::string& cfg) {
    Lz4Config c;
    std::string tier = extract_str(cfg, "tier", "fast");
    if (tier == "fast") {
        c.tier = Lz4Config::Tier::Fast;
    } else if (tier == "hc") {
        c.tier = Lz4Config::Tier::HC;
    } else {
        throw std::runtime_error("lz4: invalid tier '" + tier + "' (expected fast|hc)");
    }
    c.accel = static_cast<int>(extract_int(cfg, "accel", 1));
    if (c.accel < 1) c.accel = 1;
    c.level = static_cast<int>(extract_int(cfg, "level", 9));
    if (c.level < 1) c.level = 1;
    if (c.level > LZ4HC_CLEVEL_MAX) c.level = LZ4HC_CLEVEL_MAX;
    long long b = extract_int(cfg, "block_budget_bytes", 1 << 20);
    if (b < 1024) b = 1024;
    c.block_budget_bytes = static_cast<uint32_t>(b);
    long long bs = extract_int(cfg, "block_strings", 0);
    if (bs < 0) bs = 0;
    c.block_strings = static_cast<uint32_t>(bs);
    return c;
}

Lz4Runner::Lz4Runner() = default;
Lz4Runner::~Lz4Runner() = default;

OpResult Lz4Runner::compress(const std::string& in_path,
                             const std::string& out_path,
                             const std::string& cfg_json) {
    Column col      = read_column(in_path);
    Lz4Config cfg   = parse_lz4_config(cfg_json);

    BlockPlan plan  = cfg.block_strings > 0
                          ? plan_blocks_by_strings(col, cfg.block_strings)
                          : plan_blocks(col, cfg.block_budget_bytes);
    const uint32_t B = static_cast<uint32_t>(plan.block_first_string.size() - 1);
    const uint32_t N = static_cast<uint32_t>(col.num_rows());

    // Lazy-size the per-tier state buffers once. One-time alloc — not what
    // the paper measures.
    if (cfg.tier == Lz4Config::Tier::Fast) {
        size_t need = static_cast<size_t>(LZ4_sizeofState());
        if (state_.size() < need) state_.resize(need);
    } else {
        size_t need = static_cast<size_t>(LZ4_sizeofStateHC());
        if (state_hc_.size() < need) state_hc_.resize(need);
    }

    std::vector<uint32_t> block_offsets(B + 1, 0);
    std::vector<uint32_t> block_raw_sizes(B, 0);
    std::vector<uint32_t> within_offsets;          // concat of per-block local offsets
    within_offsets.reserve(static_cast<size_t>(N) + B);
    std::vector<uint8_t> compressed_blocks;
    std::vector<uint8_t> payload, frame;

    uint64_t tokens_bytes_acc      = 0;
    uint64_t boundaries_within_acc = 0;

    // TIMED: in-RAM block-compression loop only (file read above / wire write
    // below are excluded so T_c reflects the codec, not the disk).
    const auto t_comp0 = std::chrono::steady_clock::now();
    for (uint32_t b = 0; b < B; ++b) {
        uint32_t lo = plan.block_first_string[b];
        uint32_t hi = plan.block_first_string[b + 1];
        build_block_payload(col, lo, hi, payload, within_offsets);

        uint32_t k = hi - lo;
        boundaries_within_acc += static_cast<uint64_t>(k + 1) * 4;

        int src_size = static_cast<int>(payload.size());
        block_raw_sizes[b] = static_cast<uint32_t>(payload.size());
        int bound    = LZ4_compressBound(src_size);
        if (bound <= 0) {
            throw std::runtime_error("lz4: LZ4_compressBound returned non-positive");
        }
        frame.resize(static_cast<size_t>(bound));

        int csize_i = 0;
        if (cfg.tier == Lz4Config::Tier::Fast) {
            csize_i = LZ4_compress_fast_extState(
                state_.data(),
                reinterpret_cast<const char*>(payload.data()),
                reinterpret_cast<char*>(frame.data()),
                src_size, bound, cfg.accel);
        } else {
            csize_i = LZ4_compress_HC_extStateHC(
                state_hc_.data(),
                reinterpret_cast<const char*>(payload.data()),
                reinterpret_cast<char*>(frame.data()),
                src_size, bound, cfg.level);
        }
        if (csize_i <= 0) {
            throw std::runtime_error("lz4: compress_extState returned non-positive");
        }
        size_t csize = static_cast<size_t>(csize_i);

        block_offsets[b] = static_cast<uint32_t>(compressed_blocks.size());
        compressed_blocks.insert(compressed_blocks.end(), frame.begin(), frame.begin() + csize);

        // Payload-only frame: its compressed size IS the codec's token cost exactly.
        tokens_bytes_acc += csize;
    }
    block_offsets[B] = static_cast<uint32_t>(compressed_blocks.size());
    const uint64_t t_compress_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t_comp0).count());

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("lz4: cannot open out_path " + out_path);
    f.write(reinterpret_cast<const char*>(&N), 4);
    f.write(reinterpret_cast<const char*>(&B), 4);
    f.write(reinterpret_cast<const char*>(block_offsets.data()), (B + 1) * 4);
    f.write(reinterpret_cast<const char*>(plan.block_first_string.data()), (B + 1) * 4);
    if (B > 0) f.write(reinterpret_cast<const char*>(block_raw_sizes.data()), B * 4);
    if (!within_offsets.empty())
        f.write(reinterpret_cast<const char*>(within_offsets.data()),
                static_cast<std::streamsize>(within_offsets.size()) * 4);
    if (!compressed_blocks.empty()) {
        f.write(reinterpret_cast<const char*>(compressed_blocks.data()),
                static_cast<std::streamsize>(compressed_blocks.size()));
    }

    uint64_t boundaries_bytes = 8
                              + (uint64_t)(B + 1) * 4   // block_offsets
                              + (uint64_t)(B + 1) * 4   // block_first_string
                              + (uint64_t)B * 4         // block_raw_sizes
                              + boundaries_within_acc;
    uint64_t input_bytes  = col.bytes.size();
    uint64_t output_bytes = 8 + (uint64_t)(B + 1) * 8 + (uint64_t)B * 4
                          + (uint64_t)within_offsets.size() * 4
                          + compressed_blocks.size();

    OpResult r;
    r.metrics.emplace_back("dict_bytes",       0.0);
    r.metrics.emplace_back("tokens_bytes",     static_cast<double>(tokens_bytes_acc));
    r.metrics.emplace_back("boundaries_bytes", static_cast<double>(boundaries_bytes));
    r.metrics.emplace_back("input_bytes",      static_cast<double>(input_bytes));
    r.metrics.emplace_back("output_bytes",     static_cast<double>(output_bytes));
    r.metrics.emplace_back("num_rows",         static_cast<double>(N));
    r.metrics.emplace_back("n_blocks",         static_cast<double>(B));
    r.metrics.emplace_back("t_compress_ns",    static_cast<double>(t_compress_ns));
    return r;
}

OpResult Lz4Runner::bulk_decode(const std::string& in_path,
                                const std::string& out_path,
                                const std::string& /*cfg*/) {
    Lz4Wire w  = read_wire(in_path);                 // UNTIMED: file read
    Column out;
    lz4_prealloc(w, out);                            // UNTIMED: pre-allocate output buffer
    const auto t0 = std::chrono::steady_clock::now();
    decode_all_payload(w, out);                      // TIMED: decompress straight into out.bytes (1 pass)
    const uint64_t t_decode_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t0).count());
    build_offsets(w, out);                           // UNTIMED: per-row offsets from wire for round-trip
    write_column(out_path, out);                     // UNTIMED: file write
    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(out.bytes.size()));
    r.metrics.emplace_back("num_rows",     static_cast<double>(w.N));
    r.metrics.emplace_back("n_blocks",     static_cast<double>(w.B));
    r.metrics.emplace_back("t_decode_ns",  static_cast<double>(t_decode_ns));
    return r;
}

OpResult Lz4Runner::random_decode(const std::string& in_path,
                                  size_t idx,
                                  const std::string& out_path,
                                  const std::string& /*cfg*/) {
    Lz4Wire w = read_wire(in_path);
    if (idx >= w.N) {
        OpResult r;
        r.status = "crash";
        r.notes  = "idx out of range: " + std::to_string(idx);
        return r;
    }
    auto it = std::upper_bound(w.block_first_string.begin(),
                               w.block_first_string.end(),
                               static_cast<uint32_t>(idx));
    uint32_t b = static_cast<uint32_t>(std::distance(w.block_first_string.begin(), it) - 1);
    std::vector<uint8_t> raw;
    decode_block(w, b, raw);
    const size_t base = woff_base(w, b);
    uint32_t local = static_cast<uint32_t>(idx - w.block_first_string[b]);
    uint32_t lo    = w.within_offsets[base + local];
    uint32_t hi    = w.within_offsets[base + local + 1];
    uint32_t len   = hi - lo;

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("lz4: cannot open " + out_path);
    if (len > 0) f.write(reinterpret_cast<const char*>(raw.data() + lo), len);
    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(len));
    return r;
}

OpResult Lz4Runner::prefix_search(const std::string& in_path,
                                  const std::string& needle,
                                  const std::string& out_path,
                                  const std::string& /*cfg*/) {
    Lz4Wire w = read_wire(in_path);
    auto decode = [w = std::move(w)]() mutable {
        return decode_all(w);
    };
    size_t n_hits = decompress_then_scan(decode, needle, out_path);
    OpResult r;
    r.metrics.emplace_back("n_hits",   static_cast<double>(n_hits));
    r.metrics.emplace_back("num_rows", static_cast<double>(w.N));
    return r;
}

// Times n_samples random accesses; each pays the
// full cost of decoding the enclosing block (so L_ra grows with block size,
// as the A5 block-size sweep shows). No file I/O inside the timed
// loop. config_json: n_samples (default 1e6), seed (42), warmup (1 → one
// warm pre-pass decode_all before timing; 0 → cold-ish, no in-process warmup).
OpResult Lz4Runner::ra_latency(const std::string& in_path,
                               const std::string& /*out_path*/,
                               const std::string& cfg_json) {
    try {
        Lz4Wire w = read_wire(in_path);
        if (w.N == 0) {
            OpResult r;
            r.status = "crash";
            r.notes  = "lz4: empty column for ra_latency";
            return r;
        }
        uint64_t n_samples =
            static_cast<uint64_t>(extract_int(cfg_json, "n_samples", 1000000));
        if (n_samples == 0) n_samples = 1;
        uint64_t seed   = static_cast<uint64_t>(extract_int(cfg_json, "seed", 42));
        bool     warmup = extract_int(cfg_json, "warmup", 1) != 0;

        // Warm pre-pass (warm cache_state): decode every block once so the
        // page cache + allocator are hot before timing begins. Skipped for cold.
        if (warmup) { Column warm = decode_all(w); (void)warm; }

        // Query indices generated up-front (A6: distribution + layout from cfg)
        // so the distribution math never enters the timed region. lz4 is
        // block-based like zstd — `scramble` scatters the hot set across blocks.
        const QuerySpec qspec = parse_query_spec(cfg_json);
        const std::vector<uint64_t> indices =
            generate_query_indices(qspec, n_samples, w.N, seed);

        // Per-access rdtscp timing (ra_timer_tsc.h); identical primitive to zstd.
        // Each access decodes the enclosing block, locates the row, returns a
        // payload byte (anti-DCE via the accumulated, barrier'd value).
        std::vector<uint8_t> raw;
        RaLatencyResult m;
        std::string ra_reason;
        if (!ra_measure_shipping(indices,
            [&](uint64_t idx64) -> unsigned char {
                const uint32_t idx = static_cast<uint32_t>(idx64);
                auto it = std::upper_bound(w.block_first_string.begin(),
                                           w.block_first_string.end(), idx);
                uint32_t b = static_cast<uint32_t>(
                    std::distance(w.block_first_string.begin(), it) - 1);
                decode_block(w, b, raw);
                const size_t base = woff_base(w, b);
                uint32_t local = idx - w.block_first_string[b];
                uint32_t lo    = w.within_offsets[base + local];
                uint32_t hi    = w.within_offsets[base + local + 1];
                return (hi > lo) ? raw[lo] : 0;
            }, m, ra_reason)) {
            return OpResult{.status = "refused-env", .notes = "lz4: " + ra_reason};
        }
        if (m.stats.n > 0 && m.stats.p50 < 1.0) {
            return OpResult{.status = "crash",
                .notes = "lz4: implausible sub-ns p50 (latency primitive failure)"};
        }
        const LatencyStats& st = m.stats;
        OpResult r;
        r.metrics.emplace_back("l_ra_p50_ns",  st.p50);
        r.metrics.emplace_back("l_ra_p95_ns",  st.p95);
        r.metrics.emplace_back("l_ra_p99_ns",  st.p99);
        r.metrics.emplace_back("l_ra_mean_ns", st.mean);
        r.metrics.emplace_back("l_ra_min_ns",  st.min);
        r.metrics.emplace_back("l_ra_max_ns",  st.max);
        r.metrics.emplace_back("n_samples",    static_cast<double>(st.n));
        r.metrics.emplace_back("batch_k",      static_cast<double>(m.batch_k));
        r.metrics.emplace_back("clock_resolution_ns", m.clock_resolution_ns);
        r.metrics.emplace_back("num_rows",     static_cast<double>(w.N));
        r.metrics.emplace_back("n_blocks",     static_cast<double>(w.B));
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "lz4: bad_alloc ra_latency"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("lz4: ") + e.what()};
    }
}

} // namespace onpair_bench
