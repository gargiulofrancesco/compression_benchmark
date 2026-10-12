// Non-overlapping timers for OnPair's training finalization (stage `finalize`).
// This is an experiment-only, timed clone of the pinned production
// trainer. The clone is validated against onpair::encoding::train() for every
// input before measurements are emitted.
//
// Source mirrored from the pinned trainer as patched by
// external/onpair_local_shims.patch (SHA-256 of the patched trainer.cpp):
//   7f93c9d53d0a7d191f2e6f0f4bd2db9f11f579add79c8e4a7e67a9469b23af76
// Only phase boundaries and result bookkeeping differ.

#include <onpair/core/dictionary_view.h>
#include <onpair/core/store.h>
#include <onpair/core/types.h>
#include <onpair/encoding/fast_hash.h>
#include <onpair/encoding/parsing/parser.h>
#include <onpair/encoding/training/config.h>
#include <onpair/encoding/training/dynamic_threshold.h>
#include <onpair/encoding/training/trainer.h>

#include <boost/unordered/unordered_flat_map.hpp>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <optional>
#include <random>
#include <sched.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

namespace diagnostic {

using Clock = std::chrono::steady_clock;

uint64_t elapsed_ns(Clock::time_point begin, Clock::time_point end)
{
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(end - begin).count());
}

struct PhaseTimes {
    uint64_t discovery_ns = 0;
    uint64_t permutation_ns = 0;
    uint64_t relayout_ns = 0;
    uint64_t lpm_rebuild_ns = 0;
    uint64_t finalization_ns = 0;
    uint64_t padding_ns = 0;
    uint64_t train_ns = 0;
};

struct TimedTrainResult {
    onpair::Dictionary dict;
    onpair::encoding::LongestPrefixMatcher lpm;
    uint64_t sample_bytes_consumed = 0;
    PhaseTimes times;
};

void finalize_dictionary(TimedTrainResult& result)
{
    using namespace onpair;
    using namespace onpair::encoding;

    const auto finalization_begin = Clock::now();
    const DictionaryView src(result.dict);
    const size_t n = src.num_tokens();

    const auto permutation_begin = Clock::now();
    std::vector<Token> perm(n);
    std::iota(perm.begin(), perm.end(), Token(0));
    std::sort(perm.begin(), perm.end(), [&](const Token& a, const Token& b) {
        const int cmp = std::memcmp(src.data(a), src.data(b),
                                    std::min(src.token_size(a), src.token_size(b)));
        return cmp != 0 ? cmp < 0 : src.token_size(a) < src.token_size(b);
    });
    const auto permutation_end = Clock::now();

    Dictionary sorted_dict;
    sorted_dict.bytes.reserve(src.bytes_used());
    sorted_dict.offsets.reserve(n + 1);
    sorted_dict.offsets.push_back(0);
    for (size_t new_id = 0; new_id < n; ++new_id) {
        const Token old_id = perm[new_id];
        const ByteSpan span = src.span(old_id);
        sorted_dict.bytes.insert(sorted_dict.bytes.end(),
                                 src.raw_bytes() + span.begin,
                                 src.raw_bytes() + span.end);
        sorted_dict.offsets.push_back(uint32_t(sorted_dict.bytes.size()));
    }
    result.dict = std::move(sorted_dict);
    const auto relayout_end = Clock::now();

    result.lpm = LongestPrefixMatcher::from_dictionary(DictionaryView(result.dict));
    const auto lpm_end = Clock::now();

    result.times.permutation_ns = elapsed_ns(permutation_begin, permutation_end);
    result.times.relayout_ns = elapsed_ns(permutation_end, relayout_end);
    result.times.lpm_rebuild_ns = elapsed_ns(relayout_end, lpm_end);
    result.times.finalization_ns = elapsed_ns(finalization_begin, lpm_end);
}

