#include "onpair_runner.h"

#include <onpair/column/column.h>
#include <onpair/column/column_view.h>
#include <onpair/core/dictionary.h>
#include <onpair/core/dictionary_view.h>
#include <onpair/core/store.h>
#include <onpair/core/store_view.h>
#include <onpair/core/types.h>
#include <onpair/decoding/decoder.h>
#include <onpair/encoding/training/config.h>
#include <onpair/encoding/training/trainer.h>
#include <onpair/encoding/parsing/parser.h>
#include <onpair/encoding/parsing/bit_writer.h>
#include <onpair/search/automata/prefix_automaton.h>

#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include <sys/resource.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <new>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace onpair_bench {

namespace {

// JSON helpers (mirror rlz_runner.cpp; each runner keeps its own copy).

uint64_t extract_u64(const std::string& cfg, const std::string& key, uint64_t def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    if (pos >= cfg.size()) return def;
    uint64_t v = 0;
    bool any = false;
    while (pos < cfg.size() && cfg[pos] >= '0' && cfg[pos] <= '9') {
        v = v * 10 + static_cast<uint64_t>(cfg[pos] - '0');
        ++pos;
        any = true;
    }
    return any ? v : def;
}

double extract_double(const std::string& cfg, const std::string& key, double def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    std::string tok;
    while (pos < cfg.size()
           && cfg[pos] != ','
           && cfg[pos] != '}'
           && cfg[pos] != ' '
           && cfg[pos] != '\t'
           && cfg[pos] != '\n') {
        tok.push_back(cfg[pos]);
        ++pos;
    }
    if (tok.empty()) return def;
    try {
        return std::stod(tok);
    } catch (...) {
        return def;
    }
}

// Extract a JSON string value (no escape handling — sufficient for our
// short ASCII enum values). Returns `def` if key absent.
std::string extract_string(const std::string& cfg, const std::string& key,
                           const std::string& def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    if (pos >= cfg.size() || cfg[pos] != '"') return def;
    ++pos;
    std::string out;
    while (pos < cfg.size() && cfg[pos] != '"') {
        out.push_back(cfg[pos]);
        ++pos;
    }
    return out;
}

void set_address_space_cap(uint64_t mem_cap_bytes) {
    if (mem_cap_bytes == 0) return;
    struct rlimit rl{};
    rl.rlim_cur = mem_cap_bytes;
    rl.rlim_max = mem_cap_bytes;
    (void)::setrlimit(RLIMIT_AS, &rl);
}

uint64_t file_size_bytes(const std::string& path) {
    std::error_code ec;
    auto sz = std::filesystem::file_size(path, ec);
    return ec ? 0u : static_cast<uint64_t>(sz);
}

OnPairConfig validate(OnPairConfig c) {
    if (c.bits < 9 || c.bits > 16)
        throw std::runtime_error(
            "onpair: bits out of range [9, 16]; got "
            + std::to_string(c.bits));
    if (!(c.sample_fraction > 0.0 && c.sample_fraction <= 1.0))
        throw std::runtime_error(
            "onpair: sample_fraction out of range (0.0, 1.0]");
    if (c.threads != 1)
        throw std::runtime_error("onpair: only threads=1 supported");
    if (c.mem_cap_gb < 1 || c.mem_cap_gb > 1024)
        throw std::runtime_error(
            "onpair: mem_cap_gb out of range [1, 1024]");
    if (c.threshold_strategy != "dynamic"
     && c.threshold_strategy != "fixed"
     && c.threshold_strategy != "fixed_dynamic_paired")
        throw std::runtime_error(
            "onpair: threshold_strategy must be 'dynamic', 'fixed', or "
            "'fixed_dynamic_paired'; got '" + c.threshold_strategy + "'");
    if (c.threshold_strategy != "dynamic"
     && (c.threshold_value < 2 || c.threshold_value > 255))
        throw std::runtime_error(
            "onpair: threshold_value out of range [2, 255]; got "
            + std::to_string(c.threshold_value));
    if (c.dictionary_order != "lexicographic"
     && c.dictionary_order != "construction")
        throw std::runtime_error(
            "onpair: dictionary_order must be 'lexicographic' or "
            "'construction'; got '" + c.dictionary_order + "'");
    return c;
}

} // namespace

OnPairConfig parse_onpair_config(const std::string& cfg_json) {
    OnPairConfig c;
    c.bits            = static_cast<uint32_t>(extract_u64(cfg_json, "bits",            c.bits));
    c.sample_fraction =                       extract_double(cfg_json, "sample_fraction", c.sample_fraction);
    c.seed            =                       extract_u64(cfg_json, "seed",            c.seed);
    c.threads         = static_cast<uint32_t>(extract_u64(cfg_json, "threads",         c.threads));
    c.mem_cap_gb      =                       extract_u64(cfg_json, "mem_cap_gb",      c.mem_cap_gb);
    c.threshold_strategy =                    extract_string(cfg_json, "threshold_strategy", c.threshold_strategy);
    c.threshold_value =   static_cast<uint32_t>(extract_u64(cfg_json, "threshold_value", c.threshold_value));
    c.dictionary_order =                       extract_string(cfg_json, "dictionary_order", c.dictionary_order);
    return validate(c);
}

