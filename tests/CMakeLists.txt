enable_testing()

add_executable(stub_roundtrip_test stub_roundtrip_test.cpp)
target_link_libraries(stub_roundtrip_test PRIVATE runner_core)
add_test(NAME stub_roundtrip COMMAND stub_roundtrip_test)

add_executable(scan_prefix_test scan_prefix_test.cpp)
target_link_libraries(scan_prefix_test PRIVATE runner_core)
add_test(NAME scan_prefix COMMAND scan_prefix_test)

add_executable(decompress_then_scan_test decompress_then_scan_test.cpp)
target_link_libraries(decompress_then_scan_test PRIVATE runner_core)
add_test(NAME decompress_then_scan COMMAND decompress_then_scan_test)

add_executable(zstd_roundtrip_test zstd_roundtrip_test.cpp)
target_link_libraries(zstd_roundtrip_test PRIVATE zstd_runner_lib)
add_test(NAME zstd_roundtrip COMMAND zstd_roundtrip_test)

add_executable(zstd_random_decode_test zstd_random_decode_test.cpp)
target_link_libraries(zstd_random_decode_test PRIVATE zstd_runner_lib)
add_test(NAME zstd_random_decode COMMAND zstd_random_decode_test)

add_executable(zstd_prefix_search_test zstd_prefix_search_test.cpp)
target_link_libraries(zstd_prefix_search_test PRIVATE zstd_runner_lib)
add_test(NAME zstd_prefix_search COMMAND zstd_prefix_search_test)

add_executable(lz4_roundtrip_test lz4_roundtrip_test.cpp)
target_link_libraries(lz4_roundtrip_test PRIVATE lz4_runner_lib)
add_test(NAME lz4_roundtrip COMMAND lz4_roundtrip_test)

add_executable(lz4_random_decode_test lz4_random_decode_test.cpp)
target_link_libraries(lz4_random_decode_test PRIVATE lz4_runner_lib)
add_test(NAME lz4_random_decode COMMAND lz4_random_decode_test)

add_executable(lz4_prefix_search_test lz4_prefix_search_test.cpp)
target_link_libraries(lz4_prefix_search_test PRIVATE lz4_runner_lib)
add_test(NAME lz4_prefix_search COMMAND lz4_prefix_search_test)

add_executable(lz4_cross_tier_decode_test lz4_cross_tier_decode_test.cpp)
target_link_libraries(lz4_cross_tier_decode_test PRIVATE lz4_runner_lib)
add_test(NAME lz4_cross_tier_decode COMMAND lz4_cross_tier_decode_test)

add_executable(fsst_roundtrip_test fsst_roundtrip_test.cpp)
target_link_libraries(fsst_roundtrip_test PRIVATE fsst_runner_lib)
add_test(NAME fsst_roundtrip COMMAND fsst_roundtrip_test)

add_executable(fsst_random_decode_test fsst_random_decode_test.cpp)
target_link_libraries(fsst_random_decode_test PRIVATE fsst_runner_lib)
add_test(NAME fsst_random_decode COMMAND fsst_random_decode_test)

add_executable(fsst_prefix_search_test fsst_prefix_search_test.cpp)
target_link_libraries(fsst_prefix_search_test PRIVATE fsst_runner_lib)
add_test(NAME fsst_prefix_search COMMAND fsst_prefix_search_test)

add_executable(fsst_decoder_symmetry_test fsst_decoder_symmetry_test.cpp)
target_link_libraries(fsst_decoder_symmetry_test PRIVATE fsst_runner_lib)
add_test(NAME fsst_decoder_symmetry COMMAND fsst_decoder_symmetry_test)

# rpdac_* and pfc_htfc_* tests removed (dict baselines dropped).

add_executable(repair_roundtrip_test repair_roundtrip_test.cpp)
target_link_libraries(repair_roundtrip_test PRIVATE repair_runner_lib)
add_test(NAME repair_roundtrip COMMAND repair_roundtrip_test)

# bigrepair tests removed (BigRePair baseline dropped).

# BPE — 8 new tests.
add_executable(bitpack_unit_test bitpack_unit_test.cpp)
target_link_libraries(bitpack_unit_test PRIVATE runner_core)
add_test(NAME bitpack_unit COMMAND bitpack_unit_test)

