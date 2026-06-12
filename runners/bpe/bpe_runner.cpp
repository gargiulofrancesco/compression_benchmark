// BpeRunner implementation. See bpe_runner.h for class shape.

#include "bpe_runner.h"
#include "bitpack.h"
#include "decompress_then_scan.h"
#include "scan_prefix.h"
#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include "sentencepiece_processor.h"
#include "sentencepiece_trainer.h"

#include <sys/resource.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cstring>
#include <fstream>
#include <memory>
#include <new>
#include <numeric>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace onpair_bench {

namespace {

// --- JSON helpers (mirror bigrepair_runner.cpp; intentional duplication) ---

std::string extract_str(const std::string& cfg, const std::string& key, const std::string& def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    if (pos >= cfg.size() || cfg[pos] != '"') return def;
    auto start = ++pos;
    while (pos < cfg.size() && cfg[pos] != '"') ++pos;
    if (pos > cfg.size()) return def;
    return cfg.substr(start, pos - start);
}

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

double extract_f64(const std::string& cfg, const std::string& key, double def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    size_t start = pos;
    while (pos < cfg.size() && (std::isdigit(static_cast<unsigned char>(cfg[pos]))
                                || cfg[pos] == '.' || cfg[pos] == '-' || cfg[pos] == '+'
                                || cfg[pos] == 'e' || cfg[pos] == 'E')) ++pos;
    if (pos == start) return def;
    try { return std::stod(cfg.substr(start, pos - start)); }
    catch (...) { return def; }
}

bool extract_bool(const std::string& cfg, const std::string& key, bool def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    if (cfg.compare(pos, 4, "true") == 0) return true;
    if (cfg.compare(pos, 5, "false") == 0) return false;
    return def;
}

// --- Byte <-> Unicode bijection (GPT-2 `bytes_to_unicode`) ---------------
//
// SentencePiece requires valid UTF-8 input and (even with byte_fallback) decodes
// lone high-bit bytes as U+FFFD — i.e. it is NOT byte-lossless on raw bytes. We
// sidestep this exactly as GPT-2/RoBERTa byte-level BPE does: map each of the 256
// byte values to a distinct, printable Unicode codepoint (a reversible bijection),
// feed SentencePiece the mapped text, and invert on decode. Control/whitespace
// bytes map into U+0100.. so they can never perturb SentencePiece's tokenisation.
// Combined with a one-line "coverage" sentence containing all 256 codepoints +
// character_coverage=1.0, every byte value is an atomic symbol, so a vocab trained
// on a SAMPLE still round-trips any byte in the full column. Verified lossless over
// 0x00..0xFF incl. lone 0x80/0xFF/NUL/newline and empty rows.
struct ByteUnicode {
    std::array<std::string, 256> b2u;              // byte -> UTF-8 of its codepoint
    std::unordered_map<uint32_t, uint8_t> cp2b;    // codepoint -> byte
    std::string coverage;                          // all 256 codepoints, once

