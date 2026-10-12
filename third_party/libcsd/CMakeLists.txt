# Vendored migumar2/libCSD master HEAD SHA 568bf8a876684d871b97c93b4322ddf71eb830b3
# → libcsd_static.a (single archive covering ALL StringDictionary* variants).
#
# Sidesteps upstream's Makefile to control the flags.
#
# Only the Re-Pair grammar builder is used, but the whole tree is compiled:
# StringDictionary.h itself includes all 11 sibling type headers unconditionally
# at the bottom of the file, and StringDictionary.cpp::load() factory references every
# StringDictionary*::load static symbol. We cannot compile/link the RPDAC subset alone.
# We therefore vendor and link the full libCSD tree.
#
# Single-thread fairness: libCSD has no MT API (no pthread / std::thread / OpenMP) —
# nothing to disable.
#

# C++17; the warnings that libCSD's 2011-era idioms raise (auto_ptr, register,
# throw specifications) are silenced below.
set(LIBCSD_CXX_STANDARD 17)

# --- libCSD top-level StringDictionary* sources ---
# StringDictionaryFMINDEX.cpp is EXCLUDED on macOS clang due to a
# name-lookup ambiguity in upstream code (bitset(...) call where both
# cds_utils::bitset and std::bitset are visible). RPDAC and PFC/HTFC do
# not use FMINDEX; we stub StringDictionaryFMINDEX::load in our runner
# code to satisfy the linker for StringDictionary::load's switch-case.
# Same goes for StringDictionaryXBW.cpp which also has the ambiguity.
set(LIBCSD_TOPLEVEL_SOURCES
    StringDictionary.cpp
    StringDictionaryHASHHF.cpp
    StringDictionaryHASHRPDAC.cpp
    StringDictionaryHASHRPF.cpp
    StringDictionaryHASHUFFDAC.cpp
    StringDictionaryHHTFC.cpp
    StringDictionaryHTFC.cpp
    StringDictionaryPFC.cpp
    StringDictionaryRPDAC.cpp
    StringDictionaryRPFC.cpp
    StringDictionaryRPHTFC.cpp
)

# --- libCSD subdirs ---
set(LIBCSD_REPAIR_SOURCES
    RePair/RePair.cpp
    RePair/Coder/arrayg.cpp
    RePair/Coder/basics.cpp
    RePair/Coder/hash.cpp
    RePair/Coder/heap.cpp
    RePair/Coder/records.cpp
    RePair/Coder/dictionary.cpp
    RePair/Coder/IRePair.cpp
    RePair/Coder/CRePair.cpp
)
set(LIBCSD_HUFFMAN_SOURCES
    Huffman/huff.cpp
    Huffman/Huffman.cpp
)
set(LIBCSD_HUTUCKER_SOURCES
    HuTucker/HuTucker.cpp
)
set(LIBCSD_UTILS_SOURCES
    utils/VByte.cpp
    utils/LogSequence.cpp
    utils/DAC_VLS.cpp
    utils/DAC_BVLS.cpp
    utils/Coder/BinaryNode.cpp
    utils/Coder/Coder.cpp
    utils/Coder/DecodingTable.cpp
    utils/Coder/DecodingTableBuilder.cpp
    utils/Coder/DecodingTree.cpp
    utils/Coder/StatCoder.cpp
)
set(LIBCSD_HASH_SOURCES
    Hash/Hash.cpp
    Hash/HashBBdh.cpp
    Hash/HashBdh.cpp
    Hash/HashDAC.cpp
    Hash/Hashdh.cpp
)
# XBW/ and FMIndex/ EXCLUDED on macOS clang due to bitset name-lookup
# ambiguity in upstream code. Stubs in runner code satisfy the linker
# for StringDictionary::load's switch-cases targeting DXBW and FMINDEX.
set(LIBCSD_XBW_SOURCES "")
set(LIBCSD_FMINDEX_SOURCES "")

