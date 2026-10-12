// BPE empty-column refusal. Asserts:
//   - N=0       → status='unsupported'
//   - all rows empty (N>0, input_bytes=0) → status='unsupported'

#include "bpe_runner.h"
#include "runner_protocol.h"

#include <filesystem>
#include <iostream>

using namespace onpair_bench;

int main() {
    namespace fs = std::filesystem;
    fs::path tmp = fs::temp_directory_path() / "onpair_bpe_empty";
    fs::create_directories(tmp);

    const std::string cfg = R"({"vocab_size":4096,"byte_fallback":true,"normalization":"identity","threads":1,"mem_cap_gb":4})";
    BpeRunner r;

    // Case 1: N=0.
    {
        Column c;
        c.offsets.push_back(0);
        fs::path in_path = tmp / "in0.col";
        fs::path cz_path = tmp / "z0.bin";
        write_column(in_path.string(), c);
        auto res = r.compress(in_path.string(), cz_path.string(), cfg);
        if (res.status != "unsupported") {
            std::cerr << "[bpe_empty] N=0 expected 'unsupported' got '" << res.status << "'\n";
            return 1;
        }
    }

    // Case 2: N>0 but all rows empty.
    {
        Column c;
        c.offsets = {0, 0, 0, 0};
        fs::path in_path = tmp / "in_all_empty.col";
        fs::path cz_path = tmp / "z_all_empty.bin";
        write_column(in_path.string(), c);
        auto res = r.compress(in_path.string(), cz_path.string(), cfg);
        if (res.status != "unsupported") {
            std::cerr << "[bpe_empty] all-empty expected 'unsupported' got '" << res.status << "'\n";
            return 1;
        }
    }

    fs::remove_all(tmp);
    return 0;
}