    static std::string utf8(uint32_t cp) {
        std::string s;
        if (cp < 0x80) { s.push_back(static_cast<char>(cp)); }
        else {                                     // all our codepoints are < 0x800
            s.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            s.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
        return s;
    }

    ByteUnicode() {
        std::vector<int> bs, cs;
        auto add_range = [&](int lo, int hi) { for (int b = lo; b <= hi; ++b) bs.push_back(b); };
        add_range('!', '~'); add_range(0xA1, 0xAC); add_range(0xAE, 0xFF);
        cs = bs;
        std::array<bool, 256> in{}; for (int b : bs) in[b] = true;
        int n = 0;
        for (int b = 0; b < 256; ++b) if (!in[b]) { bs.push_back(b); cs.push_back(256 + n++); }
        for (size_t i = 0; i < bs.size(); ++i) {
            b2u[bs[i]] = utf8(static_cast<uint32_t>(cs[i]));
            cp2b[static_cast<uint32_t>(cs[i])] = static_cast<uint8_t>(bs[i]);
        }
        for (int b = 0; b < 256; ++b) coverage += b2u[b];
    }

    // bytes -> mapped UTF-8 text (fed to SentencePiece).
    std::string to_text(const char* data, size_t len) const {
        std::string out; out.reserve(len * 2);
        for (size_t i = 0; i < len; ++i)
            out += b2u[static_cast<uint8_t>(data[i])];
        return out;
    }

    // mapped UTF-8 text (from SentencePiece decode) -> original bytes.
    void to_bytes(const std::string& s, std::vector<uint8_t>& out) const {
        size_t i = 0;
        while (i < s.size()) {
            unsigned char c = static_cast<unsigned char>(s[i]);
            uint32_t cp; size_t adv;
            if (c < 0x80) { cp = c; adv = 1; }
            else if ((c & 0xE0) == 0xC0 && i + 1 < s.size()) {
                cp = (static_cast<uint32_t>(c & 0x1F) << 6)
                   | (static_cast<unsigned char>(s[i + 1]) & 0x3F);
                adv = 2;
            } else {
                throw std::runtime_error("bpe: malformed UTF-8 in decode output");
            }
            auto it = cp2b.find(cp);
            if (it == cp2b.end())
                throw std::runtime_error("bpe: decode produced an unmapped codepoint");
            out.push_back(it->second);
            i += adv;
        }
    }
};

const ByteUnicode& byte_unicode() { static const ByteUnicode bu; return bu; }

// --- Wire I/O helpers ---

struct LoadedWire {
    std::vector<uint8_t>   model_bytes;
    uint32_t               N = 0;
    uint32_t               V = 0;
    uint32_t               T = 0;
    uint64_t               decoded_bytes = 0;  // exact total decoded payload size
    std::vector<uint32_t>  tok_offsets;        // size = N + 1
    std::vector<uint8_t>   packed_tokens;      // size = ceil(T*B/8)
};

LoadedWire load_wire(const std::string& in_path) {
    std::ifstream f(in_path, std::ios::binary);
    if (!f) throw std::runtime_error("bpe: cannot open in_path " + in_path);
    LoadedWire w;
    uint32_t M = 0;
    f.read(reinterpret_cast<char*>(&M), 4);
    w.model_bytes.resize(M);
    if (M > 0) f.read(reinterpret_cast<char*>(w.model_bytes.data()),
                       static_cast<std::streamsize>(M));
    f.read(reinterpret_cast<char*>(&w.N), 4);
    f.read(reinterpret_cast<char*>(&w.V), 4);
    f.read(reinterpret_cast<char*>(&w.T), 4);
    f.read(reinterpret_cast<char*>(&w.decoded_bytes), 8);
    w.tok_offsets.resize(static_cast<size_t>(w.N) + 1);
    f.read(reinterpret_cast<char*>(w.tok_offsets.data()),
           static_cast<std::streamsize>((w.N + 1) * 4));
    const uint32_t B = bitpack::bits_for_vocab(w.V);
    const size_t packed_bytes =
        (static_cast<size_t>(w.T) * static_cast<size_t>(B) + 7) / 8;
    w.packed_tokens.resize(packed_bytes);
    if (packed_bytes > 0) {
        f.read(reinterpret_cast<char*>(w.packed_tokens.data()),
               static_cast<std::streamsize>(packed_bytes));
    }
    if (!f) throw std::runtime_error("bpe: short read on wire " + in_path);
    return w;
}

void set_address_space_cap(uint64_t mem_cap_bytes) {
    if (mem_cap_bytes == 0) return;
    struct rlimit rl{};
    rl.rlim_cur = mem_cap_bytes;
    rl.rlim_max = mem_cap_bytes;
    (void)::setrlimit(RLIMIT_AS, &rl);
}

bool is_oom_status(const sentencepiece::util::Status& s) {
    // Map allocator-style failure codes to status='oom'. SentencePiece uses
    // absl-style codes; RESOURCE_EXHAUSTED is the canonical OOM signal.
    using sentencepiece::util::StatusCode;
    return s.code() == StatusCode::kResourceExhausted
        || s.code() == StatusCode::kOutOfRange;
}

// Compose CLI-style training args per + +.
// max_sentence_length must accommodate the longest column row.
std::string make_train_args(uint32_t V, size_t max_row_size) {
    std::ostringstream o;
    o << "--model_type=bpe"
      << " --minloglevel=2"        // suppress trainer LOG(INFO) noise
      << " --vocab_size=" << V
      << " --normalization_rule_name=identity"
      << " --add_dummy_prefix=false"
      << " --remove_extra_whitespaces=false"
      << " --character_coverage=1.0"
      << " --num_threads=1"
      << " --shuffle_input_sentence=false"
      << " --train_extremely_large_corpus=false"
      << " --hard_vocab_limit=false"   // V is target, allow shortfall on small corpora
      << " --unk_id=0 --bos_id=-1 --eos_id=-1 --pad_id=-1"
      << " --input_format=text"
      << " --split_by_whitespace=false"
      << " --split_by_unicode_script=false"
      << " --split_by_number=false"
      << " --split_digits=false"
      << " --treat_whitespace_as_suffix=false"
      << " --allow_whitespace_only_pieces=true"
      << " --max_sentence_length=" << std::max<size_t>(max_row_size + 16, 8192);
    return o.str();
}

} // anonymous namespace

BpeConfig parse_bpe_config(const std::string& cfg) {
    BpeConfig c;
    c.vocab_size = static_cast<uint32_t>(extract_u64(cfg, "vocab_size", 16384));
    if (c.vocab_size != 4096 && c.vocab_size != 16384
        && c.vocab_size != 65536 && c.vocab_size != 262144) {
        throw std::runtime_error("bpe: vocab_size not in {4096, 16384, 65536, 262144} "
                                 "(headline set)");
    }
    c.normalization = extract_str(cfg, "normalization", "identity");
    if (c.normalization != "identity") {
        throw std::runtime_error("bpe: normalization must be 'identity' (lock)");
    }
    c.sample_fraction = extract_f64(cfg, "sample_fraction", 0.15);
    if (!(c.sample_fraction > 0.0 && c.sample_fraction <= 1.0)) {
        throw std::runtime_error("bpe: sample_fraction must be in (0, 1]");
    }
    c.threads = static_cast<uint32_t>(extract_u64(cfg, "threads", 1));
    if (c.threads != 1) {
        throw std::runtime_error("bpe: threads must be 1 (single-thread fairness lock)");
    }
    c.mem_cap_gb = extract_u64(cfg, "mem_cap_gb", 64);
    if (c.mem_cap_gb < 1 || c.mem_cap_gb > 1024) {
        throw std::runtime_error("bpe: mem_cap_gb out of range (must be 1..1024)");
    }
    return c;
}

BpeRunner::BpeRunner() = default;
BpeRunner::~BpeRunner() = default;

OpResult BpeRunner::compress(const std::string& in_path,
                              const std::string& out_path,
                              const std::string& cfg_json) {
    BpeConfig cfg = parse_bpe_config(cfg_json);
    Column col = read_column(in_path);
    const size_t N = col.num_rows();

    if (N == 0) {
        OpResult r;
        r.status = "unsupported";
        r.notes  = "empty column not supported by BPE";
        return r;
    }

    size_t input_bytes = 0;
    size_t max_row = 0;
    for (size_t i = 0; i < N; ++i) {
        const auto sv = col.at(i);
        input_bytes += sv.size();
        if (sv.size() > max_row) max_row = sv.size();
    }
    if (input_bytes == 0) {
        OpResult r;
        r.status = "unsupported";
        r.notes  = "all-empty-string column not supported by BPE";
        return r;
    }

    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));
    (void)max_row;
    const ByteUnicode& bu = byte_unicode();

    // Training corpus: a uniformly random sample of rows totalling
    // ~sample_fraction of the column bytes, drawn in seed-42-shuffled order —
    // the SAME sampling semantics as OnPair's trainer (which shuffles row
    // order with seed 42 and consumes rows until its byte budget). The earlier
    // head-prefix sampling was biased on
    // columns with positional drift and depressed BPE's dictionary quality.
    // Each row is byte<->unicode mapped, plus one coverage line of all 256
    // codepoints so every byte is an atomic symbol even if absent from the
    // sample. ENCODING (below) is always over the FULL column.
    const size_t sample_budget =
        static_cast<size_t>(static_cast<double>(input_bytes) * cfg.sample_fraction);
    std::vector<uint32_t> order(N);
    std::iota(order.begin(), order.end(), 0u);
    {
        std::mt19937_64 rng(42);                  // OnPair headline seed parity
        std::shuffle(order.begin(), order.end(), rng);
    }
    std::vector<std::string> sentences;
    size_t sampled_bytes = 0, sampled_rows = 0, max_mapped = bu.coverage.size();
    for (uint32_t i : order) {
        const auto sv = col.at(i);
        if (sv.size() == 0) continue;             // empty rows carry no training signal
        std::string mapped = bu.to_text(sv.data(), sv.size());
        if (mapped.size() > max_mapped) max_mapped = mapped.size();
        sentences.emplace_back(std::move(mapped));
        sampled_bytes += sv.size(); ++sampled_rows;
        if (sampled_bytes >= sample_budget && sampled_rows >= 1) break;
    }
    sentences.push_back(bu.coverage);             // 256-codepoint coverage line

    std::string serialized_proto;
    uint64_t t_train_ns = 0;
    try {
        const std::string args = make_train_args(cfg.vocab_size, max_mapped);
        const auto t0 = std::chrono::steady_clock::now();
        auto status = sentencepiece::SentencePieceTrainer::Train(
            args, sentences, &serialized_proto);
        t_train_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());
        if (!status.ok()) {
            OpResult r;
            r.status = is_oom_status(status) ? "oom" : "crash";
            r.notes  = std::string("SentencePieceTrainer::Train failed: ") + status.message();
            return r;
        }
    } catch (const std::bad_alloc&) {
        OpResult r;
        r.status = "oom";
        r.notes  = "bad_alloc in SentencePieceTrainer::Train";
        return r;
    }

    const uint32_t M = static_cast<uint32_t>(serialized_proto.size());

    sentencepiece::SentencePieceProcessor proc;
    {
        auto status = proc.LoadFromSerializedProto(serialized_proto);
        if (!status.ok()) {
            OpResult r;
            r.status = "crash";
            r.notes  = std::string("LoadFromSerializedProto failed: ") + status.message();
            return r;
        }
    }

    std::vector<uint32_t> tokens;
    std::vector<uint32_t> tok_offsets;
    tok_offsets.reserve(N + 1);
    tok_offsets.push_back(0);
    const uint32_t V = cfg.vocab_size;
    uint64_t t_encode_ns = 0;
    try {
        const auto te0 = std::chrono::steady_clock::now();
        for (size_t i = 0; i < N; ++i) {
            const auto sv = col.at(i);
            std::string mapped = bu.to_text(sv.data(), sv.size());
            std::vector<int> ids;
            auto st = proc.Encode(mapped, &ids);
            if (!st.ok()) {
                OpResult r;
                r.status = is_oom_status(st) ? "oom" : "crash";
                r.notes  = std::string("Encode failed: ") + st.message();
                return r;
            }
            for (int id : ids) {
                if (id < 0 || static_cast<uint32_t>(id) >= V) {
                    OpResult r;
                    r.status = "crash";
                    r.notes  = "encoded id outside [0, V)";
                    return r;
                }
                tokens.push_back(static_cast<uint32_t>(id));
            }
            tok_offsets.push_back(static_cast<uint32_t>(tokens.size()));
        }
        t_encode_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - te0).count());
    } catch (const std::bad_alloc&) {
        OpResult r;
        r.status = "oom";
        r.notes  = "bad_alloc in per-row Encode loop";
        return r;
    }

    const uint32_t T = static_cast<uint32_t>(tokens.size());
    const uint32_t B = bitpack::bits_for_vocab(V);

    std::vector<uint8_t> packed_tokens;
    try {
        bitpack::pack_uniform(tokens, B, packed_tokens);
    } catch (const std::bad_alloc&) {
        OpResult r;
        r.status = "oom";
        r.notes  = "bad_alloc in bitpack::pack_uniform";
        return r;
    }

    // Wire materialisation:
    //   [M][model_bytes][N][V][T][decoded_bytes u64][tok_offsets[N+1]][packed_tokens]
    // decoded_bytes = exact total decoded payload size (== input_bytes, lossless);
    // lets bulk_decode size its output buffer exactly OUTSIDE the timer.
    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("bpe: cannot open out_path " + out_path);
    f.write(reinterpret_cast<const char*>(&M), 4);
    if (M > 0) f.write(serialized_proto.data(),
                       static_cast<std::streamsize>(M));
    uint32_t N_u32 = static_cast<uint32_t>(N);
    f.write(reinterpret_cast<const char*>(&N_u32), 4);
    f.write(reinterpret_cast<const char*>(&V), 4);
    f.write(reinterpret_cast<const char*>(&T), 4);
    const uint64_t decoded_bytes_u64 = static_cast<uint64_t>(input_bytes);
    f.write(reinterpret_cast<const char*>(&decoded_bytes_u64), 8);
    f.write(reinterpret_cast<const char*>(tok_offsets.data()),
            static_cast<std::streamsize>((N + 1) * 4));
    if (!packed_tokens.empty()) {
        f.write(reinterpret_cast<const char*>(packed_tokens.data()),
                static_cast<std::streamsize>(packed_tokens.size()));
    }
    f.close();

    // FAIR DICTIONARY ACCOUNTING. The compression-ratio model cost is
    // the DECODE-MINIMAL id->bytes table — concatenated RAW piece bytes (mapping
    // inverted) + one u32 offset per vocab id — symmetric with OnPair's
    // dict_content_bytes + dict_offset_bytes and FSST's symbol-table header. We do
    // NOT charge the full serialized SentencePiece ModelProto (M): it additionally
    // carries per-piece float scores + protobuf framing the decoder never touches,
    // and stores pieces in 2-byte-per-original-byte GPT-2-mapped form. M is still
    // what the wire serialises (reported as model_proto_bytes for transparency);
    // the ratio charges only the decode-necessary model, like every other baseline.
    uint64_t dict_content_bytes = 0;
    {
        std::vector<uint8_t> piece_raw;
        const uint32_t vsize = static_cast<uint32_t>(proc.GetPieceSize());
        for (uint32_t id = 0; id < V; ++id) {
            if (id < vsize
                && !proc.IsUnknown(static_cast<int>(id))
                && !proc.IsControl(static_cast<int>(id))) {
                piece_raw.clear();
                bu.to_bytes(proc.IdToPiece(static_cast<int>(id)), piece_raw);  // appends raw bytes
                dict_content_bytes += piece_raw.size();
            }
        }
    }
    const uint64_t dict_offset_bytes = static_cast<uint64_t>(V + 1) * 4;  // u32 per id, like OnPair
    const uint64_t dict_bytes        = dict_content_bytes + dict_offset_bytes;
    const uint64_t model_proto_bytes = M;                 // full proto actually on the wire
    const uint64_t tokens_bytes     = packed_tokens.size();
    // 24 = wire scalar overhead (M,N,V,T = 16) + decoded_bytes u64 (8); plus the
    // per-row string offsets (N+1)*4. All EXCLUDED from the payload-only ratio.
    const uint64_t boundaries_bytes = 24 + static_cast<uint64_t>(N + 1) * 4;
    // output_bytes is the ACCOUNTED (fair) compressed size = decode model + token
    // stream + boundaries; bit-tight by construction. The literal on-disk file is
    // larger by (model_proto_bytes - dict_bytes) — exposed as wire_bytes below — and
    // that excess is encoder-only proto overhead, not a decode cost.
    const uint64_t output_bytes     = dict_bytes + tokens_bytes + boundaries_bytes;
    const uint64_t wire_bytes       = model_proto_bytes + 24
                                    + static_cast<uint64_t>(N + 1) * 4
                                    + static_cast<uint64_t>(packed_tokens.size());

    OpResult r;
    r.metrics.emplace_back("dict_bytes",         static_cast<double>(dict_bytes));
    r.metrics.emplace_back("dict_content_bytes", static_cast<double>(dict_content_bytes));
    r.metrics.emplace_back("dict_offset_bytes",  static_cast<double>(dict_offset_bytes));
    r.metrics.emplace_back("model_proto_bytes",  static_cast<double>(model_proto_bytes));
    r.metrics.emplace_back("tokens_bytes",     static_cast<double>(tokens_bytes));
    r.metrics.emplace_back("boundaries_bytes", static_cast<double>(boundaries_bytes));
    r.metrics.emplace_back("input_bytes",      static_cast<double>(input_bytes));
    r.metrics.emplace_back("output_bytes",     static_cast<double>(output_bytes));
    r.metrics.emplace_back("wire_bytes",       static_cast<double>(wire_bytes));
    r.metrics.emplace_back("num_rows",         static_cast<double>(N));
    // Training vs. encoding time reported SEPARATELY (fairness-audit requirement
    // for baselines with a training phase): T_c derives from t_encode_ns over the
    // full column; t_train_ns is the appendix build-time row (alongside Re-Pair).
    r.metrics.emplace_back("t_train_ns",       static_cast<double>(t_train_ns));
    r.metrics.emplace_back("t_encode_ns",      static_cast<double>(t_encode_ns));
    // Uniform throughput key for the figures: T_c is the per-row ENCODE rate over
    // the full column (training is the separate appendix t_train_ns, not in T_c).
    r.metrics.emplace_back("t_compress_ns",    static_cast<double>(t_encode_ns));
    r.metrics.emplace_back("sampled_rows",     static_cast<double>(sampled_rows));
    return r;
}

