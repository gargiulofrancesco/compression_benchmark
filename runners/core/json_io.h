#pragma once
// Minimal NDJSON helpers for the runner ↔ orchestrator envelope.
// Bespoke and intentionally small: flat objects, string/number/bool values,
// supports the metrics array (list of {"name":..,"value":..} objects) on output only.

#include <cstdint>
#include <istream>
#include <map>
#include <ostream>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace onpair_bench {

struct JsonObj {
    std::map<std::string, std::string> kv;  // values stored as canonical strings; consumer parses

    bool has(const std::string& k) const { return kv.find(k) != kv.end(); }
    std::string get(const std::string& k, const std::string& def = "") const {
        auto it = kv.find(k);
        return it == kv.end() ? def : it->second;
    }
    long long get_int(const std::string& k, long long def = 0) const {
        auto it = kv.find(k);
        if (it == kv.end() || it->second.empty()) return def;
        return std::stoll(it->second);
    }
};

// Parse one NDJSON line of the bespoke envelope shape. Returns false on EOF / blank.
// Supports: {"k":"v","k":num,"k":true,"k":false} with \" and \\ escapes inside strings.
// Does NOT support nested objects or arrays inside the parsed input
// (we only receive flat envelopes; metrics arrays appear only on output).
bool read_envelope(std::istream& in, JsonObj& out);

// JSON string escape: only \" \\ \n \r \t — enough for our envelopes.
std::string json_escape(std::string_view s);

// Serialize a metric (name, value) pair as a JSON object literal.
std::string metric_obj(const std::string& name, double value);

// Write a complete result envelope to `out`. Caller composes `metrics_arr` as
// a comma-joined list of metric_obj() results.
void write_result(std::ostream& out,
                  const std::string& cell_id,
                  const std::string& op,
                  const std::string& status,
                  const std::string& metrics_arr,
                  const std::string& notes);

// Write the startup hello envelope.
void write_hello(std::ostream& out,
                 const std::string& name,
                 const std::string& version,
                 const std::string& prefix_search_mode,
                 const std::string& build_flags_sha,
                 const std::string& language);

} // namespace onpair_bench