// Version string. SHA pin must stay in lockstep with ONPAIR_CPP_GIT_TAG in
// external/onpair_cpp.cmake; the two are synced by hand at every SHA bump.
#define ONPAIR_BENCH_ONPAIR_SHA_SHORT "f8ecc64f"

const char* OnPairRunner::version() const {
    return "onpair-cpp+" ONPAIR_BENCH_ONPAIR_SHA_SHORT;
}

OpResult OnPairRunner::compress(const std::string& in_path,
                                const std::string& out_path,
                                const std::string& config_json) {
    OnPairConfig cfg = parse_onpair_config(config_json);
    if (cfg.threshold_strategy == "fixed_dynamic_paired") {
        return OpResult{.status = "unsupported",
                        .notes  = "onpair: 'fixed_dynamic_paired' strategy is "
                                  "only supported by op=held_out_compress (A1)"};
    }

    Column col_in = read_column(in_path);
    const uint64_t input_bytes = col_in.bytes.size();
    const size_t   N           = col_in.num_rows();

    if (N == 0 || input_bytes == 0) {
        return OpResult{.status = "unsupported",
                        .metrics = {},
                        .notes   = "onpair: empty column not supported"};
    }

    set_address_space_cap(cfg.mem_cap_gb << 30);

    try {
        // Build zero-copy string_view range over column rows. Lifetime ties
        // to col_in.bytes; OnPair only reads inside compress().
        std::vector<std::string_view> views;
        views.reserve(N);
        for (size_t i = 0; i < N; ++i) views.push_back(col_in.at(i));

        onpair::encoding::TrainingConfig tc;
        tc.bits      = static_cast<onpair::BitWidth>(cfg.bits);
        if (cfg.threshold_strategy == "fixed") {
            tc.threshold = onpair::encoding::FixedThreshold{
                static_cast<uint8_t>(cfg.threshold_value)};
        } else {
            // "dynamic" (default) — "fixed_dynamic_paired" is not a valid
            // strategy for the standard compress op (the A1 protocol invokes
            // held_out_compress instead); validate() catches it earlier.
            tc.threshold = onpair::encoding::DynamicThreshold{cfg.sample_fraction};
        }
        tc.seed      = cfg.seed;
        tc.sort_lexicographically = (cfg.dictionary_order == "lexicographic");

        // t_train_ns: standalone train timing for the training-cost
        // comparison of Section VIII-B2 (vs BPE's t_train_ns at the same
        // sample budget). Runs BEFORE — in addition to — the one-shot
        // compress, so t_compress_ns still times the one-shot call alone;
        // costs one extra phase-probe pipeline per sample (sub-second to seconds).
        uint64_t t_train_ns = 0;
        uint64_t t_parse_pack_ns = 0;
        {
            const auto t_tr0 = std::chrono::steady_clock::now();
            auto tr_probe = onpair::encoding::train(
                col_in.bytes.data(), col_in.offsets.data(), N, tc);
            const auto t_tr1 = std::chrono::steady_clock::now();
            t_train_ns = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    t_tr1 - t_tr0).count());

            onpair::Store phase_store{};
            phase_store.bit_width = static_cast<onpair::BitWidth>(cfg.bits);
            onpair::encoding::parse(
                col_in.bytes.data(), col_in.offsets.data(), N, tr_probe.lpm,
                phase_store.bit_width, phase_store);
            const auto t_parse1 = std::chrono::steady_clock::now();
            t_parse_pack_ns = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    t_parse1 - t_tr1).count());
        } // release phase-probe dictionary/store before the one-shot
        const uint64_t t_codec_first_use_ns = t_train_ns + t_parse_pack_ns;

        // As for every baseline, training (pair-merge over
        // input + dictionary construction + bit-packed token-stream emission)
        // is inside the compress timed region.
        // TIMED: OnPair train + dictionary build + bit-packed token emission
        // (the codec). Input column already read; wire write below excluded so
        // T_c reflects the algorithm, not the disk.
        const auto t_comp0 = std::chrono::steady_clock::now();
        onpair::OnPairColumn col = onpair::OnPairColumn::compress(views, tc);
        const uint64_t t_compress_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t_comp0).count());

        // Wire materialisation via library serialiser (row order
        // preserved natively via boundaries[N+1]).
        const auto t_ser0 = std::chrono::steady_clock::now();
        {
            std::ofstream f(out_path, std::ios::binary);
            if (!f) throw std::runtime_error("onpair: cannot open out_path");
            col.write_to(f);
            f.flush();
            if (!f) throw std::runtime_error("onpair: write_to failed");
        }
        const uint64_t t_serialize_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t_ser0).count());
        // Conservative externally observed runner first use: the
        // one-shot includes its input materialisation in addition to train/parse.
        const uint64_t t_first_use_ns = t_compress_ns + t_serialize_ns;

        const auto view       = col.view();
        const auto dict       = view.dictionary();
        const auto store      = view.store();
        const size_t num_tokens   = dict.num_tokens();
        const size_t total_tokens = store.num_tokens();
        const uint32_t bits_b     = static_cast<uint32_t>(store.bits());

        // Space sub-counter mapping. The DICTIONARY (the trained model) splits
        // into its token content and its own offset array — both reported
        // separately (via the local shim accessors; their sum == dict.bytes_used()):
        //   dict_content_bytes — concatenated token byte sequences (model content)
        //   dict_offset_bytes  — the dictionary's own offset array (indexes the
        //                        content; part of the model, NOT string offsets)
        // dict_bytes = their sum = the full model cost.
        const uint64_t dict_content_bytes =
            static_cast<uint64_t>(dict.token_content_bytes());
        const uint64_t dict_offset_bytes  =
            static_cast<uint64_t>(dict.offset_bytes());
        const uint64_t dict_bytes         = dict_content_bytes + dict_offset_bytes;
        const uint64_t tokens_bytes       =
            (static_cast<uint64_t>(total_tokens) * bits_b + 63ull) / 64ull * 8ull;
        // boundaries_bytes = the per-row STRING offset array (+ wire scalars).
        // These are the per-row string boundaries, distinct from the dictionary's
        // own offsets above. EXCLUDED from the payload-only compression ratio:
        // this benchmark compresses string payload, not
        // string offsets. Derived structurally from the library wire:
        //   8 MAGIC + 1 bit_width + 4 true_bytes_scalar + 4 dict_offsets_size
        //   + 4 real_words + 4 boundaries_size + (N+1)*4  =  25 + (N+1)*4.
        const uint64_t boundaries_bytes   =
            25ull + static_cast<uint64_t>(N + 1) * 4ull;
        const uint64_t output_bytes       = file_size_bytes(out_path);

        OpResult r;
        r.status = "ok";
        r.metrics = {
            {"dict_bytes",         static_cast<double>(dict_bytes)},
            {"tokens_bytes",       static_cast<double>(tokens_bytes)},
            {"boundaries_bytes",   static_cast<double>(boundaries_bytes)},
            {"input_bytes",        static_cast<double>(input_bytes)},
            {"output_bytes",       static_cast<double>(output_bytes)},
            {"num_rows",           static_cast<double>(N)},
            // Separated dictionary sub-counters (sum == dict_bytes) so a consumer
            // can choose what to include. dict_offset_bytes are DICTIONARY
            // offsets (kept in the model); the string per-row offsets live in
            // boundaries_bytes and are excluded from the payload-only ratio.
            {"dict_content_bytes", static_cast<double>(dict_content_bytes)},
            {"dict_offset_bytes",  static_cast<double>(dict_offset_bytes)},
            {"num_dict_entries",   static_cast<double>(num_tokens)},
            // Legacy alias for the dictionary offset array (diagnostic):
            // identical to dict_offset_bytes. Kept so existing
            // consumers/tests that read 'dict_offsets_bytes' stay valid.
            {"dict_offsets_bytes", static_cast<double>(dict_offset_bytes)},
            {"t_compress_ns",      static_cast<double>(t_compress_ns)},
            // Standalone training time (see probe above); parse/encode time
            {"t_train_ns",         static_cast<double>(t_train_ns)},
            {"t_parse_pack_ns",    static_cast<double>(t_parse_pack_ns)},
            {"t_serialize_ns",     static_cast<double>(t_serialize_ns)},
            {"t_codec_first_use_ns", static_cast<double>(t_codec_first_use_ns)},
            {"t_first_use_ns",     static_cast<double>(t_first_use_ns)},
        };
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom",
                        .metrics = {},
                        .notes   = "onpair: bad_alloc during compress"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash",
                        .metrics = {},
                        .notes   = std::string("onpair: ") + e.what()};
    }
}

