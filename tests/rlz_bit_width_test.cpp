// RLZ mixed-width packing test. At each R% in {0.01, 0.05, 0.25},
// after compress, verify:
//   (a) the wire's packed_phrases region size matches
//       ceil(P_total * (bits_per_pos + bits_per_len + 8) / 8), with the
//       default bits_per_len == 8 (short-string len field) read from the wire;
//   (b) bits_per_pos == max(10, ceil_log2(R_len + 1));
//   (c) bit-tight identity: dict + tokens + boundaries == output_bytes.

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static double metric(const OpResult& r, const std::string& name) {
    for (auto& [k, v] : r.metrics) if (k == name) return v;
    return -1.0;
}

static uint32_t expect_bits_per_pos(uint32_t R_len) {
    uint32_t b = 0;
    uint64_t x = R_len;
    if (x == 0) b = 0;
    else { while (x > 0) { ++b; x >>= 1; } }
    if (b < 10u) b = 10u;
    if (b > 32u) b = 32u;
    return b;
}

static int test_one(double frac, const std::filesystem::path& tmp) {
    namespace fs = std::filesystem;
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / ("z_" + std::to_string(static_cast<int>(frac*100)) + ".bin");

    RlzRunner r;
    const std::string cfg =
        "{\"reference_size_fraction\":" + std::to_string(frac)
        + ",\"block_size\":16384,\"seed\":42,\"threads\":1,\"mem_cap_gb\":4}";
    auto res = r.compress(in_path.string(), cz_path.string(), cfg);
    if (res.status != "ok") {
        std::cerr << "[rlz_bw] frac=" << frac
                  << " status=" << res.status << " notes=" << res.notes << "\n";
        return 1;
    }
    const double dict_bytes       = metric(res, "dict_bytes");
    const double tokens_bytes     = metric(res, "tokens_bytes");
    const double boundaries_bytes = metric(res, "boundaries_bytes");
    const double output_bytes     = metric(res, "output_bytes");
    if (dict_bytes + tokens_bytes + boundaries_bytes != output_bytes) {
        std::cerr << "[rlz_bw] frac=" << frac << " bit-tight identity VIOLATED: "
                  << dict_bytes << " + " << tokens_bytes << " + "
                  << boundaries_bytes << " != " << output_bytes << "\n";
        return 1;
    }
    // Read wire header to extract P_total + bits_per_pos.
    std::ifstream f(cz_path.string(), std::ios::binary);
    uint32_t R_len = 0, N = 0, P_total = 0, bpp = 0, bpl = 0;
    f.read(reinterpret_cast<char*>(&R_len), 4);
    f.seekg(R_len, std::ios::cur);
    f.read(reinterpret_cast<char*>(&N), 4);
    f.read(reinterpret_cast<char*>(&P_total), 4);
    f.read(reinterpret_cast<char*>(&bpp), 4);
    f.read(reinterpret_cast<char*>(&bpl), 4);
    if (!f) { std::cerr << "[rlz_bw] short read on wire\n"; return 1; }
    const uint32_t expect = expect_bits_per_pos(R_len);
    if (bpp != expect) {
        std::cerr << "[rlz_bw] frac=" << frac << " bits_per_pos=" << bpp
                  << " expected=" << expect << " (R_len=" << R_len << ")\n";
        return 1;
    }
    if (bpl != 8u) {   // revised default len field width
        std::cerr << "[rlz_bw] frac=" << frac << " bits_per_len=" << bpl
                  << " expected=8\n";
        return 1;
    }
    const uint64_t expected_packed_bytes =
        (static_cast<uint64_t>(P_total) * (bpp + bpl + 8) + 7) / 8;
    if (static_cast<uint64_t>(tokens_bytes) != expected_packed_bytes) {
        std::cerr << "[rlz_bw] frac=" << frac << " tokens_bytes=" << tokens_bytes
                  << " expected=" << expected_packed_bytes << "\n";
        return 1;
    }
    std::cout << "[rlz_bw] frac=" << frac
              << " R_len=" << R_len << " bpp=" << bpp
              << " P_total=" << P_total
              << " tokens=" << tokens_bytes
              << "  bit-tight OK\n";
    return 0;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rlz_bw";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";

    // ~2 MB column (matches disjointness test).
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

    int fail = 0;
    fail |= test_one(0.01, tmp);
    fail |= test_one(0.05, tmp);
    fail |= test_one(0.25, tmp);
    if (fail == 0) std::cout << "rlz_bit_width_test: OK (mixed-width)\n";
    return fail;
}
