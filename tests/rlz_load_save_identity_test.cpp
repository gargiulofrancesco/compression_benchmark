// RLZ load/save identity (RNG-seed determinism gate).
// At R%=5% + seed=42, two consecutive compress invocations must produce
// bytewise-identical wire output.

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG_R5 =
    R"({"reference_size_fraction":0.05,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":4})";

static Column make_column(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        int L = 80 + static_cast<int>(rng() % 40);
        for (int j = 0; j < L; ++j) {
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static std::vector<uint8_t> read_all(const std::string& p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)),
                                 std::istreambuf_iterator<char>());
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rlz_lsi";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz1 = tmp / "z1.bin";
    fs::path cz2 = tmp / "z2.bin";

    Column orig = make_column(4000, 7);
    write_column(in_path.string(), orig);

    RlzRunner r;
    auto r1 = r.compress(in_path.string(), cz1.string(), CFG_R5);
    auto r2 = r.compress(in_path.string(), cz2.string(), CFG_R5);
    if (r1.status != "ok" || r2.status != "ok") {
        std::cerr << "[rlz_load_save_identity] compress failed: "
                  << r1.notes << " | " << r2.notes << "\n";
        return 1;
    }
    auto b1 = read_all(cz1.string());
    auto b2 = read_all(cz2.string());
    if (b1 != b2) {
        std::cerr << "[rlz_load_save_identity] determinism violated: "
                  << b1.size() << " vs " << b2.size() << " bytes\n";
        return 2;
    }
    std::cout << "rlz_load_save_identity_test: OK (deterministic; "
              << b1.size() << " bytes)\n";
    return 0;
}
