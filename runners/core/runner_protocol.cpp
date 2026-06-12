#include "runner_protocol.h"
#include "json_io.h"
#include "timing.h"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>

namespace onpair_bench {

Column read_column(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("read_column: cannot open " + path);
    uint32_t n = 0, total = 0;
    f.read(reinterpret_cast<char*>(&n),     sizeof(n));
    f.read(reinterpret_cast<char*>(&total), sizeof(total));
    Column c;
    c.offsets.resize(n + 1);
    f.read(reinterpret_cast<char*>(c.offsets.data()), (n + 1) * sizeof(uint32_t));
    c.bytes.resize(total);
    if (total > 0) f.read(reinterpret_cast<char*>(c.bytes.data()), total);
    if (!f) throw std::runtime_error("read_column: short read on " + path);
    return c;
}

void write_column(const std::string& path, const Column& c) {
    std::ofstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("write_column: cannot open " + path);
    uint32_t n     = static_cast<uint32_t>(c.num_rows());
    uint32_t total = static_cast<uint32_t>(c.bytes.size());
    f.write(reinterpret_cast<const char*>(&n),     sizeof(n));
    f.write(reinterpret_cast<const char*>(&total), sizeof(total));
    f.write(reinterpret_cast<const char*>(c.offsets.data()), (n + 1) * sizeof(uint32_t));
    if (total > 0) f.write(reinterpret_cast<const char*>(c.bytes.data()), total);
    if (!f) throw std::runtime_error("write_column: write failure on " + path);
}

const char* psm_str(PrefixSearchMode m) noexcept {
    switch (m) {
        case PrefixSearchMode::NativeOnCompressed:  return "NativeOnCompressed";
        case PrefixSearchMode::DecompressThenScan:  return "DecompressThenScan";
        case PrefixSearchMode::Uncompressed:        return "Uncompressed";
    }
    return "Uncompressed";
}

namespace {
std::string metrics_to_json(const std::vector<std::pair<std::string, double>>& m,
                            double t_op_ns) {
    std::ostringstream os;
    bool first = true;
    auto emit = [&](const std::string& name, double value) {
        if (!first) os << ",";
        os << metric_obj(name, value);
        first = false;
    };
    for (const auto& [k, v] : m) emit(k, v);
    emit("t_op_ns", t_op_ns);
    return os.str();
}
} // namespace

int run_protocol_loop(Runner& r, const std::string& build_flags_sha) {
    write_hello(std::cout, r.name(), r.version(), psm_str(r.prefix_search_mode()),
                build_flags_sha, "C++20");

    JsonObj env;
    while (read_envelope(std::cin, env)) {
        const std::string type = env.get("type");
        if (type == "bye") return 0;
        if (type != "op") {
            write_result(std::cout, env.get("cell_id"), env.get("op"),
                         "crash", "", "unknown envelope type: " + type);
            continue;
        }
        const std::string op          = env.get("op");
        const std::string cell_id     = env.get("cell_id");
        const std::string in_path     = env.get("input_path");
        const std::string out_path    = env.get("output_path");
        const std::string config_json = env.get("config_json", "{}");

        OpResult result;
        result.status = "crash";
        result.notes  = "uncaught: unknown op " + op;
        auto t0 = std::chrono::steady_clock::now();
        try {
            if      (op == "compress")       result = r.compress(in_path, out_path, config_json);
            else if (op == "bulk_decode")    result = r.bulk_decode(in_path, out_path, config_json);
            else if (op == "random_decode")  result = r.random_decode(in_path,
                                                                       static_cast<size_t>(env.get_int("idx", 0)),
                                                                       out_path, config_json);
            else if (op == "prefix_search")  result = r.prefix_search(in_path, env.get("needle"),
                                                                       out_path, config_json);
            else if (op == "held_out_compress") {
                // step 2 A1 ablation. Reads two input paths:
                // `train_input_path` + `heldout_input_path`. OnPair-only;
                // other runners inherit the ABC default and respond
                // status='unsupported' cleanly.
                result = r.held_out_compress(env.get("train_input_path"),
                                             env.get("heldout_input_path"),
                                             out_path, config_json);
            }
            else if (op == "ra_latency") {
                // step 6 A5 ablation. Times n_samples in-process random
                // accesses on the compressed column at `input_path`; emits
                // L_ra p50/p95/p99 (+ mean/min/max) in result.metrics. Block-
                // based baselines (zstd, lz4) + OnPair override; other runners
                // inherit the ABC default and respond status='unsupported'.
                result = r.ra_latency(in_path, out_path, config_json);
            }
            else {
                result.status = "unsupported";
                result.notes  = "unknown op: " + op;
            }
        } catch (const std::exception& e) {
            result.status = "crash";
            result.notes  = std::string("exception: ") + e.what();
        }
        auto t1 = std::chrono::steady_clock::now();
        double t_op_ns = std::chrono::duration<double, std::nano>(t1 - t0).count();

        std::string arr = metrics_to_json(result.metrics, t_op_ns);
        write_result(std::cout, cell_id, op, result.status, arr, result.notes);
    }
    return 0;
}

} // namespace onpair_bench
