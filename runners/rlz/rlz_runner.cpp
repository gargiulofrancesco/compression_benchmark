#include "rlz_runner.h"
#include "rlz_core.h"
#include "bitpack_mixed.h"
#include "decompress_then_scan.h"
#include "scan_prefix.h"
#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include <sys/resource.h>
#include <algorithm>

#include <cstring>
#include <cmath>
#include <fstream>
#include <memory>
#include <new>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include <chrono>

namespace onpair_bench {

namespace {

// JSON helpers (mirror rpdac_runner.cpp; intentional duplication).

uint64_t extract_u64(const std::string& cfg, const std::string& key, uint64_t def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    if (pos >= cfg.size()) return def;
    uint64_t v = 0;
    bool any = false;
    while (pos < cfg.size() && cfg[pos] >= '0' && cfg[pos] <= '9') {
        v = v * 10 + static_cast<uint64_t>(cfg[pos] - '0');
        ++pos;
        any = true;
    }
    return any ? v : def;
}

double extract_double(const std::string& cfg, const std::string& key, double def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    // Read up to comma, brace, or whitespace.
    std::string tok;
    while (pos < cfg.size()
           && cfg[pos] != ','
           && cfg[pos] != '}'
           && cfg[pos] != ' '
           && cfg[pos] != '\t'
           && cfg[pos] != '\n') {
        tok.push_back(cfg[pos]);
        ++pos;
    }
    if (tok.empty()) return def;
    try {
        return std::stod(tok);
    } catch (...) {
        return def;
    }
}

void set_address_space_cap(uint64_t mem_cap_bytes) {
    if (mem_cap_bytes == 0) return;
    struct rlimit rl{};
    rl.rlim_cur = mem_cap_bytes;
    rl.rlim_max = mem_cap_bytes;
    (void)::setrlimit(RLIMIT_AS, &rl);
}

uint32_t ceil_log2_plus_one(uint64_t v) noexcept {
    // Returns ceil(log2(v+1)). v=0 -> 0; v=1 -> 1; v=2 -> 2; v=4 -> 3 ...
    if (v == 0) return 0;
    uint64_t x = v;
    uint32_t b = 0;
    while (x > 0) { ++b; x >>= 1; }
    return b;
}

struct LoadedWire {
    uint32_t              R_len = 0;
    std::vector<uint8_t>  R;
    uint32_t              N = 0;
    uint32_t              P_total = 0;
    uint32_t              bits_per_pos = 0;
    uint32_t              bits_per_len = 0;   // per-factor len field width (default 8)
    uint64_t              decoded_bytes = 0;  // exact total decoded payload size
    std::vector<uint32_t> phrase_offsets;   // size N+1
    std::vector<uint8_t>  packed_phrases;
};

// Over-read/over-write slack so the fused decode kernel can wildcopy without
// per-factor bounds branches (all sizing happens here, UNTIMED):
//   - R gets +kRSlack so a 32-byte wildcopy from R[pos] (pos <= R_len-1) never
//     reads unmapped memory.
//   - packed_phrases gets +kPackedSlack so the single 8-byte triple load (+1
//     straddle byte) at the last triple's bit offset stays in-bounds.
//   - out.bytes gets +kOutSlack (added by the caller) so the final factor's
//     32-byte wildcopy can overrun past the true decoded tail.
constexpr size_t kRSlack      = 64;
constexpr size_t kPackedSlack = 16;
constexpr size_t kOutSlack    = 64;

LoadedWire load_wire(const std::string& in_path) {
    std::ifstream f(in_path, std::ios::binary);
    if (!f) throw std::runtime_error("rlz: cannot open in_path " + in_path);
    LoadedWire w;
    f.read(reinterpret_cast<char*>(&w.R_len), 4);
    w.R.resize(static_cast<size_t>(w.R_len) + kRSlack, 0u);  // +slack for wildcopy over-read
    if (w.R_len > 0) {
        f.read(reinterpret_cast<char*>(w.R.data()),
               static_cast<std::streamsize>(w.R_len));
    }
    f.read(reinterpret_cast<char*>(&w.N), 4);
    f.read(reinterpret_cast<char*>(&w.P_total), 4);
    f.read(reinterpret_cast<char*>(&w.bits_per_pos), 4);
    f.read(reinterpret_cast<char*>(&w.bits_per_len), 4);
    f.read(reinterpret_cast<char*>(&w.decoded_bytes), 8);
    w.phrase_offsets.resize(static_cast<size_t>(w.N) + 1);
    f.read(reinterpret_cast<char*>(w.phrase_offsets.data()),
           static_cast<std::streamsize>((w.N + 1) * 4));
    const uint64_t total_bits =
        static_cast<uint64_t>(w.P_total) *
        (static_cast<uint64_t>(w.bits_per_pos) + w.bits_per_len + 8);
    const size_t packed_bytes = static_cast<size_t>((total_bits + 7) / 8);
    w.packed_phrases.resize(packed_bytes + kPackedSlack, 0u);  // +slack for 8-byte triple load
    if (packed_bytes > 0) {
        f.read(reinterpret_cast<char*>(w.packed_phrases.data()),
               static_cast<std::streamsize>(packed_bytes));
    }
    if (!f) throw std::runtime_error("rlz: short read on wire " + in_path);
    return w;
}

// FUSED single-pass decode: unpack each (pos,len,lit) triple inline (one 64-bit
// load + three shift/masks) and wildcopy it STRAIGHT from R into out.bytes — no
// 3×P_total uint32 scratch (the old unpack_triples materialisation = ~107 MB of
// extra memory traffic) and no per-factor variable-length memcpy dispatch (the
// over-copy of a fixed 32 B, LZ4-style, advancing by the true len). Requires the
// caller to have sized out.bytes to decoded_bytes + kOutSlack and load_wire to
// have padded w.R / w.packed_phrases (done UNTIMED). Trusts the wire's pos+len <=
// R_len invariant in the hot loop (same posture as lz4/fsst decode). Returns the
// total decoded bytes (== w.decoded_bytes).
// Copy exactly W bytes (W in {8,16,32,64}) as a fixed unrolled run of 8/16-byte
// memcpys — the compiler lowers these to wide vector load/stores.
template<unsigned W>
inline void copy_fixed(uint8_t* d, const uint8_t* s) {
    static_assert(W == 8 || W == 16 || W == 32 || W == 64, "W in {8,16,32,64}");
    if constexpr (W == 8)  { std::memcpy(d, s, 8); }
    else { for (unsigned k = 0; k < W; k += 16) std::memcpy(d + k, s + k, 16); }
}

// WC = wildcopy width (the fixed over-copy granularity). kRSlack/kOutSlack must be
// >= WC so the final factor's over-copy/over-read stays in the padded buffers.
template<unsigned WC>
size_t decode_fused_into_w(const LoadedWire& w, Column& out) {
    const uint32_t total_bits = w.bits_per_pos + w.bits_per_len + 8u;  // <= 56
    const uint64_t mask_pos   = (w.bits_per_pos >= 32)
        ? ~uint64_t{0} : ((uint64_t{1} << w.bits_per_pos) - 1);
    const uint64_t mask_len   = (uint64_t{1} << w.bits_per_len) - 1;
    const uint32_t shift_len  = w.bits_per_pos;
    const uint32_t shift_lit  = w.bits_per_pos + w.bits_per_len;
    const uint8_t* base = w.packed_phrases.data();
    const uint8_t* R    = w.R.data();
    uint8_t*       dst  = out.bytes.data();
    size_t cursor = 0;
    out.offsets[0] = 0;
    for (uint32_t i = 0; i < w.N; ++i) {
        const uint32_t hi = w.phrase_offsets[i + 1];
        for (uint32_t j = w.phrase_offsets[i]; j < hi; ++j) {
            const uint64_t bitpos = static_cast<uint64_t>(j) * total_bits;
            const size_t   b      = static_cast<size_t>(bitpos >> 3);
            const uint32_t sh     = static_cast<uint32_t>(bitpos & 7);
            uint64_t word;
            std::memcpy(&word, base + b, 8);                   // padded: always in-bounds
            const uint64_t val = word >> sh;                   // total_bits<=56, sh<=7 => no straddle
            const uint32_t p = static_cast<uint32_t>(val & mask_pos);
            const uint32_t l = static_cast<uint32_t>((val >> shift_len) & mask_len);
            const uint8_t  c = static_cast<uint8_t>((val >> shift_lit) & 0xFFu);
            uint8_t*       d = dst + cursor;
            const uint8_t* s = R + p;
            if (l <= WC) {                                     // common case (avg len ~22)
                copy_fixed<WC>(d, s);
            } else {
                size_t k = 0;
                do { copy_fixed<WC>(d + k, s + k); k += WC; } while (k < l);
            }
            cursor += l;
            dst[cursor++] = c;                                 // mandatory trailing literal
        }
        out.offsets[i + 1] = static_cast<uint32_t>(cursor);
    }
    return cursor;
}

// Dispatch on the runtime wildcopy width (each variant fully unrolled).
size_t decode_fused_into(const LoadedWire& w, Column& out, uint32_t wildcopy_bytes = 32) {
    switch (wildcopy_bytes) {
        case 8:  return decode_fused_into_w<8>(w, out);
        case 16: return decode_fused_into_w<16>(w, out);
        case 64: return decode_fused_into_w<64>(w, out);
        default: return decode_fused_into_w<32>(w, out);
    }
}

// Full decode for NON-timed callers (prefix_search): pre-size + fused decode.
// load_wire has already padded w.R / w.packed_phrases for the wildcopy kernel.
Column extract_all(const LoadedWire& w) {
    Column out;
    out.bytes.resize(static_cast<size_t>(w.decoded_bytes) + kOutSlack);
    out.offsets.assign(static_cast<size_t>(w.N) + 1, 0u);
    const size_t total = decode_fused_into(w, out);
    out.bytes.resize(total);
    return out;
}

// PER-STRING row decode (random access, O(|s_i|)). Decodes ONLY row `row`'s phrases
// [phrase_offsets[row], phrase_offsets[row+1]) — each triple read directly at its
// bit offset (j*total_bits), wildcopied from the resident reference R — never
// touching the other rows' P_total factors. `dst` must hold the row's decoded bytes
// + kOutSlack (the final factor's WC-wide over-copy overruns the true tail).
// `want` caps the decode: stop once >= want bytes are produced (for the prefix
// scan, want = needle length). Default = whole row (random access / ra_latency).
template<unsigned WC>
size_t decode_row_into_w(const LoadedWire& w, uint32_t row, uint8_t* dst, size_t want) {
    const uint32_t total_bits = w.bits_per_pos + w.bits_per_len + 8u;   // <= 56
    const uint64_t mask_pos   = (w.bits_per_pos >= 32)
        ? ~uint64_t{0} : ((uint64_t{1} << w.bits_per_pos) - 1);
    const uint64_t mask_len   = (uint64_t{1} << w.bits_per_len) - 1;
    const uint32_t shift_len  = w.bits_per_pos;
    const uint32_t shift_lit  = w.bits_per_pos + w.bits_per_len;
    const uint8_t* base = w.packed_phrases.data();
    const uint8_t* R    = w.R.data();
    size_t cursor = 0;
    const uint32_t hi = w.phrase_offsets[row + 1];
    for (uint32_t j = w.phrase_offsets[row]; j < hi; ++j) {
        const uint64_t bitpos = static_cast<uint64_t>(j) * total_bits;
        const size_t   b      = static_cast<size_t>(bitpos >> 3);
        const uint32_t sh     = static_cast<uint32_t>(bitpos & 7);
        uint64_t word;
        std::memcpy(&word, base + b, 8);                   // padded: always in-bounds
        const uint64_t val = word >> sh;                   // total_bits<=56, sh<=7 => no straddle
        const uint32_t p = static_cast<uint32_t>(val & mask_pos);
        const uint32_t l = static_cast<uint32_t>((val >> shift_len) & mask_len);
        const uint8_t  c = static_cast<uint8_t>((val >> shift_lit) & 0xFFu);
        uint8_t*       d = dst + cursor;
        const uint8_t* s = R + p;
        if (l <= WC) {
            copy_fixed<WC>(d, s);
        } else {
            size_t k = 0;
            do { copy_fixed<WC>(d + k, s + k); k += WC; } while (k < l);
        }
        cursor += l;
        dst[cursor++] = c;                                 // mandatory trailing literal
        if (cursor >= want) break;                         // prefix-scan early-out
    }
    return cursor;
}

size_t decode_row_into(const LoadedWire& w, uint32_t row, uint8_t* dst,
                       uint32_t wildcopy_bytes = 32, size_t want = static_cast<size_t>(-1)) {
    switch (wildcopy_bytes) {
        case 8:  return decode_row_into_w<8>(w, row, dst, want);
        case 16: return decode_row_into_w<16>(w, row, dst, want);
        case 64: return decode_row_into_w<64>(w, row, dst, want);
        default: return decode_row_into_w<32>(w, row, dst, want);
    }
}

} // anonymous

RlzConfig parse_rlz_config(const std::string& cfg_json) {
    RlzConfig c;
    c.reference_size_fraction = extract_double(cfg_json,
                                                 "reference_size_fraction", 0.05);
    // EXPERIMENTAL absolute-size override (off by default).
    c.reference_size_bytes = extract_u64(cfg_json, "reference_size_bytes", 0);
    //: in {0.01, 0.05, 0.25} — enforced ONLY on the headline (fraction) path.
    if (c.reference_size_bytes == 0) {
        const double tol = 1e-9;
        if (!(std::fabs(c.reference_size_fraction - 0.01) < tol
              || std::fabs(c.reference_size_fraction - 0.05) < tol
              || std::fabs(c.reference_size_fraction - 0.25) < tol)) {
            throw std::runtime_error("rlz: reference_size_fraction not in "
                "{0.01, 0.05, 0.25} (headline set)");
        }
    }
    c.wildcopy_bytes = static_cast<uint32_t>(extract_u64(cfg_json, "wildcopy_bytes", 32));
    if (c.wildcopy_bytes != 8 && c.wildcopy_bytes != 16
        && c.wildcopy_bytes != 32 && c.wildcopy_bytes != 64) {
        throw std::runtime_error("rlz: wildcopy_bytes must be one of {8,16,32,64}");
    }
    // (revised): per-factor len field width; default 8 (short-string regime).
    c.max_len_bits = static_cast<uint32_t>(extract_u64(cfg_json, "max_len_bits", 8));
    if (c.max_len_bits < 4 || c.max_len_bits > 16) {
        throw std::runtime_error("rlz: max_len_bits out of range (must be 4..16)");
    }
    c.block_size = static_cast<uint32_t>(extract_u64(cfg_json, "block_size", 16384));
    if (c.block_size != 16384u) {
        throw std::runtime_error("rlz: block_size must be 16384 (lock)");
    }
    c.seed = static_cast<uint32_t>(extract_u64(cfg_json, "seed", 42));
    if (c.seed != 42u) {
        throw std::runtime_error("rlz: seed must be 42 (lock)");
    }
    c.threads = static_cast<uint32_t>(extract_u64(cfg_json, "threads", 1));
    if (c.threads != 1) {
        throw std::runtime_error("rlz: threads must be 1 (single-thread fairness)");
    }
    c.mem_cap_gb = extract_u64(cfg_json, "mem_cap_gb", 64);
    if (c.mem_cap_gb < 1 || c.mem_cap_gb > 1024) {
        throw std::runtime_error("rlz: mem_cap_gb out of range (must be 1..1024)");
    }
    return c;
}

RlzRunner::RlzRunner() = default;
RlzRunner::~RlzRunner() = default;

const char* RlzRunner::version() const {
    return "rlz-pureCpp+libdivsufsort-2.0.1";
}

OpResult RlzRunner::compress(const std::string& in_path,
                              const std::string& out_path,
                              const std::string& cfg_json) {
    RlzConfig cfg = parse_rlz_config(cfg_json);
    Column col = read_column(in_path);
    const size_t N = col.num_rows();

    if (N == 0) {
        OpResult r;
        r.status = "unsupported";
        r.notes  = "empty column not supported by rlz";
        return r;
    }
    size_t input_bytes = 0;
    for (size_t i = 0; i < N; ++i) input_bytes += col.at(i).size();
    if (input_bytes == 0) {
        OpResult r;
        r.status = "unsupported";
        r.notes  = "all-empty-string column not supported by rlz";
        return r;
    }

    const uint64_t L = static_cast<uint64_t>(input_bytes);
    uint32_t K;
    if (cfg.reference_size_bytes > 0) {
        // EXPERIMENTAL absolute dictionary size: K = ceil(bytes/block_size),
        // clamped so K*block_size <= L (sampling needs offsets in [0, L-block_size]).
        const uint64_t bs = cfg.block_size;
        uint64_t k = (cfg.reference_size_bytes + bs - 1) / bs;
        const uint64_t max_K = L / bs;
        if (k > max_K) k = max_K;
        K = static_cast<uint32_t>(k);
    } else {
        K = rlz_core::compute_K(L, cfg.reference_size_fraction, cfg.block_size);
    }
    if (K == 0) {
        OpResult r;
        r.status = "refused-env";
        r.notes  = "reference fraction too small for block_size; "
                   "column_size * R% = "
                 + std::to_string(static_cast<uint64_t>(
                     L * cfg.reference_size_fraction))
                 + " < block_size = " + std::to_string(cfg.block_size);
        return r;
    }
    const uint64_t R_len = static_cast<uint64_t>(K) * cfg.block_size;
    if (R_len > UINT32_MAX) {
        OpResult r; r.status = "crash";
        r.notes = "rlz: R_len exceeds 32-bit capacity";
        return r;
    }

    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    // TIMED: sample R + build SA + factor-parse + bitpack (/the codec
    // work). Input column already read; the wire write below is excluded.
    const auto t_comp0 = std::chrono::steady_clock::now();
    // / INSIDE timed region: sample R + build SA + factor-parse.
    std::vector<uint8_t>  R;
    std::vector<int32_t>  SA;
    std::vector<uint32_t> pos_stream, len_stream, lit_stream;
    std::vector<uint32_t> phrase_offsets;
    phrase_offsets.reserve(N + 1);
    phrase_offsets.push_back(0);
    try {
        // The column payload is already the contiguous corpus (col.bytes.size() ==
        // col.offsets.back() == L), so sample directly from it — no setup copy in
        // the timed region.
        rlz_core::sample_reference(col.bytes.data(), L, K,
                                    cfg.block_size, cfg.seed, R);
        rlz_core::build_sa(R, SA);

        //: cap every match at the len field's max (255 for the 8-bit
        // default). Matches longer than this are split into successive factors —
        // negligible for the short independently-parsed strings we target.
        const uint32_t max_match_len = (1u << cfg.max_len_bits) - 1u;
        std::vector<rlz_core::Factor> row_factors;
        row_factors.reserve(64);
        for (size_t i = 0; i < N; ++i) {
            const auto sv = col.at(i);
            row_factors.clear();
            const size_t emitted = rlz_core::factor_parse_row(
                reinterpret_cast<const uint8_t*>(sv.data()),
                sv.size(),
                R.data(), R.size(),
                SA.data(),
                max_match_len,
                row_factors);
            for (size_t j = 0; j < emitted; ++j) {
                pos_stream.push_back(row_factors[j].pos);
                len_stream.push_back(row_factors[j].len);
                lit_stream.push_back(row_factors[j].literal);
            }
            phrase_offsets.push_back(static_cast<uint32_t>(pos_stream.size()));
        }
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom";
        r.notes = "bad_alloc in sample/SA-build/factor-parse";
        return r;
    } catch (const std::runtime_error& e) {
        OpResult r; r.status = "crash"; r.notes = e.what(); return r;
    }

    const uint32_t P_total = static_cast<uint32_t>(pos_stream.size());
    //: bits_per_pos = max(10, ceil_log2(R_len + 1)); clamp <= 32.
    uint32_t bits_per_pos = ceil_log2_plus_one(R_len);
    if (bits_per_pos < 10u) bits_per_pos = 10u;
    if (bits_per_pos > 32u) bits_per_pos = 32u;
    // (revised): len field width from config (default 8). Matches are
    // already capped at (1<<bits_per_len)-1 in the parse above, so every len fits.
    const uint32_t bits_per_len = cfg.max_len_bits;

    std::vector<uint8_t> packed_phrases;
    try {
        bitpack_mixed::pack_triples(pos_stream, len_stream, lit_stream,
                                     bits_per_pos, bits_per_len, 8u, packed_phrases);
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom";
        r.notes = "bad_alloc in bitpack_mixed::pack_triples"; return r;
    } catch (const std::runtime_error& e) {
        OpResult r; r.status = "crash"; r.notes = e.what(); return r;
    }
    const uint64_t t_compress_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t_comp0).count());

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("rlz: cannot open out_path " + out_path);
    const uint32_t R_len_u32 = static_cast<uint32_t>(R_len);
    f.write(reinterpret_cast<const char*>(&R_len_u32), 4);
    if (R_len_u32 > 0) {
        f.write(reinterpret_cast<const char*>(R.data()),
                static_cast<std::streamsize>(R_len_u32));
    }
    const uint32_t N_u32 = static_cast<uint32_t>(N);
    f.write(reinterpret_cast<const char*>(&N_u32), 4);
    f.write(reinterpret_cast<const char*>(&P_total), 4);
    f.write(reinterpret_cast<const char*>(&bits_per_pos), 4);
    f.write(reinterpret_cast<const char*>(&bits_per_len), 4);
    // decoded_bytes (== input_bytes, lossless): lets bulk_decode size its output
    // buffer EXACTLY outside the timed region (uniform decode-timer contract).
    const uint64_t decoded_bytes_u64 = static_cast<uint64_t>(input_bytes);
    f.write(reinterpret_cast<const char*>(&decoded_bytes_u64), 8);
    f.write(reinterpret_cast<const char*>(phrase_offsets.data()),
            static_cast<std::streamsize>((N + 1) * 4));
    if (!packed_phrases.empty()) {
        f.write(reinterpret_cast<const char*>(packed_phrases.data()),
                static_cast<std::streamsize>(packed_phrases.size()));
    }
    f.close();

    const uint64_t dict_bytes       = R_len_u32;
    const uint64_t tokens_bytes     = packed_phrases.size();
    // 28 = wire scalars R_len,N,P_total,bits_per_pos,bits_per_len (20) +
    // decoded_bytes u64 (8); + the per-row phrase offsets (N+1)*4. All EXCLUDED
    // from the payload ratio.
    const uint64_t boundaries_bytes = 28ull + static_cast<uint64_t>(N + 1) * 4;
    const uint64_t output_bytes     = dict_bytes + tokens_bytes + boundaries_bytes;

    OpResult r;
    r.metrics.emplace_back("dict_bytes",       static_cast<double>(dict_bytes));
    r.metrics.emplace_back("tokens_bytes",     static_cast<double>(tokens_bytes));
    r.metrics.emplace_back("boundaries_bytes", static_cast<double>(boundaries_bytes));
    r.metrics.emplace_back("input_bytes",      static_cast<double>(input_bytes));
    r.metrics.emplace_back("output_bytes",     static_cast<double>(output_bytes));
    r.metrics.emplace_back("num_rows",         static_cast<double>(N));
    r.metrics.emplace_back("t_compress_ns",    static_cast<double>(t_compress_ns));
    return r;
}

