// RepairRunner — plain Re-Pair grammar over the full column. See repair_runner.h.

#include "repair_runner.h"
#include "bitpack.h"
#include "decompress_then_scan.h"
#include "latency_stats.h"
#include "query_sampler.h"
#include "ra_timer_tsc.h"

#include "RePair/RePair.h"   // libCSD Re-Pair grammar builder (IRePair + grammar G)

#include <sys/resource.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace onpair_bench {

namespace {

// RePair keeps terminals/rules/G protected; expose them via a thin subclass
// (no third-party edits). The inherited constructor does all the Re-Pair work.
struct RePairAccess : public RePair {
    using RePair::RePair;
    uint64_t     get_terminals() const { return terminals; }
    uint64_t     get_rules()     const { return rules; }
    LogSequence* get_G()         const { return G; }
};

uint64_t extract_u64(const std::string& cfg, const std::string& key, uint64_t def) {
    std::string needle = "\"" + key + "\"";
    auto pos = cfg.find(needle);
    if (pos == std::string::npos) return def;
    pos = cfg.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < cfg.size() && (cfg[pos] == ' ' || cfg[pos] == '\t')) ++pos;
    uint64_t v = 0; bool any = false;
    while (pos < cfg.size() && cfg[pos] >= '0' && cfg[pos] <= '9') {
        v = v * 10 + static_cast<uint64_t>(cfg[pos] - '0'); ++pos; any = true;
    }
    return any ? v : def;
}

void set_address_space_cap(uint64_t mem_cap_bytes) {
    if (mem_cap_bytes == 0) return;
    struct rlimit rl{};
    rl.rlim_cur = mem_cap_bytes; rl.rlim_max = mem_cap_bytes;
    (void)::setrlimit(RLIMIT_AS, &rl);
}

struct LoadedWire {
    uint32_t terminals = 0, rules = 0, n_seq = 0, bits = 0, N = 0;
    uint64_t decoded_bytes = 0;
    std::vector<uint32_t> row_offsets;     // size N+1
    std::vector<uint8_t>  packed_grammar;  // 2*rules symbols @ bits
    std::vector<uint8_t>  packed_seq;      // n_seq symbols @ bits
};

LoadedWire load_wire(const std::string& in_path) {
    std::ifstream f(in_path, std::ios::binary);
    if (!f) throw std::runtime_error("repair: cannot open in_path " + in_path);
    LoadedWire w;
    f.read(reinterpret_cast<char*>(&w.terminals), 4);
    f.read(reinterpret_cast<char*>(&w.rules), 4);
    f.read(reinterpret_cast<char*>(&w.n_seq), 4);
    f.read(reinterpret_cast<char*>(&w.bits), 4);
    f.read(reinterpret_cast<char*>(&w.N), 4);
    f.read(reinterpret_cast<char*>(&w.decoded_bytes), 8);
    w.row_offsets.resize(static_cast<size_t>(w.N) + 1);
    f.read(reinterpret_cast<char*>(w.row_offsets.data()),
           static_cast<std::streamsize>((static_cast<size_t>(w.N) + 1) * 4));
    const size_t gbytes = (static_cast<uint64_t>(2) * w.rules * w.bits + 7) / 8;
    const size_t sbytes = (static_cast<uint64_t>(w.n_seq) * w.bits + 7) / 8;
    w.packed_grammar.resize(gbytes);
    if (gbytes) f.read(reinterpret_cast<char*>(w.packed_grammar.data()),
                       static_cast<std::streamsize>(gbytes));
    w.packed_seq.resize(sbytes);
    if (sbytes) f.read(reinterpret_cast<char*>(w.packed_seq.data()),
                       static_cast<std::streamsize>(sbytes));
    if (!f) throw std::runtime_error("repair: short read on wire " + in_path);
    return w;
}

// Expand the full top-level symbol sequence into the PRE-SIZED dst (exact decoded
// size). Iterative (explicit stack) so a deep grammar can't overflow the C stack.
// symbol < terminals -> terminal byte; else rule (symbol - terminals): grammar[2r]
// then grammar[2r+1] (left-first). Returns bytes written.
size_t expand_sequence(const uint32_t* grammar, uint32_t terminals,
                       const uint32_t* seq, size_t n_seq, uint8_t* dst) {
    size_t cum = 0;
    std::vector<uint32_t> stack;
    stack.reserve(64);
    for (size_t i = 0; i < n_seq; ++i) {
        uint32_t s = seq[i];
        if (s < terminals) { dst[cum++] = static_cast<uint8_t>(s); continue; }
        stack.push_back(s);
        while (!stack.empty()) {
            uint32_t sym = stack.back(); stack.pop_back();
            if (sym < terminals) {
                dst[cum++] = static_cast<uint8_t>(sym);
            } else {
                const uint32_t r = sym - terminals;
                stack.push_back(grammar[2u * r + 1u]);  // right pushed first
                stack.push_back(grammar[2u * r]);        // left popped first
            }
        }
    }
    return cum;
}

} // namespace

