// RLZ empty-column refusal: N=0 → status='unsupported'.
//                         all-empty rows → status='unsupported'.

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <filesystem>
#include <iostream>
#include <string>

using namespace onpair_bench;

static const std::string CFG_R5 =
    R"({"reference_size_fraction":0.05,"block_size":16384,"seed":42,"threads":1,"mem_cap_gb":4})";

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_rlz_empty";
    fs::create_directories(tmp);

    // N=0
    {
        Column c;
        c.offsets.push_back(0);
        fs::path in_path = tmp / "n0.col";
        fs::path cz_path = tmp / "z.bin";
        write_column(in_path.string(), c);
        RlzRunner r;
        auto res = r.compress(in_path.string(), cz_path.string(), CFG_R5);
        if (res.status != "unsupported") {
            std::cerr << "[rlz_empty] N=0 expected unsupported, got "
                      << res.status << "\n";
            return 1;
        }
    }

    // All-empty rows (N > 0, total bytes = 0)
    {
        Column c;
        c.offsets = {0, 0, 0, 0, 0};   // 4 empty rows
        fs::path in_path = tmp / "allempty.col";
        fs::path cz_path = tmp / "z2.bin";
        write_column(in_path.string(), c);
        RlzRunner r;
        auto res = r.compress(in_path.string(), cz_path.string(), CFG_R5);
        if (res.status != "unsupported") {
            std::cerr << "[rlz_empty] all-empty expected unsupported, got "
                      << res.status << "\n";
            return 1;
        }
    }
    std::cout << "rlz_empty_column_test: OK\n";
    return 0;
}