OpResult RlzRunner::bulk_decode(const std::string& in_path,
                                  const std::string& out_path,
                                  const std::string& cfg_json) {
    RlzConfig cfg = parse_rlz_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    Column out;
    uint64_t t_decode_ns = 0;
    try {
        // UNTIMED: wire load = read the RLZ reference (the dictionary/model) +
        // the factor stream. Loading the model is one-time setup, not decode work
        // — excluded so the timer measures only the decode kernel (uniform
        // decode-timer contract: time only in-RAM payload decompression; wire/
        // model load, file I/O, AND output/scratch allocation excluded).
        LoadedWire w = load_wire(in_path);
        // UNTIMED: pre-allocate the EXACT output buffer (decoded_bytes from the
        // wire) + per-row offsets + the unpack scratch, all OUTSIDE the timer.
        // resize() value-inits the output here so its pages are resident before
        // the timed region (no first-touch faults), and — unlike the old path —
        // the timer no longer pays for the output vector growing via insert().
        out.bytes.resize(static_cast<size_t>(w.decoded_bytes) + kOutSlack);
        out.offsets.assign(static_cast<size_t>(w.N) + 1, 0u);
        // TIMED: the decode kernel ONLY — one fused pass that unpacks each triple
        // inline and wildcopies it from the reference into the pre-sized output.
        // No scratch unpack arrays, no per-factor memcpy dispatch, no allocation.
        const auto t0 = std::chrono::steady_clock::now();
        const size_t total = decode_fused_into(w, out, cfg.wildcopy_bytes);
        t_decode_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());
        out.bytes.resize(total);                     // UNTIMED shrink (== decoded_bytes)
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in bulk_decode"; return r;
    }
    write_column(out_path, out);                     // UNTIMED: file write

    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(out.bytes.size()));
    r.metrics.emplace_back("num_rows",     static_cast<double>(out.num_rows()));
    r.metrics.emplace_back("t_decode_ns",  static_cast<double>(t_decode_ns));
    return r;
}

