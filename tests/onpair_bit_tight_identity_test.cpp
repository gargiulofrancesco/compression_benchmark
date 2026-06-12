// OnPair bit-tight identity gate (target ninth bit-tight baseline).
// At every bits ∈ {9, 12, 14, 16}, the headline metrics MUST satisfy
//   dict_bytes + tokens_bytes + boundaries_bytes ≡ output_bytes (exactly).
// This is the structural guarantee of the library's wire format combined
// with the sub-counter mapping (NON-collapse, fifth
// natural split in the lineup).

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>

using namespace onpair_bench;

static Column simple_column(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        size_t L = 8 + (rng() % 64);
        for (size_t j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static int check_bits(uint32_t bits, const std::string& in_path,
                      const std::string& cz_path) {
    OnPairRunner r;
    std::string cfg = R"({"bits":)" + std::to_string(bits)
        + R"(,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";
    auto res = r.compress(in_path, cz_path, cfg);
    if (res.status != "ok") {
        std::cerr << "compress bits=" << bits << " status=" << res.status << "\n";
        return 1;
    }
    double d = 0, t = 0, b = 0, o = 0;
    for (auto& m : res.metrics) {
        if (m.first == "dict_bytes")       d = m.second;
        if (m.first == "tokens_bytes")     t = m.second;
        if (m.first == "boundaries_bytes") b = m.second;
        if (m.first == "output_bytes")     o = m.second;
    }
    const double sum = d + t + b;
    if (sum != o) {
        std::cerr << "bits=" << bits << ": NOT bit-tight: "
                  << d << " + " << t << " + " << b << " = " << sum
                  << " ≠ output=" << o << " (delta=" << (sum - o) << ")\n";
        return 2;
    }
    std::cout << "bits=" << bits << ": " << d << " + " << t << " + " << b
              << " = " << sum << " ≡ output\n";
    return 0;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_btid";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    write_column(in_path.string(), simple_column(2000, 23));

    int fails = 0;
    fails += check_bits(9,  in_path.string(), cz_path.string());
    fails += check_bits(12, in_path.string(), cz_path.string());
    fails += check_bits(14, in_path.string(), cz_path.string());
    fails += check_bits(16, in_path.string(), cz_path.string());
    if (fails) return 1;
    std::cout << "onpair_bit_tight_identity: OK (4/4)\n";
    return 0;
}
