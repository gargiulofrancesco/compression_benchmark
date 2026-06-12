// random_decode under multi-block: 32 KB block budget to force several blocks
// across the synthetic 5K-row column. Probes block boundaries explicitly.

#include "lz4_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

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

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_lz4_rd";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path one_path = tmp / "one.bin";

    Column original = make_column(5000, 96, 17);
    write_column(in_path.string(), original);

    std::string cfg = R"({"tier":"fast","accel":1,"block_budget_bytes":32768})";
    Lz4Runner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(), cfg);
    if (c_res.status != "ok") {
        std::cerr << "compress status=" << c_res.status << " notes=" << c_res.notes << "\n";
        return 1;
    }
    double n_blocks = 0;
    for (auto& [k, v] : c_res.metrics) if (k == "n_blocks") n_blocks = v;
    if (n_blocks < 2) {
        std::cerr << "expected multi-block layout, got n_blocks=" << n_blocks << "\n";
        return 2;
    }

    const uint32_t N = original.num_rows();
    std::vector<uint32_t> probes = {0, N / 4, N / 2, 3 * N / 4, N - 1};
    for (uint32_t i = 200; i < N; i += 200) probes.push_back(i);

    for (uint32_t idx : probes) {
        auto d = r.random_decode(cz_path.string(), idx, one_path.string(), cfg);
        if (d.status != "ok") {
            std::cerr << "random_decode status=" << d.status << " idx=" << idx << "\n";
            return 3;
        }
        std::ifstream f(one_path, std::ios::binary);
        std::string got((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
        std::string want{original.at(idx)};
        if (got != want) {
            std::cerr << "random_decode mismatch at idx=" << idx
                      << " got_len=" << got.size() << " want_len=" << want.size() << "\n";
            return 4;
        }
    }
    fs::remove_all(tmp);
    std::cout << "lz4_random_decode_test: OK (n_blocks=" << n_blocks << ")\n";
    return 0;
}
