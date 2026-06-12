// fsst round-trip: build column → compress → bulk_decode → assert column equality.
// Three variants per FSST card §"Round-trip self-tests":
//   1. vanilla token-vocabulary column;
//   2. empty-string stress: a few zero-length rows interleaved;
//   3. escape-code stress: high-entropy rows that force FSST_ESC emission.

#include "fsst_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static Column make_vanilla(uint32_t n_rows, uint32_t mean_len, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> len_d(mean_len, mean_len / 4.0);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        int raw_len = static_cast<int>(std::round(len_d(rng)));
        if (raw_len < 1) raw_len = 1;
        for (int j = 0; j < raw_len; ++j) {
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

// Inject a handful of empty rows at known positions.
static Column make_with_empty_rows(uint32_t n_rows, uint32_t seed) {
    Column c = make_vanilla(n_rows, 64, seed);
    // Zero out a few rows by shifting bytes — easier to just rebuild offsets.
    Column out;
    out.offsets.push_back(0);
    for (uint32_t i = 0; i < c.num_rows(); ++i) {
        if (i == 0 || i == 7 || i == 42 || i == c.num_rows() - 1) {
            // empty row — append nothing
        } else {
            auto sv = c.at(i);
            out.bytes.insert(out.bytes.end(), sv.begin(), sv.end());
        }
        out.offsets.push_back(static_cast<uint32_t>(out.bytes.size()));
    }
    return out;
}

// Mix of vocab strings (compresses well via symbol table) and high-entropy
// random-byte rows (will force escape-code emission).
static Column make_escape_stress(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        if (i % 3 == 0) {
            // High-entropy random bytes — forces FSST escape coding.
            int L = 16 + static_cast<int>(rng() % 32);
            for (int j = 0; j < L; ++j) {
                c.bytes.push_back(static_cast<uint8_t>(rng() & 0xFF));
            }
        } else {
            // Vocab strings — compress well.
            const char* vocab[] = {
                "GET /api/v1/users", "POST /login HTTP/1.1", "DELETE /orders/42",
                "session=abc123", "INFO ready", "WARN slow",
            };
            const char* s = vocab[rng() % 6];
            while (*s) c.bytes.push_back(static_cast<uint8_t>(*s++));
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
    fs::path tmp = fs::temp_directory_path() / ("onpair_fsst_rt_" + label);
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path rt_path = tmp / "rt.col";

    write_column(in_path.string(), original);

    std::string cfg = R"({"variant":"fsst","training":"full_column"})";
    FsstRunner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(), cfg);
    if (c_res.status != "ok") {
        std::cerr << "compress status=" << c_res.status << " notes=" << c_res.notes
                  << " (label=" << label << ")\n";
        return 1;
    }
    auto d_res = r.bulk_decode(cz_path.string(), rt_path.string(), cfg);
    if (d_res.status != "ok") {
        std::cerr << "bulk_decode status=" << d_res.status << " notes=" << d_res.notes
                  << " (label=" << label << ")\n";
        return 2;
    }

    Column recovered = read_column(rt_path.string());
    if (!columns_equal(original, recovered)) {
        std::cerr << "round-trip mismatch label=" << label
                  << " orig_bytes=" << original.bytes.size()
                  << " recv_bytes=" << recovered.bytes.size()
                  << " orig_rows=" << original.num_rows()
                  << " recv_rows=" << recovered.num_rows() << "\n";
        return 3;
    }
    fs::remove_all(tmp);
    return 0;
}

int main() {
    Column vanilla = make_vanilla(5000, 96, 42);
    if (int rc = roundtrip_one("vanilla", vanilla); rc != 0) {
        std::cerr << "FAIL vanilla rc=" << rc << "\n";
        return 10 + rc;
    }
    Column empties = make_with_empty_rows(500, 7);
    if (int rc = roundtrip_one("empty_stress", empties); rc != 0) {
        std::cerr << "FAIL empty_stress rc=" << rc << "\n";
        return 20 + rc;
    }
    Column escapes = make_escape_stress(1500, 11);
    if (int rc = roundtrip_one("escape_stress", escapes); rc != 0) {
        std::cerr << "FAIL escape_stress rc=" << rc << "\n";
        return 30 + rc;
    }
    std::cout << "fsst_roundtrip_test: OK (vanilla + empty_stress + escape_stress)\n";
    return 0;
}