# --- libcds bundled subdirs (libcds 1.0.x from migumar2/libCSD/libcds/) ---
# Enumerated to match libcds/src/Makefile's OBJECTS lists at the pinned SHA.
# Glob would pick up additional .cpp files (SequenceBuilderStr, BWBuilder,
# etc.) that upstream does NOT compile and that reference identifiers
# (PermutationBuilderMRRR, tokenize) the build configuration doesn't expose.
set(LIBCDS_SOURCES
    # UTILS_OBJ
    libcds/src/utils/Array.cpp
    libcds/src/utils/BitString.cpp
    libcds/src/utils/cppUtils.cpp
    # STATIC_MAPPER_OBJ
    libcds/src/static/mapper/Mapper.cpp
    libcds/src/static/mapper/MapperNone.cpp
    libcds/src/static/mapper/MapperCont.cpp
    # STATIC_BITSEQ_OBJ
    libcds/src/static/bitsequence/BitSequence.cpp
    libcds/src/static/bitsequence/BitSequenceRG.cpp
    libcds/src/static/bitsequence/BitSequence375.cpp
    libcds/src/static/bitsequence/BitSequenceSDArray.cpp
    libcds/src/static/bitsequence/sdarraySadakane.cpp
    libcds/src/static/bitsequence/BitSequenceDArray.cpp
    libcds/src/static/bitsequence/BitSequenceRRR.cpp
    libcds/src/static/bitsequence/BitSequenceBuilderRG.cpp
    libcds/src/static/bitsequence/BitSequenceBuilder375.cpp
    libcds/src/static/bitsequence/BitSequenceBuilderRRR.cpp
    libcds/src/static/bitsequence/BitSequenceBuilderSDArray.cpp
    libcds/src/static/bitsequence/BitSequenceBuilderDArray.cpp
    libcds/src/static/bitsequence/TableOffsetRRR.cpp
    # STATIC_CODER_OBJ
    libcds/src/static/coders/huff.cpp
    libcds/src/static/coders/HuffmanCoder.cpp
    # STATIC_SEQ_OBJ
    libcds/src/static/sequence/Sequence.cpp
    libcds/src/static/sequence/BitmapsSequence.cpp
    libcds/src/static/sequence/wt_coder.cpp
    libcds/src/static/sequence/wt_coder_binary.cpp
    libcds/src/static/sequence/wt_coder_huff.cpp
    libcds/src/static/sequence/wt_node.cpp
    libcds/src/static/sequence/wt_node_internal.cpp
    libcds/src/static/sequence/wt_node_leaf.cpp
    libcds/src/static/sequence/WaveletTree.cpp
    libcds/src/static/sequence/WaveletTreeNoptrs.cpp
    libcds/src/static/sequence/SequenceGMR.cpp
    libcds/src/static/sequence/SequenceGMRChunk.cpp
    libcds/src/static/sequence/SequenceBuilderWaveletTreeNoptrs.cpp
    libcds/src/static/sequence/SequenceBuilderWaveletTree.cpp
    libcds/src/static/sequence/SequenceBuilderGMR.cpp
    libcds/src/static/sequence/SequenceBuilderGMRChunk.cpp
    # STATIC_PERM_OBJ
    libcds/src/static/permutation/Permutation.cpp
    libcds/src/static/permutation/PermutationMRRR.cpp
    libcds/src/static/permutation/PermutationBuilderMRRR.cpp
    libcds/src/static/permutation/perm.cpp
    # STATIC_TEXTIN_OBJ and STATIC_TREE_OBJ excluded — these subdirs are only
    # referenced by libCSD's FMIndex/ subdir, which is excluded due to the
    # cds_utils::bitset name-lookup ambiguity. Excluding them avoids further
    # cascade of C++17-introduced name conflicts (std::byte vs cds_utils::byte
    # in factorization.h, etc.). The StringDictionary{HASHHF, HASHRPDAC,
    # HASHRPF, HASHUFFDAC, HHTFC, HTFC, PFC, RPDAC, RPFC, RPHTFC} subset
    # we DO link does not transitively reference these libcds modules.
)

add_library(libcsd_static STATIC
    ${LIBCSD_TOPLEVEL_SOURCES}
    ${LIBCSD_REPAIR_SOURCES}
    ${LIBCSD_HUFFMAN_SOURCES}
    ${LIBCSD_HUTUCKER_SOURCES}
    ${LIBCSD_UTILS_SOURCES}
    ${LIBCSD_HASH_SOURCES}
    ${LIBCSD_XBW_SOURCES}
    ${LIBCSD_FMINDEX_SOURCES}
    ${LIBCDS_SOURCES}
)

# Include paths: libCSD root (for "StringDictionaryRPDAC.h" and friends),
# plus each libcds subdir (the upstream Makefile flattens these into libcds/includes/
# before compiling; we instead add each subdir as a -I path which has identical
# resolution semantics for both `#include <foo.h>` and `#include "foo.h"`).
target_include_directories(libcsd_static PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/utils
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/static/bitsequence
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/static/coders
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/static/mapper
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/static/permutation
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/static/sequence
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/static/textindex
    ${CMAKE_CURRENT_SOURCE_DIR}/libcds/src/static/suffixtree
)

target_compile_features(libcsd_static PRIVATE cxx_std_${LIBCSD_CXX_STANDARD})

# Force-include our compatibility shim (third_party/libcsd_compat.h, not vendored)
# before every libCSD TU's content. Defines ulong/uchar/uint/ushort that
# libCSD assumes from Linux <sys/types.h> but macOS does not provide.
target_compile_options(libcsd_static PRIVATE
    -include ${CMAKE_SOURCE_DIR}/third_party/libcsd_compat.h
    -O3 -DNDEBUG -fvisibility=hidden -fno-omit-frame-pointer
    -Wno-deprecated-declarations
    -Wno-deprecated
    -Wno-unused-but-set-variable
    -Wno-unused-parameter
    -Wno-unused-variable
    -Wno-unused-function
    -Wno-unused-result
    -Wno-sign-compare
    -Wno-narrowing
    -Wno-write-strings
    -Wno-register
    -Wno-format
    -Wno-pessimizing-move
    -Wno-deprecated-copy
    -Wno-class-memaccess
    -Wno-stringop-overflow
    -Wno-stringop-truncation
    -Wno-misleading-indentation
    -Wno-implicit-fallthrough
    -Wno-shift-negative-value
    -Wno-parentheses
    -Wno-array-bounds
    -Wno-maybe-uninitialized
    -Wno-uninitialized
    -Wno-overflow
    -Wno-char-subscripts
    -Wno-int-in-bool-context
)

set_target_properties(libcsd_static PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    POSITION_INDEPENDENT_CODE ON
)
