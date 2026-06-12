// Unit tests for runners/core/bitpack_mixed.{h,cpp} — mixed-width
// packing helper. OnPair may reuse the degenerate uniform-width case.
//
// Coverage:
//   (a) Round-trip at heterogeneous widths
//       {(10,16,8), (12,16,8), (18,16,8), (24,16,8), (32,16,8),
//        (10,12,8), (10,10,10), (32,32,0)}.
//   (b) Empty stream edge.
//   (c) Single-triple edge.
//   (d) Multi-byte-boundary stress (32+16+8 = 56 bits per triple — crosses
//       byte boundaries every triple).
//   (e) LSB-first byte order spot-check (well-known triple → known byte).

#include "bitpack_mixed.h"

#include <cstdio>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using namespace onpair_bench;

static uint32_t mask_field(uint32_t v, uint32_t bits) {
    if (bits == 0) return 0;
    if (bits >= 32) return v;
    return v & ((1u << bits) - 1);
}

static int round_trip(const std::string& label,
                      uint32_t bits_pos, uint32_t bits_len, uint32_t bits_lit,
                      size_t n, uint32_t seed) {
    std::mt19937 rng(seed);
    std::vector<uint32_t> pos(n), len(n), lit(n);
    for (size_t i = 0; i < n; ++i) {
        pos[i] = mask_field(rng(), bits_pos);
        len[i] = mask_field(rng(), bits_len);
        lit[i] = mask_field(rng(), bits_lit);
    }
    std::vector<uint8_t> packed;
    bitpack_mixed::pack_triples(pos, len, lit, bits_pos, bits_len, bits_lit, packed);

    const uint32_t total_bits = bits_pos + bits_len + bits_lit;
    const size_t expected_bytes =
        bitpack_mixed::packed_bytes_for(n, total_bits);
    if (packed.size() != expected_bytes) {
        std::cerr << "[bpm_unit] " << label << ": packed.size()="
                  << packed.size() << " expected=" << expected_bytes << "\n";
        return 1;
    }

    std::vector<uint32_t> out_pos, out_len, out_lit;
    bitpack_mixed::unpack_triples(packed, bits_pos, bits_len, bits_lit, n,
                                   out_pos, out_len, out_lit);
    if (out_pos != pos || out_len != len || out_lit != lit) {
        std::cerr << "[bpm_unit] " << label << ": round-trip MISMATCH\n";
        return 1;
    }
    return 0;
}

int main() {
    int fail = 0;

    // (a) Heterogeneous widths.
    fail |= round_trip("10_16_8",   10, 16,  8, 1000, 1);
    fail |= round_trip("12_16_8",   12, 16,  8, 1000, 2);
    fail |= round_trip("18_16_8",   18, 16,  8, 1000, 3);
    fail |= round_trip("24_16_8",   24, 16,  8, 1000, 4);
    fail |= round_trip("32_16_8",   32, 16,  8, 1000, 5);
    fail |= round_trip("10_12_8",   10, 12,  8, 1000, 6);
    fail |= round_trip("10_10_10",  10, 10, 10, 1000, 7);
    fail |= round_trip("32_32_0",   32, 32,  0, 1000, 8);

    // (b) Empty stream.
    {
        std::vector<uint32_t> p, l, x;
        std::vector<uint8_t> packed;
        bitpack_mixed::pack_triples(p, l, x, 10, 16, 8, packed);
        if (!packed.empty()) {
            std::cerr << "[bpm_unit] empty: packed not empty\n"; ++fail;
        }
        std::vector<uint32_t> a, b, c;
        bitpack_mixed::unpack_triples(packed, 10, 16, 8, 0, a, b, c);
        if (!a.empty() || !b.empty() || !c.empty()) {
            std::cerr << "[bpm_unit] empty: unpack produced non-empty\n"; ++fail;
        }
    }

    // (c) Single triple.
    {
        std::vector<uint32_t> p = {0x3FFu};   // 10 bits
        std::vector<uint32_t> l = {0xFFFFu};  // 16 bits
        std::vector<uint32_t> x = {0xFFu};    // 8 bits
        std::vector<uint8_t> packed;
        bitpack_mixed::pack_triples(p, l, x, 10, 16, 8, packed);
        // 10+16+8 = 34 bits → 5 bytes.
        if (packed.size() != 5) {
            std::cerr << "[bpm_unit] single: bytes=" << packed.size() << "\n"; ++fail;
        }
        std::vector<uint32_t> oa, ob, oc;
        bitpack_mixed::unpack_triples(packed, 10, 16, 8, 1, oa, ob, oc);
        if (oa[0] != 0x3FFu || ob[0] != 0xFFFFu || oc[0] != 0xFFu) {
            std::cerr << "[bpm_unit] single: round-trip MISMATCH\n"; ++fail;
        }
    }

    // (d) Multi-byte-boundary stress.
    fail |= round_trip("stress_56", 32, 16, 8, 4096, 99);

    // (e) LSB-first byte-order spot-check.
    // 8-bit pos + 8-bit len + 8-bit lit = 24 bits = 3 bytes per triple.
    // Triple (0x01, 0x02, 0x03) packs as bytes [0x01, 0x02, 0x03] (LSB-first).
    {
        std::vector<uint32_t> p = {0x01u};
        std::vector<uint32_t> l = {0x02u};
        std::vector<uint32_t> x = {0x03u};
        std::vector<uint8_t> packed;
        bitpack_mixed::pack_triples(p, l, x, 8, 8, 8, packed);
        if (packed.size() != 3 || packed[0] != 0x01 || packed[1] != 0x02 || packed[2] != 0x03) {
            std::cerr << "[bpm_unit] LSB-first spot-check FAIL: bytes={"
                      << std::hex << static_cast<int>(packed[0]) << ","
                      << static_cast<int>(packed[1]) << ","
                      << static_cast<int>(packed[2]) << "}\n";
            ++fail;
        }
    }

    if (fail == 0)
        std::cout << "bitpack_mixed_unit_test: OK\n";
    return fail;
}
