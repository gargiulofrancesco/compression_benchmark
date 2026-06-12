#include "bitpack.h"

#include <cstring>
#include <stdexcept>

namespace onpair_bench::bitpack {

uint32_t bits_for_vocab(uint32_t V) noexcept {
    if (V <= 1) return 0;
    uint32_t b = 0;
    uint32_t v = V - 1;
    while (v > 0) { ++b; v >>= 1; }
    return b;
}

void pack_uniform(const std::vector<uint32_t>& tokens,
                  uint32_t bits_per_token,
                  std::vector<uint8_t>& out_packed) {
    if (bits_per_token > 32) {
        throw std::runtime_error("bitpack: bits_per_token > 32 not supported");
    }
    const size_t n = tokens.size();
    if (bits_per_token == 0 || n == 0) {
        out_packed.clear();
        return;
    }
    const size_t total_bits = n * static_cast<size_t>(bits_per_token);
    const size_t total_bytes = (total_bits + 7) / 8;
    out_packed.assign(total_bytes, 0u);

    uint64_t acc = 0;          // bit accumulator (LSB-first)
    uint32_t acc_bits = 0;     // valid bits in `acc`
    size_t   out_pos = 0;
    const uint64_t mask = bits_per_token == 32
        ? ~uint64_t{0}
        : ((uint64_t{1} << bits_per_token) - 1);

    for (size_t i = 0; i < n; ++i) {
        const uint64_t t = static_cast<uint64_t>(tokens[i]) & mask;
        acc |= (t << acc_bits);
        acc_bits += bits_per_token;
        while (acc_bits >= 8) {
            out_packed[out_pos++] = static_cast<uint8_t>(acc & 0xFFu);
            acc >>= 8;
            acc_bits -= 8;
        }
    }
    if (acc_bits > 0) {
        out_packed[out_pos++] = static_cast<uint8_t>(acc & 0xFFu);
    }
}

void unpack_uniform(const std::vector<uint8_t>& packed,
                    uint32_t bits_per_token,
                    size_t n_tokens,
                    std::vector<uint32_t>& out_tokens) {
    if (bits_per_token > 32) {
        throw std::runtime_error("bitpack: bits_per_token > 32 not supported");
    }
    out_tokens.assign(n_tokens, 0u);
    if (bits_per_token == 0 || n_tokens == 0) return;

    uint64_t acc = 0;
    uint32_t acc_bits = 0;
    size_t   in_pos = 0;
    const uint64_t mask = bits_per_token == 32
        ? ~uint64_t{0}
        : ((uint64_t{1} << bits_per_token) - 1);

    for (size_t i = 0; i < n_tokens; ++i) {
        while (acc_bits < bits_per_token && in_pos < packed.size()) {
            acc |= (static_cast<uint64_t>(packed[in_pos++]) << acc_bits);
            acc_bits += 8;
        }
        if (acc_bits < bits_per_token) {
            throw std::runtime_error("bitpack: short input for unpack");
        }
        out_tokens[i] = static_cast<uint32_t>(acc & mask);
        acc >>= bits_per_token;
        acc_bits -= bits_per_token;
    }
}

} // namespace onpair_bench::bitpack
