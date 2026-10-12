// BPE/OnPair size-matched pairing test.
// bpe V=16384 ↔ OnPair bits=14 (= 2^14 = 16384).
// Drives both runners on the same column and times each compress call here
// with steady_clock (the OpResult does not carry t_op_ns directly).
//
// Prints the onpair/bpe time ratio as a diagnostic; PASSES so long as both
// runners produce status='ok'. No bound on the ratio is asserted: it varies
// too much with the CPU and compile flags.

#include "onpair_runner.h"
#include "../runners/bpe/bpe_runner.h"
#include "runner_protocol.h"

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>

using namespace onpair_bench;

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_clusterb";
    fs::create_directories(tmp);
    fs::path in_path  = tmp / "in.col";
    fs::path cz_bpe   = tmp / "z_bpe.bin";
    fs::path cz_onp   = tmp / "z_onp.bin";

    // Modest column (SentencePiece BPE trains on a sample, so this is fast).
    std::mt19937 rng(2026);
    Column c;
    c.offsets.push_back(0);
    constexpr uint32_t N = 2000;
    for (uint32_t i = 0; i < N; ++i) {
        size_t L = 12 + (rng() % 64);
        for (size_t j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    auto t0 = std::chrono::steady_clock::now();
    BpeRunner bpe;
    const std::string bpe_cfg =
        R"({"vocab_size":16384,"sample_fraction":0.15,"threads":1,"mem_cap_gb":4})";
    auto bpe_res = bpe.compress(in_path.string(), cz_bpe.string(), bpe_cfg);
    auto t1 = std::chrono::steady_clock::now();
    OnPairRunner onp;
    const std::string onp_cfg =
        R"({"bits":14,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";
    auto onp_res = onp.compress(in_path.string(), cz_onp.string(), onp_cfg);
    auto t2 = std::chrono::steady_clock::now();

    if (bpe_res.status != "ok") {
        std::cerr << "bpe compress: " << bpe_res.notes << "\n";
        return 1;
    }
    if (onp_res.status != "ok") {
        std::cerr << "onpair compress: " << onp_res.notes << "\n";
        return 2;
    }
    double bpe_ms = std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count() / 1000.0;
    double onp_ms = std::chrono::duration_cast<std::chrono::microseconds>(t2 - t1).count() / 1000.0;
    double ratio = (bpe_ms > 0) ? (onp_ms / bpe_ms) : 0.0;
    std::cout << "[cluster_b] bpe V=16K compress=" << bpe_ms << " ms\n";
    std::cout << "[cluster_b] onpair bits=14 compress=" << onp_ms << " ms\n";
    std::cout << "[cluster_b] ratio onpair/bpe=" << ratio << "\n";

    // The ratio is printed, not asserted: the ctest passes as long as both
    // runners produced status='ok'.
    fs::remove_all(tmp);
    std::cout << "onpair_cluster_b_symmetry: STATUS-OK both runners (the "
              << "ratio above is informative; no bound is checked).\n";
    return 0;
}
