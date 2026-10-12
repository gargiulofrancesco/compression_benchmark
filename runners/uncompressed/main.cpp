// Uncompressed runner: the reference floor (plain bytes plus offsets).
//
// "Compression" is a verbatim copy of the input Column. Sub-counters reflect
// the natural memory cost of the Arrow-style layout the column already pays:
//   dict_bytes       = 0
//   tokens_bytes     = total payload bytes
//   boundaries_bytes = (N+1) * 4
// The payload-only ratio input_bytes / (dict_bytes + tokens_bytes) is
// therefore exactly 1.0.
//
// prefix_search delegates to runners/core/scan_prefix.h — this IS the
// canonical reference for the prefix-search check.

#include "runner_protocol.h"
#include "scan_prefix.h"
#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace onpair_bench;

namespace {

// Minimal unsigned-int field extractor over the runner cfg_json (n_samples /
// seed / warmup for ra_latency). Mirrors the other runners' JSON helpers.
uint64_t extract_u64(const std::string& cfg, const std::string& key, uint64_t def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    uint64_t v = 0; bool any = false;
    while (pos < cfg.size() && cfg[pos] >= '0' && cfg[pos] <= '9') {
        v = v * 10 + static_cast<uint64_t>(cfg[pos] - '0'); ++pos; any = true;
    }
    return any ? v : def;
}

class UncompressedRunner final : public Runner {
public:
    const char* name()    const override { return "uncompressed"; }
    const char* version() const override { return "phase3"; }
    PrefixSearchMode prefix_search_mode() const override {
        return PrefixSearchMode::Uncompressed;
    }

    OpResult compress(const std::string& in_path,
                      const std::string& out_path,
                      const std::string& /*cfg*/) override {
        Column c = read_column(in_path);                       // UNTIMED: file read
        const size_t n     = c.num_rows();
        const size_t total = c.bytes.size();
        // TIMED: the in-RAM "compression" only (identity copy into a
        // pre-allocated buffer). No file I/O, no allocation inside the timer —
        // mirrors the canonical decompress(buffer) measurement contract.
        Column out;
        out.bytes.resize(total);
        out.offsets.resize(n + 1);
        const auto t0 = std::chrono::steady_clock::now();
        std::memcpy(out.bytes.data(), c.bytes.data(), total);
        std::memcpy(out.offsets.data(), c.offsets.data(), (n + 1) * sizeof(uint32_t));
        const uint64_t t_compress_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());
        write_column(out_path, out);                           // UNTIMED: file write
        OpResult r;
        r.metrics.emplace_back("dict_bytes",       0.0);
        r.metrics.emplace_back("tokens_bytes",     static_cast<double>(total));
        r.metrics.emplace_back("boundaries_bytes", static_cast<double>((n + 1) * 4));
        r.metrics.emplace_back("input_bytes",      static_cast<double>(total));
        r.metrics.emplace_back("num_rows",         static_cast<double>(n));
        r.metrics.emplace_back("t_compress_ns",    static_cast<double>(t_compress_ns));
        return r;
    }

