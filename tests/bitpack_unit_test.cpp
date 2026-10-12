// Unit tests for runners/core/bitpack.{h,cpp}. Not BPE-specific: the
// repair runner uses the same archive. Coverage: bits_for_vocab
// boundary values + pack/unpack round-trip at B in {1,7,8,9,12,14,16,17,
// 18,24,32} + empty + single-token + multi-byte-boundary stress + LSB-first
// byte-order assertion.

#include "bitpack.h"

#include <cstdint>
#include <iostream>
#include <random>
#include <vector>

using namespace onpair_bench::bitpack;

static int fail_count = 0;

#define CHECK(cond, msg) do { if (!(cond)) { \
    std::cerr << "[bitpack_unit] FAIL " << msg << "\n"; ++fail_count; } } while (0)

static bool roundtrip(uint32_t B, const std::vector<uint32_t>& tokens) {
    std::vector<uint8_t> packed;
    pack_uniform(tokens, B, packed);
    const size_t expected_bytes = (tokens.size() * static_cast<size_t>(B) + 7) / 8;
    if (packed.size() != expected_bytes) {
        std::cerr << "[bitpack_unit] B=" << B << " packed.size()=" << packed.size()
                  << " expected " << expected_bytes << "\n";
        return false;
    }
    std::vector<uint32_t> out;
    unpack_uniform(packed, B, tokens.size(), out);
    return out == tokens;
}

int main() {
    // bits_for_vocab boundaries.
    CHECK(bits_for_vocab(0)       == 0,  "bits_for_vocab(0)");
    CHECK(bits_for_vocab(1)       == 0,  "bits_for_vocab(1)");
    CHECK(bits_for_vocab(2)       == 1,  "bits_for_vocab(2)");
    CHECK(bits_for_vocab(3)       == 2,  "bits_for_vocab(3)");
    CHECK(bits_for_vocab(4)       == 2,  "bits_for_vocab(4)");
    CHECK(bits_for_vocab(255)     == 8,  "bits_for_vocab(255)");
    CHECK(bits_for_vocab(256)     == 8,  "bits_for_vocab(256)");
    CHECK(bits_for_vocab(257)     == 9,  "bits_for_vocab(257)");
    CHECK(bits_for_vocab(4096)    == 12, "bits_for_vocab(4096) V=4K");
    CHECK(bits_for_vocab(16384)   == 14, "bits_for_vocab(16384) V=16K");
    CHECK(bits_for_vocab(65536)   == 16, "bits_for_vocab(65536) V=64K");
    CHECK(bits_for_vocab(262144)  == 18, "bits_for_vocab(262144) V=256K");

    // Empty + single token edges.
    {
        std::vector<uint8_t> packed;
        pack_uniform({}, 12, packed);
        CHECK(packed.empty(), "pack empty");
        std::vector<uint32_t> out;
        unpack_uniform({}, 12, 0, out);
        CHECK(out.empty(), "unpack empty");
    }
    {
        std::vector<uint32_t> in = {0x123u}; // 12-bit value
        CHECK(roundtrip(12, in), "single token B=12");
    }

    // LSB-first byte-order assertion: pack [0x1, 0x2] at B=8 → bytes [0x01, 0x02]
    {
        std::vector<uint32_t> in = {0x01u, 0x02u};
        std::vector<uint8_t> packed;
        pack_uniform(in, 8, packed);
        CHECK(packed.size() == 2 && packed[0] == 0x01 && packed[1] == 0x02,
              "LSB-first B=8 byte order");
    }
    // Pack [0xFFF, 0x000] at B=12 → 24 bits → 3 bytes: 0xFF 0x0F 0x00
    {
        std::vector<uint32_t> in = {0xFFFu, 0x000u};
        std::vector<uint8_t> packed;
        pack_uniform(in, 12, packed);
        CHECK(packed.size() == 3, "B=12 two tokens → 3 bytes");
        CHECK(packed[0] == 0xFF, "B=12 byte 0");
        CHECK((packed[1] & 0x0F) == 0x0F, "B=12 low nibble of byte 1");
        CHECK((packed[1] >> 4) == 0x00, "B=12 high nibble of byte 1 (token #2 low bits)");
        CHECK(packed[2] == 0x00, "B=12 byte 2");
    }

    // Roundtrips at the headline bit-widths + edges.
    const uint32_t widths[] = {1, 7, 8, 9, 12, 14, 16, 17, 18, 24, 32};
    for (uint32_t B : widths) {
        std::mt19937 rng(B);
        const uint64_t max_v = (B == 32) ? 0xFFFFFFFFull : ((1ull << B) - 1ull);
        std::vector<uint32_t> tokens;
        for (size_t i = 0; i < 1000; ++i) {
            tokens.push_back(static_cast<uint32_t>(rng() & max_v));
        }
        CHECK(roundtrip(B, tokens), "roundtrip B=" + std::to_string(B));
    }

    // Multi-byte-boundary stress: 100 tokens at B=18 → 1800 bits → 225 bytes
    {
        std::vector<uint32_t> tokens;
        std::mt19937 rng(99);
        for (int i = 0; i < 100; ++i) tokens.push_back(rng() & 0x3FFFFu);
        std::vector<uint8_t> packed;
        pack_uniform(tokens, 18, packed);
        CHECK(packed.size() == 225, "B=18 N=100 packed size");
        std::vector<uint32_t> out;
        unpack_uniform(packed, 18, 100, out);
        CHECK(out == tokens, "B=18 N=100 roundtrip");
    }

    if (fail_count) std::cerr << "[bitpack_unit] " << fail_count << " failures\n";
    else            std::cout << "[bitpack_unit] OK\n";
    return fail_count ? 1 : 0;
}