namespace {

onpair::OnPairColumn load_wire(const std::string& in_path) {
    std::ifstream f(in_path, std::ios::binary);
    if (!f) throw std::runtime_error("onpair: cannot open in_path " + in_path);
    return onpair::OnPairColumn::read_from(f);
}
uint32_t packed_token_at(onpair::StoreView store, size_t index) {
    const uint32_t bits = static_cast<uint32_t>(store.bits());
    const uint64_t bitpos = static_cast<uint64_t>(index) * bits;
    const size_t word_index = static_cast<size_t>(bitpos >> 6);
    const uint32_t shift = static_cast<uint32_t>(bitpos & 63);
    uint64_t value = store.packed_data()[word_index] >> shift;
    if (shift + bits > 64)
        value |= store.packed_data()[word_index + 1] << (64 - shift);
    return static_cast<uint32_t>(value & ((uint64_t{1} << bits) - 1));
}

void build_frequency_ordered_view(
    onpair::OnPairColumnView source,
    onpair::Dictionary& dst_dict,
    onpair::Store& dst_store) {
    const auto src_dict = source.dictionary();
    const auto src_store = source.store();
    const uint32_t V = static_cast<uint32_t>(src_dict.num_tokens());
    const size_t T = src_store.num_tokens();

    std::vector<uint64_t> frequency(V, 0);
    for (size_t j = 0; j < T; ++j)
        ++frequency[packed_token_at(src_store, j)];

    std::vector<uint32_t> new_to_old(V);
    std::iota(new_to_old.begin(), new_to_old.end(), 0u);
    std::sort(new_to_old.begin(), new_to_old.end(), [&](uint32_t a, uint32_t b) {
        if (frequency[a] != frequency[b]) return frequency[a] > frequency[b];
        const size_t al = src_dict.token_size(static_cast<onpair::Token>(a));
        const size_t bl = src_dict.token_size(static_cast<onpair::Token>(b));
        const int cmp = std::memcmp(
            src_dict.data(static_cast<onpair::Token>(a)),
            src_dict.data(static_cast<onpair::Token>(b)), std::min(al, bl));
        if (cmp != 0) return cmp < 0;
        if (al != bl) return al < bl;
        return a < b;
    });

    std::vector<onpair::Token> old_to_new(V);
    dst_dict.offsets.reserve(static_cast<size_t>(V) + 1);
    dst_dict.offsets.push_back(0);
    for (uint32_t new_id = 0; new_id < V; ++new_id) {
        const auto old_id = static_cast<onpair::Token>(new_to_old[new_id]);
        old_to_new[old_id] = static_cast<onpair::Token>(new_id);
        const auto span = src_dict.span(old_id);
        dst_dict.bytes.insert(dst_dict.bytes.end(),
                              src_dict.raw_bytes() + span.begin,
                              src_dict.raw_bytes() + span.end);
        dst_dict.offsets.push_back(static_cast<uint32_t>(dst_dict.bytes.size()));
    }
    dst_dict.pad_for_decoder();

    dst_store.bit_width = src_store.bits();
    dst_store.boundaries.assign(
        src_store.boundaries(),
        src_store.boundaries() + src_store.num_strings() + 1);
    onpair::encoding::BitWriter writer(dst_store);
    for (size_t j = 0; j < T; ++j) {
        const uint32_t old_id = packed_token_at(src_store, j);
        writer.write(old_to_new[old_id]);
    }
    writer.flush();
}


} // namespace

