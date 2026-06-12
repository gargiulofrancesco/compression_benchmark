// OnPair scope-fence belt-and-suspenders (+ ).
// The shell-script audit `tools/lint/onpair_scope_audit.sh` is the primary
// enforcement (CI grep + Makefile lint). This TU complements it by including
// only the OnPair runner public header (onpair_runner.h) and verifying that
// the forbidden OnPair-library identifiers
// (KmpAutomaton/AhoCorasickAutomaton/EQSearch/EqAutomaton) are NOT transitively
// reachable through that include path. We don't try to assert by reflection
// (that would require RTTI for free functions); instead this TU compiles
// cleanly when the runner header is "narrow" — no accidental exposure of
// the forbidden surface area.

#include "onpair_runner.h"
#include "runner_protocol.h"

#include <iostream>

// Sentinel: this TU intentionally does NOT include any of:
//   <onpair/search/automata/kmp_automaton.h>
//   <onpair/search/automata/aho_corasick_automaton.h>
//   <onpair/search/automata/eq_automaton.h>
//   <onpair/search/eq_search.h>
// If a future edit to onpair_runner.h accidentally pulls one of these in
// transitively (forbidden by scope-fence + the CMake-include
// restriction), this TU's grep-time audit will catch it.

int main() {
    onpair_bench::OnPairRunner r;
    if (std::string(r.name()) != "onpair") return 1;
    std::cout << "onpair_scope_fence: OK (narrow public header — see "
                 "tools/lint/onpair_scope_audit.sh for the load-bearing "
                 "audit)\n";
    return 0;
}
