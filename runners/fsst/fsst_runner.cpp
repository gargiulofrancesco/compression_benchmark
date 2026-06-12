// FsstRunner implementation. See fsst_runner.h for class shape.
// Wire format on disk (native column-aware layout per FSST card §"Framing"):
//   [H u32][header[H]][N u32][compressed_offsets[N+1] u32]
//   [decoded_offsets[N+1] u32][codewords[Σ Lc_i] u8]
// Bit-tight identity: dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes.
// decoded_offsets carry the per-row DECODED boundaries on the wire (UNCOMPRESSED,
// billed as boundaries → ratio-neutral) so bulk_decode can decode the whole column
// in ONE fsst_decompress call (payload only, no offsets in the timed region) and
// rebuild the output column's offsets UNTIMED — the same payload-only contract as
// zstd/lz4/onpair. compressed_offsets stay for the per-row random-access ops.

#include "fsst_runner.h"
#include "decompress_then_scan.h"
#include "scan_prefix.h"

#include "fsst.h"  // public FSST C API; fsst_decompress is inline in this header
#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

// Vendored FSST's runtime AVX-512 gate (defined in third_party/fsst/fsst_avx512.cpp,
// namespace libfsst). Forward-declared here so the runner can report which path was
// active for a given compress op (see the fsst_avx512 metric below). The public
// fsst.h does not expose it.
namespace libfsst { bool fsst_hasAVX512(); }