RepairConfig parse_repair_config(const std::string& cfg_json) {
    RepairConfig c;
    c.mem_cap_gb = extract_u64(cfg_json, "mem_cap_gb", 64);
    if (c.mem_cap_gb < 1 || c.mem_cap_gb > 1024)
        throw std::runtime_error("repair: mem_cap_gb out of range (1..1024)");
    return c;
}

RepairRunner::RepairRunner() = default;
RepairRunner::~RepairRunner() = default;

OpResult RepairRunner::compress(const std::string& in_path,
                                const std::string& out_path,
                                const std::string& cfg_json) {
    RepairConfig cfg = parse_repair_config(cfg_json);
    Column col = read_column(in_path);
    const size_t N = col.num_rows();
    const size_t input_bytes = col.offsets.empty() ? 0 : col.offsets.back();
    if (N == 0 || input_bytes == 0) {
        OpResult r; r.status = "unsupported";
        r.notes = "empty column not supported by repair"; return r;
    }
    if (input_bytes > 0x7FFFFFFFull) {
        OpResult r; r.status = "unsupported";
        r.notes = "repair: column > 2 GiB exceeds RePair 32-bit sequence"; return r;
    }
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));

    std::vector<uint32_t> grammar;   // 2*rules symbols (left,right per rule)
    std::vector<uint32_t> cseq;      // top-level symbol sequence
    uint32_t terminals = 0, rules = 0, bits = 0;
    std::vector<uint8_t> packed_grammar, packed_seq;

    // TIMED: build the Re-Pair grammar over the WHOLE column text + serialise it.
    const auto t0 = std::chrono::steady_clock::now();
    try {
        // RePair compresses the int sequence IN PLACE (kept symbols stay >=0,
        // absorbed positions become negative back-pointers) and builds grammar G.
        std::vector<int> seq(col.bytes.begin(), col.bytes.begin() + input_bytes);
        RePairAccess rp(seq.data(), static_cast<uint>(input_bytes), 255);
        terminals = static_cast<uint32_t>(rp.get_terminals());
        rules     = static_cast<uint32_t>(rp.get_rules());

        // Compact the in-place sequence -> flat top-level symbol stream (skip the
        // negative back-pointers; every non-negative entry is a real symbol).
        cseq.reserve(input_bytes / 2 + 1);
        size_t io = 0;
        while (io < input_bytes) {
            const int v = seq[io];
            if (v >= 0) { cseq.push_back(static_cast<uint32_t>(v)); ++io; }
            else        { io = static_cast<size_t>(-(v + 1)); }
        }

        // Extract the grammar rule pairs (left,right) from G.
        grammar.resize(static_cast<size_t>(2) * rules);
        LogSequence* G = rp.get_G();
        for (uint32_t i = 0; i < rules; ++i) {
            grammar[2u * i]      = static_cast<uint32_t>(G->getField(2u * i));
            grammar[2u * i + 1u] = static_cast<uint32_t>(G->getField(2u * i + 1u));
        }

        bits = bitpack::bits_for_vocab(terminals + rules);  // symbols in [0, term+rules)
        if (bits == 0) bits = 1;
        bitpack::pack_uniform(grammar, bits, packed_grammar);
        bitpack::pack_uniform(cseq, bits, packed_seq);
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in repair compress"; return r;
    }
    const uint64_t t_compress_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t0).count());

    // Wire write (UNTIMED).
    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("repair: cannot open out_path " + out_path);
    const uint32_t n_seq = static_cast<uint32_t>(cseq.size());
    const uint32_t N_u32 = static_cast<uint32_t>(N);
    const uint64_t decoded_bytes = static_cast<uint64_t>(input_bytes);
    f.write(reinterpret_cast<const char*>(&terminals), 4);
    f.write(reinterpret_cast<const char*>(&rules), 4);
    f.write(reinterpret_cast<const char*>(&n_seq), 4);
    f.write(reinterpret_cast<const char*>(&bits), 4);
    f.write(reinterpret_cast<const char*>(&N_u32), 4);
    f.write(reinterpret_cast<const char*>(&decoded_bytes), 8);
    f.write(reinterpret_cast<const char*>(col.offsets.data()),
            static_cast<std::streamsize>((N + 1) * 4));
    if (!packed_grammar.empty())
        f.write(reinterpret_cast<const char*>(packed_grammar.data()),
                static_cast<std::streamsize>(packed_grammar.size()));
    if (!packed_seq.empty())
        f.write(reinterpret_cast<const char*>(packed_seq.data()),
                static_cast<std::streamsize>(packed_seq.size()));
    f.close();

    const uint64_t dict_bytes       = packed_grammar.size();   // the grammar = dictionary
    const uint64_t tokens_bytes     = packed_seq.size();       // the sequence = payload
    const uint64_t boundaries_bytes = 28 + static_cast<uint64_t>(N + 1) * 4;  // 5*u32+u64 + offsets
    const uint64_t output_bytes     = dict_bytes + tokens_bytes + boundaries_bytes;

    OpResult r;
    r.metrics.emplace_back("dict_bytes",       static_cast<double>(dict_bytes));
    r.metrics.emplace_back("tokens_bytes",     static_cast<double>(tokens_bytes));
    r.metrics.emplace_back("boundaries_bytes", static_cast<double>(boundaries_bytes));
    r.metrics.emplace_back("input_bytes",      static_cast<double>(input_bytes));
    r.metrics.emplace_back("output_bytes",     static_cast<double>(output_bytes));
    r.metrics.emplace_back("num_rows",         static_cast<double>(N));
    r.metrics.emplace_back("num_rules",        static_cast<double>(rules));
    r.metrics.emplace_back("t_compress_ns",    static_cast<double>(t_compress_ns));
    return r;
}

