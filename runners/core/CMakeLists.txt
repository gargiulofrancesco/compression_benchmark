add_library(runner_core STATIC
    runner_protocol.cpp
    json_io.cpp
    scan_prefix.cpp
    decompress_then_scan.cpp
    bitpack.cpp
    bitpack_mixed.cpp
)
target_include_directories(runner_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_features(runner_core PUBLIC cxx_std_20)