namespace {

// Decode the full column from a loaded wire + processor. Reused by
// bulk_decode and prefix_search. SentencePiece returns byte<->unicode MAPPED
// text; we invert the mapping to recover the exact original bytes.
// token id -> raw decoded bytes, as a flat dictionary (mirrors OnPair's
// Dictionary: concatenated bytes + offsets[V+1]). This is the decode-ready form
// of the BPE model.
// Fixed over-copy width for the decode hot loop (see decode_payload). Both the
// dictionary byte arena and the decode output buffer are padded by this many
// bytes so a constant-size copy from any token offset never reads/writes OOB.
inline constexpr uint32_t PIECE_OVERCOPY = 16;

struct PieceDict {
    std::vector<uint8_t>  bytes;     // concatenated raw bytes of every piece
                                     // (+ PIECE_OVERCOPY pad bytes at the tail)
    std::vector<uint32_t> offsets;   // size V+1; piece id -> [offsets[id], offsets[id+1])
};

// Build the id->bytes table ONCE from the SentencePiece model (model setup, not
// per-decode work). With our training flags (add_dummy_prefix=false,
// remove_extra_whitespaces=false, normalization=identity, byte<->unicode mapped
// input) SentencePiece's Decode(ids) is a verbatim concatenation of the pieces'
// surface forms, so decoding a token stream == concatenating each token's raw
// bytes. We therefore invert the byte<->unicode map per VOCAB ENTRY here (V times)
// instead of per token OCCURRENCE (T times, T >> V) via the heavyweight
// proc.Decode() per row — the latter built a SentencePieceText proto per row and
// dominated decode time. unk/control pieces never appear in the encoded stream
// (the 256-codepoint coverage guarantees no unk); they get an empty byte range.
PieceDict build_piece_dict(const sentencepiece::SentencePieceProcessor& proc,
                           uint32_t V) {
    PieceDict d;
    d.offsets.reserve(V + 1);
    d.offsets.push_back(0);
    const ByteUnicode& bu = byte_unicode();
    const uint32_t vsize = static_cast<uint32_t>(proc.GetPieceSize());
    for (uint32_t id = 0; id < V; ++id) {
        if (id < vsize
            && !proc.IsUnknown(static_cast<int>(id))
            && !proc.IsControl(static_cast<int>(id))) {
            bu.to_bytes(proc.IdToPiece(static_cast<int>(id)), d.bytes);  // appends
        }
        d.offsets.push_back(static_cast<uint32_t>(d.bytes.size()));
    }
    // Tail pad so decode_payload's fixed PIECE_OVERCOPY read from the last (or any
    // short) piece never runs past the arena. offsets are unchanged.
    d.bytes.resize(d.bytes.size() + PIECE_OVERCOPY, 0u);
    return d;
}

// PAYLOAD-ONLY decode kernel — the TIMED region. Unpacks the bit-packed token
// stream into the caller-provided `all_tokens` (pre-sized to T, OUTSIDE the timer)
// and concatenates each token's raw bytes from the PieceDict into the
// caller-provided `dst` (pre-sized to the exact decoded size, OUTSIDE the timer).
// This is the OnPair-style decode kernel: bit-unpack + dictionary memcpy, and
// NOTHING ELSE — no allocation, no buffer sizing pass, and no per-row string
// offsets (the offsets are rebuilt UNTIMED by build_offsets() for the round-trip
// output only, exactly as for onpair/zstd/lz4). Returns total bytes written.
size_t decode_payload(const LoadedWire& w, const PieceDict& dict, uint8_t* dst) {
    const uint8_t*  db  = dict.bytes.data();
    const uint32_t* off = dict.offsets.data();
    size_t cum = 0;

    // Emit one token with the fixed-width over-copy trick: a single COMPILE-TIME
    // sized PIECE_OVERCOPY store (one SIMD move, no memcpy size-dispatch), then
    // advance by the TRUE length so the over-copied tail is overwritten by the
    // next token. dict arena + dst are padded by PIECE_OVERCOPY (outside the
    // timer) so the fixed read/write stays in-bounds; rare pieces > PIECE_OVERCOPY
    // take a one-off tail copy.
    //
    // MACRO, not a lambda (mirrors the OnPair library's ONPAIR_EMIT_NO_OFF): a
    // lambda shared between these loops and the for_each_token callback below got
    // OUTLINED by GCC (one call + a memory round-trip on the cum chain per token,
    // ~3x decode loss — exactly the hazard the note below warns about, hit through
    // the back door). The macro guarantees inline emission with cum in a register.
    #define BPE_EMIT(id_expr) do { \
        const uint32_t id_  = (id_expr); \
        const uint32_t o_   = off[id_]; \
        const uint32_t len_ = off[id_ + 1] - o_; \
        std::memcpy(dst + cum, db + o_, PIECE_OVERCOPY); \
        if (__builtin_expect(len_ > PIECE_OVERCOPY, 0)) \
            std::memcpy(dst + cum + PIECE_OVERCOPY, db + o_ + PIECE_OVERCOPY, \
                        len_ - PIECE_OVERCOPY); \
        cum += len_; \
    } while (0)