    OpResult bulk_decode(const std::string& in_path,
                         const std::string& out_path,
                         const std::string& /*cfg*/) override {
        Column c = read_column(in_path);                       // UNTIMED: file read
        const size_t n     = c.num_rows();
        const size_t total = c.bytes.size();
        // TIMED: pure decode into a PRE-ALLOCATED buffer (the ~memcpy floor).
        Column out;
        out.bytes.resize(total);
        out.offsets.resize(n + 1);
        const auto t0 = std::chrono::steady_clock::now();
        std::memcpy(out.bytes.data(), c.bytes.data(), total);
        std::memcpy(out.offsets.data(), c.offsets.data(), (n + 1) * sizeof(uint32_t));
        const uint64_t t_decode_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());
        write_column(out_path, out);                           // UNTIMED: file write
        OpResult r;
        r.metrics.emplace_back("output_bytes", static_cast<double>(total));
        r.metrics.emplace_back("num_rows",     static_cast<double>(n));
        r.metrics.emplace_back("t_decode_ns",  static_cast<double>(t_decode_ns));
        return r;
    }

    OpResult random_decode(const std::string& in_path,
                           size_t idx,
                           const std::string& out_path,
                           const std::string& /*cfg*/) override {
        Column c = read_column(in_path);
        if (idx >= c.num_rows()) {
            OpResult r;
            r.status = "crash";
            r.notes  = "idx out of range: " + std::to_string(idx);
            return r;
        }
        auto s = c.at(idx);
        std::ofstream f(out_path, std::ios::binary);
        if (!f) throw std::runtime_error("random_decode: cannot open " + out_path);
        if (!s.empty()) f.write(s.data(), static_cast<std::streamsize>(s.size()));
        OpResult r;
        r.metrics.emplace_back("output_bytes", static_cast<double>(s.size()));
        return r;
    }

    OpResult prefix_search(const std::string& in_path,
                           const std::string& needle,
                           const std::string& out_path,
                           const std::string& /*cfg*/) override {
        Column c = read_column(in_path);                       // UNTIMED: file read
        std::vector<uint32_t> hits;
        const auto t0 = std::chrono::steady_clock::now();
        size_t n_hits = scan_prefix(c, needle, hits);
        const uint64_t t_scan_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());

        // UNTIMED: hit-list materialisation is part of scan_prefix above, but
        // serialising that list is not part of the in-memory predicate cost.
        std::ofstream f(out_path, std::ios::binary);
        if (!f) throw std::runtime_error("prefix_search: cannot open " + out_path);
        uint32_t nh = static_cast<uint32_t>(n_hits);
        f.write(reinterpret_cast<const char*>(&nh), sizeof(nh));
        if (!hits.empty()) {
            f.write(reinterpret_cast<const char*>(hits.data()),
                    static_cast<std::streamsize>(hits.size() * sizeof(uint32_t)));
        }
        OpResult r;
        r.metrics.emplace_back("n_hits",             static_cast<double>(n_hits));
        r.metrics.emplace_back("num_rows",           static_cast<double>(c.num_rows()));
        r.metrics.emplace_back("needle_bytes",       static_cast<double>(needle.size()));
        r.metrics.emplace_back("t_query_prepare_ns", 0.0);
        r.metrics.emplace_back("t_scan_ns",          static_cast<double>(t_scan_ns));
        r.metrics.emplace_back("t_search_ns",        static_cast<double>(t_scan_ns));
        return r;
    }

    // Random-access latency FLOOR (the Raw row of Table III). The uncompressed model is
    // per-string by construction: row i is a direct (offset[i], offset[i+1])
    // slice — no codec, just offset arithmetic + the payload read. This is the
    // lower bound every compressed per-string baseline is measured against.
    // Same rdtscp primitive + query sampler as the other ra_latency runners; no
    // file I/O in the timed loop. Returns refused-env unless on x86-64 with
    // an invariant TSC.
    OpResult ra_latency(const std::string& in_path,
                        const std::string& /*out_path*/,
                        const std::string& cfg_json) override {
        try {
            Column c = read_column(in_path);
            const size_t N = c.num_rows();
            if (N == 0)
                return OpResult{.status = "crash",
                                .notes  = "uncompressed: empty column for ra_latency"};

            uint64_t n_samples = extract_u64(cfg_json, "n_samples", 1000000);
            if (n_samples == 0) n_samples = 1;
            uint64_t seed   = extract_u64(cfg_json, "seed", 42);
            bool     warmup = extract_u64(cfg_json, "warmup", 1) != 0;

            if (warmup) {                       // warm the payload pages
                uint64_t s = 0;
                for (size_t i = 0; i < N; ++i) {
                    auto sv = c.at(i);
                    if (!sv.empty()) s ^= static_cast<unsigned char>(sv.back());
                }
                ra_keep(s);
            }

            const QuerySpec qspec = parse_query_spec(cfg_json);
            const std::vector<uint64_t> indices =
                generate_query_indices(qspec, n_samples, N, seed);

            RaLatencyResult m;
            std::string ra_reason;
            if (!ra_measure_shipping(indices,
                [&](uint64_t idx64) -> unsigned char {
                    auto sv = c.at(static_cast<size_t>(idx64));   // direct slice
                    return sv.empty() ? 0 : static_cast<unsigned char>(sv.back());
                }, m, ra_reason)) {
                return OpResult{.status = "refused-env", .notes = "uncompressed: " + ra_reason};
            }
            if (m.stats.n > 0 && m.stats.p50 < 1.0) {
                return OpResult{.status = "crash",
                    .notes = "uncompressed: implausible sub-ns p50 (latency primitive failure)"};
            }
            const LatencyStats& st = m.stats;
            OpResult r;
            r.metrics.emplace_back("l_ra_p50_ns",  st.p50);
            r.metrics.emplace_back("l_ra_p95_ns",  st.p95);
            r.metrics.emplace_back("l_ra_p99_ns",  st.p99);
            r.metrics.emplace_back("l_ra_mean_ns", st.mean);
            r.metrics.emplace_back("l_ra_min_ns",  st.min);
            r.metrics.emplace_back("l_ra_max_ns",  st.max);
            r.metrics.emplace_back("n_samples",    static_cast<double>(st.n));
            r.metrics.emplace_back("batch_k",      static_cast<double>(m.batch_k));
            r.metrics.emplace_back("clock_resolution_ns", m.clock_resolution_ns);
            r.metrics.emplace_back("num_rows",     static_cast<double>(N));
            return r;
        } catch (const std::bad_alloc&) {
            return OpResult{.status = "oom", .notes = "uncompressed: bad_alloc ra_latency"};
        } catch (const std::exception& e) {
            return OpResult{.status = "crash", .notes = std::string("uncompressed: ") + e.what()};
        }
    }
};

} // namespace

int main(int /*argc*/, char** /*argv*/) {
    const char* env_sha = std::getenv("BASELINE_BUILD_FLAGS_SHA");
    std::string sha = env_sha ? env_sha : "unset";
    UncompressedRunner r;
    return run_protocol_loop(r, sha);
}
