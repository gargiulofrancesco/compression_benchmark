# Vendored cwida/fsst master HEAD SHA e638d4cf8c26129d73c242a4127b42b975de5b63
# → libfsst_static.a. Sidesteps upstream's CMake to control flags per
# Phase 2 FSST fairness card.
#
# Single-thread fairness: fsst_duplicate is never linked into the measurement
# path (Q-F* lock). FSST has no MT-gating macro; we simply never call duplicate
# from runners/fsst/.
#
# fsst12 variant NOT vendored (Q-F5). Bundled CLI (upstream fsst.cpp w/ int main)
# NOT vendored. fsst_avx512.cpp IS vendored because libfsst.cpp references its
# symbols unconditionally; the AVX-512 intrinsics inside it are gated by
# `__AVX512F__` and fall back to a scalar no-op on non-x86_64 / non-AVX-512 hosts.
#

add_library(fsst_static STATIC
    libfsst.cpp
    fsst_avx512.cpp)

target_include_directories(fsst_static
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

# FSST upstream targets C++17. Project default is C++20; scope C++17 to the
# FSST TUs so upstream's idioms compile cleanly without forcing the project down.
target_compile_features(fsst_static PRIVATE cxx_std_17)
target_compile_options(fsst_static PRIVATE
    -O3 -DNDEBUG -fvisibility=hidden -fno-omit-frame-pointer
    -Wno-deprecated-declarations -Wno-unused-but-set-variable
    -Wno-sign-compare -Wno-unused-parameter)

set_target_properties(fsst_static PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    POSITION_INDEPENDENT_CODE ON)