namespace onpair_bench {

namespace {

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

long long extract_int(const std::string& cfg, const std::string& key, long long def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    return std::strtoll(cfg.c_str() + pos, nullptr, 10);
}

bool extract_bool(const std::string& cfg, const std::string& key, bool def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    if (cfg.compare(pos, 4, "true") == 0) return true;
    if (cfg.compare(pos, 5, "false") == 0) return false;
    return def;
}

FsstWire read_wire(const std::string& in_path) {
    std::ifstream f(in_path, std::ios::binary);
    if (!f) throw std::runtime_error("fsst: cannot open in_path " + in_path);
    FsstWire w;
    f.read(reinterpret_cast<char*>(&w.H), 4);
    w.header.resize(w.H);
    if (w.H > 0) f.read(reinterpret_cast<char*>(w.header.data()), w.H);
    f.read(reinterpret_cast<char*>(&w.N), 4);
    w.compressed_offsets.resize(w.N + 1);
    f.read(reinterpret_cast<char*>(w.compressed_offsets.data()), (w.N + 1) * 4);
    w.decoded_offsets.resize(w.N + 1);
    f.read(reinterpret_cast<char*>(w.decoded_offsets.data()), (w.N + 1) * 4);
    uint32_t total = w.compressed_offsets[w.N];
    w.codewords.resize(total);
    if (total > 0) f.read(reinterpret_cast<char*>(w.codewords.data()), total);
    if (!f) throw std::runtime_error("fsst: short read on wire " + in_path);
    return w;
}

// Pre-build the output Column shell OUTSIDE the timed region: per-row boundaries
// come straight from the wire (decoded_offsets), and out.bytes is sized to the
// EXACT decoded length plus a small tail slack. The slack matters: fsst_decompress
// only takes its 8-byte-unaligned-store fast path while `posOut + 32 <= size`, so a
// dst sized exactly to the payload would drop the WHOLE column into the slow
// byte-checked tail loop. 64 bytes keeps the fast path hot and leaves room for the
// final 8-byte store; out.bytes is shrunk back to the true length after decode.
inline size_t fsst_decoded_total(const FsstWire& w) {
    return static_cast<size_t>(w.decoded_offsets[w.N]);
}

inline void fsst_prealloc(const FsstWire& w, Column& out) {
    out.offsets = w.decoded_offsets;                 // per-row boundaries, UNTIMED, from wire
    out.bytes.resize(fsst_decoded_total(w) + 64);    // exact size + fast-path tail slack
}

// Reconstruct the full Column from a parsed wire. ONE fsst_decompress over the whole
// concatenated codeword blob: FSST has no cross-row decode state (each codeword
// indexes the symbol table independently; 0xFF = escape), so stream-decoding the
// entire blob yields exactly the concatenation of the per-row decodes — the payload
// byte sequence, with NO offsets touched in the timed region. The per-row output
// boundaries are already set by fsst_prealloc from the wire (UNTIMED), exactly like
// zstd/lz4/onpair. This replaced an N-call per-row decode whose per-call overhead
// capped throughput at ~1.9 GiB/s.
void decode_all_into(const FsstWire& w, Column& out) {
    fsst_decoder_t decoder;
    unsigned int consumed = fsst_import(&decoder,
        const_cast<unsigned char*>(w.header.data()));
    if (consumed == 0) throw std::runtime_error("fsst: fsst_import returned 0 (corrupt header)");

    const size_t total_codewords = w.compressed_offsets[w.N];
    const size_t decoded_total   = fsst_decoded_total(w);
    size_t got = fsst_decompress(&decoder, total_codewords,
                                 const_cast<unsigned char*>(w.codewords.data()),
                                 out.bytes.size(), out.bytes.data());
    if (got != decoded_total) {
        throw std::runtime_error("fsst: bulk decode size mismatch (got "
            + std::to_string(got) + " expected " + std::to_string(decoded_total) + ")");
    }
    out.bytes.resize(decoded_total);   // shrink off the slack: O(1), capacity unchanged
}

// Convenience wrapper for non-timed callers (prefix_search / ra_latency warmup):
// allocates + decodes in one call.
Column decode_all(const FsstWire& w) {
    Column out;
    fsst_prealloc(w, out);
    decode_all_into(w, out);
    return out;
}

} // namespace

FsstConfig parse_fsst_config(const std::string& cfg) {
    FsstConfig c;
    std::string v = extract_str(cfg, "variant", "fsst");
    if (v != "fsst") {
        throw std::runtime_error("fsst: invalid variant '" + v + "' (only 'fsst' supported at headline)");
    }
    c.variant = v;
    std::string t = extract_str(cfg, "training", "full_column");
    if (t != "full_column") {
        throw std::runtime_error("fsst: invalid training '" + t + "' (only 'full_column' supported at headline)");
    }
    c.training = t;
    c.simd = extract_bool(cfg, "simd", true);   // false → force scalar (fsst_scalar)
    return c;
}

FsstRunner::FsstRunner() = default;
FsstRunner::~FsstRunner() = default;

OpResult FsstRunner::compress(const std::string& in_path,
                              const std::string& out_path,
                              const std::string& cfg_json) {
    Column col = read_column(in_path);
    const FsstConfig cfg = parse_fsst_config(cfg_json);

    // fsst_scalar (simd=false) forces the scalar compression path even on AVX-512
    // hardware, so the with/without-AVX-512 FSST points are measured on the SAME
    // machine. The vendored fsst_hasAVX512() honours FSST_DISABLE_AVX512 (see
    // third_party/fsst/PROVENANCE.txt local_patches). RAII guard so a process
    // serving multiple ops never leaks the env across cells — including on the
    // partial-compress error path below.
    struct ScalarEnvGuard {
        bool on;
        explicit ScalarEnvGuard(bool o) : on(o) { if (on) ::setenv("FSST_DISABLE_AVX512", "1", 1); }
        ~ScalarEnvGuard() { if (on) ::unsetenv("FSST_DISABLE_AVX512"); }
    } scalar_guard(!cfg.simd);
    // AVX-512 accelerates FSST compression ONLY (the decoder is scalar regardless).
    // Active path = hardware advertises AVX-512 AND it is not forced off here;
    // emitted per-row so the headline can label the two FSST operating points.
    const bool avx512_active = libfsst::fsst_hasAVX512();

    const size_t N = col.num_rows();

    // FSST API wants per-string size + pointer arrays. No copy.
    std::vector<size_t> lenIn(N);
    std::vector<const unsigned char*> strIn(N);
    size_t input_bytes_acc = 0;
    for (size_t i = 0; i < N; ++i) {
        auto sv = col.at(i);
        lenIn[i] = sv.size();
        strIn[i] = reinterpret_cast<const unsigned char*>(sv.data());
        input_bytes_acc += sv.size();
    }

    // UNTIMED: pre-allocate every scratch + output buffer BEFORE the timer. Buffer
    // allocation/zero-fill is not codec work — and the reference FSST benchmark
    // pre-sizes its output in the constructor, so including it here would
    // unfairly inflate T_c (the output_buf is ~2x input — the dominant cost for a
    // large column). Mirrors the decode-timer contract (exclude allocation).
    std::vector<unsigned char> header_buf(FSST_MAXHEADER, 0);
    // Worst-case compressed-bulk capacity per the public docs comment on fsst_compress:
    //   "at least 'conservative space' (7+2*inputlength) for the first string"
    // → 7 + 2*Σ lenIn + per-row tail padding; the documented upper bound.
    size_t out_capacity = 7 + 2 * input_bytes_acc + 7 * N;
    if (out_capacity < 64) out_capacity = 64;
    std::vector<unsigned char> output_buf(out_capacity);
    std::vector<size_t> lenOut(N, 0);
    std::vector<unsigned char*> strOut(N, nullptr);
    std::vector<uint32_t> compressed_offsets(N + 1, 0);

    // TIMED: FSST symbol-table training + bulk encode + offset summation — the
    // codec work ONLY (no allocation). Input read + wire write excluded.
    const auto t_comp0 = std::chrono::steady_clock::now();

    // fsst_create's zeroTerminated=0 means we pass explicit lengths.
    fsst_encoder_t* enc = fsst_create(N, lenIn.data(), strIn.data(), /*zeroTerminated=*/0);
    if (!enc) throw std::runtime_error("fsst: fsst_create returned null");
    unsigned int H = fsst_export(enc, header_buf.data());
    if (H == 0 || H > FSST_MAXHEADER) {
        fsst_destroy(enc);
        throw std::runtime_error("fsst: fsst_export returned bad header size " + std::to_string(H));
    }
    size_t n_done = fsst_compress(enc,
                                  N,
                                  lenIn.data(),
                                  strIn.data(),
                                  out_capacity,
                                  output_buf.data(),
                                  lenOut.data(),
                                  strOut.data());
    if (n_done != N) {
        fsst_destroy(enc);
        OpResult r;
        r.status = "error";
        r.notes  = "fsst_partial_compress: " + std::to_string(n_done) + "/" + std::to_string(N);
        return r;
    }
    fsst_destroy(enc);

    // Build Arrow offsets from lenOut[] (into the pre-allocated vector). Sum gives
    // codewords total.
    uint64_t cum = 0;
    for (size_t i = 0; i < N; ++i) {
        cum += lenOut[i];
        if (cum > 0xFFFFFFFFu) {
            throw std::runtime_error("fsst: codeword cumulative > 4 GiB; uint32 offsets overflow");
        }
        compressed_offsets[i + 1] = static_cast<uint32_t>(cum);
    }
    const uint64_t t_compress_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t_comp0).count());
    size_t tokens_bytes = static_cast<size_t>(compressed_offsets[N]);

    // strOut[0] points to where the first row's codewords start in output_buf.
    // FSST writes codewords contiguously, so we can copy [strOut[0], strOut[0] + tokens_bytes).
    const unsigned char* codewords_src = (N > 0) ? strOut[0] : output_buf.data();

    // Write wire: [H u32][header[H]][N u32][compressed_offsets[N+1]][codewords]
    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("fsst: cannot open out_path " + out_path);
    uint32_t H_u32 = static_cast<uint32_t>(H);
    uint32_t N_u32 = static_cast<uint32_t>(N);
    f.write(reinterpret_cast<const char*>(&H_u32), 4);
    if (H > 0) f.write(reinterpret_cast<const char*>(header_buf.data()), H);
    f.write(reinterpret_cast<const char*>(&N_u32), 4);
    f.write(reinterpret_cast<const char*>(compressed_offsets.data()), (N + 1) * 4);
    // decoded_offsets = the original per-row string boundaries (col.offsets). Stored
    // uncompressed so bulk_decode rebuilds the output column UNTIMED + sizes the
    // single-call decode buffer exactly. col.offsets is already size N+1 (Arrow).
    f.write(reinterpret_cast<const char*>(col.offsets.data()),
            static_cast<std::streamsize>((N + 1) * 4));
    if (tokens_bytes > 0) {
        f.write(reinterpret_cast<const char*>(codewords_src),
                static_cast<std::streamsize>(tokens_bytes));
    }

    uint64_t dict_bytes       = H;
    // Two u32 length headers (H,N) + two N+1 offset arrays (compressed + decoded).
    uint64_t boundaries_bytes = 8 + 2 * static_cast<uint64_t>(N + 1) * 4;
    uint64_t output_bytes     = dict_bytes + tokens_bytes + boundaries_bytes;  // bit-tight identity

    OpResult r;
    r.metrics.emplace_back("dict_bytes",        static_cast<double>(dict_bytes));
    r.metrics.emplace_back("tokens_bytes",      static_cast<double>(tokens_bytes));
    r.metrics.emplace_back("boundaries_bytes",  static_cast<double>(boundaries_bytes));
    r.metrics.emplace_back("input_bytes",       static_cast<double>(input_bytes_acc));
    r.metrics.emplace_back("output_bytes",      static_cast<double>(output_bytes));
    r.metrics.emplace_back("num_rows",          static_cast<double>(N));
    r.metrics.emplace_back("fsst_header_bytes", static_cast<double>(dict_bytes));
    r.metrics.emplace_back("t_compress_ns",     static_cast<double>(t_compress_ns));
    // 1.0 = AVX-512 compression path active this op, 0.0 = scalar (forced via
    // simd=false, or hardware lacks AVX-512). Labels the with/without-AVX-512 points.
    r.metrics.emplace_back("fsst_avx512",       avx512_active ? 1.0 : 0.0);
    return r;
}

