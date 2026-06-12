"""Machine + build-flag fingerprinting.

baseline_build_flags_sha is derived from the canonical sorted flag list — the
SAME list that runners/CMakeLists.txt embeds. will share a source of
truth; for we duplicate and document.
"""
from __future__ import annotations
import glob
import hashlib
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# dev profile (M1-grade publish flags pin -march=icelake-server in;
# the dev profile uses -march=native — see CMakeLists.txt for the lockstep list).
DEV_BUILD_FLAGS: list[str] = [
    "-DNDEBUG",
    "-O3",
    "-march=native",
    "-fno-omit-frame-pointer",
    "-fvisibility=hidden",
    "-std=c++20",
]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ── AVX-512 / FSST runtime-dispatch fingerprint (MIGRATION §3, V-A fairness) ──
# FSST only takes its AVX-512 compress path when BOTH hold: the CPU advertises
# AVX-512F, AND libfsst was compiled with __AVX512F__ (otherwise
# fsst_compressAVX512 is a scalar no-op stub and fsst_hasAVX512()'s CPUID gate
# never fires). We confirm the build half authoritatively by disassembling the
# built fsst_avx512 TU and looking for real %zmm code, and the hardware half via
# /proc/cpuinfo. The Phase-3 dev profile builds FSST WITHOUT AVX-512 flags
# (-march=icelake-server is deferred to the Phase-7 publish profile), so on any
# rig today the path is "scalar"; record that honestly as a fairness caveat.

def cpuinfo_flags() -> set[str]:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("flags") or line.startswith("Features"):
                    return set(line.split(":", 1)[1].split())
    except Exception:
        pass
    return set()


def has_avx512f() -> bool:
    return "avx512f" in cpuinfo_flags()


# ── Throughput-path environment control (W-6, machine-AGNOSTIC) ───────────────
# The headline throughput axes (T_c, T_d) are only trustworthy under a controlled
# frequency/thermal environment: performance governor + turbo off + pinning. The
# rdtscp L_ra path (runners/core/ra_timer_tsc.h) already enforces this for latency;
# these helpers let the orchestrator enforce the SAME for throughput. The check is
# on the ENVIRONMENT, not the CPU identity, so any box configured this way qualifies
# (reproduction on other hardware is unaffected).

def _read_first_line(path: str) -> str:
    try:
        with open(path) as f:
            return f.readline().strip()
    except Exception:
        return ""


def scaling_governor_summary() -> str:
    """Governor across online CPUs: 'performance' if every readable CPU is
    performance; the single governor if uniform-but-not-performance; 'mixed:a,b'
    if they disagree; '' if unreadable."""
    govs: set[str] = set()
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"):
        g = _read_first_line(p)
        if g:
            govs.add(g)
    if not govs:
        return ""
    if govs == {"performance"}:
        return "performance"
    if len(govs) == 1:
        return next(iter(govs))
    return "mixed:" + ",".join(sorted(govs))


def turbo_state() -> str:
    """'off' | 'on' | 'unknown' from intel_pstate/no_turbo or cpufreq/boost."""
    no_turbo = _read_first_line("/sys/devices/system/cpu/intel_pstate/no_turbo")
    boost    = _read_first_line("/sys/devices/system/cpu/cpufreq/boost")
    if no_turbo == "1" or boost == "0":
        return "off"
    if no_turbo == "0" or boost == "1":
        return "on"
    return "unknown"


def env_control() -> tuple[bool, str, str, str]:
    """(controlled, reason, governor_summary, turbo_state). Controlled iff the
    governor is 'performance' on every CPU AND turbo is confirmably off."""
    gov = scaling_governor_summary()
    turbo = turbo_state()
    if gov != "performance":
        return (False,
                f"scaling_governor='{gov or 'unreadable'}' (need 'performance' on all CPUs)",
                gov, turbo)
    if turbo != "off":
        return (False,
                f"turbo='{turbo}' (need intel_pstate/no_turbo=1 or cpufreq/boost=0)",
                gov, turbo)
    return (True, "performance governor + turbo off", gov, turbo)


def fsst_object_has_avx512(build_root: str | Path | None) -> bool | None:
    """True/False if the built fsst_avx512 TU contains real AVX-512 (%zmm) code;
    None if the object or objdump is unavailable (can't tell)."""
    if not build_root:
        return None
    objs = glob.glob(str(Path(build_root) / "**" / "fsst_avx512.cpp.o"),
                     recursive=True)
    if not objs or not shutil.which("objdump"):
        return None
    try:
        out = subprocess.check_output(["objdump", "-d", objs[0]],
                                      text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return bool(re.search(r"%zmm[0-9]", out))


def fsst_simd_path(build_root: str | Path | None) -> tuple[str, str]:
    """Return (path, reason) where path is 'avx512' or 'scalar'."""
    hw = has_avx512f()
    built = fsst_object_has_avx512(build_root)
    if not hw:
        return "scalar", "CPU lacks AVX-512F (avx512f flag absent in /proc/cpuinfo)"
    if built is False:
        return "scalar", "AVX-512F CPU but FSST built without __AVX512F__ (dev profile)"
    if built is True:
        return "avx512", "AVX-512F CPU and FSST built with real AVX-512 code"
    return "avx512", "AVX-512F CPU; FSST object/objdump unavailable — deduced from hardware"


def build_flags_sha(flags: list[str] | None = None) -> str:
    f = sorted(flags if flags is not None else DEV_BUILD_FLAGS)
    return _sha("\x00".join(f))


@dataclass
class EnvProbe:
    machine_id: str
    uname: str
    cpu_model: str
    compiler: str
    python_version: str
    build_flags_sha: str
    m1_grade: bool
    avx512f: bool
    fsst_simd_path: str        # "avx512" | "scalar" — V-A fairness fingerprint
    fsst_simd_reason: str
    scaling_governor: str      # governor summary across CPUs (W-6 throughput-env)
    turbo: str                 # "off" | "on" | "unknown"
    env_controlled: bool       # governor==performance on all CPUs AND turbo off
    env_control_reason: str


def probe(flags: list[str] | None = None,
          build_root: str | Path | None = None) -> EnvProbe:
    uname = platform.platform()
    sysname = platform.system()
    cpu_model = ""
    try:
        if sysname == "Linux":
            cpu_model = subprocess.check_output(
                ["sh", "-c", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2"],
                text=True).strip()
        elif sysname == "Darwin":
            cpu_model = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    except Exception:
        cpu_model = "unknown"

    compiler = "unknown"
    try:
        compiler = subprocess.check_output(["c++", "--version"], text=True).splitlines()[0]
    except Exception:
        pass

    flags_sha = build_flags_sha(flags)
    machine_id = _sha(f"{uname}|{cpu_model}|{compiler}")[:16]
    # Retained for schema compatibility; row citability is keyed on
    # env_controlled (performance governor + turbo off), not CPU identity.
    m1_grade = "Icelake" in cpu_model

    avx512f = has_avx512f()
    simd_path, simd_reason = fsst_simd_path(build_root)
    env_ctl, env_ctl_reason, gov, turbo = env_control()

    return EnvProbe(
        machine_id=machine_id,
        uname=uname,
        cpu_model=cpu_model,
        compiler=compiler,
        python_version=platform.python_version(),
        build_flags_sha=flags_sha,
        m1_grade=m1_grade,
        avx512f=avx512f,
        fsst_simd_path=simd_path,
        fsst_simd_reason=simd_reason,
        scaling_governor=gov,
        turbo=turbo,
        env_controlled=env_ctl,
        env_control_reason=env_ctl_reason,
    )