TimedTrainResult train_timed(const uint8_t* data,
                             const uint32_t* offsets,
                             size_t n,
                             const onpair::encoding::TrainingConfig& cfg)
{
    using namespace onpair;
    using namespace onpair::encoding;

    const auto train_begin = Clock::now();
    TimedTrainResult result;

    const size_t dict_capacity = max_dict_size(cfg.bits);
    const size_t total_bytes = offsets[n];
    result.lpm.reserve(dict_capacity);
    result.dict.offsets.reserve(dict_capacity + 1);
    result.dict.bytes.reserve(dict_capacity * MAX_TOKEN_SIZE);
    result.dict.offsets.push_back(0);

    for (uint16_t i = 0; i <= 255; ++i) {
        const uint8_t byte = uint8_t(i);
        result.dict.bytes.push_back(byte);
        result.dict.offsets.push_back(uint32_t(result.dict.bytes.size()));
    }

    uint8_t threshold;
    std::optional<DynamicThresholdController> dynamic;
    if (const auto* fixed = std::get_if<FixedThreshold>(&cfg.threshold)) {
        threshold = fixed->value;
    } else {
        const auto& dynamic_cfg = std::get<DynamicThreshold>(cfg.threshold);
        const size_t capacity = dict_capacity - 256;
        dynamic.emplace(capacity, total_bytes, dynamic_cfg.sample_fraction);
        threshold = dynamic->get();
    }

    std::mt19937_64 rng(cfg.seed.value_or(std::random_device{}()));
    std::vector<uint32_t> order(n);
    std::iota(order.begin(), order.end(), 0u);
    std::shuffle(order.begin(), order.end(), rng);

    boost::unordered_flat_map<uint32_t, uint8_t,
                              onpair::encoding::detail::FastUint32Hash> frequency;
    const size_t frequency_capacity =
        std::min(std::max(dict_capacity * 8, size_t(1024)),
                 std::max(total_bytes / 2, size_t(1024)));
    frequency.reserve(frequency_capacity);

    bool full_dictionary = false;
    bool budget_exhausted = false;
    for (uint32_t index : order) {
        if (full_dictionary || budget_exhausted) break;

        const uint8_t* string = data + offsets[index];
        const size_t length = offsets[index + 1] - offsets[index];
        if (length == 0) continue;

        auto first = result.lpm.find_longest_match(string, length);
        Token previous_id = first.first;
        size_t previous_length = first.second;
        size_t position = previous_length;
        result.sample_bytes_consumed += previous_length;

        if (dynamic) {
            dynamic->on_bytes_scanned(previous_length);
            budget_exhausted = dynamic->budget_exhausted();
            if (budget_exhausted) break;
        }

        while (position < length) {
            auto match = result.lpm.find_longest_match(
                string + position, length - position);
            const Token current_id = match.first;
            const size_t current_length = match.second;
            result.sample_bytes_consumed += current_length;

            if (dynamic) {
                dynamic->on_bytes_scanned(current_length);
                budget_exhausted = dynamic->budget_exhausted();
                if (budget_exhausted) break;
            }

            const size_t pair_length = previous_length + current_length;
            if (pair_length <= MAX_TOKEN_SIZE) {
                const uint32_t key =
                    (uint32_t(previous_id) << 16) | uint32_t(current_id);
                auto& count = frequency[key];
                count += (count < 255);
                if (count >= threshold) {
                    const Token new_id = result.lpm.insert(
                        string + position - previous_length, pair_length);
                    result.dict.bytes.insert(
                        result.dict.bytes.end(),
                        string + position - previous_length,
                        string + position + current_length);
                    result.dict.offsets.push_back(
                        uint32_t(result.dict.bytes.size()));

                    if (result.lpm.size() == dict_capacity) {
                        full_dictionary = true;
                        break;
                    }

                    if (dynamic) {
                        dynamic->on_entry_created();
                        threshold = dynamic->get();
                    }

                    frequency.erase(key);
                    previous_id = new_id;
                    previous_length = pair_length;
                    position += current_length;
                    continue;
                }
            }
            previous_id = current_id;
            previous_length = current_length;
            position += current_length;
        }
    }

    const auto finalization_begin = Clock::now();
    finalize_dictionary(result);
    const auto padding_begin = Clock::now();
    result.dict.pad_for_decoder();
    const auto train_end = Clock::now();

    result.times.discovery_ns = elapsed_ns(train_begin, finalization_begin);
    result.times.finalization_ns = elapsed_ns(finalization_begin, padding_begin);
    result.times.padding_ns = elapsed_ns(padding_begin, train_end);
    result.times.train_ns = elapsed_ns(train_begin, train_end);
    return result;
}

struct Column {
    std::vector<uint8_t> bytes;
    std::vector<uint32_t> offsets;
    size_t num_rows() const noexcept {
        return offsets.empty() ? 0 : offsets.size() - 1;
    }
};

Column read_column(const std::string& path)
{
    std::ifstream stream(path, std::ios::binary);
    if (!stream) throw std::runtime_error("cannot open column: " + path);
    uint32_t rows = 0;
    uint32_t bytes = 0;
    stream.read(reinterpret_cast<char*>(&rows), sizeof(rows));
    stream.read(reinterpret_cast<char*>(&bytes), sizeof(bytes));
    Column column;
    column.offsets.resize(size_t(rows) + 1);
    stream.read(reinterpret_cast<char*>(column.offsets.data()),
                std::streamsize(column.offsets.size() * sizeof(uint32_t)));
    column.bytes.resize(bytes);
    stream.read(reinterpret_cast<char*>(column.bytes.data()),
                std::streamsize(column.bytes.size()));
    if (!stream || column.offsets.back() != bytes)
        throw std::runtime_error("invalid or truncated column: " + path);
    return column;
}

