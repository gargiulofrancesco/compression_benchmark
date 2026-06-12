// step 2 — A1 ablation: held_out_compress paired protocol contract.
//
// Generates a 200-row synthetic column, manually splits 160/40 train/heldout,
// runs the held_out_compress op at T=10, asserts:
//   (a) sample_bytes_consumed_fixed > 0
//   (b) matched_fraction == sample_bytes_consumed_fixed / train_input_bytes
//   (c) bit-tight identity for BOTH Fixed and Dynamic blocks:
//        dict_bytes_d + tokens_bytes_d + boundaries_bytes_d == heldout_output_bytes_d
//   (d) both heldout_ratio_d > 0

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cassert>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <map>
#include <random>
#include <string>

using namespace onpair_bench;


static Column make_synthetic(uint32_t n_rows, size_t bytes_per_row, uint32_t seed) {
    std::mt19937 rng(seed);
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < n_rows; ++i) {
        for (size_t j = 0; j < bytes_per_row; ++j)
            c.bytes.push_back(static_cast<uint8_t>('a' + (rng() % 16)));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    return c;
}


static Column slice_rows(const Column& src, uint32_t row_start, uint32_t row_end) {
    Column out;
    const uint32_t byte_start = src.offsets[row_start];
    const uint32_t byte_end   = src.offsets[row_end];
    out.bytes.insert(out.bytes.end(),
                     src.bytes.begin() + byte_start,
                     src.bytes.begin() + byte_end);
    for (uint32_t i = row_start; i <= row_end; ++i)
        out.offsets.push_back(src.offsets[i] - byte_start);
    return out;
}


static std::map<std::string, double> metrics_to_map(
        const std::vector<std::pair<std::string, double>>& m) {
    std::map<std::string, double> out;
    for (const auto& [k, v] : m) out[k] = v;
    return out;
}


int main() {
    const auto tmp = std::filesystem::temp_directory_path() / "onpair_held_out_test";
    std::filesystem::create_directories(tmp);
    const auto train_path   = tmp / "train.col";
    const auto heldout_path = tmp / "heldout.col";

    // 200-row synthetic column, 20-char rows over alphabet of 16 symbols.
    Column whole = make_synthetic(200, 20, 12345);
    const uint32_t N = static_cast<uint32_t>(whole.num_rows());
    const uint32_t n_train = (N * 80) / 100;  // 160
    Column col_train   = slice_rows(whole, 0, n_train);
    Column col_heldout = slice_rows(whole, n_train, N);

    write_column(train_path.string(),   col_train);
    write_column(heldout_path.string(), col_heldout);

    OnPairRunner r;
    const std::string cfg = R"({
        "bits":16, "seed":42, "mem_cap_gb":4,
        "threshold_strategy":"fixed_dynamic_paired", "threshold_value":10
    })";
    auto res = r.held_out_compress(train_path.string(), heldout_path.string(),
                                   "/dev/null", cfg);
    if (res.status != "ok") {
        std::cerr << "held_out_compress FAILED: status=" << res.status
                  << " notes=" << res.notes << "\n";
        return 1;
    }
    auto m = metrics_to_map(res.metrics);

    // (a) sample_bytes_consumed_fixed > 0
    assert(m.at("sample_bytes_consumed_fixed") > 0.0);

    // (b) matched_fraction == sample_bytes_consumed_fixed / train_input_bytes
    const double observed_fraction =
        m.at("sample_bytes_consumed_fixed") / m.at("train_input_bytes");
    const double diff = std::abs(observed_fraction - m.at("matched_fraction"));
    if (diff > 1e-9) {
        std::cerr << "matched_fraction mismatch: got " << m.at("matched_fraction")
                  << ", expected " << observed_fraction << "\n";
        return 2;
    }

    // (c) bit-tight identity for BOTH blocks
    for (const std::string& d : {std::string("fixed"), std::string("dyn")}) {
        const double sum = m.at("dict_bytes_" + d)
                         + m.at("tokens_bytes_" + d)
                         + m.at("boundaries_bytes_" + d);
        const double observed = m.at("heldout_output_bytes_" + d);
        if (sum != observed) {
            std::cerr << "bit-tight identity broken for " << d
                      << ": " << sum << " vs " << observed << "\n";
            return 3;
        }
    }

    // (d) both heldout_ratio_d > 0
    assert(m.at("heldout_ratio_fixed") > 0.0);
    assert(m.at("heldout_ratio_dyn") > 0.0);

    std::cout << "onpair_held_out_compress_test OK\n";
    std::cout << "  sample_bytes_consumed_fixed = "
              << m.at("sample_bytes_consumed_fixed") << "\n";
    std::cout << "  matched_fraction            = "
              << m.at("matched_fraction") << "\n";
    std::cout << "  heldout_ratio_fixed         = "
              << m.at("heldout_ratio_fixed") << "\n";
    std::cout << "  heldout_ratio_dyn           = "
              << m.at("heldout_ratio_dyn") << "\n";
    return 0;
}
