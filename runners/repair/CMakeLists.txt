add_library(repair_runner_lib STATIC repair_runner.cpp)
target_include_directories(repair_runner_lib PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(repair_runner_lib PUBLIC runner_core libcsd_static)
target_compile_features(repair_runner_lib PUBLIC cxx_std_20)
# Reuse the RPDAC build-portability shim: RePair/RePair.h pulls in libCSD headers
# that trip C++20 (bitset/ulong ambiguity, `register` storage class).
target_compile_options(repair_runner_lib PRIVATE
    -include ${CMAKE_SOURCE_DIR}/third_party/libcsd_compat.h
    -Wno-deprecated-declarations
    -Wno-deprecated
    -Wno-unused-parameter
    -Wno-register
)

add_executable(repair_runner main.cpp)
target_link_libraries(repair_runner PRIVATE repair_runner_lib)
target_compile_features(repair_runner PRIVATE cxx_std_20)
