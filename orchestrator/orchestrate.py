"""orchestrator CLI.

End-to-end: synth → runner subprocess → NDJSON envelopes → SQLite + CSV →
round-trip + G6 self-consistency verification → stats aggregation.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import io
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml


def _cpu_max_freqs() -> dict[int, int]:
    """{cpu_id: cpufreq max frequency in kHz} from sysfs, for the CPUs this
    process is allowed to run on. Empty if cpufreq is unavailable."""
    freqs: dict[int, int] = {}
    try:
        allowed = os.sched_getaffinity(0)
    except Exception:
        allowed = set(range(os.cpu_count() or 0))
    for cpu in sorted(allowed):
        path = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/cpuinfo_max_freq"
        try:
            with open(path) as f:
                freqs[cpu] = int(f.read().strip())
        except Exception:
            pass
    return freqs


def _resolve_pin_cpus() -> list[int]:
    """CPU ids to pin runner subprocesses to — machine-INDEPENDENT.

    Strategy: take the highest cpufreq max-frequency present among the allowed
    CPUs; those cores are the performance cores on any hybrid x86 (Arrow Lake /
    Raptor Lake / …) and are simply "all cores" on a homogeneous CPU. Drop CPU 0
    (typically handling OS/IRQ noise) UNLESS that would empty the set. If cpufreq
    is unreadable, fall back to "all allowed CPUs except 0". Override with
    PIN_CPUS (comma list; empty string disables pinning entirely)."""
    env = os.environ.get("PIN_CPUS")
    if env is not None:
        return [int(x) for x in env.split(",") if x.strip() != ""]
    if not hasattr(os, "sched_setaffinity"):
        return []
    try:
        allowed = sorted(os.sched_getaffinity(0))
    except Exception:
        allowed = list(range(os.cpu_count() or 0))
    if not allowed:
        return []

    freqs = _cpu_max_freqs()
    if freqs:
        top = max(freqs.values())
        perf = [cpu for cpu, fq in freqs.items() if fq == top]   # the P-cores
    else:
        perf = allowed                                           # no cpufreq info

    non_zero = [c for c in perf if c != 0]
    return non_zero if non_zero else perf


_PIN_CPUS = _resolve_pin_cpus()


def _pin_to_perf_core() -> None:
    """preexec_fn: confine the child to the performance-core set (best-effort)."""
    try:
        os.sched_setaffinity(0, set(_PIN_CPUS))
    except Exception:
        pass  # non-fatal: fall back to unpinned scheduling

# Allow `python orchestrator/orchestrate.py` or `python -m orchestrator.orchestrate`.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator.column_io import Column, read_column, write_column
    from orchestrator.env_probe import probe
    from orchestrator.modes import load as load_modes, check_g7, check_runner_name
    from orchestrator.status import classify
    from orchestrator.store import ResultRow, open_store, insert_rows, export_csv
    from orchestrator.stats import aggregate
    from orchestrator.synthetic_gen import generate
    from orchestrator.synthetic_gen_vd import generate_vd
    from orchestrator.manifest import manifest, write_manifest
else:
    from .column_io import Column, read_column, write_column
    from .env_probe import probe
    from .modes import load as load_modes, check_g7, check_runner_name
    from .status import classify
    from .store import ResultRow, open_store, insert_rows, export_csv
    from .stats import aggregate
    from .synthetic_gen import generate
    from .synthetic_gen_vd import generate_vd
    from .manifest import manifest, write_manifest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _materialise_input_column(cell: dict, input_col: Path, needles: list[bytes]) -> None:
    """step 1 — route on cell["dataset_kind"] (default synthetic_simple
    preserves all 13 existing smoke cells).

    - synthetic_simple (default): existing Gaussian generator on alphabet of
      letters+digits+symbols, sprinkled with G6 needles for prefix_search hits.
    - synthetic_vd: parametric V-D Markov generator over `L × |Σ| × ρ`.
    - real: copy a previously-materialised column from disk (path under
      datasets/materialized/ or testdata/datasets/).
    """
    kind = cell.get("dataset_kind", "synthetic_simple")
    if kind == "real":
        import shutil
        src = cell.get("column_path")
        if src is None:
            raise ValueError(f"cell {cell.get('cell_id')!r}: dataset_kind=real requires column_path")
        src_path = Path(src)
        if not src_path.is_absolute():
            src_path = REPO_ROOT / src
        if not src_path.exists():
            raise FileNotFoundError(
                f"real dataset column missing: {src_path}; run `make datasets` to materialise"
            )
        shutil.copy(src_path, input_col)
        return
    if kind == "synthetic_vd":
        col = generate_vd(
            L=int(cell["L"]),
            sigma_size=int(cell["sigma"]),
            rho=float(cell["rho"]),
            n_rows=int(cell["n_rows"]),
            seed=int(cell["seed"]),
        )
        write_column(str(input_col), col)
        return
    if kind == "synthetic_simple":
        col = generate(cell["n_rows"], cell["mean_len"], cell["seed"], needles)
        write_column(str(input_col), col)
        return
    raise ValueError(f"cell {cell.get('cell_id')!r}: unknown dataset_kind={kind!r}")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _send(proc: subprocess.Popen, env: dict) -> None:
    proc.stdin.write(json.dumps(env) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict:
    line = proc.stdout.readline()
    if not line:
        raise EOFError("runner closed stdout")
    return json.loads(line)


class CorrectnessGateError(RuntimeError):
    """A baseline failed a per-cell correctness gate (compress-identity /
    bulk_decode / random_decode round-trip). Distinct from protocol/infra
    RuntimeErrors so the driver can record the cell as status='lossy' data and
    continue the grid, rather than aborting the whole run (mirrors the
    status='oom' as-data pattern). Infra RuntimeErrors still propagate."""


def _drive_runner(runner_path: Path, cell: dict, needles: list[bytes],
                  workdir: Path, n_samples: int,
                  modes_table: dict[str, str], env_dict: dict,
                  run_id: str) -> tuple[list[ResultRow], bool]:
    """Run one cell against one runner. Returns (rows, g6_passed)."""
    input_col = workdir / f"{cell['cell_id']}.in.col"
    _materialise_input_column(cell, input_col, needles)
    ref_input_bytes = input_col.read_bytes()
    col = read_column(str(input_col))  # for G6 expected hit-set + random_decode oracle

    flags_sha = env_dict["baseline_build_flags_sha"]
    machine_id = env_dict["machine_id"]
    dev_only   = not env_dict["env_controlled"]

    sub_env = {"BASELINE_BUILD_FLAGS_SHA": flags_sha, "PATH": "/usr/bin:/bin"}
    # Pin the runner subprocess to a performance core. On a hybrid x86 CPU the
    # scheduler otherwise scatters runners across P- and E-cores, adding ~25%
    # throughput variance (an E-core landing decodes ~20% slower). The P-core set
    # is detected from cpufreq max frequencies (machine-independent — see
    # _resolve_pin_cpus); PIN_CPUS overrides it, empty disables pinning.
    proc = subprocess.Popen(
        [str(runner_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=sub_env,
        preexec_fn=_pin_to_perf_core if _PIN_CPUS else None,
    )
    rows: list[ResultRow] = []
    g6_passed = False
    try:
        # Handshake (G7 gate)
        hello = _recv(proc)
        if hello.get("type") != "hello":
            raise RuntimeError(f"expected hello, got {hello}")
        # Variant-multiplexed runners (e.g., libcsd_pfc_htfc) report a single
        # hello.name but serve multiple cell-level baselines via config_json
        # variant. Record identity uses cell["baseline"] (the variant-specific
        # name) so §8 plots can distinguish PFC vs HTFC rows.
        check_runner_name(cell["baseline"], hello["name"])
        baseline = cell["baseline"]
        psm = hello["prefix_search_mode"]
        check_g7(baseline, psm, modes_table)
        if hello.get("baseline_build_flags_sha") != flags_sha:
            raise RuntimeError(f"flags_sha mismatch: runner={hello.get('baseline_build_flags_sha')} expected={flags_sha}")

        needle = needles[0].decode()  # G6: first needle drives the prefix_search sample loop
        # Path used by bulk_decode / random_decode / prefix_search as input (the compressed payload).
        compressed_path = workdir / f"{cell['cell_id']}.compress.s0.out"

        for op in ("compress", "bulk_decode", "random_decode", "prefix_search"):
            for sample_idx in range(n_samples):
                stem = f"{cell['cell_id']}.{op}.s{sample_idx}"
                out_path = workdir / f"{stem}.out"

                if op == "compress":
                    in_path = input_col
                else:
                    in_path = compressed_path

                env = {"type": "op", "op": op, "cell_id": cell["cell_id"],
                       "input_path": str(in_path), "output_path": str(out_path),
                       "config_json": cell.get("config_json", "{}")}
                if op == "random_decode":
                    env["idx"] = 42
                if op == "prefix_search":
                    env["needle"] = needle

                _send(proc, env)
                resp = _recv(proc)
                if resp.get("type") != "result":
                    raise RuntimeError(f"expected result, got {resp}")
                status = resp.get("status", "crash")
                if dev_only and status == "ok":
                    status = "dev-only"
                rsm = psm if op == "prefix_search" else None
                metrics = resp.get("metrics", [])
                if not metrics:
                    metrics = [{"name": "no_metric", "value": None}]
                for m in metrics:
                    rows.append(ResultRow(
                        run_id=run_id, cell_id=cell["cell_id"], baseline=baseline,
                        op=op, metric_name=m["name"], metric_value=m.get("value"),
                        status=status, prefix_search_mode=rsm,
                        config_json=cell.get("config_json", "{}"),
                        dataset=cell["dataset"], sample_idx=sample_idx,
                        machine_id=machine_id, baseline_build_flags_sha=flags_sha,
                        notes=resp.get("notes", ""), ts_utc=_now_iso(),
                    ))

                # Correctness verifications on the first sample of each op.
                # Per-baseline branch: Uncompressed is identity (compress output == input bytes);
                # every other baseline is opaque on `compress` and we instead verify via
                # bulk_decode round-trip equality.
                if sample_idx == 0:
                    if op == "compress" and baseline == "uncompressed":
                        if Path(out_path).read_bytes() != ref_input_bytes:
                            raise CorrectnessGateError("compress identity failed (Uncompressed)")
                    elif op == "bulk_decode":
                        if Path(out_path).read_bytes() != ref_input_bytes:
                            raise CorrectnessGateError(f"bulk_decode round-trip failed (baseline={baseline})")
                    elif op == "random_decode":
                        expected = col.at(42)
                        if Path(out_path).read_bytes() != expected:
                            raise CorrectnessGateError(f"random_decode round-trip failed (baseline={baseline})")
                    elif op == "prefix_search":
                        g6_passed = _verify_g6(col, needle.encode(), out_path)

        _send(proc, {"type": "bye"})
        proc.stdin.close()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            # Surface stderr for diagnosability when the runner crashed.
            err = proc.stderr.read() if proc.stderr else ""
            if err:
                print(f"orchestrate: runner stderr:\n{err}", file=sys.stderr)
    return rows, g6_passed


def _verify_g6(col: Column, needle: bytes, hits_path: Path) -> bool:
    """G6 self-consistency: runner's hit list == Python reference scan."""
    raw = hits_path.read_bytes()
    n_hits = int.from_bytes(raw[:4], "little")
    runner_hits = [int.from_bytes(raw[4 + 4 * i: 8 + 4 * i], "little") for i in range(n_hits)]
    ref = [i for i in range(col.num_rows) if col.at(i).startswith(needle)]
    return runner_hits == ref


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runner",  type=Path, required=False, default=None,
                   help="Default runner binary; per-cell `runner_path` overrides.")
    p.add_argument("--build-root", type=Path, required=False, default=None,
                   help="Root for resolving relative `runner_path` values in cells YAML.")
    p.add_argument("--cells",   type=Path, required=True)
    p.add_argument("--modes",   type=Path, required=True)
    p.add_argument("--needles", type=Path, required=True)
    p.add_argument("--db",      type=Path, required=True)
    p.add_argument("--csv",     type=Path, required=True)
    p.add_argument("--manifest", type=Path, default=None)
    p.add_argument("--workdir", type=Path, default=None)
    a = p.parse_args()

    cells_doc = yaml.safe_load(a.cells.read_text())
    n_samples = int(cells_doc.get("n_samples", 5))
    # Statistical-robustness floor (W-5): a cell doc may declare `min_samples`
    # (the headline grid stamps 11). Refuse to run — and thus to record — if the
    # repetition count has drifted below it, so an under-sampled run can never
    # masquerade as a headline measurement. Smoke docs omit min_samples (floor 0).
    min_samples = int(cells_doc.get("min_samples", 0))
    if n_samples < min_samples:
        raise SystemExit(
            f"orchestrate: n_samples={n_samples} is below the declared "
            f"min_samples={min_samples} for this cell doc — refusing to record an "
            f"under-sampled headline run. Regenerate the grid or raise n_samples.")
    modes_table = load_modes(a.modes)
    needles = [ln.encode() for ln in a.needles.read_text().splitlines() if ln]

    env = probe(build_root=a.build_root)
    env_dict = {
        "machine_id": env.machine_id,
        "uname": env.uname,
        "cpu_model": env.cpu_model,
        "compiler": env.compiler,
        "python_version": env.python_version,
        "baseline_build_flags_sha": env.build_flags_sha,
        "m1_grade": env.m1_grade,
        "avx512f": env.avx512f,
        "fsst_simd_path": env.fsst_simd_path,
        "fsst_simd_reason": env.fsst_simd_reason,
        "scaling_governor": env.scaling_governor,
        "turbo": env.turbo,
        "env_controlled": env.env_controlled,
        "env_control_reason": env.env_control_reason,
    }
    print(f"orchestrate: env machine_id={env.machine_id} governor={env.scaling_governor} "
          f"turbo={env.turbo} controlled={env.env_controlled} "
          f"avx512f={env.avx512f} fsst_simd_path={env.fsst_simd_path} "
          f"flags_sha={env.build_flags_sha[:12]}...")

    # Throughput-path environment gate (W-6). A cell doc may declare
    # `require_controlled_env` (the headline grid stamps it). Refuse to run — and
    # thus to record — T_c/T_d under an uncontrolled frequency/thermal environment,
    # symmetric with the rdtscp L_ra path. Machine-AGNOSTIC: any box with the
    # performance governor + turbo off qualifies, so reproduction elsewhere is fine.
    # Smoke/dev docs omit the flag and are exempt.
    if bool(cells_doc.get("require_controlled_env", False)) and not env.env_controlled:
        raise SystemExit(
            f"orchestrate: refusing to record a headline run under an uncontrolled "
            f"environment — {env.env_control_reason}. Set the performance governor and "
            f"disable turbo on the measurement box, or run a non-headline grid.")

    run_id = uuid.uuid4().hex
    workdir = a.workdir or Path(tempfile.mkdtemp(prefix="onpair_bench_smoke_"))
    workdir.mkdir(parents=True, exist_ok=True)

    conn = open_store(a.db)
    all_rows: list[ResultRow] = []
    lossy_cells: list[tuple[str, str]] = []
    g6_all = True
    for cell in cells_doc["cells"]:
        rp = cell.get("runner_path")
        if rp:
            runner_path = (a.build_root / rp) if a.build_root else Path(rp)
        elif a.runner:
            runner_path = a.runner
        else:
            raise SystemExit(f"cell {cell['cell_id']}: no runner_path and no --runner default")
        if not runner_path.exists():
            raise SystemExit(f"cell {cell['cell_id']}: runner binary not found at {runner_path}")
        print(f"orchestrate: driving cell {cell['cell_id']} baseline={cell['baseline']} "
              f"runner={runner_path.name}", flush=True)
        # Per-cell failures (a single baseline×column) are recorded as data and
        # do NOT abort the whole grid — one run surfaces ALL failing combos at
        # once. CorrectnessGateError → status='lossy' (compresses but round-trips
        # wrong); runner death (EOFError/BrokenPipe — segfault/exit mid-protocol)
        # → status='crash'. Infra/orchestrator bugs still propagate.
        try:
            rows, g6 = _drive_runner(runner_path, cell, needles, workdir, n_samples,
                                     modes_table, env_dict, run_id)
        except (CorrectnessGateError, EOFError, BrokenPipeError, ConnectionError) as e:
            cell_status = "lossy" if isinstance(e, CorrectnessGateError) else "crash"
            print(f"orchestrate: cell {cell['cell_id']} baseline={cell['baseline']} "
                  f"FAILED ({cell_status}: {e}) — recording + continuing",
                  file=sys.stderr)
            lossy_cells.append((cell["cell_id"], f"{cell_status}: {e}"))
            all_rows.append(ResultRow(
                run_id=run_id, cell_id=cell["cell_id"], baseline=cell["baseline"],
                op="bulk_decode", metric_name="roundtrip_ok", metric_value=0.0,
                status=cell_status, prefix_search_mode=modes_table.get(cell["baseline"]),
                config_json=cell.get("config_json", "{}"), dataset=cell["dataset"],
                sample_idx=0, machine_id=env.machine_id,
                baseline_build_flags_sha=env.build_flags_sha,
                notes=f"{cell_status}: {e}", ts_utc=_now_iso(),
            ))
            insert_rows(conn, all_rows[-1:])
            for stale in workdir.glob(f"{cell['cell_id']}.*"):
                stale.unlink(missing_ok=True)
            continue
        all_rows.extend(rows)
        # Durability: persist this cell's rows immediately — a long
        # grid interrupted at hour N keeps its first N hours of measurements.
        # insert_rows is idempotent-safe here because each row is unique per
        # (run_id, cell, op, metric, sample) and cells are driven exactly once.
        insert_rows(conn, rows)
        # Full-size columns × 11 samples would otherwise accumulate hundreds of
        # GB of per-sample op outputs in the workdir; everything below is
        # re-derivable, and all correctness gates for this cell have already run.
        for stale in workdir.glob(f"{cell['cell_id']}.*"):
            stale.unlink(missing_ok=True)
        g6_all = g6_all and g6
        # G6 gate result is itself a metric row
        all_rows.append(ResultRow(
            run_id=run_id, cell_id=cell["cell_id"], baseline=cell["baseline"],
            op="prefix_search", metric_name="g6_passed",
            metric_value=1.0 if g6 else 0.0,
            status="ok" if g6 else "crash",
            prefix_search_mode=modes_table[cell["baseline"]],
            config_json=cell.get("config_json", "{}"), dataset=cell["dataset"],
            sample_idx=0, machine_id=env.machine_id,
            baseline_build_flags_sha=env.build_flags_sha,
            notes="G6 self-consistency", ts_utc=_now_iso(),
        ))
        insert_rows(conn, all_rows[-1:])

    # Rows were inserted per cell (durability patch); all_rows is
    # retained only for the summary count.
    print(f"orchestrate: inserted {len(all_rows)} rows into results")
    if lossy_cells:
        print(f"orchestrate: {len(lossy_cells)} cell(s) recorded as failed "
              f"(status='lossy'/'crash'), grid continued:", file=sys.stderr)
        for cid, reason in lossy_cells:
            print(f"  - {cid}: {reason}", file=sys.stderr)
    n_agg = aggregate(conn)
    print(f"orchestrate: aggregated {n_agg} rows into results_agg")
    n_csv = export_csv(conn, "results", a.csv)
    print(f"orchestrate: exported {n_csv} rows to {a.csv}")

    if a.manifest:
        m = manifest(run_id, env_dict, cells_doc["cells"], {"n_samples": n_samples})
        write_manifest(a.manifest, m)
        print(f"orchestrate: wrote manifest to {a.manifest} sha={m['manifest_sha'][:12]}...")

    if not g6_all:
        print("orchestrate: G6 FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