OpResult FsstRunner::bulk_decode(const std::string& in_path,
                                 const std::string& out_path,
                                 const std::string& /*cfg*/) {
    FsstWire w  = read_wire(in_path);                // UNTIMED: file read
    Column out;
    fsst_prealloc(w, out);                           // UNTIMED: pre-allocate output
    const auto t0 = std::chrono::steady_clock::now();
    decode_all_into(w, out);                         // TIMED: pure decompress only
    const uint64_t t_decode_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t0).count());
    write_column(out_path, out);                     // UNTIMED: file write
    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(out.bytes.size()));
    r.metrics.emplace_back("num_rows",     static_cast<double>(w.N));
    r.metrics.emplace_back("t_decode_ns",  static_cast<double>(t_decode_ns));
    return r;
}

OpResult FsstRunner::random_decode(const std::string& in_path,
                                   size_t idx,
                                   const std::string& out_path,
                                   const std::string& /*cfg*/) {
    FsstWire w = read_wire(in_path);
    if (idx >= w.N) {
        OpResult r;
        r.status = "crash";
        r.notes  = "idx out of range: " + std::to_string(idx);
        return r;
    }
    fsst_decoder_t decoder;
    unsigned int consumed = fsst_import(&decoder,
        const_cast<unsigned char*>(w.header.data()));
    if (consumed == 0) throw std::runtime_error("fsst: fsst_import returned 0 (corrupt header)");

    uint32_t lo = w.compressed_offsets[idx];
    uint32_t hi = w.compressed_offsets[idx + 1];
    size_t   Lc = hi - lo;
    size_t   cap = 8 * Lc + 8;
    std::vector<unsigned char> buf(cap);
    size_t got = fsst_decompress(&decoder, Lc,
        const_cast<unsigned char*>(w.codewords.data()) + lo,
        cap, buf.data());
    if (got > cap) throw std::runtime_error("fsst: random_decode exceeded buffer");

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("fsst: cannot open " + out_path);
    if (got > 0) f.write(reinterpret_cast<const char*>(buf.data()),
                         static_cast<std::streamsize>(got));
    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(got));
    return r;
}

