// OnPair random_decode — boundary + stride-313 probes at bits=16.
// Every probed row must decode bytewise to its original.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG =
    R"({"bits":16,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rand";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path out_path = tmp / "row.bin";

    std::mt19937 rng(2026);
    Column c;
    c.offsets.push_back(0);
    constexpr uint32_t N = 5000;
    for (uint32_t i = 0; i < N; ++i) {
        size_t L = 16 + (rng() % 96);
        for (size_t j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    OnPairRunner r;
    auto res = r.compress(in_path.string(), cz_path.string(), CFG);
    if (res.status != "ok") { std::cerr << "compress failed: " << res.notes << "\n"; return 1; }

    std::vector<size_t> probes = {0, 1, 2, N - 1, N - 2};
    for (size_t i = 5; i < N; i += 313) probes.push_back(i);

    int fails = 0;
    for (size_t i : probes) {
        auto rr = r.random_decode(cz_path.string(), i, out_path.string(), "{}");
        if (rr.status != "ok") {
            std::cerr << "random_decode[" << i << "] status=" << rr.status << "\n";
            ++fails;
            continue;
        }
        std::ifstream f(out_path.string(), std::ios::binary);
        std::vector<uint8_t> got((std::istreambuf_iterator<char>(f)),
                                  std::istreambuf_iterator<char>());
        size_t exp_len = c.offsets[i + 1] - c.offsets[i];
        if (got.size() != exp_len ||
            std::memcmp(got.data(), c.bytes.data() + c.offsets[i], exp_len) != 0) {
            std::cerr << "random_decode[" << i << "] bytewise mismatch (exp_len="
                      << exp_len << " got_len=" << got.size() << ")\n";
            ++fails;
        }
    }
    std::cout << "[random_decode] " << probes.size() << " probes; fails=" << fails << "\n";
    return fails ? 1 : 0;
}
