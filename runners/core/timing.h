#pragma once
#include <chrono>

namespace onpair_bench {

// Scoped steady_clock guard. Writes elapsed ns to *out at destruction.
class Timer {
public:
    explicit Timer(double* out) noexcept : out_(out), t0_(std::chrono::steady_clock::now()) {}
    ~Timer() {
        auto t1 = std::chrono::steady_clock::now();
        if (out_) *out_ = std::chrono::duration<double, std::nano>(t1 - t0_).count();
    }
    Timer(const Timer&) = delete;
    Timer& operator=(const Timer&) = delete;
private:
    double* out_;
    std::chrono::steady_clock::time_point t0_;
};

} // namespace onpair_bench
