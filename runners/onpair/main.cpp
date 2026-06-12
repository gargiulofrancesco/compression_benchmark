// OnPair runner binary entry point.
// (last; closes ). Consumes onpair_cpp via
// FetchContent + commit SHA per audit-trail negative.
// See onpair_runner.{h,cpp} and external/onpair_cpp.cmake
// + docs/benchmark/fairness/onpair.md for the binding decisions.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <cstdlib>
#include <string>

int main(int /*argc*/, char** /*argv*/) {
    const char* env_sha = std::getenv("BASELINE_BUILD_FLAGS_SHA");
    std::string sha = env_sha ? env_sha : "unset";
    onpair_bench::OnPairRunner r;
    return onpair_bench::run_protocol_loop(r, sha);
}
