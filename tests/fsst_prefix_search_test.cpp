// G6 self-consistency: fsst's prefix_search hit-set must match the canonical
// scan_prefix output bytewise on the 16 fixed needles.

#include "fsst_runner.h"
#include "runner_protocol.h"
#include "scan_prefix.h"

#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static Column make_column_with_needles(uint32_t n_rows, uint32_t mean_len,
                                       uint32_t seed,
                                       const std::vector<std::string>& needles) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> len_d(mean_len, mean_len / 4.0);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        std::string s;
        if ((rng() % 100) < 30) {
            s = needles[rng() % needles.size()];
        }
        int raw_len = static_cast<int>(std::round(len_d(rng)));
        if (raw_len < 1) raw_len = 1;
        for (int j = 0; j < raw_len; ++j) {
            s.push_back(static_cast<char>('a' + (rng() % 26)));
        }
        c.bytes.insert(c.bytes.end(), s.begin(), s.end());
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

int main() {
    namespace fs = std::filesystem;
    const std::vector<std::string> needles = {
        "a", "the", "http://", "https://", "www.", ".com", ".org", "2026",
        "SELECT", "INSERT", "user_", "log_", "ERROR", "WARN", "{\"id\":", "</tag>",
    };
    fs::path tmp = fs::temp_directory_path() / "onpair_fsst_ps";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path hits_path = tmp / "hits.bin";

    Column original = make_column_with_needles(3000, 64, 99, needles);
    write_column(in_path.string(), original);
    FsstRunner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(),
                            R"({"variant":"fsst","training":"full_column"})");
    if (c_res.status != "ok") {
        std::cerr << "compress failed\n";
        return 1;
    }

    int n_checked = 0;
    for (const auto& needle : needles) {
        auto ps = r.prefix_search(cz_path.string(), needle, hits_path.string(), "{}");
        if (ps.status != "ok") {
            std::cerr << "prefix_search status=" << ps.status << " for needle=" << needle << "\n";
            return 2;
        }
        std::ifstream f(hits_path, std::ios::binary);
        uint32_t nh = 0;
        f.read(reinterpret_cast<char*>(&nh), 4);
        std::vector<uint32_t> got_hits(nh);
        if (nh > 0) f.read(reinterpret_cast<char*>(got_hits.data()), nh * 4);

        std::vector<uint32_t> ref;
        scan_prefix(original, needle, ref);
        if (got_hits != ref) {
            std::cerr << "G6 mismatch on needle='" << needle << "' got_n=" << nh
                      << " ref_n=" << ref.size() << "\n";
            return 3;
        }
        ++n_checked;
    }
    fs::remove_all(tmp);
    std::cout << "fsst_prefix_search_test: OK (" << n_checked << "/16 needles)\n";
    return 0;
}
