// lz4_runner binary entrypoint — symmetric with zstd / uncompressed runners.

#include "lz4_runner.h"
#include "runner_protocol.h"

#include <cstdlib>
#include <string>

int main() {
    const char* env_sha = std::getenv("BASELINE_BUILD_FLAGS_SHA");
    std::string sha = env_sha ? env_sha : "unset";
    onpair_bench::Lz4Runner r;
    return onpair_bench::run_protocol_loop(r, sha);
}