void validate_clone(const Column& column,
                    const onpair::encoding::TrainingConfig& config)
{
    auto canonical = onpair::encoding::train(
        column.bytes.data(), column.offsets.data(), column.num_rows(), config);
    auto timed = train_timed(
        column.bytes.data(), column.offsets.data(), column.num_rows(), config);

    const size_t logical_bytes = canonical.dict.offsets.back();
    if (canonical.sample_bytes_consumed != timed.sample_bytes_consumed ||
        canonical.dict.offsets != timed.dict.offsets ||
        logical_bytes != timed.dict.offsets.back() ||
        !std::equal(canonical.dict.bytes.begin(),
                    canonical.dict.bytes.begin() + std::ptrdiff_t(logical_bytes),
                    timed.dict.bytes.begin())) {
        throw std::runtime_error("timed trainer differs from production dictionary");
    }

    onpair::Store canonical_store{};
    onpair::Store timed_store{};
    onpair::encoding::parse(column.bytes.data(), column.offsets.data(),
                            column.num_rows(), canonical.lpm, config.bits,
                            canonical_store);
    onpair::encoding::parse(column.bytes.data(), column.offsets.data(),
                            column.num_rows(), timed.lpm, config.bits,
                            timed_store);
    if (canonical_store.bit_width != timed_store.bit_width ||
        canonical_store.packed != timed_store.packed ||
        canonical_store.boundaries != timed_store.boundaries) {
        throw std::runtime_error("timed trainer differs from production token stream");
    }
}

void pin_to_cpu(int cpu)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0)
        throw std::runtime_error("sched_setaffinity failed");
}

std::pair<std::string, std::string> split_column_arg(const std::string& arg)
{
    const size_t separator = arg.find('=');
    if (separator == std::string::npos || separator == 0 || separator + 1 == arg.size())
        throw std::runtime_error("column argument must be label=path: " + arg);
    return {arg.substr(0, separator), arg.substr(separator + 1)};
}

} // namespace diagnostic

int main(int argc, char** argv)
{
    using namespace diagnostic;
    try {
        if (argc < 5) {
            std::cerr << "usage: " << argv[0]
                      << " CPU SAMPLES WARMUPS label=column [label=column ...]\n";
            return 2;
        }
        const int cpu = std::stoi(argv[1]);
        const int samples = std::stoi(argv[2]);
        const int warmups = std::stoi(argv[3]);
        if (samples < 3 || warmups < 1)
            throw std::runtime_error("samples must be >=3 and warmups >=1");
        pin_to_cpu(cpu);

        onpair::encoding::TrainingConfig config;
        config.bits = onpair::BitWidth(16);
        config.threshold = onpair::encoding::DynamicThreshold{0.15};
        config.seed = uint64_t(42);
        config.sort_lexicographically = true;

        std::cout
            << "dataset,sample_idx,input_bytes,num_rows,num_dict_tokens,"
               "sample_bytes_consumed,discovery_ns,permutation_ns,relayout_ns,"
               "dictionary_sort_ns,lpm_rebuild_ns,finalization_ns,padding_ns,"
               "train_internal_ns,train_ns,parse_pack_ns,total_ns,finalization_pct_train,"
               "finalization_pct_total\n";
        std::cout << std::setprecision(17);

        for (int argument = 4; argument < argc; ++argument) {
            const auto [label, path] = split_column_arg(argv[argument]);
            Column column = read_column(path);
            validate_clone(column, config);

            for (int iteration = -warmups; iteration < samples; ++iteration) {
                const auto train_call_begin = Clock::now();
                auto trained = train_timed(column.bytes.data(), column.offsets.data(),
                                           column.num_rows(), config);
                const auto train_call_end = Clock::now();
                onpair::Store store{};
                const auto parse_begin = Clock::now();
                onpair::encoding::parse(column.bytes.data(), column.offsets.data(),
                                        column.num_rows(), trained.lpm, config.bits,
                                        store);
                const auto parse_end = Clock::now();
                if (iteration < 0) continue;

                const uint64_t train_ns = elapsed_ns(train_call_begin, train_call_end);
                const uint64_t parse_ns = elapsed_ns(parse_begin, parse_end);
                const uint64_t total_ns = train_ns + parse_ns;
                const uint64_t dictionary_sort_ns =
                    trained.times.permutation_ns + trained.times.relayout_ns;
                const double final_pct_train =
                    100.0 * double(trained.times.finalization_ns) /
                    double(train_ns);
                const double final_pct_total =
                    100.0 * double(trained.times.finalization_ns) /
                    double(total_ns);

                std::cout
                    << label << ',' << iteration << ',' << column.bytes.size() << ','
                    << column.num_rows() << ',' << trained.dict.num_tokens() << ','
                    << trained.sample_bytes_consumed << ','
                    << trained.times.discovery_ns << ','
                    << trained.times.permutation_ns << ','
                    << trained.times.relayout_ns << ',' << dictionary_sort_ns << ','
                    << trained.times.lpm_rebuild_ns << ','
                    << trained.times.finalization_ns << ','
                    << trained.times.padding_ns << ',' << trained.times.train_ns << ','
                    << train_ns << ','
                    << parse_ns << ',' << total_ns << ',' << final_pct_train << ','
                    << final_pct_total << '\n';

                // Make the parse result observably live until after the row is emitted.
                if (store.boundaries.empty())
                    throw std::runtime_error("unexpected empty parsed store");
            }
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "error: " << error.what() << '\n';
        return 1;
    }
}
