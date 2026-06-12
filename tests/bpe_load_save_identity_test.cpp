// BPE card mandate: determinism. Compress the same column
// twice at V=16384 with the same config; assert wire bytes bit-identical.
// `enable_sampling=false` makes BPE training fully deterministic given the
// same corpus + args; protobuf serialization is deterministic.

#include "bpe_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_bpe_lsi";
    fs::create_directories(tmp);
    fs::path in_path  = tmp / "in.col";
    fs::path cz1_path = tmp / "z1.bin";
    fs::path cz2_path = tmp / "z2.bin";

    Column c;
    std::mt19937 rng(2026);
    c.offsets.push_back(0);
    static const std::vector<std::string> toks = {
        "common_prefix_abc", "common_prefix_xyz", "session=", "user_=",
    };
    for (int i = 0; i < 300; ++i) {
        int n = 3 + static_cast<int>(rng() % 5);
        for (int j = 0; j < n; ++j) {
            const auto& s = toks[rng() % toks.size()];
            for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    const std::string cfg = R"({"vocab_size":16384,"byte_fallback":true,"normalization":"identity","threads":1,"mem_cap_gb":4})";
    BpeRunner r1, r2;
    if (r1.compress(in_path.string(), cz1_path.string(), cfg).status != "ok") {
        std::cerr << "compress #1 failed\n"; return 1;
    }
    if (r2.compress(in_path.string(), cz2_path.string(), cfg).status != "ok") {
        std::cerr << "compress #2 failed\n"; return 1;
    }

    auto load = [](const std::string& p) {
        std::ifstream f(p, std::ios::binary);
        return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)),
                                     std::istreambuf_iterator<char>());
    };
    auto b1 = load(cz1_path.string());
    auto b2 = load(cz2_path.string());
    if (b1 != b2) {
        std::cerr << "[bpe_load_save_identity] mismatch: "
                  << b1.size() << " vs " << b2.size() << " bytes\n";
        return 1;
    }
    fs::remove_all(tmp);
    return 0;
}
