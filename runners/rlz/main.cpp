// rlz runner binary entry point.
// See rlz_runner.{h,cpp} + rlz_core.{h,cpp} for the pure-C++ Hoobin/
// Puglisi/Zobel 2011 RLZ algorithm against libdivsufsort 2.0.1.

#include "rlz_runner.h"
#include "runner_protocol.h"

#include <cstdlib>
#include <string>

int main(int /*argc*/, char** /*argv*/) {
    const char* env_sha = std::getenv("BASELINE_BUILD_FLAGS_SHA");
    std::string sha = env_sha ? env_sha : "unset";
    onpair_bench::RlzRunner r;
    return onpair_bench::run_protocol_loop(r, sha);
}
