#pragma once
// Random-access latency summary statistics.
//
// Shared by the `ra_latency` op of every runner. Each ra_latency op times N
// in-process single-string random accesses
// (no file I/O in the loop) and hands the per-sample nanosecond costs here to
// reduce them to the p50/p95/p99 (+ mean/min/max) summary reported in the
// point-access stores (ra_headline_results, a5_results, a6_results).
//
// Percentile convention: nearest-rank on the sorted sample vector
// (rank = ceil(p/100 · n), clamped to [1, n]). Deterministic, no
// interpolation — keeps the
// values reproducible across runs given a fixed query seed.

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace onpair_bench {

struct LatencyStats {
    double p50  = 0.0;
    double p95  = 0.0;
    double p99  = 0.0;
    double mean = 0.0;
    double min  = 0.0;
    double max  = 0.0;
    std::size_t n = 0;
};

// Sorts `samples` in place and returns its summary. Empty input yields a
// zero-filled struct (caller treats n==0 as a degenerate/empty column).
inline LatencyStats compute_latency_stats(std::vector<double>& samples) {
    LatencyStats s;
    s.n = samples.size();
    if (samples.empty()) return s;

    std::sort(samples.begin(), samples.end());

    auto nearest_rank = [&](double p) -> double {
        std::size_t rank = static_cast<std::size_t>(std::ceil(p / 100.0 * s.n));
        if (rank == 0)   rank = 1;
        if (rank > s.n)  rank = s.n;
        return samples[rank - 1];
    };

    s.min = samples.front();
    s.max = samples.back();
    s.p50 = nearest_rank(50.0);
    s.p95 = nearest_rank(95.0);
    s.p99 = nearest_rank(99.0);

    double acc = 0.0;
    for (double v : samples) acc += v;
    s.mean = acc / static_cast<double>(s.n);
    return s;
}

// ── Random-access latency BATCHED-CHRONO primitive — NON-SHIPPING ORACLE ─────
//
// NOTE: this batched-std::chrono primitive is NOT the
// measurement path. The shipping L_ra path is the x86_64 rdtscp timer in
// ra_timer_tsc.h (true per-access, tail-preserving). measure_ra_latency below is
// kept ONLY as a portable correctness oracle exercised by tests/ra_measure_test
// (it proves batched timing + the anti-DCE barriers defeat the 0 ns artifact on
// any clock). The runner ops call ra_measure_shipping() (rdtscp), never this.
//
// Why batching exists at all: per-access timing of a single decode with
// std::chrono is
// unreliable when the decode is sub-100 ns. On Apple-silicon steady_clock the
// tick is ~41 ns and one now() read costs ~17 ns, so a ~10 ns per-string decode
// either floors at one tick (p50 = 41 ns) or rounds to 0 ns — both artifacts,
// not measurements. Even on a fine-grained Linux clock (~1 ns tick) the ~20 ns
// vDSO read cost contaminates a 20 ns decode. The fix is the standard
// microbenchmark technique: time a BATCH of K accesses between two clock reads
// and divide by K, so the per-batch timer error (tick + read cost) is amortised
// to a small fraction. K is auto-tuned from the measured clock resolution + read
// cost, so on a coarse clock K grows and on a fine clock K shrinks toward 1.
// Identical across baselines by construction (only the `access` lambda differs).

// Anti-DCE barriers. ra_keep() forces a value to be treated as observed;
// ra_clobber_memory() forces preceding memory writes to be considered live, so a
// decode whose output is otherwise unused cannot be optimised away (which would
// also yield a spurious ~0 ns). Applied identically inside the shared loop.
inline void ra_keep(uint64_t v) {
#if defined(__GNUC__) || defined(__clang__)
    asm volatile("" : : "r,m"(v) : "memory");
#else
    volatile uint64_t sink = v; (void)sink;
#endif
}
inline void ra_clobber_memory() {
#if defined(__GNUC__) || defined(__clang__)
    asm volatile("" : : : "memory");
#endif
}

// Effective steady_clock resolution (smallest positive tick), ns.
inline double ra_clock_resolution_ns() {
    using clk = std::chrono::steady_clock;
    double best = 1e18;
    for (int i = 0; i < 256; ++i) {
        auto a = clk::now();
        clk::time_point b;
        do { b = clk::now(); } while (b == a);
        const double d = std::chrono::duration<double, std::nano>(b - a).count();
        if (d > 0.0 && d < best) best = d;
    }
    return best;
}

// Approximate cost of one now()-pair (the timer overhead a batch pays once), ns.
inline double ra_clock_overhead_ns() {
    using clk = std::chrono::steady_clock;
    const int iters = 20000;
    const auto t0 = clk::now();
    uint64_t acc = 0;
    for (int i = 0; i < iters; ++i) {
        const auto x = clk::now();
        acc += static_cast<uint64_t>(x.time_since_epoch().count());
    }
    const auto t1 = clk::now();
    ra_keep(acc);
    return std::chrono::duration<double, std::nano>(t1 - t0).count() / double(iters);
}

struct RaLatencyResult {
    LatencyStats stats;
    uint64_t     batch_k             = 1;
    double       clock_resolution_ns = 0.0;
    double       clock_overhead_ns   = 0.0;
};

// Measure random-access latency over `indices`. `access(idx)` performs ONE
// decode of string `idx` and returns a byte that depends on the decoded output
// (e.g. the last decoded byte), so the accumulated, barrier'd return value
// defeats dead-code elimination of the decode. The timed unit is a batch of K
// consecutive (sliding, wrapping) indices; per-access latency = batch_ns / K.
// One percentile sample per starting index → indices.size() samples.
template <class AccessFn>
RaLatencyResult measure_ra_latency(const std::vector<uint64_t>& indices,
                                   AccessFn&& access) {
    using clk = std::chrono::steady_clock;
    RaLatencyResult out;
    if (indices.empty()) return out;

    const size_t M = indices.size();
    out.clock_resolution_ns = ra_clock_resolution_ns();
    out.clock_overhead_ns   = ra_clock_overhead_ns();

    // Each batch must comfortably exceed BOTH the tick and the read cost so the
    // timer error is a small fraction of the batch. 64× leaves it under ~2%.
    const double target_ns =
        std::max(64.0 * out.clock_resolution_ns, 32.0 * out.clock_overhead_ns);

    // Probe per-access cost to choose K (over a fixed warmup batch).
    const uint64_t kp = static_cast<uint64_t>(std::min<size_t>(M, 4096));
    uint64_t probe_acc = 0;
    const auto p0 = clk::now();
    for (uint64_t j = 0; j < kp; ++j)
        probe_acc ^= static_cast<uint64_t>(access(indices[j % M]));
    const auto p1 = clk::now();
    ra_keep(probe_acc);
    double per_access =
        std::chrono::duration<double, std::nano>(p1 - p0).count() / double(kp);
    if (per_access <= 0.0) per_access = out.clock_resolution_ns;

    uint64_t K = static_cast<uint64_t>(std::ceil(target_ns / per_access));
    if (K < 1) K = 1;
    if (K > 100000) K = 100000;
    if (K > M) K = M;
    out.batch_k = K;

    std::vector<double> samples;
    samples.reserve(M);
    uint64_t acc = 0;
    for (size_t s = 0; s < M; ++s) {
        const auto t0 = clk::now();
        for (uint64_t k = 0; k < K; ++k)
            acc ^= static_cast<uint64_t>(access(indices[(s + k) % M]));
        const auto t1 = clk::now();
        ra_clobber_memory();
        const double batch_ns =
            std::chrono::duration<double, std::nano>(t1 - t0).count();
        samples.push_back(batch_ns / double(K));
    }
    ra_keep(acc);

    out.stats = compute_latency_stats(samples);
    return out;
}

} // namespace onpair_bench
