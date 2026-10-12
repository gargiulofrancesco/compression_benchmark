#pragma once
// Uniform-width bit-packed token-ID stream.
// LSB-first within each byte; tokens written in increasing position order.
// Consumers: the BPE runner (token IDs) and the repair runner (grammar
// symbols) — rlz uses the sibling bitpack_mixed.
// NOTE: OnPair does NOT use this archive; it bit-packs/unpacks via its own
// library (BitWriter + decode_all<Bits>/extract16). For FAIRNESS — so a baseline
// that uses this shared utility isn't slower than OnPair purely because of our
// code — for_each_token() below ports OnPair's batched extract16 technique here.

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <utility>
#include <vector>

namespace onpair_bench::bitpack {

// The tiny extract primitives must inline into the group loop (always_inline);
// the larger loop/dispatch helpers are left as plain `inline` so the optimiser
// does NOT fold all 7 width branches into one caller (that bloats the function
// and spills registers on the trivial byte-aligned path). Callers that need the
// absolute fastest BYTE-ALIGNED (8/16-bit) path should keep their own tight typed
// loop (local output cursor) and use this primitive only for sub-byte widths.
#if defined(__GNUC__) || defined(__clang__)
#  define ONPAIR_BENCH_BITPACK_AINLINE inline __attribute__((always_inline))
#else
#  define ONPAIR_BENCH_BITPACK_AINLINE inline
#endif

// ceil(log2(V)). bits_for_vocab(0) == 0 (degenerate); bits_for_vocab(1) == 0;
// bits_for_vocab(2) == 1; bits_for_vocab(4096) == 12; bits_for_vocab(262144) == 18.
uint32_t bits_for_vocab(uint32_t V) noexcept;

// Pack `tokens` at `bits_per_token` width (range [0, 32]; 0 packs nothing).
// Each token MUST satisfy `t < (1u << bits_per_token)` for `bits_per_token < 32`.
// out_packed is resized to ceil(tokens.size() * bits_per_token / 8).
void pack_uniform(const std::vector<uint32_t>& tokens,
                  uint32_t bits_per_token,
                  std::vector<uint8_t>& out_packed);

// Unpack exactly `n_tokens` tokens from `packed`.
// out_tokens.size() set to n_tokens.
void unpack_uniform(const std::vector<uint8_t>& packed,
                    uint32_t bits_per_token,
                    size_t n_tokens,
                    std::vector<uint32_t>& out_tokens);

// ─────────────────────────────────────────────────────────────────────────────
// for_each_token<Emit> — FAST batched, branchless token reader.
//
// Fairness note: OnPair's library unpacks its bit-packed token stream with a
// compile-time-unrolled `extract16` (16 token IDs per step, fixed bit positions,
// no per-token refill branch). The scalar unpack_uniform() above is far slower,
// so any lineup baseline that reused it would lose decode throughput purely
// because of OUR utility — not the codec. This primitive ports OnPair's technique
// into the shared archive so its consumers (BPE) decode
// at OnPair-equivalent bit-level speed.
//
// It calls `emit(uint32_t id)` for every token in stream order WITHOUT
// materialising a token array, so the caller can fuse extraction with its own
// work (e.g. a dictionary copy). Byte-aligned widths (8/16) are a single typed
// load; widths 9–15 use the batched extractor; everything else falls back to a
// correct scalar reader. LSB-first packing == little-endian typed reads (x86).
//
// `packed`/`packed_size` is the exact pack_uniform() output (no padding needed:
// full natural-groups never read past their own bytes; the remainder is scalar).
// ─────────────────────────────────────────────────────────────────────────────

namespace detail {

// Natural-group metrics (smallest token count whose bits are a multiple of 64),
// identical to OnPair's decode_all group_traits.
template<uint32_t Bits> struct group_traits;
template<> struct group_traits< 9> { static constexpr uint32_t tokens = 64, words =  9, subs = 4; };
template<> struct group_traits<10> { static constexpr uint32_t tokens = 32, words =  5, subs = 2; };
template<> struct group_traits<11> { static constexpr uint32_t tokens = 64, words = 11, subs = 4; };
template<> struct group_traits<12> { static constexpr uint32_t tokens = 16, words =  3, subs = 1; };
template<> struct group_traits<13> { static constexpr uint32_t tokens = 64, words = 13, subs = 4; };
template<> struct group_traits<14> { static constexpr uint32_t tokens = 32, words =  7, subs = 2; };
template<> struct group_traits<15> { static constexpr uint32_t tokens = 64, words = 15, subs = 4; };

// Extract one Bits-wide token at a COMPILE-TIME bit position from the byte
// stream. uint64 words are read by memcpy (alignment-safe) and interpreted
// little-endian, matching pack_uniform's LSB-first layout.
template<uint32_t Bits, uint32_t BitPos>
ONPAIR_BENCH_BITPACK_AINLINE uint32_t extract_one(const uint8_t* base) noexcept {
    constexpr uint64_t MASK = (uint64_t{1} << Bits) - 1;
    constexpr uint32_t w = BitPos / 64;
    constexpr uint32_t s = BitPos % 64;
    uint64_t lo;
    std::memcpy(&lo, base + static_cast<size_t>(w) * 8, 8);
    if constexpr (s + Bits <= 64) {
        return static_cast<uint32_t>((lo >> s) & MASK);
    } else {
        uint64_t hi;
        std::memcpy(&hi, base + static_cast<size_t>(w + 1) * 8, 8);
        return static_cast<uint32_t>(((lo >> s) | (hi << (64 - s))) & MASK);
    }
}

template<uint32_t Bits, uint32_t StartBit, size_t... Is>
ONPAIR_BENCH_BITPACK_AINLINE void extract16_impl(const uint8_t* base, uint32_t* out,
                           std::index_sequence<Is...>) noexcept {
    ((out[Is] = extract_one<Bits, StartBit + static_cast<uint32_t>(Is) * Bits>(base)), ...);
}
template<uint32_t Bits, uint32_t StartBit>
ONPAIR_BENCH_BITPACK_AINLINE void extract16(const uint8_t* base, uint32_t* out) noexcept {
    extract16_impl<Bits, StartBit>(base, out, std::make_index_sequence<16>{});
}

// Scalar reader for the remainder / fallback widths.
template<typename Emit>
inline void scalar_each(const uint8_t* packed, size_t packed_size, size_t in_pos,
                        uint32_t bits, size_t n, Emit&& emit) {
    const uint64_t mask = (bits >= 32) ? ~uint64_t{0} : ((uint64_t{1} << bits) - 1);
    uint64_t acc = 0; uint32_t acc_bits = 0;
    for (size_t j = 0; j < n; ++j) {
        while (acc_bits < bits && in_pos < packed_size) {
            acc |= static_cast<uint64_t>(packed[in_pos++]) << acc_bits;
            acc_bits += 8;
        }
        emit(static_cast<uint32_t>(acc & mask));
        acc >>= bits; acc_bits -= bits;
    }
}

template<uint32_t Bits, typename Emit>
inline void for_each_token_bits(const uint8_t* packed, size_t packed_size,
                                size_t n_tokens, Emit&& emit) {
    using G = group_traits<Bits>;
    const size_t full_g = n_tokens / G::tokens;
    uint32_t t[16];
    const uint8_t* base = packed;
    for (size_t g = 0; g < full_g; ++g, base += static_cast<size_t>(G::words) * 8) {
        extract16<Bits, 0>(base, t);                  for (int i = 0; i < 16; ++i) emit(t[i]);
        if constexpr (G::subs >= 2) { extract16<Bits, 16 * Bits>(base, t); for (int i = 0; i < 16; ++i) emit(t[i]); }
        if constexpr (G::subs >= 3) { extract16<Bits, 32 * Bits>(base, t); for (int i = 0; i < 16; ++i) emit(t[i]); }
        if constexpr (G::subs >= 4) { extract16<Bits, 48 * Bits>(base, t); for (int i = 0; i < 16; ++i) emit(t[i]); }
    }
    const size_t done = full_g * G::tokens;
    if (done < n_tokens) {
        scalar_each(packed, packed_size,
                    static_cast<size_t>(full_g) * G::words * 8,
                    Bits, n_tokens - done, std::forward<Emit>(emit));
    }
}

} // namespace detail

template<typename Emit>
inline void for_each_token(const uint8_t* packed, size_t packed_size,
                           uint32_t bits, size_t n_tokens, Emit&& emit) {
    switch (bits) {
        case 8:
            for (size_t j = 0; j < n_tokens; ++j) emit(static_cast<uint32_t>(packed[j]));
            return;
        case 16: {
            // The packed stream IS a uint16_t token array; a typed load per token
            // (no memcpy) is what the optimiser turns into a tight movzx loop —
            // empirically ~3x the per-token memcpy form. data() is suitably
            // aligned; LSB-first == little-endian uint16 read on x86.
            const auto* toks = reinterpret_cast<const uint16_t*>(packed);
            for (size_t j = 0; j < n_tokens; ++j) emit(static_cast<uint32_t>(toks[j]));
            return;
        }
        case  9: detail::for_each_token_bits< 9>(packed, packed_size, n_tokens, emit); return;
        case 10: detail::for_each_token_bits<10>(packed, packed_size, n_tokens, emit); return;
        case 11: detail::for_each_token_bits<11>(packed, packed_size, n_tokens, emit); return;
        case 12: detail::for_each_token_bits<12>(packed, packed_size, n_tokens, emit); return;
        case 13: detail::for_each_token_bits<13>(packed, packed_size, n_tokens, emit); return;
        case 14: detail::for_each_token_bits<14>(packed, packed_size, n_tokens, emit); return;
        case 15: detail::for_each_token_bits<15>(packed, packed_size, n_tokens, emit); return;
        default:  // 0–7, 17–32: correct scalar fallback
            detail::scalar_each(packed, packed_size, 0, bits, n_tokens,
                                std::forward<Emit>(emit));
            return;
    }
}

} // namespace onpair_bench::bitpack