    // FAIRNESS: BPE's bit-unpacking now matches OnPair's library speed instead of
    // riding a slow scalar reader. Byte-aligned widths take a tight typed loop with
    // the output cursor (cum) kept LOCAL (the fastest path; this is what bpe_v65536
    // → B=16 uses for the size-matched comparison vs OnPair bits=16). Sub-byte
    // widths go through the shared OnPair-equivalent batched extract16 in
    // runners/core/bitpack.h. Both fuse the emit body — no intermediate token array.
    // (Byte-aligned widths are NOT routed through the generic callback: there the
    // cursor cannot stay register-resident and the trivial per-token path loses ~3x.)
    const uint32_t B  = bitpack::bits_for_vocab(w.V);
    const uint8_t*  pk = w.packed_tokens.data();
    if (B == 16) {
        const auto* toks = reinterpret_cast<const uint16_t*>(pk);
        for (uint32_t j = 0; j < w.T; ++j) BPE_EMIT(toks[j]);
        return cum;
    }
    if (B == 8) {
        for (uint32_t j = 0; j < w.T; ++j) BPE_EMIT(pk[j]);
        return cum;
    }
    bitpack::for_each_token(pk, w.packed_tokens.size(), B, w.T,
                            [&](uint32_t id) { BPE_EMIT(id); });
    #undef BPE_EMIT
    return cum;
}

