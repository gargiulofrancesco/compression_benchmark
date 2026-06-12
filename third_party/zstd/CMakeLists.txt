# Vendored zstd v1.5.6 lib/ subset → libzstd_static.a.
# Sidesteps upstream's CMake to control flags per Phase 2 zstd fairness card.
# Single-thread (ZSTD_MULTITHREAD=0); no legacy decoders (ZSTD_LEGACY_SUPPORT=0).

# Trained-dictionary builder (lib/dictBuilder + lib/zdict.h) REMOVED 2026-06-08:
# zstd runs in its as-deployed streaming configuration only (no ZDICT / trained-
# dict cell), so the dictionary builder was dead code. Removed from the repo to
# eliminate the capability from the build entirely.
file(GLOB ZSTD_SRC CONFIGURE_DEPENDS
    "lib/common/*.c"
    "lib/compress/*.c"
    "lib/decompress/*.c")

# Multithreaded compress TU never linked (single-thread fairness lock).
list(FILTER ZSTD_SRC EXCLUDE REGEX "zstdmt_compress\\.c$")

# x86_64 huf decompress asm: only assemble on x86_64; everything else disables it.
if(CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64")
    list(APPEND ZSTD_SRC "${CMAKE_CURRENT_SOURCE_DIR}/lib/decompress/huf_decompress_amd64.S")
    enable_language(ASM)
    set(ZSTD_ASM_DEFINE "")
else()
    set(ZSTD_ASM_DEFINE "HUF_DISABLE_ASM=1")
endif()

add_library(zstd_static STATIC ${ZSTD_SRC})

target_include_directories(zstd_static
    PUBLIC  ${CMAKE_CURRENT_SOURCE_DIR}/lib
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/lib/common
            ${CMAKE_CURRENT_SOURCE_DIR}/lib/compress
            ${CMAKE_CURRENT_SOURCE_DIR}/lib/decompress)

target_compile_options(zstd_static PRIVATE
    $<$<COMPILE_LANGUAGE:C>:-std=c11>
    -O3 -DNDEBUG -fvisibility=hidden -fno-omit-frame-pointer
    -Wno-deprecated-declarations)

# NOTE: zstd's source uses `#ifdef ZSTD_MULTITHREAD`, so defining it AT ALL
# (even to 0) enables the MT codepath. To disable MT we must leave it undefined
# AND exclude zstdmt_compress.c from sources (done above).
target_compile_definitions(zstd_static PRIVATE
    ZSTD_LEGACY_SUPPORT=0
    ${ZSTD_ASM_DEFINE})

set_target_properties(zstd_static PROPERTIES
    C_VISIBILITY_PRESET hidden
    POSITION_INDEPENDENT_CODE ON)
