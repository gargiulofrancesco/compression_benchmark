# — RLZ runner.
# Pure C++ Hoobin/Puglisi/Zobel 2011 RLZ + libdivsufsort 2.0.1 (MIT).

add_library(rlz_runner_lib STATIC
    rlz_runner.cpp
    rlz_core.cpp)

target_include_directories(rlz_runner_lib PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR})

target_link_libraries(rlz_runner_lib PUBLIC
    runner_core
    libdivsufsort_static)

target_compile_features(rlz_runner_lib PUBLIC cxx_std_20)

add_executable(rlz_runner main.cpp)
target_link_libraries(rlz_runner PRIVATE rlz_runner_lib)
target_compile_features(rlz_runner PRIVATE cxx_std_20)