namespace {
// Decode a loaded wire into a Column (used by bulk_decode + prefix_search).
// Grammar unpack + output sizing are UNTIMED setup; only seq-unpack + expand are
// the decode kernel. If `t_decode_ns` is non-null it is filled with the kernel time.
Column decode_all(const LoadedWire& w, uint64_t* t_decode_ns) {
    Column out;
    out.bytes.resize(static_cast<size_t>(w.decoded_bytes));
    out.offsets = w.row_offsets;                       // per-row boundaries from wire
    std::vector<uint32_t> grammar;
    bitpack::unpack_uniform(w.packed_grammar, w.bits,
                            static_cast<size_t>(2) * w.rules, grammar);
    std::vector<uint32_t> seq(w.n_seq);
    const auto t0 = std::chrono::steady_clock::now();
    bitpack::unpack_uniform(w.packed_seq, w.bits, w.n_seq, seq);
    const size_t got = expand_sequence(grammar.data(), w.terminals,
                                       seq.data(), w.n_seq, out.bytes.data());
    if (t_decode_ns) *t_decode_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - t0).count());
    if (got != w.decoded_bytes)
        throw std::runtime_error("repair: decode size mismatch");
    return out;
}

// ── Per-string random access via grammar descent ─────────
//
// Plain Re-Pair's grammar is built over the WHOLE concatenated column, so rows
// do not align to top-level symbols. Row-granular access is nonetheless the
// standard grammar-access construction (cf. Bille et al., random access to
// grammar-compressed strings): an UNTIMED load-time index stores
//   (a) each rule's expansion length (one bottom-up O(rules) pass), and
//   (b) prefix-summed byte offsets of the top-level symbols.
// Access(i) then binary-searches the start symbol and DESCENDS into it,
// skipping `skip` bytes by comparing against subtree expansion lengths (whole
// subtrees are pruned without expansion), then expands forward until |s_i|
// bytes are produced: O(log n_seq + depth + |s_i|) per access, grammar
// resident. This is the honest per-string entry point for Re-Pair — without
// it the only option is decoding the entire column per query.
struct RaIndex {
    uint32_t terminals = 0, N = 0;
    std::vector<uint32_t> grammar;     // 2*rules (left,right)
    std::vector<uint32_t> seq;         // top-level symbols
    std::vector<uint32_t> rule_len;    // expansion length per rule
    std::vector<uint32_t> seq_prefix;  // size n_seq+1, byte offset per symbol
                                       // (32 bits, like row_offsets)
    std::vector<uint32_t> row_offsets; // size N+1 (byte offsets into decoded)
};

