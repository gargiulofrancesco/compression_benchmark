// bpe round-trip — the PRIMARY correctness gate for the canonical BPE baseline
// (SentencePiece BPE driven through the GPT-2 byte<->unicode bijection). Build
// column → compress → bulk_decode → assert bytewise column equality. Sub-variants:
//   1. vanilla random alpha column;
//   2. heavy-repetition (BPE shines);
//   3. whitespace-edge column (leading/trailing/multiple spaces);
//   4. all-bytes column — EXHAUSTIVE 0x00..0xFF coverage (incl. lone high bytes
//      0x80..0xFF, NUL, newline). The byte<->unicode mapping makes this lossless;
//      this is the load-bearing is_lossless: true assertion.
//   5. empty-string-mixed column.
// All run at V=16384 (canonical headline middle vocab).

#include "bpe_runner.h"
#include "runner_protocol.h"

#include <cstdio>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG_V16K =
    R"({"vocab_size":16384,"sample_fraction":0.15,"normalization":"identity","threads":1,"mem_cap_gb":4})";

static Column make_vanilla(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    std::uniform_int_distribution<int> len_d(20, 80);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        int L = len_d(rng);
        for (int j = 0; j < L; ++j) {
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
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
        int n = 3 + static_cast<int>(rng() % 6);
        for (int j = 0; j < n; ++j) {
            const auto& s = tokens[rng() % tokens.size()];
            for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

// EXHAUSTIVE byte coverage 0x00..0xFF (incl. lone high bytes 0x80..0xFF, NUL,
// newline). The byte<->unicode bijection makes every byte round-trip; the first
// row deterministically lists all 256 values so coverage is guaranteed, the rest
// are random bytes.
static Column make_all_bytes(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (int b = 0; b < 256; ++b) c.bytes.push_back(static_cast<uint8_t>(b));
    c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    for (uint32_t i = 1; i < n_rows; ++i) {
        int L = 24 + static_cast<int>(rng() % 24);
        for (int j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>(rng() % 0x100));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

// Leading/trailing/multiple whitespace. verifies
// add_dummy_prefix=false + remove_extra_whitespaces=false.
static Column make_whitespace_edge(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    static const std::vector<std::string> samples = {
        "   leading_spaces", "trailing_spaces   ",
        "  multiple   spaces   inside  ", "\tno tab squashing\t",
        "    ", "single", " a", "a ",
    };
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        const auto& s = samples[rng() % samples.size()];
        for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

static Column make_mixed_empty(uint32_t n_rows, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        if (i % 5 == 0) {
            // empty row
        } else {
            std::string s = "row_" + std::to_string(i);
            for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    (void)rng;
    return c;
}

static bool columns_equal(const Column& a, const Column& b) {
    return a.bytes == b.bytes && a.offsets == b.offsets;
}

static double metric(const OpResult& result, const std::string& name) {
    for (const auto& [key, value] : result.metrics)
        if (key == name) return value;
    std::cerr << "[bpe_roundtrip] missing metric " << name << "\n";
    return -1.0;
}

static int roundtrip_one(const std::string& label, const Column& original) {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / ("onpair_bpe_rt_" + label);
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";
    fs::path rt_path = tmp / "rt.col";

    write_column(in_path.string(), original);

    BpeRunner r;
    auto c_res = r.compress(in_path.string(), cz_path.string(), CFG_V16K);
    if (c_res.status != "ok") {
        std::cerr << "[bpe_roundtrip] compress " << label
                  << " status=" << c_res.status << " notes=" << c_res.notes << "\n";
        return 1;
    }

    const double reuse = metric(c_res, "t_encode_ns") + metric(c_res, "t_pack_ns");
    const double codec_first = metric(c_res, "t_sample_prepare_ns")
        + metric(c_res, "t_train_ns") + metric(c_res, "t_model_load_ns")
        + metric(c_res, "t_encode_ns") + metric(c_res, "t_pack_ns");
    const double first = codec_first + metric(c_res, "t_serialize_ns");
    if (metric(c_res, "t_reuse_compress_ns") != reuse
        || metric(c_res, "t_codec_first_use_ns") != codec_first
        || metric(c_res, "t_compress_ns") != codec_first
        || metric(c_res, "t_first_use_ns") != first) {
        std::cerr << "[bpe_roundtrip] " << label
                  << ": phase timing identity failed\n";
        return 1;
    }

    auto d_res = r.bulk_decode(cz_path.string(), rt_path.string(), CFG_V16K);
    if (d_res.status != "ok") {
        std::cerr << "[bpe_roundtrip] bulk_decode " << label
                  << " status=" << d_res.status << " notes=" << d_res.notes << "\n";
        return 1;
    }

    Column rt = read_column(rt_path.string());
    if (!columns_equal(original, rt)) {
        std::cerr << "[bpe_roundtrip] " << label
                  << ": round-trip mismatch (orig N=" << original.num_rows()
                  << " rt N=" << rt.num_rows() << "; orig bytes="
                  << original.bytes.size() << " rt bytes=" << rt.bytes.size()
                  << ")\n";
        return 1;
    }
    return 0;
}

int main() {
    int fail = 0;
    fail |= roundtrip_one("vanilla",     make_vanilla(400, 42));
    fail |= roundtrip_one("heavy_rep",   make_heavy_repetition(400, 42));
    fail |= roundtrip_one("whitespace",  make_whitespace_edge(400, 42));
    fail |= roundtrip_one("all_bytes",   make_all_bytes(400, 42));
    fail |= roundtrip_one("mixed_empty", make_mixed_empty(400, 42));
    return fail ? 1 : 0;
}
