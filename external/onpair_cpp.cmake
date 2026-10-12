# OnPair library — FetchContent pinned to a commit SHA.
#
# The library under measurement is consumed via FetchContent (not vendored
# under third_party/), so it is fetched and built exactly the way any
# downstream user would consume it.
# License: MIT (verified against upstream LICENSE at SHA f8ecc64f).

include(FetchContent)

set(ONPAIR_CPP_GIT_REPOSITORY "https://github.com/gargiulofrancesco/onpair_cpp.git"
    CACHE STRING "OnPair upstream git URL")
set(ONPAIR_CPP_GIT_TAG "f8ecc64fa7786cfa6fc46d46564135ee99a8ba49"
    CACHE STRING "OnPair upstream commit SHA")

# OnPair library defaults to embedded mode automatically (ONPAIR_BUILD_TESTS=OFF,
# ONPAIR_BUILD_EXAMPLES=OFF, ONPAIR_INSTALL=OFF when PROJECT_IS_TOP_LEVEL is false).
# Boost.Unordered >= 1.81 is required transitively; upstream FetchContent fallback
# handles missing system Boost (see onpair_cpp/CMakeLists.txt).
#
# Note: Debian's libboost-all-dev 1.83
# ships NO `boost_unordered` CMake component config (only boost_headers), yet
# `find_package(Boost 1.81 CONFIG COMPONENTS unordered)` still reports Boost_FOUND,
# so upstream's "missing/older system Boost" fallback never fires and the
# `Boost::unordered` IMPORTED target is never created → onpair link fails at
# generate time. Force the upstream FetchContent fallback (Boost 1.91)
# by hiding the broken system Boost config from THIS
# fetch only. Override with -DONPAIR_FORCE_FETCH_BOOST=OFF if a future system
# Boost exports the unordered component. Touches neither upstream source nor the
# pinned SHA.

option(ONPAIR_FORCE_FETCH_BOOST
    "Hide system Boost so onpair_cpp fetches Boost.Unordered via FetchContent" ON)

# Local shims (repo-local, NOT upstream): carried as external/onpair_local_shims.patch
# and applied post-clone via PATCH_COMMAND so they survive `make clean` / build/
# wipes. The pinned SHA is left unchanged.
# Idempotent: a tree that already carries exactly this patch is left untouched
# (so nothing is rebuilt); any other state is reset to the pinned commit
# before the patch is applied. Contents:
# 1. TrainResult.sample_bytes_consumed — the pinned SHA f8ecc64f
#      predates this field that A1 reads.
#   2. Dictionary::{token_content_bytes,offset_bytes} + DictionaryView mirrors
# — split the lumped bytes_used so a consumer can choose what
#      to count (dictionary content vs the dictionary's own offset array). Lets
#      the runner report separated space sub-counters for payload-only accounting.
#   3. TrainingConfig::sort_lexicographically (default true) — diagnostic-only
#      switch used to preserve pre-sort construction order without changing the
#      production/default lexicographic layout.
set(_onpair_shim_patch "${CMAKE_CURRENT_LIST_DIR}/onpair_local_shims.patch")

FetchContent_Declare(
    onpair_cpp
    GIT_REPOSITORY ${ONPAIR_CPP_GIT_REPOSITORY}
    GIT_TAG        ${ONPAIR_CPP_GIT_TAG}
    GIT_SHALLOW    FALSE   # full clone so the SHA tag resolves cleanly
    PATCH_COMMAND  sh -c "git apply --reverse --check '${_onpair_shim_patch}' 2>/dev/null || (git checkout -q -- . && git apply '${_onpair_shim_patch}')"
)

if(ONPAIR_FORCE_FETCH_BOOST)
    set(_onpair_saved_disable_boost "${CMAKE_DISABLE_FIND_PACKAGE_Boost}")
    set(CMAKE_DISABLE_FIND_PACKAGE_Boost ON)
endif()

FetchContent_MakeAvailable(onpair_cpp)

if(ONPAIR_FORCE_FETCH_BOOST)
    set(CMAKE_DISABLE_FIND_PACKAGE_Boost "${_onpair_saved_disable_boost}")
endif()
