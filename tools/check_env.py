"""How does this machine compare with the one the paper was measured on?

The timing stages run on any x86-64 Linux machine with an invariant TSC,
which the latency timer needs. The paper's own setup is stricter: the
`performance` frequency governor on every CPU and turbo off, so that the
measurement core runs at a fixed clock. This check prints the state of the
machine, says whether it matches that setup and, if not, the commands that
set it. Timings taken outside it are recorded all the same; their rows are
labelled `dev-only` in the main grid.

With --strict, or STRICT_ENV=1 in the environment, a machine that is not set
up as for the paper is reported as unable to run the timing stages, and the
stages refuse to.

Exit status: 0 if the timing stages will run, 1 if they will refuse.

Usage: PYTHONPATH=. .venv/bin/python -m tools.check_env [--cpu N] [--strict]
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path

from orchestrator.env_probe import cpuinfo_flags, env_control, probe
from orchestrator.orchestrate import _resolve_pin_cpus


def _khz(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def measurement_cpu() -> int | None:
    """The core the timing stages are pinned to when none is named: the first
    performance core other than CPU 0."""
    cpus = _resolve_pin_cpus()
    return cpus[0] if cpus else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cpu", type=int, default=None,
                    help="core to measure on (default: first performance core)")
    ap.add_argument("--print-cpu", action="store_true",
                    help="print only the core the timing stages would use")
    ap.add_argument("--strict", action="store_true",
                    default=os.environ.get("STRICT_ENV") == "1",
                    help="insist on the paper's setup (default: STRICT_ENV=1)")
    ap.add_argument("--note", action="store_true",
                    help="print one line if the machine is not set up as for the "
                         "paper, nothing otherwise")
    a = ap.parse_args()
    cpu = a.cpu if a.cpu is not None else measurement_cpu()
    if a.print_cpu:
        print("" if cpu is None else cpu)
        return 0 if cpu is not None else 1

    controlled, reason, governor, turbo = env_control()
    if a.note:
        if not controlled:
            print(f"not the paper's setup ({reason}): timings are those of this "
                  "machine as it is")
        return 0
    env = probe()
    flags = cpuinfo_flags()
    tsc = {"constant_tsc", "nonstop_tsc"} <= flags
    print(f"machine        {env.cpu_model}")
    print(f"system         {env.uname}")
    print(f"compiler       {env.compiler}")
    print(f"governor       {governor or 'unreadable'}"
          f"{'' if governor == 'performance' else '   (the paper: performance on every CPU)'}")
    print(f"turbo          {turbo}{'' if turbo == 'off' else '   (the paper: off)'}")
    print(f"invariant TSC  {'yes' if tsc else 'no   <- the latency timer needs it'}")
    print(f"AVX-512        {'yes' if env.avx512f else 'no'} "
          f"(FSST encoder path: {env.fsst_simd_path})")
    usable = cpu is not None and (
        not hasattr(os, "sched_getaffinity") or cpu in os.sched_getaffinity(0))
    if cpu is None:
        print("measurement CPU: none could be chosen; pass CPU=<n> to reproduce.sh")
    elif not usable:
        print(f"measurement CPU {cpu}   <- not a CPU this process may run on")
    else:
        base = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq"
        fmax, fbase = _khz(f"{base}/scaling_max_freq"), _khz(f"{base}/base_frequency")
        clock = "" if fmax is None else f", clock limit {fmax / 1e6:.2f} GHz"
        if fbase is not None:
            clock += f" (base {fbase / 1e6:.2f} GHz)"
        print(f"measurement CPU {cpu}{clock}")

    runnable = tsc and usable
    if runnable and controlled:
        print("\nREADY: the timing stages can run, in the paper's setup "
              "(performance governor, turbo off).")
        return 0
    setup = ("    sudo cpupower frequency-set -g performance\n"
             "    echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo"
             "   # Intel\n"
             "    echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost"
             "            # AMD")
    if runnable and not a.strict:
        print("\nREADY: the timing stages can run. This is not the paper's setup:\n"
              f"  {reason}\n"
              "  Timings will be those of this machine as it is, and vary more from "
              "run to run;\n  compression ratios are not affected. To measure as the "
              f"paper did (root):\n{setup}\n"
              "  STRICT_ENV=1 makes the timing stages refuse to run until then.")
        return 0
    print("\nNOT READY: the timing stages will refuse to run.")
    if not tsc:
        print("  This CPU does not advertise an invariant TSC, which the latency "
              "timer needs.")
    if not usable:
        print("  The measurement core is not one this process may run on; "
              "choose another with CPU=<n>.")
    if runnable or (a.strict and not controlled):
        print(f"  STRICT_ENV=1 asks for the paper's setup, and {reason}.\n"
              f"  Set it with (root):\n{setup}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