// UNTIMED: per-row string offsets from the already-unpacked tokens. Needed only
// to materialise the round-trip verification Column; NOT part of the measured
// decode (mirrors zstd/lz4 build_offsets and onpair's untimed offset pass).
void build_offsets(const LoadedWire& w, const std::vector<uint32_t>& all_tokens,
                   const PieceDict& dict, std::vector<uint32_t>& offsets) {
    offsets.assign(w.N + 1, 0);
    size_t cum = 0;
    for (uint32_t i = 0; i < w.N; ++i) {
        const uint32_t lo = w.tok_offsets[i];
        const uint32_t hi = w.tok_offsets[i + 1];
        for (uint32_t j = lo; j < hi; ++j) {
            const uint32_t id = all_tokens[j];
            cum += dict.offsets[id + 1] - dict.offsets[id];
        }
        offsets[i + 1] = static_cast<uint32_t>(cum);
    }
}

// Full decode (payload + offsets) for non-timed callers (prefix_search), which
// need a complete Column. Buffer sized exactly from the wire's decoded_bytes.
Column extract_all(const LoadedWire& w, const PieceDict& dict) {
    Column out;
    out.bytes.resize(static_cast<size_t>(w.decoded_bytes) + PIECE_OVERCOPY);  // over-copy slack
    const size_t total = decode_payload(w, dict, out.bytes.data());
    out.bytes.resize(total);
    // Offsets reconstructed from a separate (here untimed) token unpack.
    const uint32_t B = bitpack::bits_for_vocab(w.V);
    std::vector<uint32_t> all_tokens;
    bitpack::unpack_uniform(w.packed_tokens, B, w.T, all_tokens);
    build_offsets(w, all_tokens, dict, out.offsets);
    return out;
}

