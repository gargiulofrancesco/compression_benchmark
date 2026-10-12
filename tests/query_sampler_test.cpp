// Unit test for the shared query-index generator
// (runners/core/query_sampler.h). This is the component the A6 ra_latency ops
// reuse across every baseline, so it is verified once here, independently of any
// runner: parse correctness, in-range output, determinism, the per-distribution
// concentration shape, and the identity↔scramble layout bracket semantics.

#include "query_sampler.h"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <vector>

using namespace onpair_bench;

namespace {

// Fraction of draws whose index falls in [lo, hi).
double frac_in(const std::vector<uint64_t>& v, uint64_t lo, uint64_t hi) {
    uint64_t c = 0;
    for (uint64_t x : v) if (x >= lo && x < hi) ++c;
    return static_cast<double>(c) / static_cast<double>(v.size());
}

QuerySpec spec(QueryDistribution d, double s = 1.0,
               QueryLayout l = QueryLayout::Identity) {
    QuerySpec q; q.dist = d; q.zipf_s = s; q.layout = l; return q;
}

} // namespace

int main() {
    const uint64_t N = 100000;
    const uint64_t n = 200000;
    const uint64_t seed = 42;
    const uint64_t head = N / 100;  // top 1% of positions

    // ── parse_query_spec ───────────────────────────────────────────────────
    {
        auto q = parse_query_spec(R"({"query_distribution":"uniform"})");
        assert(q.dist == QueryDistribution::Uniform);
        assert(q.layout == QueryLayout::Identity);  // default

        q = parse_query_spec(R"({"query_distribution":"zipf1.0"})");
        assert(q.dist == QueryDistribution::Zipf && q.zipf_s == 1.0);
        q = parse_query_spec(R"({"query_distribution":"zipf1.5"})");
        assert(q.dist == QueryDistribution::Zipf && q.zipf_s == 1.5);
        q = parse_query_spec(R"({"query_distribution":"beta2_5"})");
        assert(q.dist == QueryDistribution::Beta);
        q = parse_query_spec(R"({"query_distribution":"poisson_n2"})");
        assert(q.dist == QueryDistribution::Poisson);

        q = parse_query_spec(R"({"query_distribution":"zipf1.0","query_layout":"scramble"})");
        assert(q.layout == QueryLayout::Scramble);

        bool threw = false;
        try { parse_query_spec(R"({"query_distribution":"weibull"})"); }
        catch (const std::invalid_argument&) { threw = true; }
        assert(threw && "unknown distribution must throw");

        threw = false;
        try { parse_query_spec(R"({"query_distribution":"uniform","query_layout":"zigzag"})"); }
        catch (const std::invalid_argument&) { threw = true; }
        assert(threw && "unknown layout must throw");
    }

    // ── determinism + range ──────────────────────────────────────────────────
    {
        auto a = generate_query_indices(spec(QueryDistribution::Zipf, 1.5), n, N, seed);
        auto b = generate_query_indices(spec(QueryDistribution::Zipf, 1.5), n, N, seed);
        assert(a == b && "same (spec, n, N, seed) must reproduce the vector");
        assert(a.size() == n);
        for (uint64_t x : a) assert(x < N);
    }

    // ── uniform: ~flat (top 1% holds ~1% of draws) ───────────────────────────
    {
        auto u = generate_query_indices(spec(QueryDistribution::Uniform), n, N, seed);
        double f = frac_in(u, 0, head);
        assert(f > 0.005 && f < 0.02 && "uniform top-1% fraction must be ~0.01");
    }

    // ── Zipf identity: head-heavy, and steeper for s=1.5 than s=1.0 ──────────
    double z10_head, z15_head;
    {
        auto z10 = generate_query_indices(spec(QueryDistribution::Zipf, 1.0), n, N, seed);
        auto z15 = generate_query_indices(spec(QueryDistribution::Zipf, 1.5), n, N, seed);
        z10_head = frac_in(z10, 0, head);
        z15_head = frac_in(z15, 0, head);
        assert(z10_head > 0.40 && "Zipf(1.0) top-1% must dominate");
        assert(z15_head > z10_head && "Zipf(1.5) must be more concentrated than Zipf(1.0)");
    }

    // ── Zipf scramble: contiguity destroyed → head fraction collapses to ~uniform
    {
        auto z15s = generate_query_indices(
            spec(QueryDistribution::Zipf, 1.5, QueryLayout::Scramble), n, N, seed);
        double f = frac_in(z15s, 0, head);
        assert(f < 0.05 && "scramble must scatter the hot set (head ~ uniform)");
        assert(f < z15_head && "scramble head fraction must be far below identity");

        // Same draws, different layout → different physical-index vectors.
        auto z15i = generate_query_indices(
            spec(QueryDistribution::Zipf, 1.5, QueryLayout::Identity), n, N, seed);
        assert(z15s != z15i && "identity vs scramble must differ");
    }

    // ── Beta(2,5) identity: mass in the lower portion (mean = 2/7 ≈ 0.286) ───
    {
        auto bt = generate_query_indices(spec(QueryDistribution::Beta), n, N, seed);
        double lower_half = frac_in(bt, 0, N / 2);
        assert(lower_half > 0.85 && "Beta(2,5) must concentrate in the lower half");
    }

    // ── Poisson(N/2) identity: tight band around the middle index ────────────
    {
        auto po = generate_query_indices(spec(QueryDistribution::Poisson), n, N, seed);
        double mid = frac_in(po, N / 2 - N / 100, N / 2 + N / 100);
        assert(mid > 0.99 && "Poisson(N/2) must concentrate near the middle");
        double at_head = frac_in(po, 0, head);
        assert(at_head < 0.001 && "Poisson(N/2) must put ~no mass at the front");
    }

    // ── degenerate inputs ────────────────────────────────────────────────────
    {
        assert(generate_query_indices(spec(QueryDistribution::Uniform), 0, N, seed).empty());
        assert(generate_query_indices(spec(QueryDistribution::Uniform), n, 0, seed).empty());
        auto one = generate_query_indices(spec(QueryDistribution::Zipf, 1.5), 10, 1, seed);
        assert(one.size() == 10);
        for (uint64_t x : one) assert(x == 0);
    }

    std::cout << "query_sampler_test: OK (z10_head=" << z10_head
              << " z15_head=" << z15_head << ")\n";
    return 0;
}
