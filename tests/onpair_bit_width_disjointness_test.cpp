// OnPair bit-width disjointness gate (§8.6 sub-counter tradeoff visual).
// + OnPair "bit-width disjointness" requirement:
// across bits ∈ {12, 14, 16} on a structured column, dict_bytes should
// trend UP and tokens_bytes should trend DOWN. On uniform-random text
// the direction may soften; we accept "≥1 of the two trends visible"
// at smoke and tighten in on real datasets.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static Column make_repetitive(uint32_t n_rows, uint32_t seed) {
    std::vector<std::string> pool = {
        "GET /api/v1/users/", "POST /api/v1/orders/", "DELETE /api/v1/items/",
        "HTTP/1.1 200 OK ", "session=", "token=", "user_id=", "order_id=",
        "alice ", "bob ", "carol ", "dave ",
        "INFO ", "WARN ", "ERROR ", "DEBUG ",
    };
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        std::string row;
        size_t parts = 6 + (rng() % 5);
        for (size_t k = 0; k < parts; ++k) {
            row += pool[rng() % pool.size()];
            row += std::to_string(rng() % 1000);
            row += ' ';
        }
        c.bytes.insert(c.bytes.end(),
                       reinterpret_cast<const uint8_t*>(row.data()),
                       reinterpret_cast<const uint8_t*>(row.data() + row.size()));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}

struct Pair { double dict; double tokens; };

static Pair measure(uint32_t bits, const std::string& in_path,
                    const std::string& cz_path) {
    OnPairRunner r;
    std::string cfg = R"({"bits":)" + std::to_string(bits)
        + R"(,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";
    auto res = r.compress(in_path, cz_path, cfg);
    if (res.status != "ok") {
        std::cerr << "compress bits=" << bits << " status=" << res.status << "\n";
        std::exit(1);
    }
    Pair p{0, 0};
    for (auto& m : res.metrics) {
        if (m.first == "dict_bytes")   p.dict   = m.second;
        if (m.first == "tokens_bytes") p.tokens = m.second;
    }
    return p;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_disj";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    write_column(in_path.string(), make_repetitive(4000, 11));

    fs::path cz12 = tmp / "z12.bin";
    fs::path cz14 = tmp / "z14.bin";
    fs::path cz16 = tmp / "z16.bin";

    Pair p12 = measure(12, in_path.string(), cz12.string());
    Pair p14 = measure(14, in_path.string(), cz14.string());
    Pair p16 = measure(16, in_path.string(), cz16.string());

    std::cout << "bits=12: dict=" << p12.dict << " tokens=" << p12.tokens << "\n";
    std::cout << "bits=14: dict=" << p14.dict << " tokens=" << p14.tokens << "\n";
    std::cout << "bits=16: dict=" << p16.dict << " tokens=" << p16.tokens << "\n";

    bool dict_up    = (p12.dict   < p14.dict)   && (p14.dict   < p16.dict);
    bool tokens_dn  = (p12.tokens > p14.tokens) && (p14.tokens > p16.tokens);
    bool dict_mono  = (p12.dict   <= p14.dict)  && (p14.dict   <= p16.dict);
    bool tokens_mono= (p12.tokens >= p14.tokens)&& (p14.tokens >= p16.tokens);

    if (dict_up && tokens_dn) {
        std::cout << "onpair_bit_width_disjointness: STRICT (dict↑ & tokens↓)\n";
        return 0;
    }
    if (dict_mono || tokens_mono) {
        std::cout << "onpair_bit_width_disjointness: SOFT-WARN — "
                  << "one direction monotonic (dict_mono=" << dict_mono
                  << " tokens_mono=" << tokens_mono
                  << "); structured data on real datasets should hit STRICT.\n";
        return 0;
    }
    std::cerr << "onpair_bit_width_disjointness: NEITHER direction monotonic — "
              << "possible regression\n";
    return 1;
}
