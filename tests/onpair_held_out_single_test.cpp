// A3 ablation: held_out_compress single-strategy contract.
//
// Generates a 200-row synthetic column, manually splits 160/40 train/heldout,
// runs the held_out_compress op in single-strategy mode
// (threshold_strategy="dynamic", sample_fraction=0.30), asserts:
//   (a) status == "ok"
//   (b) sample_bytes_consumed > 0 and effective_fraction in (0, 1]
//   (c) bit-tight identity: dict_bytes + tokens_bytes + boundaries_bytes
//        == heldout_output_bytes
//   (d) heldout_ratio > 0; num_dict_tokens > 0; train_ns/encode_ns >= 0
//   (e) the paired A1 metric names (_fixed/_dyn) are ABSENT (single block only)

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cassert>
#include <cstdint>
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
    const auto tmp = std::filesystem::temp_directory_path() / "onpair_held_out_single_test";
    std::filesystem::create_directories(tmp);
    const auto train_path   = tmp / "train.col";
    const auto heldout_path = tmp / "heldout.col";

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
        "threshold_strategy":"dynamic", "sample_fraction":0.30
    })";
    auto res = r.held_out_compress(train_path.string(), heldout_path.string(),
                                   "/dev/null", cfg);
    if (res.status != "ok") {
        std::cerr << "held_out_compress(single) FAILED: status=" << res.status
                  << " notes=" << res.notes << "\n";
        return 1;
    }
    auto m = metrics_to_map(res.metrics);

    // (b) training observables.
    assert(m.at("sample_bytes_consumed") > 0.0);
    const double eff = m.at("effective_fraction");
    if (!(eff > 0.0 && eff <= 1.0)) {
        std::cerr << "effective_fraction out of (0,1]: " << eff << "\n";
        return 2;
    }

    // (c) bit-tight identity.
    const double sum = m.at("dict_bytes") + m.at("tokens_bytes")
                     + m.at("boundaries_bytes");
    if (sum != m.at("heldout_output_bytes")) {
        std::cerr << "bit-tight identity broken: " << sum
                  << " vs " << m.at("heldout_output_bytes") << "\n";
        return 3;
    }

    // (d) ratio + dict + timing sanity.
    assert(m.at("heldout_ratio") > 0.0);
    assert(m.at("num_dict_tokens") > 0.0);
    assert(m.at("train_ns")  >= 0.0);
    assert(m.at("encode_ns") >= 0.0);

    // (e) single block only — paired A1 names must be absent.
    if (m.count("heldout_ratio_fixed") || m.count("heldout_ratio_dyn")
        || m.count("matched_fraction")) {
        std::cerr << "unexpected paired (A1) metric names in single-strategy result\n";
        return 4;
    }

    std::cout << "onpair_held_out_single_test OK\n";
    std::cout << "  sample_bytes_consumed = " << m.at("sample_bytes_consumed") << "\n";
    std::cout << "  effective_fraction    = " << eff << "\n";
    std::cout << "  num_dict_tokens       = " << m.at("num_dict_tokens") << "\n";
    std::cout << "  heldout_ratio         = " << m.at("heldout_ratio") << "\n";
    return 0;
}
