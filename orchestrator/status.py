"""Failure-as-data classifier.

Maps (exit_code, signal, peak_rss_bytes, wall_seconds, mem_cap_bytes,
timeout_seconds, declared_status) → one of the eight status strings:

    ok | oom | timeout | refused-env | crash | lossy | unsupported | dev-only

`dev-only` is cross-cutting: an uncontrolled environment (env_probe.env_controlled=False) annotates every row in
the orchestrator pass, regardless of per-op success.

`lossy` is set by the orchestrator (not classify()) when a baseline compresses
but fails the bytewise round-trip gate — recorded as data, excluded from ratios
by the lossless guard. `crash` covers a runner that dies mid-protocol.
"""
from __future__ import annotations

REFUSED_ENV_EXIT = 78  # reserved sentinel, RLZ-style

VALID = {"ok", "oom", "timeout", "refused-env", "crash", "lossy", "unsupported", "dev-only"}


def classify(exit_code: int,
             signal_num: int | None,
             peak_rss_bytes: int,
             wall_seconds: float,
             mem_cap_bytes: int,
             timeout_seconds: float,
             declared_status: str | None) -> str:
    if declared_status in VALID and declared_status != "ok":
        # runner self-declared (e.g., "unsupported" for an op it cannot handle)
        return declared_status

    if exit_code == REFUSED_ENV_EXIT:
        return "refused-env"

    if signal_num is not None:
        # SIGKILL near memory cap → oom; otherwise crash
        if signal_num == 9 and peak_rss_bytes >= int(0.85 * mem_cap_bytes):
            return "oom"
        return "crash"

    if wall_seconds > timeout_seconds:
        return "timeout"

    if exit_code != 0:
        return "crash"

    return declared_status or "ok"
