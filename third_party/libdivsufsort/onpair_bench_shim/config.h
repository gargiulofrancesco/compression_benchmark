/* Hand-rolled config.h shim for onpair_bench (Phase 4 RLZ Step 1).
 * Replaces the upstream cmake-2.4-era configure_file machinery.
 * Targets POSIX (darwin/linux) with C99 stdint+inttypes available.
 */
#ifndef _CONFIG_H
#define _CONFIG_H 1

#ifdef __cplusplus
extern "C" {
#endif

#define PROJECT_VERSION_FULL "2.0.1+onpair_bench_shim"

#define HAVE_INTTYPES_H 1
#define HAVE_STDDEF_H 1
#define HAVE_STDINT_H 1
#define HAVE_STDLIB_H 1
#define HAVE_STRING_H 1
#define HAVE_STRINGS_H 1
#define HAVE_MEMORY_H 1
#define HAVE_SYS_TYPES_H 1

#define INLINE inline

#ifdef __cplusplus
}
#endif

#endif /* _CONFIG_H */