RaIndex build_ra_index(const LoadedWire& w) {
    RaIndex ix;
    ix.terminals = w.terminals;
    ix.N = w.N;
    ix.row_offsets = w.row_offsets;
    bitpack::unpack_uniform(w.packed_grammar, w.bits,
                            static_cast<size_t>(2) * w.rules, ix.grammar);
    bitpack::unpack_uniform(w.packed_seq, w.bits, w.n_seq, ix.seq);
    // Rule expansion lengths, bottom-up. libCSD emits rules in dependency
    // order (a rule only references terminals or earlier rules), so one
    // forward pass suffices; verified by the final prefix-sum identity.
    ix.rule_len.resize(w.rules);
    auto sym_len = [&](uint32_t s) -> uint64_t {
        return s < w.terminals ? 1u : ix.rule_len[s - w.terminals];
    };
    for (uint32_t r = 0; r < w.rules; ++r) {
        const uint64_t l = sym_len(ix.grammar[2u * r]) +
                           sym_len(ix.grammar[2u * r + 1u]);
        ix.rule_len[r] = static_cast<uint32_t>(l);
    }
    // Byte offsets fit 32 bits: the row offsets of the wire already do.
    if (w.decoded_bytes > UINT32_MAX)
        throw std::runtime_error("repair: column too large for the ra index");
    ix.seq_prefix.resize(w.n_seq + 1);
    uint64_t cum = 0;
    for (size_t i = 0; i < w.n_seq; ++i) {
        ix.seq_prefix[i] = static_cast<uint32_t>(cum);
        cum += sym_len(ix.seq[i]);
    }
    ix.seq_prefix[w.n_seq] = static_cast<uint32_t>(cum);
    if (cum != w.decoded_bytes)
        throw std::runtime_error("repair: ra index prefix-sum mismatch");
    return ix;
}

// Decode row `idx` into dst (must hold the row). Returns bytes written.
size_t ra_decode_row(const RaIndex& ix, uint32_t idx, uint8_t* dst) {
    const uint64_t b0 = ix.row_offsets[idx];
    const uint64_t b1 = ix.row_offsets[idx + 1];
    size_t want = static_cast<size_t>(b1 - b0);
    if (want == 0) return 0;
    // First top-level symbol covering b0.
    const auto it = std::upper_bound(ix.seq_prefix.begin(),
                                     ix.seq_prefix.end(), b0);
    size_t k = static_cast<size_t>(it - ix.seq_prefix.begin()) - 1;
    uint64_t skip = b0 - ix.seq_prefix[k];
    size_t written = 0;
    std::vector<uint32_t> stack;
    stack.reserve(64);
    auto sym_len = [&](uint32_t s) -> uint64_t {
        return s < ix.terminals ? 1u : ix.rule_len[s - ix.terminals];
    };
    for (; k < ix.seq.size() && written < want; ++k) {
        stack.push_back(ix.seq[k]);
        while (!stack.empty() && written < want) {
            const uint32_t sym = stack.back();
            stack.pop_back();
            if (skip > 0) {
                const uint64_t l = sym_len(sym);
                if (skip >= l) { skip -= l; continue; }  // prune whole subtree
            }
            if (sym < ix.terminals) {
                dst[written++] = static_cast<uint8_t>(sym);  // skip==0 here
            } else {
                const uint32_t r = sym - ix.terminals;
                stack.push_back(ix.grammar[2u * r + 1u]);   // right popped last
                stack.push_back(ix.grammar[2u * r]);        // left popped first
            }
        }
        stack.clear();
    }
    if (written != want)
        throw std::runtime_error("repair: ra row decode mismatch");
    return written;
}
} // namespace

OpResult RepairRunner::bulk_decode(const std::string& in_path,
                                   const std::string& out_path,
                                   const std::string& cfg_json) {
    RepairConfig cfg = parse_repair_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));
    LoadedWire w = load_wire(in_path);   // UNTIMED
    uint64_t t_decode_ns = 0;
    Column out;
    try { out = decode_all(w, &t_decode_ns); }
    catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in bulk_decode"; return r;
    }
    write_column(out_path, out);          // UNTIMED
    OpResult r;
    r.metrics.emplace_back("output_bytes", static_cast<double>(out.bytes.size()));
    r.metrics.emplace_back("num_rows",     static_cast<double>(out.num_rows()));
    r.metrics.emplace_back("t_decode_ns",  static_cast<double>(t_decode_ns));
    return r;
}

