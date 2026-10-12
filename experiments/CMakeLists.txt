# Standalone diagnostics used by the experiment drivers in this directory.
# Binaries land in build/diagnostics/, where the drivers look for them.

# Timed clone of the pinned OnPair trainer with non-overlapping phase timers
# (discovery, sort, relayout, matcher rebuild); validated against
# onpair::encoding::train() on every input before it reports anything.
add_executable(onpair_finalization_phase_probe onpair_finalization_phase_probe.cpp)
target_link_libraries(onpair_finalization_phase_probe PRIVATE onpair)
target_compile_features(onpair_finalization_phase_probe PRIVATE cxx_std_20)

set_target_properties(onpair_finalization_phase_probe
    PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/diagnostics)
