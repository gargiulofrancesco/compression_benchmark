cmake_minimum_required(VERSION 3.20)
project(onpair_bench C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# -march=native adapts the build to the host CPU. No ISA extension is
# required: FSST's AVX-512 encoder path is compile-time gated (__AVX512F__)
# and falls back to a scalar stub on machines without it; whether the
# AVX-512 path is present is recorded in the environment fingerprint
# (orchestrator/env_probe.py) so runs on different hosts stay comparable.
# CANONICAL FLAG LIST — MUST stay in lockstep with orchestrator/env_probe.py:DEV_BUILD_FLAGS.
add_compile_options(
    -O3
    -DNDEBUG
    -march=native
    -fvisibility=hidden
    -fno-omit-frame-pointer
)

add_subdirectory(third_party/zstd)
add_subdirectory(third_party/lz4)
add_subdirectory(third_party/fsst)
add_subdirectory(third_party/libcsd)   # KEPT: `repair` uses libCSD's Re-Pair engine
                                       # (dict-baseline RUNNERS rpdac/pfc/rpfc removed)
# (BigRePair was evaluated and dropped: an out-of-core Re-Pair variant not
# exercised at this benchmark's data sizes; libCSD covers in-memory Re-Pair.)

# SentencePiece v0.2.2 (BPE). Options set before add_subdirectory; upstream
# CMakeLists.txt invoked as-is. Protobuf-lite is the copy bundled in the
# vendored tree. Abseil is not bundled: SentencePiece's own CMake fetches the
# pinned abseil-cpp release into third_party/sentencepiece/third_party/ at
# configure time (SPM_ABSL_PROVIDER="module"); both paths are git-ignored.
set(SPM_ENABLE_SHARED            OFF        CACHE BOOL   "" FORCE)
set(SPM_BUILD_TEST               OFF        CACHE BOOL   "" FORCE)
set(SPM_ENABLE_TCMALLOC          OFF        CACHE BOOL   "" FORCE)
set(SPM_PROTOBUF_PROVIDER        "internal" CACHE STRING "" FORCE)
set(SPM_ABSL_PROVIDER            "module"   CACHE STRING "" FORCE)
set(CMAKE_POLICY_VERSION_MINIMUM 3.5        CACHE STRING "" FORCE)
add_subdirectory(third_party/sentencepiece EXCLUDE_FROM_ALL)

# libdivsufsort 2.0.1 (RLZ; MIT). Onpair-bench thin shim
# bypasses upstream's cmake_minimum_required(VERSION 2.4).
# It is the sole vendored dep for RLZ (skuruppu/RLZ + libcds are not used;
# pure C++ reimpl at runners/rlz/rlz_core.{h,cpp}). See
# third_party/libdivsufsort/PROVENANCE.txt for full rationale.
add_subdirectory(third_party/libdivsufsort)

# OnPair. Consumed via FetchContent + commit SHA
# (NOT vendored under third_party/; the library is the
# artefact under measurement). See external/onpair_cpp.cmake
# for the SHA pin and license.
include(${CMAKE_SOURCE_DIR}/external/onpair_cpp.cmake)

add_subdirectory(runners/core)
add_subdirectory(runners/uncompressed)
add_subdirectory(runners/zstd)
add_subdirectory(runners/lz4)
add_subdirectory(runners/fsst)
# runners/libcsd_rpdac + libcsd_pfc_htfc removed (dict baselines dropped).
add_subdirectory(runners/repair)
add_subdirectory(runners/bpe)
add_subdirectory(runners/rlz)
add_subdirectory(runners/onpair)
add_subdirectory(experiments)

enable_testing()
add_subdirectory(tests)
