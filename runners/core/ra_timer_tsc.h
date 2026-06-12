#pragma once
// step 7 (A6) — SHIPPING random-access latency measurement path.
//
// x86_64 invariant-TSC (rdtscp) timing: the headline L_ra path on the x86 Linux
// measurement rig. rdtscp has ~6 ns overhead and 1-cycle resolution, so a single
// access (≈10 ns per-string up to µs cache-miss) is timed PER-ACCESS (K=1),
// preserving the tail that batched std::chrono would compress. The portable
// batched-chrono primitive in latency_stats.h (measure_ra_latency) is kept ONLY
// as a NON-shipping correctness oracle (tests/ra_measure_test.cpp), never as a
// measurement path.
//
// Hardware requirements, ASSERTED at startup; measurement is REFUSED (the op
// returns status="refused-env") if any is unmet:
//   - invariant TSC: constant_tsc + nonstop_tsc        (/proc/cpuinfo flags)
//   - scaling governor == "performance"                (/sys .../scaling_governor)
//   - turbo disabled: intel_pstate/no_turbo==1 OR cpufreq/boost==0
//   - thread pinned to one CPU                          (sched_setaffinity)
// These are two INDEPENDENT requirements from the AVX-512 fairness constraint
// (Ice Lake / Sapphire Rapids for FSST, V-A): any server-class x86 Linux box can
// satisfy the TSC requirements; AVX-512 dispatch is verified separately and
// recorded in the environment fingerprint (rig task).
//
// TSC→ns calibration: TSC cycles measured against CLOCK_MONOTONIC over a short
// window → tsc_ghz; per-access ns = (cycles − calibrated rdtscp-pair overhead) /
// tsc_ghz, floored at 0.
//
// ⚠️ UNVERIFIED: this header was written on an arm64 (Apple) dev box where the
// x86 block is preprocessed out — it has NOT been compiled, run, or measured.
// Build/run/verify (timing correctness, TSC asserts, plausible per-access tail,
// FSST AVX-512 dispatch) is RIG-GATED ( "write rdtscp
// now, rig-verify later"). On any non-x86 build the op cleanly refuses.

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

inline std::string read_first_line(const char* path) {
    std::ifstream f(path);
    std::string s;
    if (f) std::getline(f, s);
    return s;
}

inline uint64_t rdtscp_now() { unsigned aux; return __rdtscp(&aux); }

} // namespace ra_tsc_detail

// Check the TSC measurement prerequisites, pin the thread, calibrate TSC→GHz.
// Returns env.ok=false with a specific reason if any requirement is unmet.
inline RaTscEnv ra_tsc_setup() {
    using namespace ra_tsc_detail;
    RaTscEnv e;

    if (!cpuinfo_has("constant_tsc") || !cpuinfo_has("nonstop_tsc")) {
        e.reason = "invariant TSC absent (need constant_tsc + nonstop_tsc in "
                   "/proc/cpuinfo)";
        return e;
    }
    const std::string gov = read_first_line(
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor");
    if (gov != "performance") {
        e.reason = "scaling_governor='" + gov + "' (need 'performance')";
        return e;
    }
    const std::string no_turbo =
        read_first_line("/sys/devices/system/cpu/intel_pstate/no_turbo");
    const std::string boost =
        read_first_line("/sys/devices/system/cpu/cpufreq/boost");
    if (!(no_turbo == "1" || boost == "0")) {
        e.reason = "turbo not disabled (need intel_pstate/no_turbo=1 or "
                   "cpufreq/boost=0)";
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
        "non-x86 build — measurement refused (rig-only)",
        0.0, 0.0, -1};
}

template <class AccessFn>
inline bool ra_measure_shipping(const std::vector<uint64_t>& /*indices*/,
                                AccessFn&& /*access*/,
                                RaLatencyResult& /*out*/, std::string& reason) {
    reason = "ra_latency requires an x86_64 invariant-TSC (rdtscp) build; this "
             "is a non-x86 build — measurement refused (rig-only)";
    return false;
}

} // namespace onpair_bench

#endif
