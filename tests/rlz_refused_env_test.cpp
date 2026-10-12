// RLZ refused-env test.
// When column_size * R% < block_size (16 KB), K=0 and sampling can't
// produce a block. Runner must emit status='refused-env' cleanly with
// non-empty notes; no crash, no SIGKILL.
//
// We construct a tiny column (5000 bytes total) and request R%=1% →
// 5000 * 0.01 = 50 < 16384 → K=0.

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <filesystem>
#include <iostream>
#include <string>

using namespace onpair_bench;

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rlz_refused";
    fs::create_directories(tmp);
    fs::path in_path = tmp / "tiny.col";
    fs::path cz_path = tmp / "z.bin";

    // 100 rows × 50 bytes/row = 5000 bytes.
    Column c;
    c.offsets.push_back(0);
    for (uint32_t i = 0; i < 100; ++i) {
        std::string s = "row_" + std::to_string(i) + "_payload_xxxxx_yyyyy_zzzzz";
        for (char ch : s) c.bytes.push_back(static_cast<uint8_t>(ch));
        c.offsets.push_back(static_cast<uint32_t>(c.bytes.size()));
    }
    write_column(in_path.string(), c);

    const std::string cfg =
        R"({"reference_size_fraction":0.01,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":4})";
    RlzRunner r;
    auto res = r.compress(in_path.string(), cz_path.string(), cfg);
    if (res.status != "refused-env") {
        std::cerr << "[rlz_refused_env] expected status='refused-env', got "
                  << res.status << " notes=" << res.notes << "\n";
        return 1;
    }
    if (res.notes.find("reference fraction too small") == std::string::npos) {
        std::cerr << "[rlz_refused_env] notes missing expected substring; got "
                  << res.notes << "\n";
        return 2;
    }
    // Metrics must be empty for refused-style statuses.
    if (!res.metrics.empty()) {
        std::cerr << "[rlz_refused_env] expected empty metrics; got "
                  << res.metrics.size() << " entries\n";
        return 3;
    }
    std::cout << "rlz_refused_env_test: OK (status='refused-env'; "
              << "notes=\"" << res.notes << "\")\n";
    fs::remove_all(tmp);
    return 0;
}