// step 7 (A6). Times n_samples random-access decodes drawn from a query
// distribution + layout (runners/core/query_sampler.h). FSST decodes each row
// independently against the shared symbol table — O(|s_i|), position-independent
// — so its L_ra is flat under skew/scramble, the per-string-class evidence that
// OnPair's skew-insensitivity is a property of the per-string family, not a
// special OnPair claim. The decoder is imported once and the decode buffer sized
// once (widest row), both outside the timed loop, so timing isolates the decode.
OpResult FsstRunner::ra_latency(const std::string& in_path,
                                const std::string& /*out_path*/,
                                const std::string& cfg_json) {
    try {
        FsstWire w = read_wire(in_path);
        if (w.N == 0) {
            return OpResult{.status = "crash",
                            .notes  = "fsst: empty column for ra_latency"};
        }
        uint64_t n_samples =
            static_cast<uint64_t>(extract_int(cfg_json, "n_samples", 1000000));
        if (n_samples == 0) n_samples = 1;
        uint64_t seed   = static_cast<uint64_t>(extract_int(cfg_json, "seed", 42));
        bool     warmup = extract_int(cfg_json, "warmup", 1) != 0;

        fsst_decoder_t decoder;
        unsigned int consumed = fsst_import(&decoder,
            const_cast<unsigned char*>(w.header.data()));
        if (consumed == 0)
            throw std::runtime_error("fsst: fsst_import returned 0 (corrupt header)");

        // Size the decode buffer once to the widest row's worst-case expansion
        // (each codeword ≤ 8 bytes → 8·Lc + tail slack).
        size_t maxLc = 0;
        for (uint32_t i = 0; i < w.N; ++i)
            maxLc = std::max(maxLc,
                static_cast<size_t>(w.compressed_offsets[i + 1] - w.compressed_offsets[i]));
        const size_t cap = 8 * maxLc + 8;
        std::vector<unsigned char> buf(cap);

        // Warm pre-pass (warm cache_state): decode the whole column once.
        if (warmup) { Column warm = decode_all(w); (void)warm; }

        const QuerySpec qspec = parse_query_spec(cfg_json);
        const std::vector<uint64_t> indices =
            generate_query_indices(qspec, n_samples, w.N, seed);

        // Batched timing (latency_stats.h); identical primitive to OnPair. The
        // access decodes one row and returns its LAST byte, so the accumulated,
        // barrier'd value forces the full per-string decode (anti-DCE) and K is
        // auto-tuned to the clock.
        RaLatencyResult m;
        std::string ra_reason;
        if (!ra_measure_shipping(indices,
            [&](uint64_t idx64) -> unsigned char {
                const uint32_t idx = static_cast<uint32_t>(idx64);
                const uint32_t lo = w.compressed_offsets[idx];
                const uint32_t hi = w.compressed_offsets[idx + 1];
                const size_t   Lc = hi - lo;
                size_t got = fsst_decompress(&decoder, Lc,
                    const_cast<unsigned char*>(w.codewords.data()) + lo,
                    cap, buf.data());
                return got ? buf[got - 1] : 0;
            }, m, ra_reason)) {
            return OpResult{.status = "refused-env", .notes = "fsst: " + ra_reason};
        }
        if (m.stats.n > 0 && m.stats.p50 < 1.0) {
            return OpResult{.status = "crash",
                .notes = "fsst: implausible sub-ns p50 (latency primitive failure)"};
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
        // No n_blocks: FSST is per-string (driver writes NULL, like OnPair).
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "fsst: bad_alloc ra_latency"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("fsst: ") + e.what()};
    }
}

