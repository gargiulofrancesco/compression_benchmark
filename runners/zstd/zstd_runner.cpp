// ZstdRunner implementation. See zstd_runner.h for class shape.
// Wire format on disk:
//   [N u32][B u32][block_offsets[B+1] u32][block_first_string[B+1] u32]
//   [within_offsets[N+B] u32][compressed_blocks[total] u8]
// Each block's uncompressed payload is its strings concatenated; the k+1 local
// row offsets of every block are stored uncompressed in within_offsets and
// billed to boundaries_bytes.

#include "zstd_runner.h"
#include "decompress_then_scan.h"
#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include <zstd.h>

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

// Build one block's PAYLOAD (the strings concatenated, no header) and append its
// k+1 LOCAL row offsets (0..payload_size) to woff. The row offsets travel
// uncompressed on the wire (not inside the compressed frame), so the frame is
// payload-only and decode is single-pass.
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

// Index into ZstdWire::within_offsets where block b's k_b+1 local offsets begin.
inline size_t woff_base(const ZstdWire& w, uint32_t b) {
    return static_cast<size_t>(w.block_first_string[b]) + b;
}

// Decompressed payload size of block b (== last local offset of the block).
inline uint32_t block_payload_size(const ZstdWire& w, uint32_t b) {
    const uint32_t k = w.block_first_string[b + 1] - w.block_first_string[b];
    return w.within_offsets[woff_base(w, b) + k];
}

ZstdWire read_wire(const std::string& in_path) {
    std::ifstream f(in_path, std::ios::binary);
    if (!f) throw std::runtime_error("zstd: cannot open in_path " + in_path);
    ZstdWire w;
    f.read(reinterpret_cast<char*>(&w.N), 4);
    f.read(reinterpret_cast<char*>(&w.B), 4);
    w.block_offsets.resize(w.B + 1);
    w.block_first_string.resize(w.B + 1);
    f.read(reinterpret_cast<char*>(w.block_offsets.data()), (w.B + 1) * 4);
    f.read(reinterpret_cast<char*>(w.block_first_string.data()), (w.B + 1) * 4);
    w.within_offsets.resize(static_cast<size_t>(w.N) + w.B);  // sum_b (k_b+1)
    if (!w.within_offsets.empty())
        f.read(reinterpret_cast<char*>(w.within_offsets.data()),
               static_cast<std::streamsize>(w.within_offsets.size()) * 4);
    uint32_t total = w.block_offsets[w.B];
    w.compressed_blocks.resize(total);
    if (total > 0) f.read(reinterpret_cast<char*>(w.compressed_blocks.data()), total);
    if (!f) throw std::runtime_error("zstd: short read on wire " + in_path);
    return w;
}

// Self-sizing single-block PAYLOAD decode for the non-bulk callers (random_decode,
// ra_latency): frame content size == block payload size (no header in the frame).
void decode_block(ZSTD_DCtx* dctx, const ZstdWire& w, uint32_t b,
                  std::vector<uint8_t>& out) {
    const uint8_t* src = w.compressed_blocks.data() + w.block_offsets[b];
    size_t src_size    = w.block_offsets[b + 1] - w.block_offsets[b];
    out.resize(block_payload_size(w, b));
    ZSTD_DCtx_reset(dctx, ZSTD_reset_session_and_parameters);
    size_t got = ZSTD_decompressDCtx(dctx, out.data(), out.size(), src, src_size);
    if (ZSTD_isError(got))
        throw std::runtime_error(std::string("zstd: decompress error ") + ZSTD_getErrorName(got));
    if (got != out.size()) throw std::runtime_error("zstd: short decode");
}

// Pre-allocate the output column buffer OUTSIDE the timed region. The total decoded
// payload size is the sum of the per-block payload sizes (last local offset of each
// block), known from the wire without touching the compressed bytes.
void zstd_prealloc(const ZstdWire& w, Column& out) {
    out.offsets.assign(w.N + 1, 0);
    uint64_t total = 0;
    for (uint32_t b = 0; b < w.B; ++b) total += block_payload_size(w, b);
    out.bytes.resize(static_cast<size_t>(total));
}

// PAYLOAD-ONLY decode into the PRE-ALLOCATED out.bytes — the timed codec region.
// Each compressed frame IS the block's payload, so it decompresses straight into
// the column at the running payload offset: ONE pass, no scratch, no second
// payload-concat copy (such a copy would cost ~14-27% that onpair/bpe/rlz
// don't pay). No per-row offset bookkeeping here — offsets are rebuilt UNTIMED by
// build_offsets() from the wire for the round-trip output only.
void decode_all_payload(ZSTD_DCtx* dctx, const ZstdWire& w, Column& out) {
    uint64_t cum = 0;
    for (uint32_t b = 0; b < w.B; ++b) {
        const uint8_t* src = w.compressed_blocks.data() + w.block_offsets[b];
        size_t src_size    = w.block_offsets[b + 1] - w.block_offsets[b];
        ZSTD_DCtx_reset(dctx, ZSTD_reset_session_and_parameters);
        size_t cap = out.bytes.size() - cum;
        size_t got = ZSTD_decompressDCtx(dctx, out.bytes.data() + cum, cap, src, src_size);
        if (ZSTD_isError(got))
            throw std::runtime_error(std::string("zstd: decompress error ") + ZSTD_getErrorName(got));
        cum += got;
    }
}

