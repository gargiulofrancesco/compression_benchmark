// RLZ round-trip — 6 sub-variants including exhaustive byte coverage.
// Byte-completeness gate: literal-escape path (Hoobin 2011 / LZ77 (pos, len,
// literal) formulation) must preserve every 0x00..0xFF byte verbatim.
// At R%=5% on ~400-600 KB columns (K = 1..4 blocks of 16 KB each).

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG_R5 =
    R"({"reference_size_fraction":0.05,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":4})";

// Column with every byte 0x00..0xFF. First row contains all 256 bytes; other
// rows random bytes.
static Column make_all_bytes(uint32_t n_rows, size_t bytes_per_row) {
    Column c;
    c.offsets.push_back(0);
    for (int b = 0; b < 256; ++b) c.bytes.push_back(static_cast<uint8_t>(b));
    c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    std::mt19937 rng(2026);
    for (uint32_t i = 1; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j) {
            c.bytes.push_back(static_cast<uint8_t>(rng() % 256));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_random_bytes(uint32_t n_rows, size_t bytes_per_row, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j) {
            c.bytes.push_back(static_cast<uint8_t>(rng() % 256));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_non_utf8(uint32_t n_rows, size_t bytes_per_row, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j) {
            // Lone high-bit bytes (0x80..0xFF), not valid UTF-8 — the case
            // that SentencePiece fails on. RLZ preserves verbatim.
            c.bytes.push_back(static_cast<uint8_t>(0x80 + (rng() % 0x80)));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_nul_byte(uint32_t n_rows, size_t bytes_per_row, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j) {
            uint8_t b = (rng() % 4 == 0)
                ? static_cast<uint8_t>('a' + (rng() % 26))
                : static_cast<uint8_t>(rng() % 0x20);
            c.bytes.push_back(b);
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_heavy_repetition(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    static const std::vector<std::string> tokens = {
        "GET /api/v1/users/", "POST /api/v1/orders/",
        "DELETE /api/v1/items/", "HTTP/1.1 200 OK ",
        "session=", "token=", "user_id=", "alice", "bob ",
    };
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        int n = 4 + static_cast<int>(rng() % 8);
        for (int j = 0; j < n; ++j) {
            const auto& s = tokens[rng() % tokens.size()];
            for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_mixed_empty(uint32_t n_rows) {
    Column c;
    c.offsets.push_back(0);
    std::mt19937 rng(99);
    for (uint32_t i = 0; i < n_rows; ++i) {
        if (i % 5 == 0) {
            // empty row
        } else {
            // Make rows long enough that the total column is > 327680 bytes
            // (so K >= 1 at R%=5%).
            int L = 80 + static_cast<int>(rng() % 50);
            std::string s = "row_" + std::to_string(i) + "_";
            for (int j = 0; j < L; ++j) s += static_cast<char>('a' + (rng() % 26));
            for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static bool columns_equal(const Column& a, const Column& b) {
    return a.bytes == b.bytes && a.offsets == b.offsets;
}

static int roundtrip_one(const std::string& label, const Column& original) {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / ("onpair_rlz_rt_" + label);
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path rt_path = tmp / "rt.col";

    write_column(in_path.string(), original);

    RlzRunner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(), CFG_R5);
    if (c_res.status != "ok") {
        std::cerr << "[rlz_roundtrip] compress " << label
                  << " status=" << c_res.status << " notes=" << c_res.notes << "\n";
        return 1;
    }
    auto d_res = r.bulk_decode(cz_path.string(), rt_path.string(), CFG_R5);
    if (d_res.status != "ok") {
        std::cerr << "[rlz_roundtrip] bulk_decode " << label
                  << " status=" << d_res.status << " notes=" << d_res.notes << "\n";
        return 1;
    }
    Column rt = read_column(rt_path.string());
    if (!columns_equal(original, rt)) {
        std::cerr << "[rlz_roundtrip] " << label << ": mismatch ("
                  << original.bytes.size() << " vs " << rt.bytes.size()
                  << " bytes; " << original.num_rows() << " vs " << rt.num_rows()
                  << " rows)\n";
        return 1;
    }
    return 0;
}

int main() {
    int fail = 0;
    // ~400 KB columns to exceed 327680 byte threshold for K>=1 at R%=5%.
    fail |= roundtrip_one("all_bytes",    make_all_bytes(4000, 100));
    fail |= roundtrip_one("random_bytes", make_random_bytes(4000, 100, 42));
    fail |= roundtrip_one("non_utf8",     make_non_utf8(4000, 100, 42));
    fail |= roundtrip_one("nul_byte",     make_nul_byte(4000, 100, 42));
    fail |= roundtrip_one("heavy_rep",    make_heavy_repetition(2000, 42));
    fail |= roundtrip_one("mixed_empty",  make_mixed_empty(4000));
    if (fail == 0) std::cout << "rlz_roundtrip_test: OK (6 sub-variants)\n";
    return fail;
}