OpResult FsstRunner::prefix_search(const std::string& in_path,
                                   const std::string& needle,
                                   const std::string& out_path,
                                   const std::string& /*cfg*/) {
    FsstWire w = read_wire(in_path);                          // UNTIMED: file read
    fsst_decoder_t decoder;                                   // UNTIMED: model setup
    if (fsst_import(&decoder, const_cast<unsigned char*>(w.header.data())) == 0)
        throw std::runtime_error("fsst: fsst_import returned 0 (corrupt header)");
    const unsigned char* cw = w.codewords.data();

    // FAIR PREFIX SCAN (W-7): FSST is per-string, so we decode only ~|needle| bytes
    // of EACH row (the capped fsst_decompress stops once the output buffer fills) and
    // test the prefix — O(min(|needle|,|s_i|)) per row — instead of decoding the whole
    // column. Identical hit-set to the uncompressed scan; far less work for short
    // needles. TIMED region is the scan only (wire load + import excluded).
    std::vector<uint32_t> hits;
    const std::string_view nv{needle};
    const auto t0 = std::chrono::steady_clock::now();
    size_t n_hits = partial_prefix_scan(w.N, nv,
        [&](size_t row, uint8_t* dst, size_t want) -> size_t {
            const uint32_t lo = w.compressed_offsets[row];
            const uint32_t hi = w.compressed_offsets[row + 1];
            return fsst_decompress(&decoder, hi - lo,
                const_cast<unsigned char*>(cw) + lo, want + 64, dst);
        }, hits);
    const uint64_t t_scan_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t0).count());

    // UNTIMED: write the hit list ([n_hits u32][hits[n_hits] u32]) — same wire as
    // the shared decompress_then_scan path, so the G6 bit-equality gate is unchanged.
    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("fsst: cannot open out_path " + out_path);
    uint32_t nh = static_cast<uint32_t>(n_hits);
    f.write(reinterpret_cast<const char*>(&nh), 4);
    if (!hits.empty())
        f.write(reinterpret_cast<const char*>(hits.data()),
                static_cast<std::streamsize>(hits.size() * 4));

    OpResult r;
    r.metrics.emplace_back("n_hits",    static_cast<double>(n_hits));
    r.metrics.emplace_back("num_rows",  static_cast<double>(w.N));
    r.metrics.emplace_back("t_scan_ns", static_cast<double>(t_scan_ns));
    return r;
}

} // namespace onpair_bench