OpResult OnPairRunner::bulk_decode(const std::string& in_path,
                                   const std::string& out_path,
                                   const std::string& config_json) {
    const std::string decode_variant =
        extract_string(config_json, "decode_variant", "native");
    if (decode_variant != "native" && decode_variant != "frequency") {
        return OpResult{.status = "crash",
                        .notes = "onpair: unknown decode_variant "
                               + decode_variant};
    }
    // PAYLOAD-ONLY decode timing: the timed region wraps ONLY the
    // payload-only bulk decompress into a PRE-ALLOCATED buffer. This benchmark
    // measures string-PAYLOAD (de)compression and does not (de)compress the
    // per-row string offsets, so the timed work unit excludes the offset
    // bookkeeping (symmetric with the payload-only ratio).
    //
    // The library already ships a payload-only bulk decoder: the no-offsets
    // OnPairColumnView::decompress_all(buf) -> decoding::decompress_all(sv,dv,buf)
    // -> decode_all<Bits>(...) which writes just the concatenated token bytes
    // and skips the Arrow-style out_offsets array entirely (that array is the
    // per-row STRING offsets we exclude — distinct from the dictionary's own
    // offsets, which are part of the model). We call exactly that here.
    //
    // The per-row offsets are still needed to materialise the round-trip
    // verification column, so we rebuild them via the offset-aware decode in an
    // UNTIMED pass after the measurement, and assert the payload bytes match.
    try {
        onpair::OnPairColumn col = load_wire(in_path);
        const auto native_view = col.view();
        onpair::Dictionary remapped_dict;
        onpair::Store remapped_store;
        std::optional<onpair::OnPairColumnView> remapped_view;
        if (decode_variant == "frequency") {
            build_frequency_ordered_view(
                native_view, remapped_dict, remapped_store);
            remapped_view.emplace(
                onpair::StoreView(remapped_store),
                onpair::DictionaryView(remapped_dict));
        }
        const auto& view = remapped_view ? *remapped_view : native_view;
        const size_t N  = view.num_strings();

        // Worst-case decompressed payload bound: walk dict to find total
        // decoded bytes is too expensive — instead size buf at a generous
        // upper bound proportional to total tokens × max-token-size. Per
        // OnPair convention (decoder over-copies MAX_TOKEN_SIZE per token)
        // the safe upper bound is total_tokens · MAX_TOKEN_SIZE + padding;
        // however decompress_all() returns the true byte count. std::vector's
        // value-init zero-fills here, so the pages are resident before the
        // timed region (no first-touch faults contaminate t_decode_ns).
        const size_t total_tokens = view.store().num_tokens();
        std::vector<uint8_t> buf(total_tokens * onpair::MAX_TOKEN_SIZE
                                 + onpair::DECOMPRESS_BUFFER_PADDING);

        // TIMED: payload-only decode into the PRE-ALLOCATED buffer. No offset
        // array is produced — this is the codec's true payload decode
        // throughput. Measured COLD (first decode after load); the untimed
        // offset-aware pass below runs afterwards. Wire load + allocation above
        // and the column write below are all excluded.
        const auto t0 = std::chrono::steady_clock::now();
        const size_t total =
            view.decompress_all(reinterpret_cast<char*>(buf.data()));
        const uint64_t t_decode_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());

        // UNTIMED: rebuild the per-row string offsets for the round-trip
        // verification column via the offset-aware decoder into a scratch
        // buffer, then cross-check its payload against the timed payload-only
        // decode. A mismatch means the payload path is lossy → fail loudly.
        std::vector<uint8_t>  scratch(buf.size());
        std::vector<uint32_t> offsets(N + 1, 0);
        const size_t total_off =
            view.decompress_all(reinterpret_cast<char*>(scratch.data()),
                                offsets.data());
        if (total_off != total
            || std::memcmp(buf.data(), scratch.data(), total) != 0) {
            return OpResult{.status = "lossy",
                            .notes  = "onpair: payload-only decode disagrees "
                                      "with offset-aware decode"};
        }

        Column col_out;
        col_out.bytes.assign(buf.begin(), buf.begin() + total);
        col_out.offsets = std::move(offsets);
        write_column(out_path, col_out);
        return OpResult{.status = "ok",
                        .metrics = {{"t_decode_ns", static_cast<double>(t_decode_ns)}}};
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "onpair: bad_alloc bulk_decode"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("onpair: ") + e.what()};
    }
}

