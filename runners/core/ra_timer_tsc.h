#pragma once
// SHIPPING random-access latency measurement path.
//
// x86_64 invariant-TSC (rdtscp) timing: the L_ra path of every point-access
// measurement (x86-64 Linux). rdtscp has ~6 ns overhead and 1-cycle resolution, so a single
// access (≈10 ns per-string up to µs cache-miss) is timed PER-ACCESS (K=1),
// preserving the tail that batched std::chrono would compress. The portable
// batched-chrono primitive in latency_stats.h (measure_ra_latency) is kept ONLY
// as a NON-shipping correctness oracle (tests/ra_measure_test.cpp), never as a
// measurement path.
//
// What the timer needs, ASSERTED at startup; measurement is REFUSED (the op
// returns status="refused-env") if either is unmet:
//   - invariant TSC: constant_tsc + nonstop_tsc        (/proc/cpuinfo flags)
//   - thread pinned to one CPU                          (sched_setaffinity)
// Any x86-64 Linux machine with an invariant TSC satisfies them. The frequency
// governor and turbo are not among them: an invariant TSC ticks at a constant
// rate whatever the core's frequency, so they change how fast the timed code
// runs, not whether the timer is right. They are the setup of a measurement,
// reported by tools/check_env.py and insisted on only under STRICT_ENV=1.
//
// TSC→ns calibration: TSC cycles measured against CLOCK_MONOTONIC over a short
// window → tsc_ghz; per-access ns = (cycles − calibrated rdtscp-pair overhead) /
// tsc_ghz, floored at 0.
//
// On a non-x86 build the x86 block below is preprocessed out and the op
// refuses to measure.

#include "latency_stats.h"   // LatencyStats, compute_latency_stats, RaLatencyResult, ra_keep

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace onpair_bench {

struct RaTscEnv {
    bool        ok          = false;
    std::string reason;          // why measurement was refused (when !ok)
    double      tsc_ghz      = 0.0;   // calibrated TSC frequency (cycles/ns)
    double      overhead_ns  = 0.0;   // calibrated empty rdtscp-pair cost
    int         pinned_cpu   = -1;
};

} // namespace onpair_bench

#if defined(__x86_64__) || defined(_M_X64)

#include <x86intrin.h>
#include <sched.h>
#include <unistd.h>
#include <chrono>
#include <fstream>

namespace onpair_bench {
namespace ra_tsc_detail {

// True if /proc/cpuinfo advertises `flag` on the CPU flags line.
inline bool cpuinfo_has(const char* flag) {
    std::ifstream f("/proc/cpuinfo");
    if (!f) return false;
    std::string line;
    const std::string needle = std::string(" ") + flag;
    while (std::getline(f, line)) {
        if (line.rfind("flags", 0) == 0 || line.rfind("Features", 0) == 0) {
            // pad so the needle " flag" matches a flag at the start too
            if ((" " + line).find(needle) != std::string::npos) return true;
        }
    }
    return false;
}

inline uint64_t rdtscp_now() { unsigned aux; return __rdtscp(&aux); }

} // namespace ra_tsc_detail

// Check for an invariant TSC, pin the thread, calibrate TSC→GHz.
// Returns env.ok=false with a specific reason if any requirement is unmet.
inline RaTscEnv ra_tsc_setup() {
    using namespace ra_tsc_detail;
    RaTscEnv e;

    if (!cpuinfo_has("constant_tsc") || !cpuinfo_has("nonstop_tsc")) {
        e.reason = "invariant TSC absent (need constant_tsc + nonstop_tsc in "
                   "/proc/cpuinfo)";
        return e;
    }
    cpu_set_t set;
    CPU_ZERO(&set);
    int cpu = sched_getcpu();
    if (cpu < 0) cpu = 0;
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        e.reason = "sched_setaffinity failed (cannot pin thread to a CPU)";
        return e;
    }
    e.pinned_cpu = cpu;

