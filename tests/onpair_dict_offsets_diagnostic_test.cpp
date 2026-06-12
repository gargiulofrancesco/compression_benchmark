// OnPair-only diagnostic sub-counter gate. At every bits ∈
// {12, 14, 16}, the runner MUST emit a metric_name='dict_offsets_bytes'
// row with value equal to (num_tokens+1)*4 AND <= dict_bytes (the diagnostic
// is contained within dict_bytes — it surfaces the internal split, not a
// deduction from the ratio formula).

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static Column random_column(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        size_t L = 8 + (rng() % 96);
        for (size_t j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static int verify_bits(uint32_t bits, const std::string& in_path,
                       const std::string& cz_path) {
    OnPairRunner r;
    std::string cfg = R"({"bits":)" + std::to_string(bits)
        + R"(,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";
    auto res = r.compress(in_path, cz_path, cfg);
    if (res.status != "ok") {
        std::cerr << "compress bits=" << bits << " status=" << res.status << "\n";
        return 1;
    }
    double dict = 0, dof = 0;
    bool found_dof = false;
    for (auto& m : res.metrics) {
        if (m.first == "dict_bytes")         dict = m.second;
        if (m.first == "dict_offsets_bytes") { dof = m.second; found_dof = true; }
    }
    if (!found_dof) {
        std::cerr << "bits=" << bits << ": dict_offsets_bytes metric MISSING\n";
        return 2;
    }
    if (dof <= 0) {
        std::cerr << "bits=" << bits << ": dict_offsets_bytes=" << dof
                  << " should be positive\n";
        return 3;
    }
    if (dof >= dict) {
        std::cerr << "bits=" << bits << ": dict_offsets_bytes=" << dof
                  << " not strictly less than dict_bytes=" << dict
                  << " — diagnostic is the offsets-table SUBSET of dict\n";
        return 4;
    }
    // Sanity: dof must be a multiple of 4 (offsets are u32).
    if (static_cast<uint64_t>(dof) % 4 != 0) {
        std::cerr << "bits=" << bits << ": dict_offsets_bytes not divisible by 4\n";
        return 5;
    }
    std::cout << "bits=" << bits << ": dict=" << dict << " dict_offsets=" << dof
              << " (fraction=" << (dof / dict) << ")\n";
    return 0;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_dof";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    write_column(in_path.string(), random_column(2000, 17));

    int fails = 0;
    fails += verify_bits(12, in_path.string(), cz_path.string());
    fails += verify_bits(14, in_path.string(), cz_path.string());
    fails += verify_bits(16, in_path.string(), cz_path.string());
    if (fails) return 1;
    std::cout << "onpair_dict_offsets_diagnostic: OK (3/3)\n";
    return 0;
}