OpResult OnPairRunner::random_decode(const std::string& in_path,
                                     size_t idx,
                                     const std::string& out_path,
                                     const std::string& /*config_json*/) {
    try {
        onpair::OnPairColumn col = load_wire(in_path);
        const auto view = col.view();
        if (idx >= view.num_strings())
            return OpResult{.status = "crash", .notes = "onpair: idx out of range"};

        // Upper bound on a single row's decoded size: max-token-size × tokens
        // in this row + padding. Cheap derivation via string_span.
        const auto span = view.store().string_span(idx);
        const size_t row_tokens = span.end - span.begin;
        std::vector<uint8_t> buf(row_tokens * onpair::MAX_TOKEN_SIZE
                                 + onpair::DECOMPRESS_BUFFER_PADDING);

        const size_t len =
            view.decompress(idx, reinterpret_cast<char*>(buf.data()));

        std::ofstream f(out_path, std::ios::binary);
        if (!f) throw std::runtime_error("onpair: cannot open out_path");
        if (len > 0) f.write(reinterpret_cast<const char*>(buf.data()),
                             static_cast<std::streamsize>(len));
        return OpResult{.status = "ok"};
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "onpair: bad_alloc random_decode"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("onpair: ") + e.what()};
    }
}

// Times n_samples random accesses. OnPair seeks
// per-string via the global boundaries (Section V, O(|s_i|)) — there is NO block to
// decode — so its L_ra is independent of any block size: this is the flat
// reference of the A5 block-size sweep, against which the zstd/lz4 block-decode
// latencies degrade. config_json: n_samples (default 1e6), seed (42), warmup (1 → one
// decompress_all pre-pass before timing; 0 → no in-process warmup). The output
// buffer is sized once (to the widest row) outside the timed loop so the timed
// region is purely the per-string decompress.
OpResult OnPairRunner::ra_latency(const std::string& in_path,
                                  const std::string& /*out_path*/,
                                  const std::string& cfg_json) {
    try {
        onpair::OnPairColumn col = load_wire(in_path);
        const auto view = col.view();
        const size_t N = view.num_strings();
        if (N == 0) {
            return OpResult{.status = "crash",
                            .notes  = "onpair: empty column for ra_latency"};
        }

        uint64_t n_samples = extract_u64(cfg_json, "n_samples", 1000000);
        if (n_samples == 0) n_samples = 1;
        uint64_t seed   = extract_u64(cfg_json, "seed", 42);
        bool     warmup = extract_u64(cfg_json, "warmup", 1) != 0;

        // Size the decode buffer once to the widest row (max tokens × max-token
        // size + padding) so per-sample timing excludes allocation.
        const auto store = view.store();
        size_t max_row_tokens = 0;
        for (size_t i = 0; i < N; ++i) {
            const auto span = store.string_span(i);
            max_row_tokens = std::max(max_row_tokens,
                                      static_cast<size_t>(span.end - span.begin));
        }
        std::vector<uint8_t> buf(max_row_tokens * onpair::MAX_TOKEN_SIZE
                                 + onpair::DECOMPRESS_BUFFER_PADDING);

        // Warm pre-pass (warm cache_state): one full decode so caches/allocator
        // are hot before timing. Skipped for cold.
        if (warmup) {
            const size_t total_tokens = store.num_tokens();
            std::vector<uint8_t>  wbuf(total_tokens * onpair::MAX_TOKEN_SIZE
                                       + onpair::DECOMPRESS_BUFFER_PADDING);
            std::vector<uint32_t> woff(N + 1, 0);
            view.decompress_all(reinterpret_cast<char*>(wbuf.data()), woff.data());
        }

        // Query indices generated up-front (A6: distribution + layout from cfg)
        // so the distribution math never enters the timed region. OnPair is
        // per-string (decompress(idx) seeks via the global boundaries), so an
        // access never decodes a neighbouring row — the per-string-class
        // companion to FSST in the A6 roster.
        const QuerySpec qspec = parse_query_spec(cfg_json);
        const std::vector<uint64_t> indices =
            generate_query_indices(qspec, n_samples, N, seed);

        // Per-access rdtscp timing (ra_timer_tsc.h ra_measure_shipping): the
        // access returns the LAST decoded byte, so the accumulated, barrier'd
        // value forces the full per-string decode (anti-DCE). Identical primitive
        // across every baseline — only the access body differs.
        RaLatencyResult m;
        std::string ra_reason;
        if (!ra_measure_shipping(indices,
            [&](uint64_t idx64) -> unsigned char {
                const size_t len = view.decompress(
                    static_cast<size_t>(idx64), reinterpret_cast<char*>(buf.data()));
                return len ? buf[len - 1] : 0;
            }, m, ra_reason)) {
            return OpResult{.status = "refused-env", .notes = "onpair: " + ra_reason};
        }
        if (m.stats.n > 0 && m.stats.p50 < 1.0) {
            return OpResult{.status = "crash",
                .notes = "onpair: implausible sub-ns p50 (latency primitive failure)"};
        }
        const LatencyStats& st = m.stats;
        OpResult r;
        r.metrics.emplace_back("l_ra_p50_ns",  st.p50);
        r.metrics.emplace_back("l_ra_p95_ns",  st.p95);
        r.metrics.emplace_back("l_ra_p99_ns",  st.p99);
        r.metrics.emplace_back("l_ra_mean_ns", st.mean);
        r.metrics.emplace_back("l_ra_min_ns",  st.min);
        r.metrics.emplace_back("l_ra_max_ns",  st.max);
        r.metrics.emplace_back("n_samples",    static_cast<double>(st.n));
        r.metrics.emplace_back("batch_k",      static_cast<double>(m.batch_k));
        r.metrics.emplace_back("clock_resolution_ns", m.clock_resolution_ns);
        r.metrics.emplace_back("num_rows",     static_cast<double>(N));
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "onpair: bad_alloc ra_latency"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("onpair: ") + e.what()};
    }
}

