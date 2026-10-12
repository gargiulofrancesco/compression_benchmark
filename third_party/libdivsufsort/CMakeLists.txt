# Thin CMake shim for libdivsufsort 2.0.1, used by the RLZ baseline.
#
# Upstream CMakeLists.txt is preserved as CMakeLists.txt.upstream (uses
# cmake_minimum_required(VERSION 2.4) which modern CMake (3.27+/4.0+) rejects).
# Rather than retrofit upstream, we compile the 4 library .c files directly
# and pre-bake the config.h + divsufsort.h headers under onpair_bench_shim/
# (those are the only outputs of upstream's configure-step that the .c files
# depend on).
#
# 32-bit suffix-array variant only (BUILD_DIVSUFSORT64=OFF): the RLZ runner
# caps the reference at 2 GiB, far above the 5% references of the paper's
# columns (at most 16 MiB).

set(LIBDIVSUFSORT_SRCS
    lib/divsufsort.c
    lib/sssort.c
    lib/trsort.c
    lib/utils.c)

add_library(libdivsufsort_static STATIC ${LIBDIVSUFSORT_SRCS})

target_include_directories(libdivsufsort_static
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/onpair_bench_shim   # pre-baked config.h + divsufsort.h
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/include             # divsufsort_private.h
)

target_compile_definitions(libdivsufsort_static PRIVATE HAVE_CONFIG_H=1)
target_compile_options(libdivsufsort_static PRIVATE -w)
set_target_properties(libdivsufsort_static PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    C_STANDARD 99)