    // Calibrate TSC frequency against CLOCK_MONOTONIC over ~30 ms.
    _mm_lfence();
    const uint64_t c0 = rdtscp_now();
    _mm_lfence();
    const auto w0 = std::chrono::steady_clock::now();
    while (std::chrono::duration<double>(
               std::chrono::steady_clock::now() - w0).count() < 0.03) { /* spin */ }
    _mm_lfence();
    const uint64_t c1 = rdtscp_now();
    _mm_lfence();
    const auto w1 = std::chrono::steady_clock::now();
    const double ns = std::chrono::duration<double, std::nano>(w1 - w0).count();
    if (ns <= 0.0 || c1 <= c0) { e.reason = "TSC calibration failed"; return e; }
    e.tsc_ghz = static_cast<double>(c1 - c0) / ns;   // cycles per ns

    // Calibrate the empty rdtscp-pair overhead (min over many trials).
    uint64_t ov = ~0ull;
    for (int i = 0; i < 4096; ++i) {
        _mm_lfence();
        const uint64_t a = rdtscp_now();
        _mm_lfence();
        _mm_lfence();
        const uint64_t b = rdtscp_now();
        _mm_lfence();
        if (b - a < ov) ov = b - a;
    }
    e.overhead_ns = static_cast<double>(ov) / e.tsc_ghz;

    e.ok = true;
    return e;
}

// Per-access rdtscp timing (K=1; tail preserved). The access returns a byte
// derived from the decoded output; the accumulated, barrier'd value defeats DCE.
template <class AccessFn>
RaLatencyResult measure_ra_latency_tsc(const RaTscEnv& env,
                                       const std::vector<uint64_t>& indices,
                                       AccessFn&& access) {
    RaLatencyResult out;
    out.batch_k = 1;                                   // true per-access
    out.clock_resolution_ns = (env.tsc_ghz > 0.0) ? 1.0 / env.tsc_ghz : 0.0;
    out.clock_overhead_ns = env.overhead_ns;
    if (indices.empty() || !env.ok) return out;

    std::vector<double> samples;
    samples.reserve(indices.size());
    uint64_t acc = 0;
    for (uint64_t idx : indices) {
        _mm_lfence();
        const uint64_t t0 = ra_tsc_detail::rdtscp_now();
        _mm_lfence();
        acc ^= static_cast<uint64_t>(access(idx));
        _mm_lfence();
        const uint64_t t1 = ra_tsc_detail::rdtscp_now();
        _mm_lfence();
        double ns = static_cast<double>(t1 - t0) / env.tsc_ghz - env.overhead_ns;
        if (ns < 0.0) ns = 0.0;
        samples.push_back(ns);
    }
    ra_keep(acc);
    out.stats = compute_latency_stats(samples);
    return out;
}

// Shipping entry point. Sets up the TSC env ONCE (per process), then measures.
// Returns false (with `reason`) if the env is unmet → op maps to refused-env.
template <class AccessFn>
inline bool ra_measure_shipping(const std::vector<uint64_t>& indices,
                                AccessFn&& access,
                                RaLatencyResult& out, std::string& reason) {
    static RaTscEnv env = ra_tsc_setup();
    if (!env.ok) { reason = env.reason; return false; }
    out = measure_ra_latency_tsc(env, indices, std::forward<AccessFn>(access));
    return true;
}

} // namespace onpair_bench

#else  // ── non-x86 build: no rdtscp measurement path ───────────────────────

namespace onpair_bench {

inline RaTscEnv ra_tsc_setup() {
    return RaTscEnv{false,
        "ra_latency requires an x86_64 invariant-TSC (rdtscp) build; this is a "
        "non-x86 build — measurement refused",
        0.0, 0.0, -1};
}

template <class AccessFn>
inline bool ra_measure_shipping(const std::vector<uint64_t>& /*indices*/,
                                AccessFn&& /*access*/,
                                RaLatencyResult& /*out*/, std::string& reason) {
    reason = "ra_latency requires an x86_64 invariant-TSC (rdtscp) build; this "
             "is a non-x86 build — measurement refused";
    return false;
}

} // namespace onpair_bench

#endif
