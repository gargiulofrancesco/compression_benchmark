add_library(lz4_runner_lib STATIC lz4_runner.cpp)
target_include_directories(lz4_runner_lib PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(lz4_runner_lib PUBLIC runner_core lz4_static)
target_compile_features(lz4_runner_lib PUBLIC cxx_std_20)

add_executable(lz4_runner main.cpp)
target_link_libraries(lz4_runner PRIVATE lz4_runner_lib)
target_compile_features(lz4_runner PRIVATE cxx_std_20)
