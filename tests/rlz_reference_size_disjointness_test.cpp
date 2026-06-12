// multi-cell tradeoff: across the three R% cells {1%, 5%, 25%},
// dict_bytes is strictly monotonic-increasing and tokens_bytes is strictly
// monotonic-decreasing.
//
// Corpus is rich enough that R always finds useful matches (tradeoff
// regime not degenerate). Column ≥ 1.7 MB so R%=1% yields K ≥ 1.

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static double metric(const OpResult& r, const std::string& name) {
    for (auto& [k, v] : r.metrics) if (k == name) return v;
    return -1.0;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rlz_disj";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";

    // ~2 MB column. Pool of 200 tokens; each row picks 6-12 tokens. At
    // R%=1% on 2 MB → R_len = 16 KB (K=1). R%=25% → R_len = 512 KB (K=32).
    std::mt19937 rng(2026);
    std::vector<std::string> pool;
    pool.reserve(200);
    for (int i = 0; i < 200; ++i) {
        std::string s;
        int L = 4 + static_cast<int>(rng() % 16);
        for (int j = 0; j < L; ++j) s += static_cast<char>('a' + (rng() % 26));
        pool.push_back(s + " ");
    }
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < 30000; ++i) {
        int n = 6 + static_cast<int>(rng() % 6);
        for (int j = 0; j < n; ++j) {
            const auto& s = pool[rng() % pool.size()];
            for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);
    std::cout << "[rlz_disj] column=" << c.bytes.size() << " bytes\n";

    RlzRunner r;
    auto run = [&](double frac) -> OpResult {
        fs::path cz = tmp / ("z_" + std::to_string(static_cast<int>(frac*100)) + ".bin");
        const std::string cfg =
            "{\"reference_size_fraction\":" + std::to_string(frac)
            + ",\"block_size\":16384,\"seed\":42,\"threads\":1,\"mem_cap_gb\":4}";
        return r.compress(in_path.string(), cz.string(), cfg);
    };
    auto r1  = run(0.01);
    auto r5  = run(0.05);
    auto r25 = run(0.25);

    if (r1.status != "ok" || r5.status != "ok" || r25.status != "ok") {
        std::cerr << "[rlz_disj] compress failed at one of the R%: "
                  << r1.status << " " << r5.status << " " << r25.status << "\n";
        return 1;
    }
    const double d1  = metric(r1,  "dict_bytes");
    const double d5  = metric(r5,  "dict_bytes");
    const double d25 = metric(r25, "dict_bytes");
    const double t1  = metric(r1,  "tokens_bytes");
    const double t5  = metric(r5,  "tokens_bytes");
    const double t25 = metric(r25, "tokens_bytes");

    std::cout << "[rlz_disj] dict   1%=" << d1 << " 5%=" << d5 << " 25%=" << d25 << "\n";
    std::cout << "[rlz_disj] tokens 1%=" << t1 << " 5%=" << t5 << " 25%=" << t25 << "\n";

    int fail = 0;
    if (!(d1 < d5 && d5 < d25)) {
        std::cerr << "[rlz_disj] dict_bytes NOT monotonic increasing\n";
        fail = 1;
    }
    if (!(t1 > t5 && t5 > t25)) {
        std::cerr << "[rlz_disj] tokens_bytes NOT monotonic decreasing\n";
        fail = 1;
    }
    if (fail == 0) {
        std::cout << "rlz_reference_size_disjointness_test: OK\n";
    }
    return fail;
}
