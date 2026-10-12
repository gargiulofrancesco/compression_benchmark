#pragma once
// In-process query-index generation for the `ra_latency`
// op. A6 sweeps the query distribution over {uniform, Zipf(1.0), Zipf(1.5),
// Beta(2,5), Poisson(N/2)} and reports L_ra p50/p95/p99 for each.
//
// This is the single, baseline-agnostic, unit-tested generator shared by the
// `ra_latency` op of every format (block-based and per-string alike).
// Sharing it guarantees that the access SEQUENCE handed to each
// format is byte-for-byte identical given the same (spec, n_samples, num_rows,
// seed): the only thing that differs between baselines is the decode work being
// timed, never the queries. The index vector is built ONCE, before the timed
// region, so the distribution math (Zipf CDF build + binary search, Beta gamma
// draws, Poisson) never pollutes the measured per-access latency.
//
// LAYOUT BRACKET. For the skewed distributions a
// draw produces a *position* in popularity space; how that position maps to a
// physical string index is a separate, layout-defining choice: it decides
// whether the hot rows, and for a block baseline the hot blocks, are adjacent
// in memory. A6 reports BOTH ends of the bracket:
//   - identity : index = position. The hot set is physically CONTIGUOUS (front
//     for Zipf/Beta, middle for Poisson) — the block baselines' best case
//     (one hot block stays cached).
//   - scramble : index = π(position) for a deterministic seeded permutation π.
//     The hot set keeps its size but is SCATTERED across the column — YCSB-style
//     realistic access; the block baselines' worst case.
// The position-draw stream is identical for both layouts (same `seed` → same
// rng sequence → same positions); scramble only post-maps through π, which is
// built from an independent derived seed. So identity↔scramble is a clean
// bracket on the SAME draws. Uniform is layout-invariant (scramble of a uniform
// stream is still uniform), so the driver runs uniform under identity only.

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <numeric>
#include <random>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace onpair_bench {

enum class QueryDistribution { Uniform, Zipf, Beta, Poisson };
enum class QueryLayout { Identity, Scramble };

struct QuerySpec {
    QueryDistribution dist   = QueryDistribution::Uniform;
    double            zipf_s = 1.0;   // Zipf exponent (1.0 or 1.5); ignored otherwise
    QueryLayout       layout = QueryLayout::Identity;
};

// Minimal string-field extractor over the runner cfg_json (mirrors the
// file-local extract_string helpers in the runners; kept here so the
// distribution/layout parse is identical across every baseline).
inline std::string qs_extract_string(const std::string& cfg,
                                      const std::string& key,
                                      const std::string& def) {
    const std::string pat = "\"" + key + "\"";
    size_t k = cfg.find(pat);
    if (k == std::string::npos) return def;
    size_t c = cfg.find(':', k + pat.size());
    if (c == std::string::npos) return def;
    size_t q1 = cfg.find('"', c + 1);
    if (q1 == std::string::npos) return def;
    size_t q2 = cfg.find('"', q1 + 1);
    if (q2 == std::string::npos) return def;
    return cfg.substr(q1 + 1, q2 - q1 - 1);
}

// Parse query_distribution + query_layout from the ra_latency cfg_json.
// Canonical distribution names: "uniform", "zipf1.0", "zipf1.5", "beta2_5",
// "poisson_n2". Canonical layout names: "identity", "scramble". Throws
// std::invalid_argument on an unknown value (the runner maps that to crash).
inline QuerySpec parse_query_spec(const std::string& cfg_json) {
    QuerySpec q;
    const std::string d = qs_extract_string(cfg_json, "query_distribution", "uniform");
    const std::string l = qs_extract_string(cfg_json, "query_layout", "identity");

    if (d == "uniform") {
        q.dist = QueryDistribution::Uniform;
    } else if (d == "zipf1.0") {
        q.dist = QueryDistribution::Zipf; q.zipf_s = 1.0;
    } else if (d == "zipf1.5") {
        q.dist = QueryDistribution::Zipf; q.zipf_s = 1.5;
    } else if (d == "beta2_5") {
        q.dist = QueryDistribution::Beta;
    } else if (d == "poisson_n2") {
        q.dist = QueryDistribution::Poisson;
    } else {
        throw std::invalid_argument(
            "unknown query_distribution '" + d + "' (expected uniform | "
            "zipf1.0 | zipf1.5 | beta2_5 | poisson_n2)");
    }

    if (l == "identity") {
        q.layout = QueryLayout::Identity;
    } else if (l == "scramble") {
        q.layout = QueryLayout::Scramble;
    } else {
        throw std::invalid_argument(
            "unknown query_layout '" + l + "' (expected identity | scramble)");
    }
    return q;
}

