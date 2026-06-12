// repair runner binary entry point. See repair_runner.{h,cpp} for the algorithm.

#include "repair_runner.h"
#include "runner_protocol.h"

#include <cstdlib>
#include <string>

int main(int /*argc*/, char** /*argv*/) {
    const char* env_sha = std::getenv("BASELINE_BUILD_FLAGS_SHA");
    std::string sha = env_sha ? env_sha : "unset";
    onpair_bench::RepairRunner r;
    return onpair_bench::run_protocol_loop(r, sha);
}
