// OnPair round-trip — six sub-variants exercising byte-completeness.
// Bytewise round-trip MUST be exact at bits=16 over all 256 byte values.
// Mirrors bpe_roundtrip_test coverage.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG_BITS16 =
    R"({"bits":16,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";

static Column make_all_bytes(uint32_t n_rows, size_t bytes_per_row) {
    Column c;
    c.offsets.push_back(0);
    for (int b = 0; b < 256; ++b) c.bytes.push_back(static_cast<uint8_t>(b));
    c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    std::mt19937 rng(2026);
    for (uint32_t i = 1; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j)
            c.bytes.push_back(static_cast<uint8_t>(rng() % 256));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_random_bytes(uint32_t n_rows, size_t bytes_per_row, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j)
            c.bytes.push_back(static_cast<uint8_t>(rng() % 256));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_nul_byte(uint32_t n_rows, size_t bytes_per_row) {
    // Every row contains at least one 0x00 byte at a random position.
    std::mt19937 rng(2027);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        std::vector<uint8_t> row(bytes_per_row);
        for (size_t j = 0; j < bytes_per_row; ++j)
            row[j] = static_cast<uint8_t>('a' + (rng() % 26));
        row[rng() % bytes_per_row] = 0x00;
        for (uint8_t b : row) c.bytes.push_back(b);
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_whitespace(uint32_t n_rows) {
    Column c;
    c.offsets.push_back(0);
    std::vector<uint8_t> pool = {' ', '\t', '\n', '\r', '\f', '\v'};
    std::mt19937 rng(2028);
    for (uint32_t i = 0; i < n_rows; ++i) {
        size_t L = 8 + (rng() % 24);
        for (size_t j = 0; j < L; ++j) c.bytes.push_back(pool[rng() % pool.size()]);
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_non_utf8(uint32_t n_rows, size_t bytes_per_row) {
    // Rows dominated by 0x80..0xFF (lone high-bit bytes, invalid UTF-8).
    std::mt19937 rng(2029);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j)
            c.bytes.push_back(static_cast<uint8_t>(0x80 + (rng() % 128)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_mixed_empty(uint32_t n_rows, size_t bytes_per_row) {
    std::mt19937 rng(2030);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        if (i % 3 == 0) {
            // empty row
        } else {
            for (size_t j = 0; j < bytes_per_row; ++j)
                c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static double metric(const OpResult& result, const std::string& name) {
    for (const auto& [key, value] : result.metrics)
        if (key == name) return value;
    std::cerr << "[onpair_roundtrip] missing metric " << name << "\n";
    return -1.0;
}

static int run_variant(const char* label, Column&& c) {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_roundtrip";
    fs::create_directories(tmp);
    fs::path in_path = tmp / (std::string("in_") + label + ".col");
    fs::path cz_path = tmp / (std::string("z_")  + label + ".bin");
    fs::path de_path = tmp / (std::string("d_")  + label + ".col");
    write_column(in_path.string(), c);

    OnPairRunner r;
    auto res = r.compress(in_path.string(), cz_path.string(), CFG_BITS16);
    if (res.status != "ok") {
        std::cerr << "[roundtrip:" << label << "] compress status=" << res.status
                  << " notes=" << res.notes << "\n";
        return 1;
    }
    const double codec_first = metric(res, "t_train_ns")
        + metric(res, "t_parse_pack_ns");
    const double runner_first = metric(res, "t_compress_ns")
        + metric(res, "t_serialize_ns");
    if (metric(res, "t_codec_first_use_ns") != codec_first
        || metric(res, "t_first_use_ns") != runner_first) {
        std::cerr << "[roundtrip:" << label
                  << "] phase timing identity failed\n";
        return 1;
    }
    auto resd = r.bulk_decode(cz_path.string(), de_path.string(), "{}");
    if (resd.status != "ok") {
        std::cerr << "[roundtrip:" << label << "] bulk_decode status=" << resd.status << "\n";
        return 1;
    }
    Column dec = read_column(de_path.string());
    if (dec.bytes != c.bytes || dec.offsets != c.offsets) {
        std::cerr << "[roundtrip:" << label << "] bytewise mismatch — "
                  << "orig N=" << c.num_rows() << " bytes=" << c.bytes.size()
                  << " ; dec N=" << dec.num_rows() << " bytes=" << dec.bytes.size() << "\n";
        return 1;
    }
    std::cout << "[roundtrip:" << label << "] OK ("
              << c.num_rows() << " rows, " << c.bytes.size() << " bytes)\n";
    return 0;
}

int main() {
    int fails = 0;
    fails += run_variant("all_bytes",     make_all_bytes(500, 64));
    fails += run_variant("random_bytes",  make_random_bytes(500, 64, 2031));
    fails += run_variant("nul_byte",      make_nul_byte(500, 32));
    fails += run_variant("whitespace",    make_whitespace(500));
    fails += run_variant("non_utf8",      make_non_utf8(500, 48));
    fails += run_variant("mixed_empty",   make_mixed_empty(500, 32));
    return fails ? 1 : 0;
}