OpResult OnPairRunner::prefix_search(const std::string& in_path,
                                     const std::string& needle,
                                     const std::string& out_path,
                                     const std::string& /*config_json*/) {
    // NativeOnCompressed prefix search.
    // PrefixAutomaton scan over
    // the bit-packed token stream — never decompresses. The prefix-search
    // tests verify hit-set equality vs the uncompressed scan.
    try {
        onpair::OnPairColumn col = load_wire(in_path);          // UNTIMED: file read
        const auto view = col.view();

        // Time query tokenisation/interval construction separately from the
        // compressed scan. The end-to-end in-memory search metric includes
        // both. Use the callback API and uint32_t row ids so hit-list
        // materialisation is byte-for-byte symmetric with scan_prefix().
        const auto t_search0 = std::chrono::steady_clock::now();
        onpair::search::PrefixAutomaton automaton(
            std::string_view{needle}, view.dictionary());
        const auto t_scan0 = std::chrono::steady_clock::now();
        const uint64_t t_query_prepare_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                t_scan0 - t_search0).count());

        std::vector<uint32_t> hits;
        view.scan(automaton, [&](size_t idx) {
            hits.push_back(static_cast<uint32_t>(idx));
        });
        const auto t_search1 = std::chrono::steady_clock::now();
        const uint64_t t_scan_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                t_search1 - t_scan0).count());
        const uint64_t t_search_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                t_search1 - t_search0).count());

        // UNTIMED: both searches already paid to append identical uint32_t row
        // ids; writing those ids to disk is outside the in-memory query cost.
        std::ofstream f(out_path, std::ios::binary);
        if (!f) throw std::runtime_error("onpair: cannot open out_path");
        const uint32_t n_hits = static_cast<uint32_t>(hits.size());
        f.write(reinterpret_cast<const char*>(&n_hits), 4);
        if (!hits.empty()) {
            f.write(reinterpret_cast<const char*>(hits.data()),
                    static_cast<std::streamsize>(hits.size() * sizeof(uint32_t)));
        }

        OpResult r;
        r.metrics.emplace_back("n_hits",             static_cast<double>(n_hits));
        r.metrics.emplace_back("num_rows",           static_cast<double>(view.num_strings()));
        r.metrics.emplace_back("needle_bytes",       static_cast<double>(needle.size()));
        r.metrics.emplace_back("t_query_prepare_ns", static_cast<double>(t_query_prepare_ns));
        r.metrics.emplace_back("t_scan_ns",          static_cast<double>(t_scan_ns));
        r.metrics.emplace_back("t_search_ns",        static_cast<double>(t_search_ns));
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "onpair: bad_alloc prefix_search"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("onpair: ") + e.what()};
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// A1 ablation op (held_out_compress).
//
// Protocol:
//   1. Train Fixed{T} on train.col with seed=42 → observe sample_bytes_consumed.
//   2. matched_fraction = sample_bytes_consumed / total_train_bytes.
//   3. Train Dynamic{matched_fraction} on the same train.col with seed=42 →
//      same shuffled-string order; same byte budget; apples-to-apples.
//   4. For each strategy d ∈ {fixed, dyn}: parse() the heldout.col with the
//      trained LPM → compute the heldout's sub-counters per the bit-tight
//      identity (dict + tokens + boundaries = output).
//   5. Emit 14 metric rows: sample_bytes_consumed_fixed, matched_fraction,
//      train_input_bytes, heldout_input_bytes, and 5×2 paired sub-counter rows.
//
// Uses the lower-level encoding API (train()+parse()+BitWriter) — NOT the
// public OnPairColumn::compress one-shot — to access the trained
// LongestPrefixMatcher directly. This is permitted by the scope-fence
// (tools/lint/onpair_scope_audit.sh deny-list is unchanged; only KMP /
// AhoCorasick / EQ / contains / equals are forbidden).
//
// Requires the local shim (external/onpair_local_shims.patch):
//   TrainResult.sample_bytes_consumed (uint64_t).
// ─────────────────────────────────────────────────────────────────────────────

namespace {

// Helper: pack the trained LPM's encoding into a fresh Store with the
// requested bit-width.  Returns the heldout Store after a parse() call.
onpair::Store encode_into_store(const onpair::encoding::LongestPrefixMatcher& lpm,
                                onpair::BitWidth bits,
                                const Column& col) {
    onpair::Store store{};
    store.bit_width = bits;
    onpair::encoding::parse(col.bytes.data(),
                            col.offsets.data(),
                            col.num_rows(),
                            lpm,
                            bits,
                            store);
    return store;
}

} // namespace

