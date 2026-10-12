#pragma once
// Heterogeneous mixed-width bit-packed triple stream.
// Pack/unpack streams of (pos, len, literal) triples where each component
// has an independent bit width. LSB-first within each byte; triples written
// in increasing position order (component order within each triple: pos,
// then len, then literal). Used by the RLZ runner.

#include <cstddef>
#include <cstdint>
#include <vector>

namespace onpair_bench::bitpack_mixed {

// Round-up bytes for n_triples triples at total_bits_per_triple bits each.
inline size_t packed_bytes_for(size_t n_triples,
                               uint32_t total_bits_per_triple) noexcept {
    if (n_triples == 0 || total_bits_per_triple == 0) return 0;
    const uint64_t total_bits =
        static_cast<uint64_t>(n_triples) * total_bits_per_triple;
    return static_cast<size_t>((total_bits + 7) / 8);
}

// Pack triples at heterogeneous widths. The three input vectors MUST have
// identical size. Each component MUST satisfy its <(1 << bits_X) bound
// (caller's responsibility; not asserted in release). bits_X each in [0, 32];
// 0 packs the corresponding component as nothing (degenerate). The total
// bits_pos + bits_len + bits_lit MUST be <= 64 (single-pass shift register).
// out_packed.size() set to packed_bytes_for(n_triples, sum_bits).
void pack_triples(const std::vector<uint32_t>& pos,
                  const std::vector<uint32_t>& len,
                  const std::vector<uint32_t>& literal,
                  uint32_t bits_pos,
                  uint32_t bits_len,
                  uint32_t bits_lit,
                  std::vector<uint8_t>& out_packed);

// Unpack exactly n_triples triples from packed.
// Each output vector size set to n_triples.
void unpack_triples(const std::vector<uint8_t>& packed,
                    uint32_t bits_pos,
                    uint32_t bits_len,
                    uint32_t bits_lit,
                    size_t n_triples,
                    std::vector<uint32_t>& out_pos,
                    std::vector<uint32_t>& out_len,
                    std::vector<uint32_t>& out_literal);

} // namespace onpair_bench::bitpack_mixed
