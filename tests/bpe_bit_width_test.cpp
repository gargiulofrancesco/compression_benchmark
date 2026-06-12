// BPE card load-bearing for: first non-power-of-2 packing.
// At each V in {4K, 16K, 64K, 256K} after compress, the wire layout must
// satisfy:
//   packed_tokens region size == ceil(T * B / 8) with B = ceil(log2(V))
// We parse the wire bytewise to recover M/N/V/T and compute the expected
// packed region size; assert against actual wire-tail bytes.
// Also asserts bit-tight identity at every V.

#include "bpe_runner.h"
#include "bitpack.h"
#include "runner_protocol.h"

#include <cstdint>
#include <cstring>  // std::memcpy — explicit; libstdc++ (gcc) doesn't pull it in transitively like libc++
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
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
    fs::path tmp = fs::temp_directory_path() / "onpair_bpe_bit";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "in.col";
    fs::path cz_path = tmp / "z.bin";

    // Modest column; goal is exercise of the wire layout, not throughput.
    Column c;
    std::mt19937 rng(42);
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < 200; ++i) {
        int L = 16 + static_cast<int>(rng() % 48);
        for (int j = 0; j < L; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 26)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    const uint32_t Vs[4] = {4096, 16384, 65536, 262144};
    const uint32_t expected_B[4] = {12, 14, 16, 18};

    int fail = 0;
    for (int idx = 0; idx < 4; ++idx) {
        uint32_t V = Vs[idx];
        BpeRunner r;
        const std::string cfg =
            "{\"vocab_size\":" + std::to_string(V)
            + ",\"byte_fallback\":true,\"normalization\":\"identity\","
              "\"threads\":1,\"mem_cap_gb\":4}";
        auto res = r.compress(in_path.string(), cz_path.string(), cfg);
        if (res.status != "ok") {
            std::cerr << "compress at V=" << V << " status=" << res.status << "\n";
            ++fail; continue;
        }

        const uint32_t B = bitpack::bits_for_vocab(V);
        if (B != expected_B[idx]) {
            std::cerr << "bits_for_vocab(" << V << ")=" << B
                      << " expected " << expected_B[idx] << "\n";
            ++fail;
        }

        // Parse the wire to recover M, N, V_wire, T and the packed region size.
        std::ifstream f(cz_path.string(), std::ios::binary);
        std::vector<uint8_t> wire((std::istreambuf_iterator<char>(f)),
                                   std::istreambuf_iterator<char>());
        if (wire.size() < 16) { std::cerr << "wire too short at V=" << V << "\n"; ++fail; continue; }
        uint32_t M = 0;        std::memcpy(&M, wire.data() + 0, 4);
        uint32_t N_wire = 0;   std::memcpy(&N_wire, wire.data() + 4 + M, 4);
        uint32_t V_wire = 0;   std::memcpy(&V_wire, wire.data() + 4 + M + 4, 4);
        uint32_t T = 0;        std::memcpy(&T, wire.data() + 4 + M + 8, 4);

        if (V_wire != V) {
            std::cerr << "V_wire=" << V_wire << " expected " << V << "\n";
            ++fail;
        }

        const size_t expected_packed = (size_t{T} * B + 7) / 8;
        // Header = [M u32][model M][N u32][V u32][T u32][decoded_bytes u64]
        //          [tok_offsets (N+1) u32]. The decoded_bytes u64 (8) lets
        //          bulk_decode size its output buffer exactly outside the timer.
        const size_t header_size = 4u + M + 4u + 4u + 4u + 8u + 4u * (N_wire + 1);
        const size_t actual_packed = wire.size() - header_size;
        if (actual_packed != expected_packed) {
            std::cerr << "V=" << V << " B=" << B << " T=" << T
                      << " expected_packed=" << expected_packed
                      << " actual_packed=" << actual_packed << "\n";
            ++fail;
        }

        // Bit-tight identity at this V.
        const uint64_t dict_b   = static_cast<uint64_t>(metric(res, "dict_bytes"));
        const uint64_t tokens_b = static_cast<uint64_t>(metric(res, "tokens_bytes"));
        const uint64_t bound_b  = static_cast<uint64_t>(metric(res, "boundaries_bytes"));
        const uint64_t out_b    = static_cast<uint64_t>(metric(res, "output_bytes"));
        if (dict_b + tokens_b + bound_b != out_b) {
            std::cerr << "V=" << V << " bit-tight violation: "
                      << dict_b << "+" << tokens_b << "+" << bound_b
                      << " != " << out_b << "\n";
            ++fail;
        }
        // 24 = wire scalars M,N,V,T (16) + decoded_bytes u64 (8); + (N+1)*4 offsets.
        const uint64_t expected_bound = 24 + 4 * static_cast<uint64_t>(N_wire + 1);
        if (bound_b != expected_bound) {
            std::cerr << "V=" << V << " boundaries_bytes=" << bound_b
                      << " expected " << expected_bound << "\n";
            ++fail;
        }
    }
    fs::remove_all(tmp);
    return fail;
}