OpResult OnPairRunner::held_out_compress(const std::string& train_in_path,
                                         const std::string& heldout_in_path,
                                         const std::string& /*out_path*/,
                                         const std::string& config_json) {
    OnPairConfig cfg = parse_onpair_config(config_json);
    // A1 drives this op with "fixed_dynamic_paired" (Fixed{T}
    // first → matched-Dynamic{f}). A3 (sample_fraction sweep)
    // reuses the same op with the single-strategy "dynamic"/"fixed" branch:
    // one trained dictionary, no Fixed/Dynamic pairing, emitting a single
    // metric block + train_ns/encode_ns timing for T_c.
    const bool paired = (cfg.threshold_strategy == "fixed_dynamic_paired");
    const bool single = (cfg.threshold_strategy == "dynamic"
                      || cfg.threshold_strategy == "fixed");
    if (!paired && !single) {
        return OpResult{.status = "unsupported",
                        .notes  = "onpair: held_out_compress supports "
                                  "threshold_strategy in {dynamic, fixed, "
                                  "fixed_dynamic_paired}; got '"
                                  + cfg.threshold_strategy + "'"};
    }
    // FixedThreshold value is only consumed by the paired and 'fixed' paths;
    // the 'dynamic' (A3) path ignores it.
    if ((paired || cfg.threshold_strategy == "fixed")
        && (cfg.threshold_value < 2 || cfg.threshold_value > 255)) {
        return OpResult{.status = "crash",
                        .notes  = "onpair: threshold_value out of range [2, 255]"};
    }

    set_address_space_cap(cfg.mem_cap_gb << 30);

    Column col_train;
    Column col_heldout;
    try {
        col_train   = read_column(train_in_path);
        col_heldout = read_column(heldout_in_path);
    } catch (const std::exception& e) {
        return OpResult{.status = "crash",
                        .notes  = std::string("onpair: load failure: ") + e.what()};
    }

    if (col_train.num_rows() == 0 || col_train.bytes.empty()) {
        return OpResult{.status = "unsupported",
                        .notes  = "onpair: empty train column"};
    }
    if (col_heldout.num_rows() == 0 || col_heldout.bytes.empty()) {
        return OpResult{.status = "unsupported",
                        .notes  = "onpair: empty heldout column"};
    }

    try {
        const onpair::BitWidth bits = static_cast<onpair::BitWidth>(cfg.bits);
        const uint64_t train_input_bytes   = col_train.bytes.size();
        const uint64_t heldout_input_bytes = col_heldout.bytes.size();
        const size_t   N_heldout           = col_heldout.num_rows();

        // Shared sub-counter mapping. Bit-tight identity:
        //   dict_bytes + tokens_bytes + boundaries_bytes == output_bytes.
        // (rounded up to whole 64-bit words to mirror Store::packed.size()).
        auto sub_counters = [&](const onpair::Dictionary& dict,
                                const onpair::Store& store) {
            const size_t   total_tokens = store.num_tokens();
            const uint32_t bits_b       = static_cast<uint32_t>(store.bit_width);
            const uint64_t dict_bytes =
                static_cast<uint64_t>(dict.bytes_used());
            const uint64_t tokens_bytes =
                (static_cast<uint64_t>(total_tokens) * bits_b + 63ull) / 64ull * 8ull;
            const uint64_t boundaries_bytes =
                25ull + static_cast<uint64_t>(N_heldout + 1) * 4ull;
            const uint64_t output_bytes =
                dict_bytes + tokens_bytes + boundaries_bytes;
            return std::tuple<uint64_t, uint64_t, uint64_t, uint64_t>{
                dict_bytes, tokens_bytes, boundaries_bytes, output_bytes};
        };

        // ── A3 — single-strategy branch ─────────────────────────────
        // One trained dictionary (DynamicThreshold{sample_fraction} for the A3
        // sample_fraction sweep, or FixedThreshold{T}); encode the heldout
        // column with it; emit a single metric block + train/encode timing for
        // T_c. No Fixed/Dynamic pairing — that is the "fixed_dynamic_paired"
        // (A1) path below.
        if (single) {
            onpair::encoding::TrainingConfig tc;
            tc.bits = bits;
            tc.seed = cfg.seed;
            if (cfg.threshold_strategy == "fixed")
                tc.threshold = onpair::encoding::FixedThreshold{
                    static_cast<uint8_t>(cfg.threshold_value)};
            else
                tc.threshold = onpair::encoding::DynamicThreshold{cfg.sample_fraction};

            const auto t_train0 = std::chrono::steady_clock::now();
            auto tr = onpair::encoding::train(
                col_train.bytes.data(),
                col_train.offsets.data(),
                col_train.num_rows(),
                tc);
            const auto t_train1 = std::chrono::steady_clock::now();

            onpair::Store store = encode_into_store(tr.lpm, bits, col_heldout);
            const auto t_enc1 = std::chrono::steady_clock::now();

            auto [db, tb, bb, ob] = sub_counters(tr.dict, store);
            const double ratio = ob > 0
                ? static_cast<double>(heldout_input_bytes) / static_cast<double>(ob)
                : 0.0;
            const double effective_fraction = train_input_bytes > 0
                ? static_cast<double>(tr.sample_bytes_consumed)
                    / static_cast<double>(train_input_bytes)
                : 0.0;
            const double train_ns =
                std::chrono::duration<double, std::nano>(t_train1 - t_train0).count();
            const double encode_ns =
                std::chrono::duration<double, std::nano>(t_enc1 - t_train1).count();

            OpResult r;
            r.status = "ok";
            r.metrics = {
                {"train_input_bytes",         static_cast<double>(train_input_bytes)},
                {"heldout_input_bytes",       static_cast<double>(heldout_input_bytes)},
                {"requested_sample_fraction", cfg.sample_fraction},
                {"sample_bytes_consumed",     static_cast<double>(tr.sample_bytes_consumed)},
                {"effective_fraction",        effective_fraction},
                {"num_dict_tokens",           static_cast<double>(tr.dict.num_tokens())},
                {"dict_bytes",                static_cast<double>(db)},
                {"tokens_bytes",              static_cast<double>(tb)},
                {"boundaries_bytes",          static_cast<double>(bb)},
                {"heldout_output_bytes",      static_cast<double>(ob)},
                {"heldout_ratio",             ratio},
                {"train_ns",                  train_ns},
                {"encode_ns",                 encode_ns},
            };
            return r;
        }

        // ── train Fixed{T} on the train column ──────────────────────
        onpair::encoding::TrainingConfig tc_fixed;
        tc_fixed.bits      = bits;
        tc_fixed.threshold = onpair::encoding::FixedThreshold{
            static_cast<uint8_t>(cfg.threshold_value)};
        tc_fixed.seed      = cfg.seed;

        auto tr_fixed = onpair::encoding::train(
            col_train.bytes.data(),
            col_train.offsets.data(),
            col_train.num_rows(),
            tc_fixed);

        const uint64_t sample_bytes_consumed = tr_fixed.sample_bytes_consumed;
        const double   matched_fraction      = train_input_bytes > 0
            ? static_cast<double>(sample_bytes_consumed)
                / static_cast<double>(train_input_bytes)
            : 0.0;

        // Dynamic strictly requires sample_fraction ∈ (0, 1]; if Fixed{T} did
        // not consume any bytes (sample_bytes_consumed == 0 → matched_fraction
        // == 0), clamp to a tiny positive value so the second train doesn't
        // throw on the variant.  Real datasets will never hit this edge.
        const double matched_fraction_safe = std::max(matched_fraction, 1.0e-9);

        // ── train Dynamic{matched_fraction} on the SAME train col ───
        onpair::encoding::TrainingConfig tc_dyn;
        tc_dyn.bits      = bits;
        tc_dyn.threshold = onpair::encoding::DynamicThreshold{matched_fraction_safe};
        tc_dyn.seed      = cfg.seed;

        auto tr_dyn = onpair::encoding::train(
            col_train.bytes.data(),
            col_train.offsets.data(),
            col_train.num_rows(),
            tc_dyn);

        // ── encode the heldout column with each trained dict ────────
        onpair::Store store_fixed = encode_into_store(tr_fixed.lpm, bits, col_heldout);
        onpair::Store store_dyn   = encode_into_store(tr_dyn.lpm,   bits, col_heldout);

        // sub_counters() is hoisted above the single/paired branch.
        auto [db_f, tb_f, bb_f, ob_f] = sub_counters(tr_fixed.dict, store_fixed);
        auto [db_d, tb_d, bb_d, ob_d] = sub_counters(tr_dyn.dict,   store_dyn);

        const double ratio_f = ob_f > 0
            ? static_cast<double>(heldout_input_bytes) / static_cast<double>(ob_f) : 0.0;
        const double ratio_d = ob_d > 0
            ? static_cast<double>(heldout_input_bytes) / static_cast<double>(ob_d) : 0.0;

        OpResult r;
        r.status = "ok";
        r.metrics = {
            {"sample_bytes_consumed_fixed", static_cast<double>(sample_bytes_consumed)},
            {"matched_fraction",            matched_fraction},
            {"train_input_bytes",           static_cast<double>(train_input_bytes)},
            {"heldout_input_bytes",         static_cast<double>(heldout_input_bytes)},
            // Fixed-trained dict applied to heldout:
            {"dict_bytes_fixed",            static_cast<double>(db_f)},
            {"tokens_bytes_fixed",          static_cast<double>(tb_f)},
            {"boundaries_bytes_fixed",      static_cast<double>(bb_f)},
            {"heldout_output_bytes_fixed",  static_cast<double>(ob_f)},
            {"heldout_ratio_fixed",         ratio_f},
            // Matched-Dynamic-trained dict applied to heldout:
            {"dict_bytes_dyn",              static_cast<double>(db_d)},
            {"tokens_bytes_dyn",            static_cast<double>(tb_d)},
            {"boundaries_bytes_dyn",        static_cast<double>(bb_d)},
            {"heldout_output_bytes_dyn",    static_cast<double>(ob_d)},
            {"heldout_ratio_dyn",           ratio_d},
        };
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom",
                        .notes  = "onpair: bad_alloc during held_out_compress"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash",
                        .notes  = std::string("onpair: ") + e.what()};
    }
}

} // namespace onpair_bench
