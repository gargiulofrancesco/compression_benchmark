#include "decompress_then_scan.h"
#include "scan_prefix.h"

#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <vector>

namespace onpair_bench {

size_t decompress_then_scan(const DecodeAllFn& decode,
                            std::string_view needle,
                            const std::string& out_path) {
    Column col = decode();
    std::vector<uint32_t> hits;
    size_t n_hits = scan_prefix(col, needle, hits);

    std::ofstream f(out_path, std::ios::binary);
    if (!f) throw std::runtime_error("decompress_then_scan: cannot open " + out_path);
    uint32_t nh = static_cast<uint32_t>(n_hits);
    f.write(reinterpret_cast<const char*>(&nh), sizeof(nh));
    if (!hits.empty()) {
        f.write(reinterpret_cast<const char*>(hits.data()),
                static_cast<std::streamsize>(hits.size() * sizeof(uint32_t)));
    }
    return n_hits;
}

} // namespace onpair_bench
