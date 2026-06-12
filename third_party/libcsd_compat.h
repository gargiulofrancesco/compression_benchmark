#pragma once
// Build-time compatibility shim for the vendored libCSD library at
// third_party/libcsd/ (SHA 568bf8a8…30b3). This file is OURS, NOT vendored;
// it lives outside third_party/libcsd/ to preserve vendored-snapshot integrity
// per Phase 0 OPEN-Q10.
//
// libCSD's 2014 code assumes Linux-isms (ulong, uchar, uint, ushort typedefs
// that the BSD <sys/types.h> provides as u_long etc.). These names are NOT
// defined by macOS's libc / libc++. Without these typedefs every TU that
// transitively includes Huffman/huff.h or FMIndex/SuffixArray.h fails to
// compile.
//
// We inject this header via `-include` at the libcsd_static target level
// (third_party/libcsd/CMakeLists.txt), so it appears before any other
// translation unit's content for every libCSD source file.

#include <sys/types.h>

#ifndef ulong
typedef unsigned long  ulong;
#endif
#ifndef uchar
typedef unsigned char  uchar;
#endif
#ifndef uint
typedef unsigned int   uint;
#endif
#ifndef ushort
typedef unsigned short ushort;
#endif

// Resolve the cds_utils::bitset vs std::bitset name-lookup ambiguity.
// libCSD's 2014 code does `using namespace std;` and `using namespace cds_utils;`
// at file scope; the unqualified call `bitset(x, y)` then becomes ambiguous on
// clang. A using-declaration doesn't disambiguate (clang reports ambiguity at
// name-lookup before overload resolution). Pure-preprocessor rewrite works:
// parse libcdsBasics.h FIRST so the declaration `cds_utils::bitset` is seen
// unmolested, then define a macro that rewrites every later unqualified call
// site `bitset(x, y)` → `cds_utils::bitset(x, y)`. Safe because grep confirms
// nothing in the vendored libCSD tree uses `std::bitset<...>` syntactically
// (no class-template usage anywhere) and the only declaration of any `bitset`
// symbol is the cds_utils free function we're aliasing to.
#include <libcdsBasics.h>
#define bitset cds_utils::bitset