// PER-STRING row decode (random access, O(|s_i|)). Extracts ONLY row i's tokens
// [lo, hi) directly at their bit offset (j*B) — never unpacking the whole T-token
// stream — and concatenates their bytes from the PieceDict. B <= 18 and shift <= 7
// so each token sits within a single 8-byte little-endian read. `dst` must hold the
// row's decoded bytes + PIECE_OVERCOPY slack. Returns bytes written.
// `want` caps the decode: stop once >= want bytes are produced (for the prefix
// scan, want = needle length). Default = whole row (random access / ra_latency).
// `dst` must hold want + SCAN_PREFIX_OVERSHOOT (or the full row) bytes.
size_t decode_row_into(const LoadedWire& w, const PieceDict& dict,
                       uint32_t lo, uint32_t hi, uint8_t* dst,
                       size_t want = static_cast<size_t>(-1)) {
    const uint32_t  B    = bitpack::bits_for_vocab(w.V);
    const uint64_t  mask = (B >= 32) ? ~uint64_t{0} : ((uint64_t{1} << B) - 1);
    const uint8_t*  pk   = w.packed_tokens.data();
    const size_t    pks  = w.packed_tokens.size();
    const uint8_t*  db   = dict.bytes.data();
    const uint32_t* off  = dict.offsets.data();
    size_t cum = 0;
    for (uint32_t j = lo; j < hi; ++j) {
        const uint64_t bitpos  = static_cast<uint64_t>(j) * B;
        const size_t   bytepos = static_cast<size_t>(bitpos >> 3);
        const uint32_t shift   = static_cast<uint32_t>(bitpos & 7);
        uint64_t word = 0;
        const size_t avail = (bytepos < pks) ? std::min<size_t>(8, pks - bytepos) : 0;
        std::memcpy(&word, pk + bytepos, avail);          // token within these bytes (shift+B<=64)
        const uint32_t id  = static_cast<uint32_t>((word >> shift) & mask);
        const uint32_t o   = off[id];
        const uint32_t len = off[id + 1] - o;
        std::memcpy(dst + cum, db + o, PIECE_OVERCOPY);   // over-copy; tail overwritten / padded
        if (__builtin_expect(len > PIECE_OVERCOPY, 0))
            std::memcpy(dst + cum + PIECE_OVERCOPY, db + o + PIECE_OVERCOPY, len - PIECE_OVERCOPY);
        cum += len;
        if (cum >= want) break;                           // prefix-scan early-out
    }
    return cum;
}

} // anonymous namespace