add_executable(bpe_roundtrip_test bpe_roundtrip_test.cpp)
target_link_libraries(bpe_roundtrip_test PRIVATE bpe_runner_lib)
add_test(NAME bpe_roundtrip COMMAND bpe_roundtrip_test)

add_executable(bpe_random_decode_test bpe_random_decode_test.cpp)
target_link_libraries(bpe_random_decode_test PRIVATE bpe_runner_lib)
add_test(NAME bpe_random_decode COMMAND bpe_random_decode_test)

add_executable(bpe_prefix_search_test bpe_prefix_search_test.cpp)
target_link_libraries(bpe_prefix_search_test PRIVATE bpe_runner_lib)
add_test(NAME bpe_prefix_search COMMAND bpe_prefix_search_test)

add_executable(bpe_load_save_identity_test bpe_load_save_identity_test.cpp)
target_link_libraries(bpe_load_save_identity_test PRIVATE bpe_runner_lib)
add_test(NAME bpe_load_save_identity COMMAND bpe_load_save_identity_test)

add_executable(bpe_empty_column_test bpe_empty_column_test.cpp)
target_link_libraries(bpe_empty_column_test PRIVATE bpe_runner_lib)
add_test(NAME bpe_empty_column COMMAND bpe_empty_column_test)

# bpe_oom_test removed — OOM/resource-exhaustion probes are out of
# scope (target columns are hundreds-of-MB to a few GB, no OOM workloads). The
# status='oom' machinery stays compiled in (runners + orchestrator/status.py)
# and is still unit-tested by tests/test_status.py.

add_executable(bpe_vocab_disjointness_test bpe_vocab_disjointness_test.cpp)
target_link_libraries(bpe_vocab_disjointness_test PRIVATE bpe_runner_lib)
add_test(NAME bpe_vocab_disjointness COMMAND bpe_vocab_disjointness_test)

add_executable(bpe_bit_width_test bpe_bit_width_test.cpp)
target_link_libraries(bpe_bit_width_test PRIVATE bpe_runner_lib)
add_test(NAME bpe_bit_width COMMAND bpe_bit_width_test)

# RLZ tests (pure C++ implementation + libdivsufsort).
add_executable(bitpack_mixed_unit_test bitpack_mixed_unit_test.cpp)
target_link_libraries(bitpack_mixed_unit_test PRIVATE runner_core)
add_test(NAME bitpack_mixed_unit COMMAND bitpack_mixed_unit_test)

add_executable(rlz_roundtrip_test rlz_roundtrip_test.cpp)
target_link_libraries(rlz_roundtrip_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_roundtrip COMMAND rlz_roundtrip_test)

add_executable(rlz_random_decode_test rlz_random_decode_test.cpp)
target_link_libraries(rlz_random_decode_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_random_decode COMMAND rlz_random_decode_test)

add_executable(rlz_prefix_search_test rlz_prefix_search_test.cpp)
target_link_libraries(rlz_prefix_search_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_prefix_search COMMAND rlz_prefix_search_test)

add_executable(rlz_load_save_identity_test rlz_load_save_identity_test.cpp)
target_link_libraries(rlz_load_save_identity_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_load_save_identity COMMAND rlz_load_save_identity_test)

add_executable(rlz_empty_column_test rlz_empty_column_test.cpp)
target_link_libraries(rlz_empty_column_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_empty_column COMMAND rlz_empty_column_test)

# rlz_oom_test removed (OOM out of scope; see bpe note above).

add_executable(rlz_refused_env_test rlz_refused_env_test.cpp)
target_link_libraries(rlz_refused_env_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_refused_env COMMAND rlz_refused_env_test)

add_executable(rlz_reference_size_disjointness_test rlz_reference_size_disjointness_test.cpp)
target_link_libraries(rlz_reference_size_disjointness_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_reference_size_disjointness COMMAND rlz_reference_size_disjointness_test)

add_executable(rlz_bit_width_test rlz_bit_width_test.cpp)
target_link_libraries(rlz_bit_width_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_bit_width COMMAND rlz_bit_width_test)

add_executable(rlz_seed_determinism_test rlz_seed_determinism_test.cpp)
target_link_libraries(rlz_seed_determinism_test PRIVATE rlz_runner_lib)
add_test(NAME rlz_seed_determinism COMMAND rlz_seed_determinism_test)

