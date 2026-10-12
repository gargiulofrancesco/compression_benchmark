"""Standalone environment fingerprint.

Writes build/env_fingerprint.json capturing the AVX-512 / FSST-dispatch fairness
record PLUS the L_ra timing prerequisites (invariant TSC, scaling governor,
turbo state) and basic machine identity. It disassembles
the built FSST object to confirm whether real AVX-512 code is present, rather than
deducing from hardware alone.

Run:  python -m tools.env_fingerprint --build-root build [--out build/env_fingerprint.json]

The per-run manifest also carries the key fields (avx512f, fsst_simd_path) via
orchestrator/env_probe.py; this file is the fuller, human-readable record.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import platform
from pathlib import Path

from orchestrator.env_probe import (
    probe,
    cpuinfo_flags,
    fsst_object_has_avx512,
)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def _governors() -> tuple[str | None, bool]:
    govs = sorted({
        p.read_text().strip()
        for p in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")
    }) if Path("/sys/devices/system/cpu").exists() else []
    cpu0 = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    uniform = len(govs) <= 1
    return cpu0, uniform


def build_fingerprint(build_root: str | Path) -> dict:
    flags = cpuinfo_flags()
    p = probe(build_root=build_root)

    no_turbo = _read("/sys/devices/system/cpu/intel_pstate/no_turbo")
    boost = _read("/sys/devices/system/cpu/cpufreq/boost")
    gov0, gov_uniform = _governors()
    tsc_invariant = "constant_tsc" in flags and "nonstop_tsc" in flags
    turbo_disabled = (no_turbo == "1") or (boost == "0")

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "machine_id": p.machine_id,
        "uname": p.uname,
        "cpu_model": p.cpu_model,
        "compiler": p.compiler,
        "python_version": p.python_version,
        "baseline_build_flags_sha": p.build_flags_sha,
        "m1_grade": p.m1_grade,
        "avx512": {
            "avx512f": p.avx512f,
            "avx512_flags_present": sorted(f for f in flags if f.startswith("avx512")),
        },
        "fsst": {
            "simd_path": p.fsst_simd_path,          # "avx512" | "scalar"
            "simd_reason": p.fsst_simd_reason,
            "object_has_avx512_code": fsst_object_has_avx512(build_root),
        },
        "timing_prereqs": {
            "tsc_invariant": tsc_invariant,
            "scaling_governor_cpu0": gov0,
            "governor_uniform_across_cpus": gov_uniform,
            "intel_pstate_no_turbo": no_turbo,
            "cpufreq_boost": boost,
            "turbo_disabled": turbo_disabled,
            "l_ra_ready": bool(tsc_invariant and gov0 == "performance" and turbo_disabled),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-root", default="build")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    fp = build_fingerprint(a.build_root)
    out = Path(a.out) if a.out else Path(a.build_root) / "env_fingerprint.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fp, indent=2, sort_keys=True) + "\n")
    print(f"env_fingerprint: wrote {out}")
    print(f"  avx512f={fp['avx512']['avx512f']} "
          f"fsst_simd_path={fp['fsst']['simd_path']} "
          f"l_ra_ready={fp['timing_prereqs']['l_ra_ready']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