OpResult BpeRunner::bulk_decode(const std::string& in_path,
                                 const std::string& out_path,
                                 const std::string& cfg_json) {
    BpeConfig cfg = parse_bpe_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    LoadedWire w;
    sentencepiece::SentencePieceProcessor proc;
    Column out;
    uint64_t t_decode_ns = 0;
    try {
        // UNTIMED: file read + SentencePiece model load. Deserialising and
        // building the BPE model is one-time setup, not decode work — excluded
        // so the timer measures only the decode kernel, consistent with every
        // baseline (uniform decode-timer contract, time only the
        // in-RAM payload decompression; wire/model load + file I/O excluded).
        w = load_wire(in_path);
        std::string blob(reinterpret_cast<const char*>(w.model_bytes.data()),
                         w.model_bytes.size());
        auto st = proc.LoadFromSerializedProto(blob);
        if (!st.ok()) {
            OpResult r; r.status = "crash";
            r.notes = std::string("LoadFromSerializedProto: ") + st.message();
            return r;
        }
        // UNTIMED: derive the id->bytes decode table from the model (one-time
        // model setup; the analogue of OnPair loading its dictionary from the
        // wire). Excluded from the timer like every other baseline's model load.
        const PieceDict dict = build_piece_dict(proc, w.V);
        // UNTIMED: pre-allocate the output payload buffer (EXACT decoded size from
        // the wire + over-copy slack). resize() value-inits here so pages are
        // resident before the timed region (no first-touch faults), and the timer
        // excludes allocation + buffer sizing entirely.
        out.bytes.resize(static_cast<size_t>(w.decoded_bytes) + PIECE_OVERCOPY);
        // TIMED: FUSED token bit-unpack + dictionary byte concatenation ONLY — the
        // decode kernel. No allocation, no sizing pass, no intermediate token
        // array, no per-row string offsets (built UNTIMED below for the round-trip
        // output), matching the uniform payload-only decode-timer contract used by
        // onpair/zstd/lz4.
        const auto t0 = std::chrono::steady_clock::now();
        const size_t total = decode_payload(w, dict, out.bytes.data());
        t_decode_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());
        // UNTIMED: shrink to the true size + rebuild per-row offsets for output
        // (re-unpack the tokens off the timed path).
        out.bytes.resize(total);
        std::vector<uint32_t> all_tokens;
        bitpack::unpack_uniform(w.packed_tokens, bitpack::bits_for_vocab(w.V),
                                w.T, all_tokens);
        build_offsets(w, all_tokens, dict, out.offsets);
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in bulk_decode"; return r;
    }
    write_column(out_path, out);                         // UNTIMED: file write

    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(out.bytes.size()));
    r.metrics.emplace_back("num_rows",     static_cast<double>(out.num_rows()));
    r.metrics.emplace_back("t_decode_ns",  static_cast<double>(t_decode_ns));
    return r;
}

OpResult BpeRunner::random_decode(const std::string& in_path,
                                   size_t idx,
                                   const std::string& out_path,
                                   const std::string& cfg_json) {
    BpeConfig cfg = parse_bpe_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    LoadedWire w;
    sentencepiece::SentencePieceProcessor proc;
    try {
        w = load_wire(in_path);
        std::string blob(reinterpret_cast<const char*>(w.model_bytes.data()),
                         w.model_bytes.size());
        auto st = proc.LoadFromSerializedProto(blob);
        if (!st.ok()) {
            OpResult r; r.status = "crash";
            r.notes = std::string("LoadFromSerializedProto: ") + st.message();
            return r;
        }
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc loading wire"; return r;
    }

    if (idx >= w.N) {
        OpResult r;
        r.status = "crash";
        r.notes  = "idx out of range: " + std::to_string(idx);
        return r;
    }

    const uint32_t lo = w.tok_offsets[idx];
    const uint32_t hi = w.tok_offsets[idx + 1];

    std::vector<uint8_t> row_bytes;
    try {
        // PER-STRING decode (O(|s_i|)): build the id->bytes table (one-time model
        // setup), then concatenate ONLY row idx's tokens — extracted directly at
        // their bit offset, never unpacking the whole T-token stream.
        const PieceDict dict = build_piece_dict(proc, w.V);
        size_t max_piece = 1;
        for (uint32_t id = 0; id < w.V; ++id)
            max_piece = std::max<size_t>(max_piece, dict.offsets[id + 1] - dict.offsets[id]);
        std::vector<uint8_t> buf(static_cast<size_t>(hi - lo) * max_piece + PIECE_OVERCOPY);
        const size_t len = decode_row_into(w, dict, lo, hi, buf.data());
        row_bytes.assign(buf.begin(), buf.begin() + len);
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in random_decode"; return r;
    }

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("bpe: cannot open " + out_path);
    if (!row_bytes.empty())
        f.write(reinterpret_cast<const char*>(row_bytes.data()),
                static_cast<std::streamsize>(row_bytes.size()));
    f.close();

    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(row_bytes.size()));
    return r;
}

