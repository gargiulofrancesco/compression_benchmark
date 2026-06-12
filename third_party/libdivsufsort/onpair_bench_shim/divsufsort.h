/* Hand-rolled divsufsort.h shim for onpair_bench (Phase 4 RLZ Step 1).
 * 32-bit suffix-array variant. Replaces the upstream divsufsort.h.cmake
 * configure_file step. Targets POSIX (darwin/linux); BUILD_SHARED_LIBS=OFF
 * so DIVSUFSORT_API expands empty.
 */
#ifndef _DIVSUFSORT_H
#define _DIVSUFSORT_H 1

#ifdef __cplusplus
extern "C" {
#endif

#include <inttypes.h>

#define DIVSUFSORT_API

typedef uint8_t sauchar_t;
typedef int32_t saint_t;
typedef int32_t saidx_t;

#define PRIdSAINT_T PRId32
#define PRIdSAIDX_T PRId32

DIVSUFSORT_API
saint_t
divsufsort(const sauchar_t *T, saidx_t *SA, saidx_t n);

DIVSUFSORT_API
saidx_t
divbwt(const sauchar_t *T, sauchar_t *U, saidx_t *A, saidx_t n);

DIVSUFSORT_API
const char *
divsufsort_version(void);

DIVSUFSORT_API
saint_t
bw_transform(const sauchar_t *T, sauchar_t *U,
             saidx_t *SA, saidx_t n, saidx_t *idx);

DIVSUFSORT_API
saint_t
inverse_bw_transform(const sauchar_t *T, sauchar_t *U,
                     saidx_t *A, saidx_t n, saidx_t idx);

DIVSUFSORT_API
saint_t
sufcheck(const sauchar_t *T, const saidx_t *SA,
         saidx_t n, saint_t verbose);

DIVSUFSORT_API
saidx_t
sa_search(const sauchar_t *T, saidx_t Tsize,
          const sauchar_t *P, saidx_t Psize,
          const saidx_t *SA, saidx_t SAsize,
          saidx_t *left);

DIVSUFSORT_API
saidx_t
sa_simplesearch(const sauchar_t *T, saidx_t Tsize,
                const saidx_t *SA, saidx_t SAsize,
                saint_t c, saidx_t *left);

#ifdef __cplusplus
}
#endif

#endif /* _DIVSUFSORT_H */