// UNTIMED: rebuild the per-row string offsets straight from the wire's uncompressed
// within_offsets (no re-decompress). Round-trip verification Column only; not measured.
void build_offsets(const ZstdWire& w, Column& out) {
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
    if (row != w.N) throw std::runtime_error("zstd: row count mismatch on decode");
}

// Convenience wrapper for non-timed callers (prefix_search / ra_latency warmup).
Column decode_all(ZSTD_DCtx* dctx, const ZstdWire& w) {
    Column out;
    zstd_prealloc(w, out);
    decode_all_payload(dctx, w, out);
    build_offsets(w, out);
    return out;
}

} // namespace

ZstdConfig parse_zstd_config(const std::string& cfg) {
    ZstdConfig c;
    c.level = static_cast<int>(extract_int(cfg, "level", 3));
    long long b = extract_int(cfg, "block_budget_bytes", 1 << 20);
    if (b < 1024) b = 1024;
    c.block_budget_bytes = static_cast<uint32_t>(b);
    long long bs = extract_int(cfg, "block_strings", 0);
    if (bs < 0) bs = 0;
    c.block_strings = static_cast<uint32_t>(bs);
    return c;
}

ZstdRunner::ZstdRunner()
    : cctx_(ZSTD_createCCtx()), dctx_(ZSTD_createDCtx()) {
    if (!cctx_ || !dctx_) throw std::runtime_error("zstd: failed to create context");
}

ZstdRunner::~ZstdRunner() {
    if (cctx_) ZSTD_freeCCtx(cctx_);
    if (dctx_) ZSTD_freeDCtx(dctx_);
}

OpResult ZstdRunner::compress(const std::string& in_path,
                              const std::string& out_path,
                              const std::string& cfg_json) {
    Column col   = read_column(in_path);
    ZstdConfig cfg = parse_zstd_config(cfg_json);

    BlockPlan plan = cfg.block_strings > 0
                         ? plan_blocks_by_strings(col, cfg.block_strings)
                         : plan_blocks(col, cfg.block_budget_bytes);
    const uint32_t B = static_cast<uint32_t>(plan.block_first_string.size() - 1);
    const uint32_t N = static_cast<uint32_t>(col.num_rows());

    std::vector<uint32_t> block_offsets(B + 1, 0);
    std::vector<uint32_t> within_offsets;          // concat of per-block local offsets
    within_offsets.reserve(static_cast<size_t>(N) + B);
    std::vector<uint8_t> compressed_blocks;
    std::vector<uint8_t> payload, frame;

    uint64_t tokens_bytes_acc      = 0;
    uint64_t boundaries_within_acc = 0;

    // TIMED: the in-RAM block-compression loop only (input column already read
    // from disk above; wire write happens below). Excludes file I/O so T_c
    // reflects the codec, not the disk.
    const auto t_comp0 = std::chrono::steady_clock::now();
    for (uint32_t b = 0; b < B; ++b) {
        uint32_t lo = plan.block_first_string[b];
        uint32_t hi = plan.block_first_string[b + 1];
        build_block_payload(col, lo, hi, payload, within_offsets);

        uint32_t k = hi - lo;
        boundaries_within_acc += static_cast<uint64_t>(k + 1) * 4;

        ZSTD_CCtx_reset(cctx_, ZSTD_reset_session_and_parameters);
        ZSTD_CCtx_setParameter(cctx_, ZSTD_c_compressionLevel, cfg.level);
        size_t bound = ZSTD_compressBound(payload.size());
        frame.resize(bound);
        size_t csize = ZSTD_compress2(cctx_, frame.data(), frame.size(),
                                      payload.data(), payload.size());
        if (ZSTD_isError(csize)) {
            throw std::runtime_error(std::string("zstd: compress error ") + ZSTD_getErrorName(csize));
        }

        block_offsets[b] = static_cast<uint32_t>(compressed_blocks.size());
        compressed_blocks.insert(compressed_blocks.end(), frame.begin(), frame.begin() + csize);

        // The frame is payload-only, so its compressed size IS the codec's
        // token cost exactly.
        tokens_bytes_acc += csize;
    }
    block_offsets[B] = static_cast<uint32_t>(compressed_blocks.size());
    const uint64_t t_compress_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t_comp0).count());

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("zstd: cannot open out_path " + out_path);
    f.write(reinterpret_cast<const char*>(&N), 4);
    f.write(reinterpret_cast<const char*>(&B), 4);
    f.write(reinterpret_cast<const char*>(block_offsets.data()), (B + 1) * 4);
    f.write(reinterpret_cast<const char*>(plan.block_first_string.data()), (B + 1) * 4);
    if (!within_offsets.empty())
        f.write(reinterpret_cast<const char*>(within_offsets.data()),
                static_cast<std::streamsize>(within_offsets.size()) * 4);
    if (!compressed_blocks.empty()) {
        f.write(reinterpret_cast<const char*>(compressed_blocks.data()),
                static_cast<std::streamsize>(compressed_blocks.size()));
    }

    uint64_t boundaries_bytes = 8
                              + (uint64_t)(B + 1) * 4
                              + (uint64_t)(B + 1) * 4
                              + boundaries_within_acc;
    uint64_t input_bytes  = col.bytes.size();
    uint64_t output_bytes = 8 + (uint64_t)(B + 1) * 8
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

