// Unit test for the runners/core/decompress_then_scan helper. Drives it with
// a stub DecodeAllFn that returns a known Column; asserts the produced hit
// file matches what scan_prefix produces directly on the same column.

#include "decompress_then_scan.h"
#include "runner_protocol.h"
#include "scan_prefix.h"

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

using namespace onpair_bench;

static Column build_col(const std::vector<std::string>& rows) {
    Column c;
    c.offsets.push_back(0);
    for (auto& s : rows) {
        c.bytes.insert(c.bytes.end(), s.begin(), s.end());
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_dts_test";
    fs::create_directories(tmp);
    fs::path hits_path = tmp / "hits.bin";

    Column col = build_col({
        "https://example.com",
        "http://other.org",
        "no_match_row",
        "https://second",
        "another",
        "https://"  // exact prefix match, body empty
    });

    auto decode = [&col]() {
        return col;  // stub: decode returns the fixed column verbatim
    };

    size_t n = decompress_then_scan(decode, "https://", hits_path.string());

    std::ifstream f(hits_path, std::ios::binary);
    uint32_t nh = 0;
    f.read(reinterpret_cast<char*>(&nh), 4);
    std::vector<uint32_t> got(nh);
    if (nh > 0) f.read(reinterpret_cast<char*>(got.data()), nh * 4);

    std::vector<uint32_t> ref;
    scan_prefix(col, "https://", ref);
    if (n != ref.size() || got != ref) {
        std::cerr << "mismatch: n=" << n << " ref_n=" << ref.size() << "\n";
        return 1;
    }
    fs::remove_all(tmp);
    std::cout << "decompress_then_scan_test: OK (" << n << " hits)\n";
    return 0;
}
