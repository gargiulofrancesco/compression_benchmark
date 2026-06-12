// bpe random_decode: 10+ probes including boundary indices (0, N-1) and
// stride-313 walk. Compares against bulk_decode'd column.

#include "bpe_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <cstring>  // std::memcmp — explicit; libstdc++ (gcc) doesn't pull it in transitively like libc++
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
    fs::path tmp = fs::temp_directory_path() / "onpair_bpe_random";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path bd_path = tmp / "bd.col";
    fs::path rd_path = tmp / "rd.bin";

    // Build column.
    Column c;
    std::mt19937 rng(7);
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < 500; ++i) {
        int L = 16 + static_cast<int>(rng() % 48);
        for (int j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    const std::string cfg = R"({"vocab_size":16384,"byte_fallback":true,"normalization":"identity","threads":1,"mem_cap_gb":4})";
    BpeRunner r;
    if (r.compress(in_path.string(), cz_path.string(), cfg).status != "ok") {
        std::cerr << "compress failed\n"; return 1;
    }
    if (r.bulk_decode(cz_path.string(), bd_path.string(), cfg).status != "ok") {
        std::cerr << "bulk_decode failed\n"; return 1;
    }
    Column bd = read_column(bd_path.string());

    auto check = [&](size_t idx) -> bool {
        auto res = r.random_decode(cz_path.string(), idx, rd_path.string(), cfg);
        if (res.status != "ok") return false;
        std::ifstream f(rd_path.string(), std::ios::binary);
        std::vector<char> got((std::istreambuf_iterator<char>(f)),
                               std::istreambuf_iterator<char>());
        auto sv = bd.at(idx);
        return got.size() == sv.size()
            && std::memcmp(got.data(), sv.data(), got.size()) == 0;
    };

    // Boundary indices + stride-313 walk.
    std::vector<size_t> probes = {0, 1, bd.num_rows() / 2, bd.num_rows() - 1};
    for (size_t i = 0; i < bd.num_rows(); i += 313) probes.push_back(i);

    int fail = 0;
    for (size_t idx : probes) {
        if (!check(idx)) {
            std::cerr << "[bpe_random_decode] mismatch at idx=" << idx << "\n";
            fail = 1;
        }
    }
    return fail;
}
