// "decode unification" validation: LZ4_decompress_safe must decode wires
// produced by fast and HC tiers byte-identically (LZ4HC decodes via the same
// path as fast).

#include "lz4_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>

using namespace onpair_bench;

static Column make_column(uint32_t n_rows, uint32_t mean_len, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> len_d(mean_len, mean_len / 4.0);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        int raw_len = static_cast<int>(std::round(len_d(rng)));
        if (raw_len < 1) raw_len = 1;
        for (int j = 0; j < raw_len; ++j) {
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static int decode_one(const std::string& cfg, Column& out, const Column& original) {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_lz4_xt";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / ("z_" + std::to_string(std::hash<std::string>{}(cfg)) + ".bin");
    fs::path rt_path = tmp / ("rt_" + std::to_string(std::hash<std::string>{}(cfg)) + ".col");
    write_column(in_path.string(), original);
    Lz4Runner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(), cfg);
    if (c_res.status != "ok") return 1;
    auto d_res = r.bulk_decode(cz_path.string(), rt_path.string(), cfg);
    if (d_res.status != "ok") return 2;
    out = read_column(rt_path.string());
    return 0;
}

int main() {
    Column original = make_column(3000, 80, 7);

    const std::vector<std::string> configs = {
        R"({"tier":"fast","accel":1,"block_budget_bytes":65536})",
        R"({"tier":"hc","level":9,"block_budget_bytes":65536})",
        R"({"tier":"hc","level":12,"block_budget_bytes":65536})",
    };
    for (const auto& cfg : configs) {
        Column got;
        int rc = decode_one(cfg, got, original);
        if (rc != 0) {
            std::cerr << "decode_one rc=" << rc << " cfg=" << cfg << "\n";
            return 10 + rc;
        }
        if (got.bytes != original.bytes || got.offsets != original.offsets) {
            std::cerr << "cross-tier mismatch at cfg=" << cfg
                      << " (LZ4_decompress_safe disagrees with original)\n";
            return 20;
        }
    }
    std::cout << "lz4_cross_tier_decode_test: OK (3 producer tiers, unified decoder)\n";
    return 0;
}