OpResult RepairRunner::random_decode(const std::string& in_path, size_t idx,
                                     const std::string& out_path,
                                     const std::string& cfg_json) {
    RepairConfig cfg = parse_repair_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));
    LoadedWire w = load_wire(in_path);
    if (idx >= w.N) {
        OpResult r; r.status = "crash";
        r.notes = "idx out of range: " + std::to_string(idx); return r;
    }
    // Per-string access via the grammar-descent index: build the
    // UNTIMED load-time index, then decode only row idx.
    try {
        const RaIndex ix = build_ra_index(w);
        const size_t len = static_cast<size_t>(
            ix.row_offsets[idx + 1] - ix.row_offsets[idx]);
        std::vector<uint8_t> buf(len);
        const size_t got = ra_decode_row(ix, static_cast<uint32_t>(idx),
                                         buf.data());
        std::ofstream f(out_path, std::ios::binary);
        if (!f) throw std::runtime_error("repair: cannot open " + out_path);
        if (got) f.write(reinterpret_cast<const char*>(buf.data()),
                         static_cast<std::streamsize>(got));
        OpResult r;
        r.metrics.emplace_back("output_bytes", static_cast<double>(got));
        return r;
    } catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in random_decode"; return r;
    }
}

OpResult RepairRunner::ra_latency(const std::string& in_path,
                                  const std::string& /*out_path*/,
                                  const std::string& cfg_json) {
    RepairConfig cfg = parse_repair_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));
    try {
        LoadedWire w = load_wire(in_path);
        if (w.N == 0)
            return OpResult{.status = "crash",
                            .notes = "repair: empty column for ra_latency"};
        uint64_t n_samples = extract_u64(cfg_json, "n_samples", 1000000);
        if (n_samples == 0) n_samples = 1;
        const uint64_t seed = extract_u64(cfg_json, "seed", 42);
        const bool warmup = extract_u64(cfg_json, "warmup", 1) != 0;

        // UNTIMED setup: the load-time random-access index.
        const RaIndex ix = build_ra_index(w);
        uint32_t max_row = 0;
        for (uint32_t i = 0; i < w.N; ++i)
            max_row = std::max(max_row, w.row_offsets[i + 1] - w.row_offsets[i]);
        std::vector<uint8_t> buf(static_cast<size_t>(max_row) + 64, 0u);

        if (warmup) {
            for (uint32_t i = 0; i < w.N; i += std::max<uint32_t>(w.N / 4096, 1))
                (void)ra_decode_row(ix, i, buf.data());
        }

        const QuerySpec qspec = parse_query_spec(cfg_json);
        const std::vector<uint64_t> indices =
            generate_query_indices(qspec, n_samples, w.N, seed);

        RaLatencyResult m;
        std::string ra_reason;
        if (!ra_measure_shipping(indices,
            [&](uint64_t idx64) -> unsigned char {
                const size_t len = ra_decode_row(
                    ix, static_cast<uint32_t>(idx64), buf.data());
                return len ? buf[len - 1] : 0;
            }, m, ra_reason)) {
            return OpResult{.status = "refused-env",
                            .notes = "repair: " + ra_reason};
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
        // Resident size of the load-time access index (not part of the
        // compressed size; reported so decode-time support can be stated).
        r.metrics.emplace_back("ra_index_bytes", static_cast<double>(
            sizeof(uint32_t) * (ix.grammar.size() + ix.seq.size()
                                + ix.rule_len.size() + ix.row_offsets.size()
                                + ix.seq_prefix.size())));
        return r;
    } catch (const std::bad_alloc&) {
        return OpResult{.status = "oom", .notes = "repair: bad_alloc ra_latency"};
    } catch (const std::exception& e) {
        return OpResult{.status = "crash", .notes = std::string("repair: ") + e.what()};
    }
}

OpResult RepairRunner::prefix_search(const std::string& in_path,
                                     const std::string& needle,
                                     const std::string& out_path,
                                     const std::string& cfg_json) {
    RepairConfig cfg = parse_repair_config(cfg_json);
    set_address_space_cap(cfg.mem_cap_gb * (1ull << 30));
    LoadedWire w = load_wire(in_path);
    const size_t N = w.N;
    auto decode = [w = std::move(w)]() mutable { return decode_all(w, nullptr); };
    size_t n_hits = 0;
    try { n_hits = decompress_then_scan(decode, needle, out_path); }
    catch (const std::bad_alloc&) {
        OpResult r; r.status = "oom"; r.notes = "bad_alloc in prefix_search"; return r;
    }
    OpResult r;
    r.metrics.emplace_back("n_hits",   static_cast<double>(n_hits));
    r.metrics.emplace_back("num_rows", static_cast<double>(N));
    return r;
}

} // namespace onpair_bench