OpResult RlzRunner::random_decode(const std::string& in_path,
                                    size_t idx,
                                    const std::string& out_path,
                                    const std::string& cfg_json) {
    RlzConfig cfg = parse_rlz_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    LoadedWire w;
    try {
        w = load_wire(in_path);
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc loading wire"; return r;
    }
    if (idx >= w.N) {
        OpResult r; r.status = "crash";
        r.notes = "idx out of range: " + std::to_string(idx);
        return r;
    }

    std::vector<uint8_t> out_bytes;
    try {
        // PER-STRING decode (O(|s_i|)): decode ONLY row idx's phrases directly at
        // their bit offset, never unpacking the whole P_total-triple stream. Each
        // phrase emits <= (1<<bits_per_len) bytes, so this bounds the row buffer;
        // +kOutSlack for the final factor's wildcopy over-run.
        const uint32_t lo = w.phrase_offsets[idx];
        const uint32_t hi = w.phrase_offsets[idx + 1];
        const size_t bound =
            static_cast<size_t>(hi - lo) * (size_t{1} << w.bits_per_len) + kOutSlack;
        std::vector<uint8_t> buf(bound, 0u);
        const size_t len = decode_row_into(w, static_cast<uint32_t>(idx),
                                           buf.data(), cfg.wildcopy_bytes);
        out_bytes.assign(buf.begin(), buf.begin() + len);
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in random_decode"; return r;
    }

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("rlz: cannot open " + out_path);
    f.write(reinterpret_cast<const char*>(out_bytes.data()),
            static_cast<std::streamsize>(out_bytes.size()));
    f.close();

    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(out_bytes.size()));
    return r;
}

