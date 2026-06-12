#include "scan_prefix.h"
#include <cstring>

namespace onpair_bench {

size_t scan_prefix(const Column& col, std::string_view needle, std::vector<uint32_t>& hits) {
    size_t before = hits.size();
    const size_t n = col.num_rows();
    for (size_t i = 0; i < n; ++i) {
        auto s = col.at(i);
        if (s.size() >= needle.size() &&
            std::memcmp(s.data(), needle.data(), needle.size()) == 0) {
            hits.push_back(static_cast<uint32_t>(i));
        }
    }
    return hits.size() - before;
}

size_t partial_prefix_scan(
    size_t n_rows, std::string_view needle,
    const std::function<size_t(size_t, uint8_t*, size_t)>& decode_prefix,
    std::vector<uint32_t>& hits) {
    const size_t before = hits.size();
    const size_t want   = needle.size();
    // Scratch reused across rows; the decoder may overshoot `want` by one unit.
    std::vector<uint8_t> buf(want + SCAN_PREFIX_OVERSHOOT);
    for (size_t i = 0; i < n_rows; ++i) {
        const size_t got = decode_prefix(i, buf.data(), want);
        if (got >= want && std::memcmp(buf.data(), needle.data(), want) == 0)
            hits.push_back(static_cast<uint32_t>(i));
    }
    return hits.size() - before;
}

} // namespace onpair_bench
