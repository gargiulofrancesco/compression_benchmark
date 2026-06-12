// Column read/write round-trip for runner_core. unit test.

#include "runner_protocol.h"

#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

using namespace onpair_bench;

namespace fs = std::filesystem;

int main() {
    Column c;
    std::vector<std::string> strs = {"alpha", "", "beta", "gamma_____", "delta"};
    uint32_t off = 0;
    c.offsets.push_back(0);
    for (const auto& s : strs) {
        c.bytes.insert(c.bytes.end(), s.begin(), s.end());
        off += static_cast<uint32_t>(s.size());
        c.offsets.push_back(off);
    }
    assert(c.num_rows() == 5);
    assert(c.at(1).size() == 0);
    assert(c.at(3) == "gamma_____");

    fs::path p = fs::temp_directory_path() / "onpair_bench_stub_roundtrip.col";
    write_column(p.string(), c);
    Column rd = read_column(p.string());

    if (rd.num_rows() != c.num_rows()) { std::cerr << "row count mismatch\n"; return 1; }
    if (rd.bytes   != c.bytes)         { std::cerr << "bytes mismatch\n";     return 1; }
    if (rd.offsets != c.offsets)       { std::cerr << "offsets mismatch\n";   return 1; }

    fs::remove(p);
    std::cout << "stub_roundtrip_test OK\n";
    return 0;
}