# — OnPair (last; closes lineup).
# Consumed via FetchContent from public github.com/gargiulofrancesco/onpair_cpp
# at SHA f8ecc64f per. See external/onpair_cpp.cmake,
# external/onpair_cpp.cmake, and docs/benchmark/fairness/onpair.md.

add_executable(onpair_roundtrip_test onpair_roundtrip_test.cpp)
target_link_libraries(onpair_roundtrip_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_roundtrip COMMAND onpair_roundtrip_test)

add_executable(onpair_random_decode_test onpair_random_decode_test.cpp)
target_link_libraries(onpair_random_decode_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_random_decode COMMAND onpair_random_decode_test)

add_executable(onpair_native_prefix_search_test onpair_native_prefix_search_test.cpp)
target_link_libraries(onpair_native_prefix_search_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_native_prefix_search COMMAND onpair_native_prefix_search_test)

add_executable(onpair_load_save_identity_test onpair_load_save_identity_test.cpp)
target_link_libraries(onpair_load_save_identity_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_load_save_identity COMMAND onpair_load_save_identity_test)

add_executable(onpair_seed_determinism_test onpair_seed_determinism_test.cpp)
target_link_libraries(onpair_seed_determinism_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_seed_determinism COMMAND onpair_seed_determinism_test)

add_executable(onpair_empty_column_test onpair_empty_column_test.cpp)
target_link_libraries(onpair_empty_column_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_empty_column COMMAND onpair_empty_column_test)

# onpair_oom_test removed (OOM out of scope; see bpe note above).

add_executable(onpair_bit_width_disjointness_test onpair_bit_width_disjointness_test.cpp)
target_link_libraries(onpair_bit_width_disjointness_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_bit_width_disjointness COMMAND onpair_bit_width_disjointness_test)

add_executable(onpair_dict_offsets_diagnostic_test onpair_dict_offsets_diagnostic_test.cpp)
target_link_libraries(onpair_dict_offsets_diagnostic_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_dict_offsets_diagnostic COMMAND onpair_dict_offsets_diagnostic_test)

add_executable(onpair_bit_tight_identity_test onpair_bit_tight_identity_test.cpp)
target_link_libraries(onpair_bit_tight_identity_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_bit_tight_identity COMMAND onpair_bit_tight_identity_test)

add_executable(onpair_cluster_b_symmetry_test onpair_cluster_b_symmetry_test.cpp)
target_link_libraries(onpair_cluster_b_symmetry_test PRIVATE
    onpair_runner_lib bpe_runner_lib)
add_test(NAME onpair_cluster_b_symmetry COMMAND onpair_cluster_b_symmetry_test)

add_executable(onpair_scope_fence_test onpair_scope_fence_test.cpp)
target_link_libraries(onpair_scope_fence_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_scope_fence COMMAND onpair_scope_fence_test)

# step 2 — A1 ablation gate. Drives held_out_compress directly on
# a synthesized 200-row column; asserts the bit-tight paired contract.
add_executable(onpair_held_out_compress_test onpair_held_out_compress_test.cpp)
target_link_libraries(onpair_held_out_compress_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_held_out_compress COMMAND onpair_held_out_compress_test)

# step 3 — A3 ablation gate. Drives held_out_compress in single-strategy
# mode (threshold_strategy="dynamic"); asserts the single-block bit-tight contract.
add_executable(onpair_held_out_single_test onpair_held_out_single_test.cpp)
target_link_libraries(onpair_held_out_single_test PRIVATE onpair_runner_lib)
add_test(NAME onpair_held_out_single COMMAND onpair_held_out_single_test)

# step 7 — A6 ablation gate. Unit-tests the shared query-index generator
# (runners/core/query_sampler.h) that the A6 ra_latency ops reuse across every
# baseline: parse, range, determinism, distribution shape, layout bracket.
add_executable(query_sampler_test query_sampler_test.cpp)
target_link_libraries(query_sampler_test PRIVATE runner_core)
add_test(NAME query_sampler COMMAND query_sampler_test)

# step 7 — random-access latency primitive gate. Regression for the
# 41ns/0ns timer artifact: batched timing + anti-DCE must yield non-zero,
# plausible per-access latency; no sub-ns p50. (Fairness-auditor invariant.)
add_executable(ra_measure_test ra_measure_test.cpp)
target_link_libraries(ra_measure_test PRIVATE runner_core)
add_test(NAME ra_measure COMMAND ra_measure_test)
