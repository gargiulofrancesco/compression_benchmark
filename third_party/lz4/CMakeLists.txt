# Vendored lz4 v1.10.0 lib/ subset → liblz4_static.a.
# Sidesteps upstream's CMake to control the flags (docs/benchmark/fairness/lz4.md).
# Single-thread fairness: LZ4 has no MT codepath in lib/ (MT lives only in
# programs/lz4.c upstream, which we never vendor). LZ4F (lz4frame.c) is linked
# but unused: the runner calls the block API only.

file(GLOB LZ4_SRC CONFIGURE_DEPENDS "lib/*.c")

add_library(lz4_static STATIC ${LZ4_SRC})

target_include_directories(lz4_static
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/lib)

target_compile_options(lz4_static PRIVATE
    $<$<COMPILE_LANGUAGE:C>:-std=c11>
    -O3 -DNDEBUG -fvisibility=hidden -fno-omit-frame-pointer
    -Wno-deprecated-declarations)

set_target_properties(lz4_static PROPERTIES
    C_VISIBILITY_PRESET hidden
    POSITION_INDEPENDENT_CODE ON)
