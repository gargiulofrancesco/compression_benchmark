#pragma once
// Pure C++ RLZ (Relative Lempel-Ziv) algorithm core.
//: re-take on — empirical
// inspection of skuruppu/RLZ revealed a file-only API + heavy GPL-3.0-or-
// later libcds dependency. Pure C++ reimplementation following
// Hoobin/Puglisi/Zobel 2011 VLDB + Kuruppu PhD 2012 + Kuruppu/Puglisi/Zobel
// "Relative Lempel-Ziv Compression of Genomes for Large-Scale Storage and
// Retrieval" SPIRE 2010. Sole external dep: libdivsufsort 2.0.1 (MIT).
//
// Algorithm summary (Hoobin 2011 §3 + §4):
//   1) Build reference R by uniformly sampling K = ceil(R% · L / block_size)
//      blocks of block_size bytes from the concatenated corpus L = Σ |row_i|.
// Seed is mt19937(seed) — locked at 42 per.
//   2) Build SA over R via divsufsort.
//   3) For each input row, greedy LZ77 factor-parse against R: at position
//      p, find the longest prefix of row[p:] appearing in R via SA binary
//      search (matchlen >= max_match cap of 65535); emit (pos, len,
//      literal=row[p+len]) and advance p by (len + 1). Literal-only case
//      (no match at p, or end-of-row): emit (0, 0, row[p]).
//
// Per-row tokens are concatenated in column order; per-row offsets in a
// separate phrase_offsets[N+1] array (Arrow-style).

#include <cstddef>
#include <cstdint>
#include <random>
#include <vector>

namespace onpair_bench::rlz_core {

// One factor of an RLZ phrase stream.
struct Factor {
    uint32_t pos;      // position in R; 0 if literal-only
    uint32_t len;      // length of R-match; 0 if literal-only; <= 65535
    uint8_t  literal;  // byte that follows the R-match (or the literal byte
                       // when len==0). Always present (Hoobin 2011 §3.2).
};

// Compute K = ceil(reference_size_fraction · L / block_size).
// If column_size · R% < block_size, K=0 (refused-env).
uint32_t compute_K(uint64_t L,
                   double reference_size_fraction,
                   uint32_t block_size) noexcept;

// Sample K uniform random blocks of block_size bytes from corpus[0..L) using
// mt19937(seed); concatenate into out_R. out_R.size() == K * block_size.
// Throws std::runtime_error if L < block_size or K == 0.
void sample_reference(const uint8_t* corpus, uint64_t L,
                      uint32_t K, uint32_t block_size, uint32_t seed,
                      std::vector<uint8_t>& out_R);

// Build a 32-bit suffix array over R via libdivsufsort.
// out_SA.size() == R.size().
void build_sa(const std::vector<uint8_t>& R,
              std::vector<int32_t>& out_SA);

// Greedy LZ77 factor-parse of `row[0..row_len)` against (R, SA).
// Appends emitted factors to out_factors. Returns the number of factors
// appended.
// max_match_len caps each (pos, len) match to fit the wire's `len` field width
// (e.g. 255 for an 8-bit field, 65535 for 16-bit). If the natural match would be
// longer, the algorithm advances (len + 1) and emits subsequent factors as
// needed — correctness is independent of the cap.
size_t factor_parse_row(const uint8_t* row, size_t row_len,
                        const uint8_t* R, size_t R_len,
                        const int32_t* SA,
                        uint32_t max_match_len,
                        std::vector<Factor>& out_factors);

// Reconstruct row bytes from a slice of factors and reference R.
// Appends decoded bytes to out_bytes.
void decode_row(const Factor* factors, size_t n_factors,
                const uint8_t* R, size_t R_len,
                std::vector<uint8_t>& out_bytes);

} // namespace onpair_bench::rlz_core
