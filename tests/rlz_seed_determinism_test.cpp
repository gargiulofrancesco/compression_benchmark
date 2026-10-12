// RNG-seed determinism. seed=42 (the only seed the runner accepts) → two
// compress invocations produce bytewise-identical wire. A different seed
// samples a different reference R.
//
// parse_rlz_config rejects seed!=42, so the second property cannot be
// checked through the runner. The test therefore verifies
// determinism by:
//   (1) running compress twice at seed=42 → identical wire.
//   (2) using rlz_core sampling helpers directly to verify that the R
//       sampling differs at seed=42 vs seed=43, demonstrating the
//       sampling is genuinely seed-dependent.

#include "rlz_runner.h"
#include "rlz_core.h"
#include "runner_protocol.h"

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static std::vector<uint8_t> read_all(const std::string& p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)),
                                 std::istreambuf_iterator<char>());
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rlz_seed";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";

    // ~600 KB column.
    Column c;
    c.offsets.push_back(0);
    std::mt19937 rng(11);
    for (uint32_t i = 0; i < 6000; ++i) {
        int L = 80 + static_cast<int>(rng() % 40);
        for (int j = 0; j < L; ++j) {
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    // (1) Two compress invocations at seed=42 must produce identical wire.
    RlzRunner r;
    const std::string cfg42 =
        R"({"reference_size_fraction":0.05,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":4})";
    fs::path cz1 = tmp / "z1.bin";
    fs::path cz2 = tmp / "z2.bin";
    auto r1 = r.compress(in_path.string(), cz1.string(), cfg42);
    auto r2 = r.compress(in_path.string(), cz2.string(), cfg42);
    if (r1.status != "ok" || r2.status != "ok") {
        std::cerr << "[rlz_seed] compress failed\n";
        return 1;
    }
    if (read_all(cz1.string()) != read_all(cz2.string())) {
        std::cerr << "[rlz_seed] seed=42 NOT deterministic across runs\n";
        return 2;
    }

    // (2) Direct rlz_core::sample_reference at seed=42 vs seed=43 must
    // produce different R (verifies sampling is genuinely seed-dependent).
    const uint64_t L = c.bytes.size();
    const uint32_t block_size = 16384;
    const uint32_t K = rlz_core::compute_K(L, 0.05, block_size);
    if (K == 0) {
        std::cerr << "[rlz_seed] K=0 on test column; bump corpus size\n";
        return 3;
    }
    std::vector<uint8_t> R_a, R_b;
    rlz_core::sample_reference(c.bytes.data(), L, K, block_size, 42, R_a);
    rlz_core::sample_reference(c.bytes.data(), L, K, block_size, 43, R_b);
    if (R_a == R_b) {
        std::cerr << "[rlz_seed] seed=42 and seed=43 yielded identical R "
                     "(sampling NOT seed-dependent)\n";
        return 4;
    }
    std::cout << "rlz_seed_determinism_test: OK (seed=42 deterministic; "
              << "seed=42 vs seed=43 differ; K=" << K << ")\n";
    fs::remove_all(tmp);
    return 0;
}
