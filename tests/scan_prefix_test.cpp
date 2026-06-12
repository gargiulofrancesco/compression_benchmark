// Canonical scan_prefix test. Verifies the G6 reference scanner returns the
// expected hit set for hand-curated columns.

#include "scan_prefix.h"
#include "runner_protocol.h"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

using namespace onpair_bench;

static Column make_column(const std::vector<std::string>& strs) {
    Column c;
    uint32_t off = 0;
    c.offsets.push_back(0);
    for (const auto& s : strs) {
        c.bytes.insert(c.bytes.end(), s.begin(), s.end());
        off += static_cast<uint32_t>(s.size());
        c.offsets.push_back(off);
    }
    return c;
}

int main() {
    auto col = make_column({
        "http://example.com", "https://example.com", "ftp://example.com",
        "the quick brown",   "the lazy fox",        "a small step",
        "", "https://onpair.dev",
    });

    std::vector<uint32_t> hits;
    size_t n = scan_prefix(col, "https://", hits);
    if (n != 2 || hits != std::vector<uint32_t>{1, 7}) {
        std::cerr << "scan_prefix https failed: n=" << n << "\n";
        return 1;
    }

    hits.clear();
    n = scan_prefix(col, "the ", hits);
    if (n != 2 || hits != std::vector<uint32_t>{3, 4}) {
        std::cerr << "scan_prefix 'the ' failed\n"; return 1;
    }

    hits.clear();
    n = scan_prefix(col, "missing-needle-XYZ", hits);
    if (n != 0 || !hits.empty()) {
        std::cerr << "scan_prefix empty failed\n"; return 1;
    }

    // Empty needle matches every row (including empty string)
    hits.clear();
    n = scan_prefix(col, "", hits);
    if (n != col.num_rows()) {
        std::cerr << "scan_prefix empty-needle failed\n"; return 1;
    }

    std::cout << "scan_prefix_test OK\n";
    return 0;
}
