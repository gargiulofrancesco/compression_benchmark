#include "json_io.h"
#include <cctype>
#include <cmath>
#include <cstdio>
#include <iomanip>
#include <limits>

namespace onpair_bench {

namespace {
void skip_ws(const std::string& s, size_t& i) {
    while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i]))) ++i;
}

bool parse_string(const std::string& s, size_t& i, std::string& out) {
    if (i >= s.size() || s[i] != '"') return false;
    ++i;
    out.clear();
    while (i < s.size() && s[i] != '"') {
        if (s[i] == '\\' && i + 1 < s.size()) {
            char c = s[i + 1];
            switch (c) {
                case '"':  out.push_back('"');  break;
                case '\\': out.push_back('\\'); break;
                case 'n':  out.push_back('\n'); break;
                case 'r':  out.push_back('\r'); break;
                case 't':  out.push_back('\t'); break;
                case '/':  out.push_back('/');  break;
                default:   out.push_back(c);    break;
            }
            i += 2;
        } else {
            out.push_back(s[i]);
            ++i;
        }
    }
    if (i >= s.size()) return false;
    ++i;  // consume closing quote
    return true;
}

bool parse_value_to_string(const std::string& s, size_t& i, std::string& out) {
    skip_ws(s, i);
    if (i >= s.size()) return false;
    if (s[i] == '"') return parse_string(s, i, out);
    if (s[i] == 't' && s.compare(i, 4, "true") == 0)  { out = "true";  i += 4; return true; }
    if (s[i] == 'f' && s.compare(i, 5, "false") == 0) { out = "false"; i += 5; return true; }
    if (s[i] == 'n' && s.compare(i, 4, "null") == 0)  { out = "";      i += 4; return true; }
    // number
    size_t start = i;
    if (s[i] == '-' || s[i] == '+') ++i;
    while (i < s.size() && (std::isdigit(static_cast<unsigned char>(s[i])) ||
                            s[i] == '.' || s[i] == 'e' || s[i] == 'E' ||
                            s[i] == '+' || s[i] == '-')) ++i;
    if (start == i) return false;
    out = s.substr(start, i - start);
    return true;
}
} // namespace

bool read_envelope(std::istream& in, JsonObj& out) {
    std::string line;
    while (std::getline(in, line)) {
        // strip CR + leading WS
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
        if (line.empty()) continue;
        out.kv.clear();
        size_t i = 0;
        skip_ws(line, i);
        if (i >= line.size() || line[i] != '{') return false;
        ++i;
        while (true) {
            skip_ws(line, i);
            if (i < line.size() && line[i] == '}') { ++i; break; }
            std::string k;
            if (!parse_string(line, i, k)) return false;
            skip_ws(line, i);
            if (i >= line.size() || line[i] != ':') return false;
            ++i;
            std::string v;
            if (!parse_value_to_string(line, i, v)) return false;
            out.kv[k] = v;
            skip_ws(line, i);
            if (i < line.size() && line[i] == ',') { ++i; continue; }
        }
        return true;
    }
    return false;
}

std::string json_escape(std::string_view s) {
    std::string r;
    r.reserve(s.size() + 4);
    for (char c : s) {
        switch (c) {
            case '"':  r += "\\\""; break;
            case '\\': r += "\\\\"; break;
            case '\n': r += "\\n";  break;
            case '\r': r += "\\r";  break;
            case '\t': r += "\\t";  break;
            default:   r += c;      break;
        }
    }
    return r;
}

std::string metric_obj(const std::string& name, double value) {
    // The default ostream double format prints 6 significant digits; at metric
    // magnitudes ≥ 10^6 that silently rounds (e.g. 1,223,329 → "1.22333e+06" →
    // 1,223,330 on parse). Many of our sub-counters (tokens_bytes,
    // output_bytes, input_bytes on real columns) are above that threshold.
    // We format integer-valued doubles as integers (the common case for
    // *_bytes / num_rows / t_op_ns); otherwise use max_digits10 round-trip
    // precision so the parsed double is bit-identical to the C++ double.
    char buf[64];
    if (std::isfinite(value) && value == std::floor(value)
        && std::abs(value) < 1e18) {
        std::snprintf(buf, sizeof(buf), "%lld",
                      static_cast<long long>(value));
    } else {
        std::snprintf(buf, sizeof(buf), "%.*g",
                      std::numeric_limits<double>::max_digits10, value);
    }
    std::ostringstream os;
    os << "{\"name\":\"" << json_escape(name) << "\",\"value\":" << buf << "}";
    return os.str();
}

void write_result(std::ostream& out,
                  const std::string& cell_id,
                  const std::string& op,
                  const std::string& status,
                  const std::string& metrics_arr,
                  const std::string& notes) {
    out << "{\"type\":\"result\""
        << ",\"cell_id\":\"" << json_escape(cell_id) << "\""
        << ",\"op\":\""      << json_escape(op)      << "\""
        << ",\"status\":\""  << json_escape(status)  << "\""
        << ",\"metrics\":["  << metrics_arr          << "]"
        << ",\"notes\":\""   << json_escape(notes)   << "\"}\n";
    out.flush();
}

void write_hello(std::ostream& out,
                 const std::string& name,
                 const std::string& version,
                 const std::string& prefix_search_mode,
                 const std::string& build_flags_sha,
                 const std::string& language) {
    out << "{\"type\":\"hello\""
        << ",\"name\":\""               << json_escape(name)               << "\""
        << ",\"version\":\""            << json_escape(version)            << "\""
        << ",\"prefix_search_mode\":\"" << json_escape(prefix_search_mode) << "\""
        << ",\"baseline_build_flags_sha\":\"" << json_escape(build_flags_sha) << "\""
        << ",\"language\":\""           << json_escape(language)           << "\"}\n";
    out.flush();
}

} // namespace onpair_bench
