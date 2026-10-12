// repair (plain Re-Pair grammar) round-trip: build column → compress →
// bulk_decode → assert column equality. Stresses: vanilla, duplicate-heavy
// (grammar must capture cross-row repeats), full byte coverage (0x00..0xFF must
// survive — the grammar is over the raw text, no NUL-separator assumption), and
// heavy-grammar (repeated substrings force a deeper grammar).

#include "repair_runner.h"
#include "runner_protocol.h"

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static Column make_vanilla(uint32_t n_rows, uint32_t mean_len, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> len_d(mean_len, mean_len / 4.0);
    Column c; c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        int len = static_cast<int>(std::round(len_d(rng)));
        if (len < 1) len = 1;
        for (int j = 0; j < len; ++j) c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

// Rows drawn from a small pool → heavy cross-row repetition (deep grammar).
static Column make_repetitive(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    std::vector<std::string> pool = {"http://example.com/", "the quick brown ",
        "SELECT * FROM ", "aaaaaaaaaaaaaaaa", "12345678901234567890"};
    Column c; c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        std::string row;
        int k = 1 + static_cast<int>(rng() % 4);
        for (int j = 0; j < k; ++j) row += pool[rng() % pool.size()];
        for (char ch : row) c.bytes.push_back(static_cast<uint8_t>(ch));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

// Every byte value 0x00..0xFF appears — lossless over arbitrary bytes.
static Column make_full_byte_coverage(uint32_t seed) {
    std::mt19937 rng(seed);
    Column c; c.offsets.push_back(0);
    for (uint32_t i = 0; i < 600; ++i) {
        int len = 1 + static_cast<int>(rng() % 40);
        for (int j = 0; j < len; ++j) c.bytes.push_back(static_cast<uint8_t>(rng() % 256));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    // guarantee every byte value present at least once
    for (int b = 0; b < 256; ++b) c.bytes.push_back(static_cast<uint8_t>(b));
    c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    return c;
}

static bool roundtrip(const Column& col, const std::string& tag) {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_repair_rt";
    fs::create_directories(tmp);
    fs::path in = tmp / (tag + ".in.col");
    fs::path wire = tmp / (tag + ".wire");
    fs::path out = tmp / (tag + ".out.col");
    write_column(in.string(), col);

    RepairRunner r;
    const std::string cfg = "{\"mem_cap_gb\":64}";
    auto cr = r.compress(in.string(), wire.string(), cfg);
    if (cr.status != "ok") {
        std::cerr << "[repair_rt] " << tag << " compress status=" << cr.status
                  << " notes=" << cr.notes << "\n";
        return false;
    }
    auto dr = r.bulk_decode(wire.string(), out.string(), cfg);
    if (dr.status != "ok") {
        std::cerr << "[repair_rt] " << tag << " decode status=" << dr.status
                  << " notes=" << dr.notes << "\n";
        return false;
    }
    Column got = read_column(out.string());
    if (got.bytes != col.bytes || got.offsets != col.offsets) {
        std::cerr << "[repair_rt] " << tag << " ROUND-TRIP MISMATCH"
                  << " (bytes " << got.bytes.size() << " vs " << col.bytes.size()
                  << ", rows " << got.num_rows() << " vs " << col.num_rows() << ")\n";
        return false;
    }
    // Row access through the grammar-descent index: first, last and a spread
    // of rows in between must each decode to exactly their own bytes.
    const size_t n = col.num_rows();
    for (size_t idx : {size_t{0}, n / 7, n / 3, n / 2, (2 * n) / 3, n - 2, n - 1}) {
        fs::path row = tmp / (tag + ".row");
        auto rr = r.random_decode(wire.string(), idx, row.string(), cfg);
        std::ifstream f(row, std::ios::binary);
        const std::string got_row((std::istreambuf_iterator<char>(f)),
                                  std::istreambuf_iterator<char>());
        const std::string want(col.bytes.begin() + col.offsets[idx],
                               col.bytes.begin() + col.offsets[idx + 1]);
        if (rr.status != "ok" || got_row != want) {
            std::cerr << "[repair_rt] " << tag << " ROW ACCESS MISMATCH at row "
                      << idx << " (status=" << rr.status << ")\n";
            return false;
        }
    }
    std::cout << "[repair_rt] " << tag << " OK ("
              << col.num_rows() << " rows, " << col.bytes.size() << " bytes)\n";
    return true;
}

int main() {
    int fail = 0;
    fail |= !roundtrip(make_vanilla(5000, 24, 1), "vanilla");
    fail |= !roundtrip(make_repetitive(8000, 2), "repetitive");
    fail |= !roundtrip(make_full_byte_coverage(3), "full_byte_coverage");
    if (!fail) std::cout << "repair_roundtrip_test: OK\n";
    return fail;
}
