#!/usr/bin/env bash
# OnPair scope-fence audit. Forbidden APIs MUST NOT appear in
# runners/onpair/.
#
# Forbidden surface area:
#   - contains(...) / equals(...)   — KMP / EQ ops (out of scope)
#   - kmp_automaton / aho_corasick / eq_automaton / eq_search — forbidden headers
#   - pthread_create / std::thread / omp_set_num_threads — single-thread fairness
#
# Wired into the Makefile `lint:` target.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TARGET="$ROOT/runners/onpair"
[[ -d "$TARGET" ]] || { echo "OnPair scope audit: not present; skip"; exit 0; }

fail=0
# Scope fence: no out-of-scope OnPair APIs in the runner.
if grep -REn --include='*.cpp' --include='*.h' \
        '\.(contains|equals)[[:space:]]*\(' "$TARGET" 2>/dev/null; then
    echo "OnPair scope audit FAILED: forbidden API (contains/equals) in runners/onpair/" >&2
    fail=1
fi
if grep -REn --include='*.cpp' --include='*.h' \
        '(kmp_automaton|aho_corasick|eq_automaton|eq_search)' "$TARGET" 2>/dev/null; then
    echo "OnPair scope audit FAILED: forbidden header (KMP/AhoCorasick/EQ) in runners/onpair/" >&2
    fail=1
fi
# Single-thread fairness.
if grep -RE --include='*.cpp' --include='*.h' \
        'pthread_create|std::thread|omp_set_num_threads' "$TARGET" 2>/dev/null; then
    echo "OnPair scope audit FAILED: forbidden threading API in runners/onpair/" >&2
    fail=1
fi
[[ $fail -eq 0 ]] && echo "OnPair scope audit OK"
exit $fail
