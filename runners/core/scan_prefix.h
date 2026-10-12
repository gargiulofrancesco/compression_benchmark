#pragma once
// Canonical raw-byte prefix scanner over a flat Arrow column.
// Reference for the prefix-search check: every baseline's prefix_search
// hit-set must match this scanner's output bytewise on the same (column, needle).

#include "runner_protocol.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string_view>
#include <vector>

namespace onpair_bench {

// Append matching row indices to `hits` (in column order). Returns count appended.
size_t scan_prefix(const Column& col, std::string_view needle, std::vector<uint32_t>& hits);

// Max bytes a per-row partial decoder may overshoot `want` by in ONE decode unit,
// so the caller's scratch buffer (sized want + this) never overflows: RLZ factor
// (<=255) + literal, BPE piece (<=16), FSST symbol (<=8) + its 32-byte fast-path tail.
inline constexpr size_t SCAN_PREFIX_OVERSHOOT = 384;

// Per-string prefix scan. For each of n_rows, `decode_prefix(row, dst,
// want)` decodes AT LEAST min(want, |s_row|) bytes of row's content into `dst` (which
// the caller has sized to want + SCAN_PREFIX_OVERSHOOT), returning bytes written. A
// row is a hit iff it produced >= needle.size() bytes whose first needle.size() equal
// the needle — the SAME hit-set as scan_prefix(), but at O(min(|needle|, |s_i|)) per
// row instead of a full-row decode. Appends matching indices (column order) to hits;
// returns the count appended. The decode work is the caller's to time (T_ps).
size_t partial_prefix_scan(
    size_t n_rows, std::string_view needle,
    const std::function<size_t(size_t /*row*/, uint8_t* /*dst*/, size_t /*want*/)>& decode_prefix,
    std::vector<uint32_t>& hits);

} // namespace onpair_bench