OpResult BpeRunner::prefix_search(const std::string& in_path,
                                   const std::string& needle,
                                   const std::string& out_path,
                                   const std::string& cfg_json) {
    BpeConfig cfg = parse_bpe_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    LoadedWire w;
    sentencepiece::SentencePieceProcessor proc;
    try {
        w = load_wire(in_path);
        std::string blob(reinterpret_cast<const char*>(w.model_bytes.data()),
                         w.model_bytes.size());
        auto st = proc.LoadFromSerializedProto(blob);
        if (!st.ok()) {
            OpResult r; r.status = "crash";
            r.notes = std::string("LoadFromSerializedProto: ") + st.message();
            return r;
        }
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc loading wire"; return r;
    }

    size_t n_hits = 0;
    uint64_t t_scan_ns = 0;
    std::vector<uint32_t> hits;
    try {
        // UNTIMED: id->bytes table (model setup). FAIR PREFIX SCAN (W-7): BPE is
        // per-string, so decode only ~|needle| bytes of EACH row (decode_row_into
        // stops at `want`) and test the prefix — not the whole column.
        const PieceDict dict = build_piece_dict(proc, w.V);
        const std::string_view nv{needle};
        const auto t0 = std::chrono::steady_clock::now();
        n_hits = partial_prefix_scan(w.N, nv,
            [&](size_t row, uint8_t* dst, size_t want) -> size_t {
                return decode_row_into(w, dict, w.tok_offsets[row],
                                       w.tok_offsets[row + 1], dst, want);
            }, hits);
        t_scan_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0).count());
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in prefix_search"; return r;
    }

    // UNTIMED: write the hit list ([n_hits u32][hits u32]) — same wire as the shared
    // decompress_then_scan path, so the G6 bit-equality gate is unchanged.
    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("bpe: cannot open out_path " + out_path);
    uint32_t nh = static_cast<uint32_t>(n_hits);
    f.write(reinterpret_cast<const char*>(&nh), 4);
    if (!hits.empty())
        f.write(reinterpret_cast<const char*>(hits.data()),
                static_cast<std::streamsize>(hits.size() * 4));

    OpResult r;
    r.metrics.emplace_back("n_hits",    static_cast<double>(n_hits));
    r.metrics.emplace_back("num_rows",  static_cast<double>(w.N));
    r.metrics.emplace_back("t_scan_ns", static_cast<double>(t_scan_ns));
    return r;
}

// Random-access latency — PER-STRING (§8.4). BPE decodes ONLY row i's token run
// (extracted at its bit offset; O(|s_i|)), against the resident id->bytes table —
// the same per-string class as FSST/OnPair, so its L_ra is skew/layout-insensitive.
// Model load + dict build + buffer sizing are one-time setup OUTSIDE the timed loop;
// no file I/O in the loop. Refuses cleanly off the controlled x86 rig.
OpResult BpeRunner::ra_latency(const std::string& in_path,
                               const std::string& /*out_path*/,
                               const std::string& cfg_json) {
    BpeConfig cfg = parse_bpe_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));
    try {
        LoadedWire w = load_wire(in_path);
        if (w.N == 0)
            return OpResult{.status = "crash", .notes = "bpe: empty column for ra_latency"};

        sentencepiece::SentencePieceProcessor proc;
        std::string blob(reinterpret_cast<const char*>(w.model_bytes.data()),
                         w.model_bytes.size());
        auto st = proc.LoadFromSerializedProto(blob);
        if (!st.ok())
            return OpResult{.status = "crash",
                            .notes  = std::string("LoadFromSerializedProto: ") + st.message()};
        const PieceDict dict = build_piece_dict(proc, w.V);   // model setup (untimed)

        uint64_t n_samples = extract_u64(cfg_json, "n_samples", 1000000);
        if (n_samples == 0) n_samples = 1;
        uint64_t seed   = extract_u64(cfg_json, "seed", 42);
        bool     warmup = extract_u64(cfg_json, "warmup", 1) != 0;

        // Size the row buffer once: widest row (tokens) × longest piece + slack.
        uint32_t max_row_tokens = 0;
        for (uint32_t i = 0; i < w.N; ++i)
            max_row_tokens = std::max(max_row_tokens, w.tok_offsets[i + 1] - w.tok_offsets[i]);
        size_t max_piece = 1;
        for (uint32_t id = 0; id < w.V; ++id)
            max_piece = std::max<size_t>(max_piece, dict.offsets[id + 1] - dict.offsets[id]);
        std::vector<uint8_t> buf(static_cast<size_t>(max_row_tokens) * max_piece + PIECE_OVERCOPY);

        if (warmup) { Column warm = extract_all(w, dict); (void)warm; }

        const QuerySpec qspec = parse_query_spec(cfg_json);
        const std::vector<uint64_t> indices =
            generate_query_indices(qspec, n_samples, w.N, seed);

        RaLatencyResult m;
        std::string ra_reason;
        if (!ra_measure_shipping(indices,
            [&](uint64_t idx64) -> unsigned char {
                const uint32_t i = static_cast<uint32_t>(idx64);
                const size_t len = decode_row_into(
                    w, dict, w.tok_offsets[i], w.tok_offsets[i + 1], buf.data());
                return len ? buf[len - 1] : 0;
            }, m, ra_reason)) {
            return OpResult{.status = "refused-env", .notes = "bpe: " + ra_reason};
        }
        if (m.stats.n > 0 && m.stats.p50 < 1.0) {
            return OpResult{.status = "crash",
                .notes = "bpe: implausible sub-ns p50 (latency primitive failure)"};
        }
        const LatencyStats& s = m.stats;
        OpResult r;
        r.metrics.emplace_back("l_ra_p50_ns",  s.p50);
        r.metrics.emplace_back("l_ra_p95_ns",  s.p95);
        r.metrics.emplace_back("l_ra_p99_ns",  s.p99);
        r.metrics.emplace_back("l_ra_mean_ns", s.mean);
        r.metrics.emplace_back("l_ra_min_ns",  s.min);
        r.metrics.emplace_back("l_ra_max_ns",  s.max);
        r.metrics.emplace_back("n_samples",    static_cast<double>(s.n));
        r.metrics.emplace_back("batch_k",      static_cast<double>(m.batch_k));
        r.metrics.emplace_back("clock_resolution_ns", m.clock_resolution_ns);
        r.metrics.emplace_back("num_rows",     static_cast<double>(w.N));
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "bpe: bad_alloc ra_latency"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("bpe: ") + e.what()};
    }
}

} // namespace onpair_bench