// Build the access sequence: n_samples string indices in [0, num_rows), drawn
// from `spec.dist` and mapped through `spec.layout`. Deterministic given seed.
// Allocations here are one-time (outside the caller's timed region).
inline std::vector<uint64_t> generate_query_indices(const QuerySpec& spec,
                                                    uint64_t n_samples,
                                                    uint64_t num_rows,
                                                    uint64_t seed) {
    std::vector<uint64_t> idx;
    if (num_rows == 0 || n_samples == 0) return idx;
    idx.reserve(static_cast<size_t>(n_samples));
    const uint64_t N = num_rows;
    std::mt19937_64 rng(seed);

    // 1. Draw positions in [0, N) per the distribution.
    switch (spec.dist) {
        case QueryDistribution::Uniform: {
            std::uniform_int_distribution<uint64_t> u(0, N - 1);
            for (uint64_t s = 0; s < n_samples; ++s) idx.push_back(u(rng));
            break;
        }
        case QueryDistribution::Zipf: {
            // Cumulative weights w(r) = 1/r^s, r = 1..N (rank 1 = hottest).
            std::vector<double> cdf(static_cast<size_t>(N));
            double acc = 0.0;
            for (uint64_t r = 1; r <= N; ++r) {
                acc += 1.0 / std::pow(static_cast<double>(r), spec.zipf_s);
                cdf[static_cast<size_t>(r - 1)] = acc;
            }
            std::uniform_real_distribution<double> u(0.0, acc);
            for (uint64_t s = 0; s < n_samples; ++s) {
                const double x = u(rng);
                size_t pos = static_cast<size_t>(
                    std::lower_bound(cdf.begin(), cdf.end(), x) - cdf.begin());
                if (pos >= N) pos = static_cast<size_t>(N - 1);
                idx.push_back(static_cast<uint64_t>(pos));   // rank-1 = position
            }
            break;
        }
        case QueryDistribution::Beta: {
            // Beta(2,5) via two Gamma draws: x = g_a / (g_a + g_b) ∈ [0,1],
            // skewed toward 0 → low positions hot. position = floor(x·N).
            std::gamma_distribution<double> ga(2.0, 1.0);
            std::gamma_distribution<double> gb(5.0, 1.0);
            for (uint64_t s = 0; s < n_samples; ++s) {
                const double a = ga(rng), b = gb(rng);
                const double x = (a + b > 0.0) ? a / (a + b) : 0.0;
                uint64_t pos = static_cast<uint64_t>(std::floor(x * static_cast<double>(N)));
                if (pos >= N) pos = N - 1;
                idx.push_back(pos);
            }
            break;
        }
        case QueryDistribution::Poisson: {
            // Poisson(N/2): mass concentrated around the middle index, sd √(N/2).
            // P(X≥N) ≈ 0 for non-trivial N, so clamping the rare tail is benign.
            std::poisson_distribution<uint64_t> p(static_cast<double>(N) / 2.0);
            for (uint64_t s = 0; s < n_samples; ++s) {
                uint64_t v = p(rng);
                if (v >= N) v = N - 1;
                idx.push_back(v);
            }
            break;
        }
    }

    // 2. Apply the layout. Identity leaves positions as physical indices.
    // Scramble maps each position through a deterministic Fisher–Yates
    // permutation π built from an independent derived seed, scattering the hot
    // set across the column without changing the skew shape.
    if (spec.layout == QueryLayout::Scramble) {
        std::vector<uint64_t> perm(static_cast<size_t>(N));
        std::iota(perm.begin(), perm.end(), 0ULL);
        std::mt19937_64 prng(seed ^ 0x9E3779B97F4A7C15ULL);
        for (uint64_t i = N - 1; i > 0; --i) {
            std::uniform_int_distribution<uint64_t> sw(0, i);
            std::swap(perm[static_cast<size_t>(i)], perm[static_cast<size_t>(sw(prng))]);
        }
        for (auto& v : idx) v = perm[static_cast<size_t>(v)];
    }

    return idx;
}

} // namespace onpair_bench