OpResult RlzRunner::prefix_search(const std::string& in_path,
                                    const std::string& needle,
                                    const std::string& out_path,
                                    const std::string& cfg_json) {
    RlzConfig cfg = parse_rlz_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    LoadedWire w;
    try {
        w = load_wire(in_path);
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc loading wire"; return r;
    }

    size_t n_hits = 0;
    uint64_t t_scan_ns = 0;
    std::vector<uint32_t> hits;
    try {
        // FAIR PREFIX SCAN (W-7): RLZ is per-string, so decode only ~|needle| bytes
        // of EACH row from the resident reference (decode_row_into stops at `want`)
        // and test the prefix — not the whole column.
        const std::string_view nv{needle};
        const auto t0 = std::chrono::steady_clock::now();
        n_hits = partial_prefix_scan(w.N, nv,
            [&](size_t row, uint8_t* dst, size_t want) -> size_t {
                return decode_row_into(w, static_cast<uint32_t>(row), dst,
                                       cfg.wildcopy_bytes, want);
            }, hits);
        t_scan_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in prefix_search"; return r;
    }

    // UNTIMED: write the hit list ([n_hits u32][hits u32]) — same wire as the shared
    // decompress_then_scan path, so the G6 bit-equality gate is unchanged.
    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("rlz: cannot open out_path " + out_path);
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

// Random-access latency — PER-STRING (§8.4). RLZ copies ONLY row i's phrases from
// the resident reference R (decoded directly at their bit offset; O(|s_i|)) — the
// same per-string class as FSST/OnPair, so its L_ra is skew/layout-insensitive.
// Wire load + buffer sizing are one-time setup OUTSIDE the timed loop; no file I/O
// in the loop. Refuses cleanly off the controlled x86 rig.
OpResult RlzRunner::ra_latency(const std::string& in_path,
                               const std::string& /*out_path*/,
                               const std::string& cfg_json) {
    RlzConfig cfg = parse_rlz_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));
    try {
        LoadedWire w = load_wire(in_path);
        if (w.N == 0)
            return OpResult{.status = "crash", .notes = "rlz: empty column for ra_latency"};

        uint64_t n_samples = extract_u64(cfg_json, "n_samples", 1000000);
        if (n_samples == 0) n_samples = 1;
        uint64_t seed   = extract_u64(cfg_json, "seed", 42);
        bool     warmup = extract_u64(cfg_json, "warmup", 1) != 0;

        // Size the row buffer once: widest row (phrases) × max factor length + slack.
        uint32_t max_row_phrases = 0;
        for (uint32_t i = 0; i < w.N; ++i)
            max_row_phrases = std::max(max_row_phrases,
                                       w.phrase_offsets[i + 1] - w.phrase_offsets[i]);
        const size_t buf_sz =
            static_cast<size_t>(max_row_phrases) * (size_t{1} << w.bits_per_len) + kOutSlack;
        std::vector<uint8_t> buf(buf_sz, 0u);

        if (warmup) { Column warm = extract_all(w); (void)warm; }

        const QuerySpec qspec = parse_query_spec(cfg_json);
        const std::vector<uint64_t> indices =
            generate_query_indices(qspec, n_samples, w.N, seed);

        RaLatencyResult m;
        std::string ra_reason;
        if (!ra_measure_shipping(indices,
            [&](uint64_t idx64) -> unsigned char {
                const size_t len = decode_row_into(
                    w, static_cast<uint32_t>(idx64), buf.data(), cfg.wildcopy_bytes);
                return len ? buf[len - 1] : 0;
            }, m, ra_reason)) {
            return OpResult{.status = "refused-env", .notes = "rlz: " + ra_reason};
        }
        if (m.stats.n > 0 && m.stats.p50 < 1.0) {
            return OpResult{.status = "crash",
                .notes = "rlz: implausible sub-ns p50 (latency primitive failure)"};
        }
        const LatencyStats& s = m.stats;
        OpResult r;
        r.metrics.emplace_back("l_ra_p50_ns",  s.p50);
        r.metrics.emplace_back("l_ra_p95_ns",  s.p95);
        r.metrics.emplace_back("l_ra_p99_ns",  s.p99);
        r.metrics.emplace_back("l_ra_mean_ns", s.mean);
        r.metrics.emplace_back("l_ra_min_ns",  s.min);
        r.metrics.emplace_back("l_ra_max_ns",  s.max);
        r.metrics.emplace_back("n_samples",    static_cast<double>(s.n));
        r.metrics.emplace_back("batch_k",      static_cast<double>(m.batch_k));
        r.metrics.emplace_back("clock_resolution_ns", m.clock_resolution_ns);
        r.metrics.emplace_back("num_rows",     static_cast<double>(w.N));
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "rlz: bad_alloc ra_latency"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("rlz: ") + e.what()};
    }
}

} // namespace onpair_bench
