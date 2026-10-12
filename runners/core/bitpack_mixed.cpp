#include "bitpack_mixed.h"

#include <cstring>
#include <stdexcept>

namespace onpair_bench::bitpack_mixed {

namespace {

inline void put_field(uint64_t& acc, uint32_t& acc_bits,
                      uint32_t value, uint32_t bits,
                      std::vector<uint8_t>& out, size_t& out_pos) {
    if (bits == 0) return;
    const uint64_t mask = (bits == 32)
        ? ~uint64_t{0}
        : ((uint64_t{1} << bits) - 1);
    acc |= (static_cast<uint64_t>(value) & mask) << acc_bits;
    acc_bits += bits;
    while (acc_bits >= 8) {
        out[out_pos++] = static_cast<uint8_t>(acc & 0xFFu);
        acc >>= 8;
        acc_bits -= 8;
    }
}

} // anonymous

void pack_triples(const std::vector<uint32_t>& pos,
                  const std::vector<uint32_t>& len,
                  const std::vector<uint32_t>& literal,
                  uint32_t bits_pos,
                  uint32_t bits_len,
                  uint32_t bits_lit,
                  std::vector<uint8_t>& out_packed) {
    if (bits_pos > 32 || bits_len > 32 || bits_lit > 32) {
        throw std::runtime_error("bitpack_mixed: per-field bits > 32 not supported");
    }
    const uint32_t total_bits = bits_pos + bits_len + bits_lit;
    if (total_bits > 64) {
        throw std::runtime_error("bitpack_mixed: total bits per triple > 64 not supported");
    }
    if (pos.size() != len.size() || pos.size() != literal.size()) {
        throw std::runtime_error("bitpack_mixed: input vectors must have matching size");
    }
    const size_t n = pos.size();
    if (n == 0 || total_bits == 0) {
        out_packed.clear();
        return;
    }
    out_packed.assign(packed_bytes_for(n, total_bits), 0u);

    uint64_t acc = 0;
    uint32_t acc_bits = 0;
    size_t   out_pos = 0;
    for (size_t i = 0; i < n; ++i) {
        put_field(acc, acc_bits, pos[i],     bits_pos, out_packed, out_pos);
        put_field(acc, acc_bits, len[i],     bits_len, out_packed, out_pos);
        put_field(acc, acc_bits, literal[i], bits_lit, out_packed, out_pos);
    }
    if (acc_bits > 0) {
        out_packed[out_pos++] = static_cast<uint8_t>(acc & 0xFFu);
    }
}

void unpack_triples(const std::vector<uint8_t>& packed,
                    uint32_t bits_pos,
                    uint32_t bits_len,
                    uint32_t bits_lit,
                    size_t n_triples,
                    std::vector<uint32_t>& out_pos,
                    std::vector<uint32_t>& out_len,
                    std::vector<uint32_t>& out_literal) {
    if (bits_pos > 32 || bits_len > 32 || bits_lit > 32) {
        throw std::runtime_error("bitpack_mixed: per-field bits > 32 not supported");
    }
    const uint32_t total_bits = bits_pos + bits_len + bits_lit;
    if (total_bits > 64) {
        throw std::runtime_error("bitpack_mixed: total bits per triple > 64 not supported");
    }
    // resize (not assign): every element is written below, so skip the redundant
    // zero-fill — and when the caller pre-sizes these (decode-timer contract: scratch
    // allocated OUTSIDE the timer) resize(n) is a no-op, keeping the timed region
    // free of allocation. (total_bits==0 path still needs them zeroed.)
    if (total_bits == 0) {
        out_pos.assign(n_triples, 0u);
        out_len.assign(n_triples, 0u);
        out_literal.assign(n_triples, 0u);
        return;
    }
    out_pos.resize(n_triples);
    out_len.resize(n_triples);
    out_literal.resize(n_triples);
    if (n_triples == 0) return;

    // FAIRNESS / speed: a whole triple is <= 64 bits, so each one is read with a
    // SINGLE 64-bit load (+ at most one straddle byte) and three mask/shifts —
    // not a byte-by-byte accumulator refill per field
    // (3 refill loops per triple). This is the mixed-width analogue of the
    // batched extract16 in bitpack.h, so RLZ's bit-unpacking is not slower
    // than OnPair's purely because of this shared utility.
    auto field_mask = [](uint32_t bits) -> uint64_t {
        return bits == 0 ? 0 : (bits == 32 ? ~uint64_t{0} : ((uint64_t{1} << bits) - 1));
    };
    const uint64_t mask_pos = field_mask(bits_pos);
    const uint64_t mask_len = field_mask(bits_len);
    const uint64_t mask_lit = field_mask(bits_lit);
    const uint8_t* base = packed.data();
    const size_t   psz  = packed.size();

    // Fast bulk path: read while a uint64 load (+1 straddle byte) at the triple's
    // bit offset is guaranteed in-bounds (b + 9 <= psz). The trailing few triples
    // (whose 64-bit window would read past the buffer) are finished scalar.
    size_t i = 0;
    for (; i < n_triples; ++i) {
        const uint64_t bitpos = static_cast<uint64_t>(i) * total_bits;
        const size_t   b      = static_cast<size_t>(bitpos >> 3);
        if (b + 9 > psz) break;
        const uint32_t shift = static_cast<uint32_t>(bitpos & 7);
        uint64_t lo;
        std::memcpy(&lo, base + b, 8);
        uint64_t val = lo >> shift;
        if (shift != 0 && shift + total_bits > 64) {
            val |= static_cast<uint64_t>(base[b + 8]) << (64 - shift);
        }
        out_pos[i]     = static_cast<uint32_t>(val & mask_pos);
        out_len[i]     = static_cast<uint32_t>((val >> bits_pos) & mask_len);
        out_literal[i] = static_cast<uint32_t>((val >> (bits_pos + bits_len)) & mask_lit);
    }

    // Scalar tail starting at bit offset i*total_bits (handles arbitrary sub-byte
    // start; no over-read past the packed buffer).
    if (i < n_triples) {
        const uint64_t bitpos = static_cast<uint64_t>(i) * total_bits;
        size_t   in_pos   = static_cast<size_t>(bitpos >> 3);
        uint64_t acc      = 0;
        uint32_t acc_bits = 0;
        const uint32_t skip = static_cast<uint32_t>(bitpos & 7);
        while (acc_bits < skip && in_pos < psz) {
            acc |= static_cast<uint64_t>(base[in_pos++]) << acc_bits;
            acc_bits += 8;
        }
        acc >>= skip; acc_bits -= skip;
        auto take = [&](uint32_t bits, uint64_t mask) -> uint32_t {
            if (bits == 0) return 0;
            while (acc_bits < bits && in_pos < psz) {
                acc |= static_cast<uint64_t>(base[in_pos++]) << acc_bits;
                acc_bits += 8;
            }
            const uint32_t v = static_cast<uint32_t>(acc & mask);
            acc >>= bits; acc_bits -= bits;
            return v;
        };
        for (; i < n_triples; ++i) {
            out_pos[i]     = take(bits_pos, mask_pos);
            out_len[i]     = take(bits_len, mask_len);
            out_literal[i] = take(bits_lit, mask_lit);
        }
    }
}

} // namespace onpair_bench::bitpack_mixed
