#pragma once
// Shared helper for the `DecompressThenScan` baselines that decode the whole
// column to search it (zstd, lz4, repair).
// The runner provides a decode functor; this helper calls it inside the runner's
// timing region, then runs the canonical `scan_prefix` over the produced column.
// Decode and scan time are both billed to `prefix_search`: decode is inside
// the timed region, and no decoded state is cached across queries.

#include "runner_protocol.h"

#include <cstddef>
#include <functional>
#include <string>
#include <string_view>

namespace onpair_bench {

using DecodeAllFn = std::function<Column()>;

// Invokes `decode()`, then `scan_prefix(col, needle, hits)`, then writes
// `[n_hits u32][hits[n_hits] u32]` to `out_path`. Returns hit count.
size_t decompress_then_scan(const DecodeAllFn& decode,
                            std::string_view needle,
                            const std::string& out_path);

} // namespace onpair_bench
