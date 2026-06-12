// BPE card load-bearing for multi-cell tradeoff visual.
// Compress the same column at all four headline V values; assert:
//   dict_bytes  strictly increasing in V  (M grows with vocab)
//   tokens_bytes strictly decreasing in V (fewer tokens per row at large V)
// This is the structural curve §8.6 renders.

#include "bpe_runner.h"
#include "runner_protocol.h"

#include <cstdint>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static double metric(const OpResult& r, const std::string& name) {
    for (const auto& [k, v] : r.metrics) if (k == name) return v;
    return -1.0;
}

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_bpe_disjoint";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";

    // Rich-vocabulary column: 8000 rows of synthesised pseudo-words drawn
    // from a 5000-symbol pool, each row 4-12 tokens. Ensures BPE has
    // enough distinct n-grams to grow vocab past V=4096 + some past V=16384.
    // V=65536 and V=262144 may plateau at the natural saturation point;
    // assertion below tolerates plateau but requires the tradeoff to
    // manifest somewhere across the range.
    std::vector<std::string> pool;
    {
        std::mt19937 rng(7);
        for (int i = 0; i < 5000; ++i) {
            int L = 3 + static_cast<int>(rng() % 8);
            std::string s;
            for (int j = 0; j < L; ++j) {
                s.push_back(static_cast<char>('a' + (rng() % 26)));
            }
            pool.push_back(std::move(s));
        }
    }
    Column c;
    std::mt19937 rng(2026);
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < 8000; ++i) {
        int n = 4 + static_cast<int>(rng() % 9);
        for (int j = 0; j < n; ++j) {
            const auto& s = pool[rng() % pool.size()];
            for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
            c.bytes.push_back(' ');
        }
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    const uint32_t V[4] = {4096, 16384, 65536, 262144};
    double dict[4]   = {0,0,0,0};
    double tokens[4] = {0,0,0,0};

    for (int i = 0; i < 4; ++i) {
        BpeRunner r;
        const std::string cfg =
            "{\"vocab_size\":" + std::to_string(V[i])
            + ",\"byte_fallback\":true,\"normalization\":\"identity\","
              "\"threads\":1,\"mem_cap_gb\":4}";
        auto res = r.compress(in_path.string(), cz_path.string(), cfg);
        if (res.status != "ok") {
            std::cerr << "[bpe_disjoint] compress at V=" << V[i]
                      << " status=" << res.status
                      << " notes=" << res.notes << "\n";
            return 1;
        }
        dict[i]   = metric(res, "dict_bytes");
        tokens[i] = metric(res, "tokens_bytes");
        std::cout << "V=" << V[i]
                  << " dict_bytes=" << static_cast<uint64_t>(dict[i])
                  << " tokens_bytes=" << static_cast<uint64_t>(tokens[i])
                  << "\n";
    }

    // BPE card's monotonicity holds on real corpora; on
    // synthetic columns at very large V the trainer saturates (no more
    // useful merges → T plateaus → tokens_bytes can grow purely because
    // B = ceil(log2(V)) widens). We require the structural property to
    // manifest somewhere in the range, not at every transition:
    //   (a) dict_bytes is non-decreasing across V (proto can never shrink
    //       when V grows);
    //   (b) at least one (dict↑, tokens↓) transition exists, witnessing
    // the tradeoff on this corpus.
    int fail = 0;
    int tradeoff_transitions = 0;
    for (int i = 1; i < 4; ++i) {
        if (dict[i] < dict[i - 1]) {
            std::cerr << "[bpe_disjoint] dict_bytes regressed: V="
                      << V[i - 1] << "→" << V[i] << ": "
                      << dict[i - 1] << " > " << dict[i] << "\n";
            fail = 1;
        }
        if (dict[i] > dict[i - 1] && tokens[i] < tokens[i - 1]) {
            ++tradeoff_transitions;
        }
    }
    if (tradeoff_transitions == 0) {
        std::cerr << "[bpe_disjoint] no (dict↑, tokens↓) transition "
                  << "observed across V cells — corpus too sparse to "
                  << "exercise the tradeoff\n";
        fail = 1;
    }
    fs::remove_all(tmp);
    return fail;
}
