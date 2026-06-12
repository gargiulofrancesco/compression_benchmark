#include "rlz_core.h"

#include "divsufsort.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>

namespace onpair_bench::rlz_core {

namespace {

// Compare row[p..p+len) against R[sa..sa+len). Returns:
//   <0 if row < R-suffix
//   >0 if row > R-suffix
//    0 if equal up to min(remaining-row, remaining-R)
int suffix_compare(const uint8_t* row, size_t row_remaining,
                   const uint8_t* R,   size_t R_remaining) noexcept {
    const size_t cmp_len = std::min(row_remaining, R_remaining);
    if (cmp_len > 0) {
        const int c = std::memcmp(row, R, cmp_len);
        if (c != 0) return c;
    }
    // Tie: shorter one is smaller. row "shorter than R-suffix" → row < R-suffix.
    if (row_remaining < R_remaining) return -1;
    if (row_remaining > R_remaining) return 1;
    return 0;
}

// Find the longest prefix of row[0..row_len) that occurs in R[0..R_len)
// using SA binary search. Returns (best_pos, best_len). If no match
// (i.e., row[0] not in R alphabet), returns (0, 0).
//
// Algorithm: SA gives suffixes of R in lex order. Binary-search to find the
// range of SA entries whose suffix starts with row[0..min(row_len, R_len)).
// The longest-match candidate is at the lex-bracket of the search; we then
// take the lcp with row at one of the bracketing SA entries.
//
// This is O(log R_len · max_match_len) per position — sufficient for smoke
// + small-to-medium columns. optimisation candidate
// (precompute LCP / use sa_search-style augmented search) but not required
// for correctness.
std::pair<uint32_t, uint32_t>
longest_match(const uint8_t* row, size_t row_len,
              const uint8_t* R, size_t R_len,
              const int32_t* SA, uint32_t max_match_len) noexcept {
    if (row_len == 0 || R_len == 0) return {0u, 0u};

    // Binary-search SA for the row prefix as a key. We find the rightmost
    // index `lo` such that R[SA[lo]..] <= row[0..min(row_len,...)] in lex
    // order. Then candidates are SA[lo] and SA[lo+1].
    int32_t lo = 0;
    int32_t hi = static_cast<int32_t>(R_len);
    while (lo < hi) {
        const int32_t mid = lo + (hi - lo) / 2;
        const int32_t sa = SA[mid];
        const size_t r_rem = R_len - static_cast<size_t>(sa);
        // Compare row[0..min(row_len, max_match_len)) vs R[sa..]
        const size_t row_cmp_len = std::min<size_t>(row_len, max_match_len);
        const int c = suffix_compare(R + sa, r_rem, row, row_cmp_len);
        // c < 0: R-suffix < row → mid is a candidate lower bound; move right.
        // c >= 0: R-suffix >= row → move left.
        if (c < 0) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }

    // Check SA[lo-1] and SA[lo] (bracketing the insertion point) — whichever
    // shares the longer prefix with `row` is the longest-match.
    uint32_t best_pos = 0;
    uint32_t best_len = 0;
    auto consider = [&](int32_t mid) {
        if (mid < 0 || mid >= static_cast<int32_t>(R_len)) return;
        const int32_t sa = SA[mid];
        const size_t r_rem = R_len - static_cast<size_t>(sa);
        const size_t cap = std::min<size_t>(
            {row_len, r_rem, static_cast<size_t>(max_match_len)});
        size_t k = 0;
        while (k < cap && row[k] == R[sa + k]) ++k;
        if (k > best_len) {
            best_len = static_cast<uint32_t>(k);
            best_pos = static_cast<uint32_t>(sa);
        }
    };
    consider(lo - 1);
    consider(lo);
    return {best_pos, best_len};
}

} // anonymous

uint32_t compute_K(uint64_t L, double reference_size_fraction,
                   uint32_t block_size) noexcept {
    if (block_size == 0) return 0;
    if (reference_size_fraction <= 0.0) return 0;
    if (L < block_size) return 0;
    // K = ceil(reference_size_fraction * L / block_size)
    const double raw = reference_size_fraction * static_cast<double>(L)
                       / static_cast<double>(block_size);
    if (raw <= 0.0) return 0;
    uint64_t k = static_cast<uint64_t>(raw);
    if (static_cast<double>(k) < raw) k += 1;  // ceil
    // Also clamp K so K*block_size doesn't exceed L (sampling without
    // wrap-around requires offsets in [0, L - block_size]).
    // For a reasonable headline (R% <= 25%, L large), this is always safe;
    // for tiny corpora the refused-env gate fires earlier in compute_K
    // (L < block_size). Belt-and-braces clamp:
    const uint64_t max_K = L / block_size;
    if (k > max_K) k = max_K;
    return static_cast<uint32_t>(k);
}

void sample_reference(const uint8_t* corpus, uint64_t L,
                      uint32_t K, uint32_t block_size, uint32_t seed,
                      std::vector<uint8_t>& out_R) {
    if (K == 0) {
        throw std::runtime_error("rlz_core: sample_reference called with K=0");
    }
    if (L < block_size) {
        throw std::runtime_error("rlz_core: sample_reference L < block_size");
    }
    const uint64_t max_off = L - block_size;  // inclusive upper bound
    out_R.assign(static_cast<size_t>(K) * block_size, 0u);
    std::mt19937 rng(seed);
    std::uniform_int_distribution<uint64_t> dist(0, max_off);
    for (uint32_t k = 0; k < K; ++k) {
        const uint64_t off = dist(rng);
        std::memcpy(out_R.data() + static_cast<size_t>(k) * block_size,
                    corpus + off, block_size);
    }
}

void build_sa(const std::vector<uint8_t>& R,
              std::vector<int32_t>& out_SA) {
    const size_t n = R.size();
    out_SA.assign(n, 0);
    if (n == 0) return;
    if (n > static_cast<size_t>(INT32_MAX)) {
        throw std::runtime_error("rlz_core: R_len exceeds 32-bit SA capacity");
    }
    const saint_t rc = divsufsort(reinterpret_cast<const sauchar_t*>(R.data()),
                                   out_SA.data(),
                                   static_cast<saidx_t>(n));
    if (rc != 0) {
        throw std::runtime_error("rlz_core: divsufsort returned non-zero");
    }
}

size_t factor_parse_row(const uint8_t* row, size_t row_len,
                        const uint8_t* R, size_t R_len,
                        const int32_t* SA,
                        uint32_t max_match_len,
                        std::vector<Factor>& out_factors) {
    size_t emitted = 0;
    size_t p = 0;
    while (p < row_len) {
        auto [m_pos, m_len] = longest_match(row + p, row_len - p,
                                             R, R_len, SA, max_match_len);
        // Cap match by kMaxMatchLen (already enforced by longest_match) and
        // by remaining row length so that we always have a literal byte at
        // row[p + m_len] (or end-of-row).
        // If m_len == row_len - p (match runs to end-of-row), the literal
        // byte is undefined; canonical Hoobin 2011 handles this by emitting
        // (pos, len) with no literal — but to keep the wire shape uniform
        // we instead emit a literal=0 and document that the decoder must
        // know to stop after this factor's pos+len bytes. Because we encode
        // row length via phrase_offsets[i+1] - phrase_offsets[i] indirectly
        // (no, we don't), we instead enforce that m_len always leaves at
        // least one byte for the literal:
        if (m_len > 0 && p + m_len >= row_len) {
            // Truncate so a literal exists.
            m_len = static_cast<uint32_t>(row_len - p - 1);
        }
        Factor f;
        if (m_len == 0) {
            // Literal-only factor.
            f.pos = 0;
            f.len = 0;
            f.literal = row[p];
            ++p;
        } else {
            f.pos = m_pos;
            f.len = m_len;
            f.literal = row[p + m_len];
            p += static_cast<size_t>(m_len) + 1;
        }
        out_factors.push_back(f);
        ++emitted;
    }
    return emitted;
}

void decode_row(const Factor* factors, size_t n_factors,
                const uint8_t* R, size_t R_len,
                std::vector<uint8_t>& out_bytes) {
    for (size_t i = 0; i < n_factors; ++i) {
        const Factor& f = factors[i];
        if (f.len > 0) {
            if (static_cast<size_t>(f.pos) + f.len > R_len) {
                throw std::runtime_error(
                    "rlz_core: decode_row factor out of R range");
            }
            out_bytes.insert(out_bytes.end(),
                              R + f.pos, R + f.pos + f.len);
        }
        out_bytes.push_back(f.literal);
    }
}

} // namespace onpair_bench::rlz_core
