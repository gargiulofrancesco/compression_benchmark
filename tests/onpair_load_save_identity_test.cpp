// OnPair determinism: two compress invocations on the same column at
// (bits=16, seed=42, DynamicThreshold{0.15}) produce bytewise-identical wire.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG =
    R"({"bits":16,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";

static std::vector<uint8_t> slurp(const std::string& p) {
    std::ifstream f(p, std::ios::binary);
    return {std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()};
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_lsid";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path a_path  = tmp / "a.bin";
    fs::path b_path  = tmp / "b.bin";

    std::mt19937 rng(2026);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < 3000; ++i) {
        size_t L = 8 + (rng() % 96);
        for (size_t j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    OnPairRunner r1, r2;
    auto ra = r1.compress(in_path.string(), a_path.string(), CFG);
    auto rb = r2.compress(in_path.string(), b_path.string(), CFG);
    if (ra.status != "ok" || rb.status != "ok") {
        std::cerr << "compress failed: a=" << ra.status << " b=" << rb.status << "\n";
        return 1;
    }
    auto A = slurp(a_path.string());
    auto B = slurp(b_path.string());
    if (A != B) {
        std::cerr << "determinism violated: wire bytes differ ("
                  << A.size() << " vs " << B.size() << ")\n";
        return 2;
    }
    std::cout << "onpair_load_save_identity: OK (" << A.size() << " bytes identical)\n";
    return 0;
}