OpResult ZstdRunner::bulk_decode(const std::string& in_path,
                                 const std::string& out_path,
                                 const std::string& /*cfg*/) {
    ZstdWire w = read_wire(in_path);                 // UNTIMED: file read
    Column out;
    zstd_prealloc(w, out);                           // UNTIMED: pre-allocate output buffer
    const auto t0 = std::chrono::steady_clock::now();
    decode_all_payload(dctx_, w, out);               // TIMED: decompress straight into out.bytes (1 pass)
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

OpResult ZstdRunner::random_decode(const std::string& in_path,
                                   size_t idx,
                                   const std::string& out_path,
                                   const std::string& /*cfg*/) {
    ZstdWire w = read_wire(in_path);
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
    decode_block(dctx_, w, b, raw);
    const size_t base = woff_base(w, b);
    uint32_t local = static_cast<uint32_t>(idx - w.block_first_string[b]);
    uint32_t lo    = w.within_offsets[base + local];
    uint32_t hi    = w.within_offsets[base + local + 1];
    uint32_t len   = hi - lo;

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("zstd: cannot open " + out_path);
    if (len > 0) f.write(reinterpret_cast<const char*>(raw.data() + lo), len);
    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(len));
    return r;
}

OpResult ZstdRunner::prefix_search(const std::string& in_path,
                                   const std::string& needle,
                                   const std::string& out_path,
                                   const std::string& /*cfg*/) {
    ZstdWire w = read_wire(in_path);
    ZSTD_DCtx* dctx = dctx_;
    auto decode = [dctx, w = std::move(w)]() mutable {
        return decode_all(dctx, w);
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
OpResult ZstdRunner::ra_latency(const std::string& in_path,
                                const std::string& /*out_path*/,
                                const std::string& cfg_json) {
    try {
        ZstdWire w = read_wire(in_path);
        if (w.N == 0) {
            OpResult r;
            r.status = "crash";
            r.notes  = "zstd: empty column for ra_latency";
            return r;
        }
        uint64_t n_samples =
            static_cast<uint64_t>(extract_int(cfg_json, "n_samples", 1000000));
        if (n_samples == 0) n_samples = 1;
        uint64_t seed   = static_cast<uint64_t>(extract_int(cfg_json, "seed", 42));
        bool     warmup = extract_int(cfg_json, "warmup", 1) != 0;

        // Warm pre-pass (warm cache_state): decode every block once so the
        // page cache + allocator are hot before timing begins. Skipped for cold.
        if (warmup) { Column warm = decode_all(dctx_, w); (void)warm; }

        // Query indices generated up-front (A6: distribution + layout from cfg)
        // so the distribution math never enters the timed region. zstd is
        // block-based: each timed access decodes the enclosing block; under
        // the `scramble` layout the hot set is scattered across blocks (the
        // layout A6 pairs with `identity`).
        const QuerySpec qspec = parse_query_spec(cfg_json);
        const std::vector<uint64_t> indices =
            generate_query_indices(qspec, n_samples, w.N, seed);

        // Per-access rdtscp timing (ra_timer_tsc.h). Each access decodes the
        // enclosing block, locates the row, and returns a payload byte; the
        // accumulated, barrier'd value forces the block decode (anti-DCE).
        // Identical primitive across every baseline.
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
                decode_block(dctx_, w, b, raw);
                const size_t base = woff_base(w, b);
                uint32_t local = idx - w.block_first_string[b];
                uint32_t lo    = w.within_offsets[base + local];
                uint32_t hi    = w.within_offsets[base + local + 1];
                return (hi > lo) ? raw[lo] : 0;
            }, m, ra_reason)) {
            return OpResult{.status = "refused-env", .notes = "zstd: " + ra_reason};
        }
        if (m.stats.n > 0 && m.stats.p50 < 1.0) {
            return OpResult{.status = "crash",
                .notes = "zstd: implausible sub-ns p50 (latency primitive failure)"};
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
        return OpResult{.status = "oom", .notes = "zstd: bad_alloc ra_latency"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("zstd: ") + e.what()};
    }
}

} // namespace onpair_bench
