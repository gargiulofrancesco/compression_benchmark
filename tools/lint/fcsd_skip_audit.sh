#!/usr/bin/env bash
# FCSD-skip audit: no FCSD runner exists in the harness; this audit
# prevents accidental re-introduction.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Negative check 1: no runners/fcsd/ directory
if [[ -d "$ROOT/runners/fcsd" ]]; then
    echo "FCSD-skip audit FAILED: runners/fcsd/ exists" >&2
    exit 1
fi

# Negative check 2: no FCSDRunner / fcsd_cpp / FCSDColumn symbols in runner code
if grep -RE --include='*.cpp' --include='*.h' --include='*.yaml' \
        'FCSDRunner|fcsd_cpp|FCSDColumn' "$ROOT/runners" "$ROOT/experiments" 2>/dev/null; then
    echo "FCSD-skip audit FAILED: forbidden FCSD symbol present" >&2
    exit 1
fi

echo "FCSD-skip audit OK"
exit 0
