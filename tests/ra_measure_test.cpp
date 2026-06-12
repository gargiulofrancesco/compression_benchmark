// step 7 (A6) — unit test for the shared random-access latency
// MEASUREMENT primitive (runners/core/latency_stats.h measure_ra_latency).
//
// Regression guard for the 41 ns / 0 ns timer artifact: with a sub-tick "decode"
// the per-access result must still be a plausible NON-zero value (batched timing
// + anti-DCE), never 0. Also checks the adaptive K (small for slow accesses,
// large for fast ones), percentile ordering, and the degenerate empty case.
// This is the fairness-primitive invariant the auditor re-runs: no baseline may
// report a sub-nanosecond p50, and the timing path must be DCE-proof.

#include "latency_stats.h"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <vector>

using namespace onpair_bench;

int main() {
    // Index stream.
    std::vector<uint64_t> idx;
    const uint64_t N = 4096;
    for (uint64_t i = 0; i < 20000; ++i) idx.push_back((i * 2654435761ULL) % N);

    // A memory-touching "decode" of ~64 bytes — single-digit ns, below the
    // Apple-silicon 41 ns tick. Pre-batching this floored at 0 / 41 ns.
    std::vector<unsigned char> buf(64 * N, 0);
    auto fast_access = [&](uint64_t i) -> unsigned char {
        unsigned char* p = buf.data() + (i % N) * 64;
        unsigned char last = 0;
        for (int j = 0; j < 64; ++j) { p[j] = static_cast<unsigned char>(p[j] + j + 1); last = p[j]; }
        return last;
    };

    RaLatencyResult m = measure_ra_latency(idx, fast_access);
    assert(m.stats.n == idx.size());
    assert(m.clock_resolution_ns > 0.0);
    assert(m.batch_k >= 1);
    // The crux: a sub-tick decode must NOT report 0/sub-ns latency.
    assert(m.stats.min > 0.0 && "min must be > 0 (batched timing defeats the 0ns artifact)");
    assert(m.stats.p50 >= 1.0 && "sub-nanosecond p50 = latency-primitive failure");
    assert(m.stats.p50 <= m.stats.p95 && m.stats.p95 <= m.stats.p99
           && m.stats.p99 <= m.stats.max && "percentile ordering");
    // Fast access below the tick ⇒ K must batch up (K > 1).
    assert(m.batch_k > 1 && "fast sub-tick access must auto-tune K above 1");

    // A deliberately slow access (touch ~16 KB) — well above the tick ⇒ K small.
    std::vector<unsigned char> big(1 << 20, 1);
    auto slow_access = [&](uint64_t i) -> unsigned char {
        unsigned char s = 0;
        size_t base = (i % 16) * 4096;
        for (int j = 0; j < 16384; ++j) s = static_cast<unsigned char>(s + big[base + (j % big.size())]);
        return s;
    };
    RaLatencyResult ms = measure_ra_latency(idx, slow_access);
    assert(ms.stats.p50 > 50.0 && "a 16KB-touch access must measure well above 50 ns");
    assert(ms.batch_k <= m.batch_k && "slow access needs no more batching than the fast one");

    // Degenerate: empty index stream.
    std::vector<uint64_t> empty;
    RaLatencyResult me = measure_ra_latency(empty, fast_access);
    assert(me.stats.n == 0);

    std::cout << "ra_measure_test: OK (fast p50=" << m.stats.p50
              << "ns K=" << m.batch_k << " res=" << m.clock_resolution_ns
              << "ns | slow p50=" << ms.stats.p50 << "ns K=" << ms.batch_k << ")\n";
    return 0;
}
