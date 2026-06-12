// RLZ random_decode probes — boundary + stride 313 at R%=5%.

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG_R5 =
    R"({"reference_size_fraction":0.05,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":4})";

static Column make_repetitive_column(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    static const std::vector<std::string> pool = {
        "user_id=", "session=", "ts=T", "GET /api/v1/",
        "POST /api/v1/", "alice ", "bob ", "carol "
    };
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        std::string row;
        for (int k = 0; k < 8; ++k) {
            row += pool[rng() % pool.size()];
            row += std::to_string(rng() % 1000);
        }
        for (char ch : row) c.bytes.push_back(static_cast<uint8_t>(ch));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rlz_rd";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path probe   = tmp / "p.bin";

    const uint32_t N = 3000;
    Column orig = make_repetitive_column(N, 42);
    write_column(in_path.string(), orig);

    RlzRunner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(), CFG_R5);
    if (c_res.status != "ok") {
        std::cerr << "[rlz_random_decode] compress failed: " << c_res.notes << "\n";
        return 1;
    }

    // Boundary indices.
    std::vector<size_t> probes = {0, 1, 2, N / 2, N - 2, N - 1};
    for (size_t i = 313; i < N; i += 313) probes.push_back(i);

    int fail = 0;
    for (size_t idx : probes) {
        auto d = r.random_decode(cz_path.string(), idx, probe.string(), CFG_R5);
        if (d.status != "ok") {
            std::cerr << "[rlz_random_decode] idx=" << idx
                      << " status=" << d.status << " notes=" << d.notes << "\n";
            ++fail; continue;
        }
        std::ifstream f(probe.string(), std::ios::binary);
        std::string actual((std::istreambuf_iterator<char>(f)),
                            std::istreambuf_iterator<char>());
        std::string_view want = orig.at(idx);
        if (actual != std::string(want)) {
            std::cerr << "[rlz_random_decode] idx=" << idx
                      << " mismatch (want " << want.size()
                      << " got " << actual.size() << ")\n";
            ++fail;
        }
    }
    if (fail == 0) std::cout << "rlz_random_decode_test: OK (" << probes.size()
                              << " probes)\n";
    return fail;
}
