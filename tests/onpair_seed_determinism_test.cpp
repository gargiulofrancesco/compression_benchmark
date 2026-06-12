// OnPair seed determinism gate:
//   (a) seed=42 × 2 → identical wire (locked-headline path).
//   (b) Round-trip correctness is seed-independent: decoded bytes
//       at seed=42 == original bytes regardless of seed.
//
// NB: parse_onpair_config does NOT enforce seed=42 (unlike RLZ); it accepts
// any uint64_t. Headline-discipline is enforced at experiment-config / smoke
// time (config_json templates pin seed=42). Lower-level seeds are still
// useful for differential tests.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static const std::string CFG_42 =
    R"({"bits":16,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";
static const std::string CFG_43 =
    R"({"bits":16,"sample_fraction":0.15,"seed":43,"threads":1,"mem_cap_gb":4})";

static std::vector<uint8_t> slurp(const std::string& p) {
    std::ifstream f(p, std::ios::binary);
    return {std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()};
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_seed";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path a_path  = tmp / "a.bin";
    fs::path b_path  = tmp / "b.bin";
    fs::path c_path  = tmp / "c.bin";   // seed=43
    fs::path da_path = tmp / "dec_a.col";
    fs::path dc_path = tmp / "dec_c.col";

    std::mt19937 rng(2026);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < 2000; ++i) {
        size_t L = 12 + (rng() % 64);
        for (size_t j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    OnPairRunner r;
    auto ra = r.compress(in_path.string(), a_path.string(), CFG_42);
    auto rb = r.compress(in_path.string(), b_path.string(), CFG_42);
    auto rc = r.compress(in_path.string(), c_path.string(), CFG_43);
    if (ra.status != "ok" || rb.status != "ok" || rc.status != "ok") {
        std::cerr << "compress failed\n";
        return 1;
    }
    auto A = slurp(a_path.string());
    auto B = slurp(b_path.string());
    auto C = slurp(c_path.string());

    // (a) seed=42 twice → identical wire.
    if (A != B) {
        std::cerr << "seed=42 determinism violated\n";
        return 2;
    }
    // Decode both seeds; correctness is seed-independent.
    auto da = r.bulk_decode(a_path.string(), da_path.string(), "{}");
    auto dc = r.bulk_decode(c_path.string(), dc_path.string(), "{}");
    if (da.status != "ok" || dc.status != "ok") {
        std::cerr << "bulk_decode failed\n";
        return 3;
    }
    Column dec_a = read_column(da_path.string());
    Column dec_c = read_column(dc_path.string());
    if (dec_a.bytes != c.bytes || dec_a.offsets != c.offsets) {
        std::cerr << "seed=42 bytewise round-trip mismatch\n";
        return 4;
    }
    if (dec_c.bytes != c.bytes || dec_c.offsets != c.offsets) {
        std::cerr << "seed=43 bytewise round-trip mismatch (correctness must be seed-independent)\n";
        return 5;
    }
    // Note: A vs C may or may not differ depending on whether the trainer
    // shuffle is consequential for this corpus; we do NOT assert wire
    // difference (would be brittle). We assert correctness both ways.
    std::cout << "onpair_seed_determinism: OK (seed=42 identical, both decode correctly)\n";
    return 0;
}
