add_library(bpe_runner_lib STATIC bpe_runner.cpp)
target_include_directories(bpe_runner_lib PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_SOURCE_DIR}/third_party/sentencepiece/src
    ${CMAKE_SOURCE_DIR}/third_party/sentencepiece/third_party)
target_link_libraries(bpe_runner_lib PUBLIC
    runner_core
    sentencepiece_train-static
    sentencepiece-static)
target_compile_features(bpe_runner_lib PUBLIC cxx_std_20)
target_compile_options(bpe_runner_lib PRIVATE
    -Wno-deprecated-declarations
    -Wno-unused-parameter)

add_executable(bpe_runner main.cpp)
target_link_libraries(bpe_runner PRIVATE bpe_runner_lib)
target_compile_features(bpe_runner PRIVATE cxx_std_20)
