# OnPair runner — (last; closes ).
#
# Consumes the OnPair library via the FetchContent target `onpair`
# (declared by external/onpair_cpp.cmake; aliased OnPair::onpair upstream).
#
# Scope-fence:
# `runners/onpair/onpair_runner.cpp` includes only the OnPair-library headers
# explicitly enumerated at the top of the TU. Forbidden headers
# (kmp_automaton.h, aho_corasick_automaton.h, eq_automaton.h, eq_search.h)
# + forbidden ops (contains/equals) + threading APIs are policed by
# tools/lint/onpair_scope_audit.sh which runs at `make lint` / `make smoke`.

add_library(onpair_runner_lib STATIC onpair_runner.cpp)
target_include_directories(onpair_runner_lib PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(onpair_runner_lib PUBLIC
    runner_core
    onpair   # FetchContent INTERFACE/STATIC target from external/onpair_cpp.cmake
)
target_compile_features(onpair_runner_lib PUBLIC cxx_std_20)

add_executable(onpair_runner main.cpp)
target_link_libraries(onpair_runner PRIVATE onpair_runner_lib)
target_compile_features(onpair_runner PRIVATE cxx_std_20)
