// OnPair empty-column refusal: N=0 or all-empty rows → status='unsupported'.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <filesystem>
#include <iostream>
#include <string>

using namespace onpair_bench;

static const std::string CFG =
    R"({"bits":16,"sample_fraction":0.15,"seed":42,"threads":1,"mem_cap_gb":4})";

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_empty";
    fs::create_directories(tmp);

    // N=0
    {
        Column c;
        c.offsets.push_back(0);
        fs::path in_path = tmp / "n0.col";
        fs::path cz_path = tmp / "z0.bin";
        write_column(in_path.string(), c);
        OnPairRunner r;
        auto res = r.compress(in_path.string(), cz_path.string(), CFG);
        if (res.status != "unsupported") {
            std::cerr << "N=0: expected status='unsupported', got '" << res.status << "'\n";
            return 1;
        }
    }
    // all-empty rows (N=10, input_bytes=0)
    {
        Column c;
        c.offsets.push_back(0);
        for (int i = 0; i < 10; ++i) c.offsets.push_back(0);
        fs::path in_path = tmp / "ae.col";
        fs::path cz_path = tmp / "z_ae.bin";
        write_column(in_path.string(), c);
        OnPairRunner r;
        auto res = r.compress(in_path.string(), cz_path.string(), CFG);
        if (res.status != "unsupported") {
            std::cerr << "all-empty: expected status='unsupported', got '" << res.status << "'\n";
            return 2;
        }
    }
    fs::remove_all(tmp);
    std::cout << "onpair_empty_column: OK\n";
    return 0;
}
