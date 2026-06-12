// zstd round-trip: build column → compress → bulk_decode → assert column equality.
// Exercises L1, L3, L9 (only L3 runs at smoke; the test covers the level surface).

#include "runner_protocol.h"
#include "zstd_runner.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>

using namespace onpair_bench;

static Column make_column(uint32_t n_rows, uint32_t mean_len, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> len_d(mean_len, mean_len / 4.0);
    Column c;
    c.offsets.reserve(n_rows + 1);
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        int raw_len = static_cast<int>(std::round(len_d(rng)));
        if (raw_len < 1) raw_len = 1;
        if (raw_len > static_cast<int>(mean_len * 3)) raw_len = mean_len * 3;
        std::string s;
        s.reserve(raw_len);
        for (int j = 0; j < raw_len; ++j) {
            s.push_back(static_cast<char>('a' + (rng() % 26)));
        }
        c.bytes.insert(c.bytes.end(), s.begin(), s.end());
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static bool columns_equal(const Column& a, const Column& b) {
    return a.bytes == b.bytes && a.offsets == b.offsets;
}

static int roundtrip_one(int level, uint32_t block_budget) {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / ("onpair_zstd_rt_" + std::to_string(level));
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path rt_path = tmp / "rt.col";

    Column original = make_column(5000, 96, 42 + level);
    write_column(in_path.string(), original);

    std::string cfg = "{\"level\":" + std::to_string(level)
                    + ",\"block_budget_bytes\":" + std::to_string(block_budget) + "}";

    ZstdRunner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(), cfg);
    if (c_res.status != "ok") {
        std::cerr << "compress status=" << c_res.status << " notes=" << c_res.notes << "\n";
        return 1;
    }
    auto d_res = r.bulk_decode(cz_path.string(), rt_path.string(), cfg);
    if (d_res.status != "ok") {
        std::cerr << "bulk_decode status=" << d_res.status << " notes=" << d_res.notes << "\n";
        return 2;
    }

    Column recovered = read_column(rt_path.string());
    if (!columns_equal(original, recovered)) {
        std::cerr << "round-trip mismatch at level=" << level << "\n";
        return 3;
    }
    fs::remove_all(tmp);
    return 0;
}

int main() {
    for (int lvl : {1, 3, 9}) {
        for (uint32_t bb : {32u * 1024u, 1u << 20}) {
            int rc = roundtrip_one(lvl, bb);
            if (rc != 0) {
                std::cerr << "FAIL level=" << lvl << " block_budget=" << bb << " rc=" << rc << "\n";
                return 10 + rc;
            }
        }
    }
    std::cout << "zstd_roundtrip_test: OK\n";
    return 0;
}
