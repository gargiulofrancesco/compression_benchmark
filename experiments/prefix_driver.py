"""Compressed prefix filter against a raw scan (stage `prefix`).

The primary comparison is an in-memory, materialising prefix predicate:

* raw: canonical scan of the Arrow-style uncompressed column;
* OnPair: PrefixAutomaton scan of the bit-packed token stream;
* FSST/LZ4/Zstd lower bound: payload-only bulk-decode time plus the measured
  raw scan time for the same query.

The additive baselines are deliberately optimistic: their codec timer excludes
file I/O, output allocation, offset reconstruction, and the intermediate-column
write. If native OnPair wins against this sum, it also wins against an actual
materialise-then-scan implementation under the benchmark's in-memory model.

Every runner is pinned to one explicitly selected CPU. The driver refuses to
record measurements unless the performance governor is active, turbo is off,
and the selected CPU's maximum frequency equals its advertised base frequency.
Raw observations, rather than only summaries, are retained in JSON and CSV.

Example:

  PIN_CPUS=1 PYTHONPATH=. .venv/bin/python -m experiments.prefix_driver \
    --column tpch/p_name=datasets/materialized/tpch/p_name.col \
    --column tpch/c_comment=datasets/materialized/tpch/c_comment.col \
    --cpu 1 --samples 11 --warmups 2
"""
from __future__ import annotations

import argparse
import base64
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import os
import statistics
import struct
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from orchestrator.column_io import Column, read_column
from orchestrator.env_probe import probe


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUILD = REPO_ROOT / "build"
DEFAULT_WORKDIR = DEFAULT_BUILD / "prefix"


@dataclass(frozen=True)
class RunnerSpec:
    name: str
    relative_path: str
    config: dict


RUNNERS = {
    "uncompressed": RunnerSpec(
        "uncompressed", "runners/uncompressed/uncompressed_runner", {}),
    "fsst": RunnerSpec(
        "fsst", "runners/fsst/fsst_runner",
        {"variant": "fsst", "training": "full_column"}),
    "lz4": RunnerSpec(
        "lz4", "runners/lz4/lz4_runner",
        {"tier": "fast", "accel": 1, "block_budget_bytes": 1048576}),
    "zstd": RunnerSpec(
        "zstd", "runners/zstd/zstd_runner",
        {"level": 3, "block_budget_bytes": 1048576}),
    "onpair": RunnerSpec(
        "onpair", "runners/onpair/onpair_runner",
        {"bits": 16, "sample_fraction": 0.15, "seed": 42,
         "threads": 1, "mem_cap_gb": 64}),
}
DECODE_CODECS = ("fsst", "lz4", "zstd")


@dataclass(frozen=True)
class PrefixQuery:
    query_id: str
    query_class: str
    needle: bytes
    n_hits: int
    n_rows: int

    @property
    def selectivity(self) -> float:
        return self.n_hits / self.n_rows if self.n_rows else 0.0


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _metric_map(response: dict) -> dict[str, float]:
    return {str(item["name"]): float(item["value"])
            for item in response.get("metrics", [])}


def _sysfs_text(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _thermal_snapshot() -> dict[str, int]:
    result: dict[str, int] = {}
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            result[str(path)] = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
    return result


def _clock_load_validation(cpu: int, expected_khz: int | None) -> dict:
    """Sample scaling_cur_freq while a two-second integer loop saturates CPU.
    With `expected_khz`, a clock more than 1% away from it is an error."""
    frequency_path = Path(
        f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq")

    def pin_child() -> None:
        os.sched_setaffinity(0, {cpu})

    script = (
        "import time\n"
        "end=time.monotonic()+2.0\n"
        "value=1\n"
        "while time.monotonic()<end:\n"
        " value=(value*6364136223846793005+1)&((1<<64)-1)\n"
    )
    load = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, preexec_fn=pin_child)
    samples: list[int] = []
    try:
        while load.poll() is None:
            value = _sysfs_text(str(frequency_path))
            if value:
                samples.append(int(value))
            time.sleep(0.05)
    finally:
        load.wait()
    steady = samples[5:]
    if not steady:
        if expected_khz is not None:
            raise RuntimeError("could not sample sustained CPU frequency")
        return {"skipped": "scaling_cur_freq could not be sampled"}
    if expected_khz is not None:
        tolerance = max(10_000, int(expected_khz * 0.01))
        if min(steady) < expected_khz - tolerance or max(steady) > expected_khz + tolerance:
            raise RuntimeError(
                f"CPU {cpu} did not sustain base clock {expected_khz} kHz: "
                f"observed [{min(steady)}, {max(steady)}] kHz")
    return {"samples_khz": samples, "steady_min_khz": min(steady),
            "steady_median_khz": int(statistics.median(steady)),
            "steady_max_khz": max(steady)}


def _machine_preflight(build_root: Path, cpu: int) -> dict:
    """The state of the machine, recorded with the results: governor, turbo,
    frequency limit, and the clock the core holds under load. The paper's
    setup (performance governor, turbo off, hence a core held at its base
    frequency) is insisted on only under STRICT_ENV=1."""
    env = probe(build_root=build_root)
    strict = os.environ.get("STRICT_ENV") == "1"
    if strict and not env.env_controlled:
        raise RuntimeError(f"STRICT_ENV=1 and this machine is not set up as for "
                           f"the paper: {env.env_control_reason}")
    try:
        allowed = sorted(os.sched_getaffinity(0))
    except AttributeError as exc:
        raise RuntimeError("sched_getaffinity is required for pinned measurements") from exc
    if cpu not in allowed:
        raise RuntimeError(f"CPU {cpu} is not in this process's allowed set {allowed}")

    prefix = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq"
    governor = _sysfs_text(f"{prefix}/scaling_governor")
    base_khz = _sysfs_text(f"{prefix}/base_frequency")
    max_khz = _sysfs_text(f"{prefix}/scaling_max_freq")
    # intel_pstate with hardware P-states exposes the base (non-turbo)
    # frequency; other cpufreq drivers do not, and the cap can then be neither
    # verified nor refused.
    capped = bool(base_khz and max_khz and int(max_khz) == int(base_khz))
    if strict and base_khz and not capped:
        raise RuntimeError(
            f"CPU {cpu} is not capped at base frequency: base={base_khz!r}, "
            f"scaling_max={max_khz!r}")
    sustained = _clock_load_validation(cpu, int(base_khz) if strict and capped else None)

    try:
        git_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        git_head = "unknown"
    dirty = subprocess.run(
        ["git", "diff", "--quiet"], cwd=REPO_ROOT, check=False).returncode != 0
    return {
        "env_probe": dataclasses.asdict(env),
        "cpu": cpu,
        "allowed_cpus": allowed,
        "cpu_governor": governor,
        "cpu_base_khz": int(base_khz) if base_khz else None,
        "cpu_scaling_max_khz": int(max_khz) if max_khz else None,
        "intel_pstate_no_turbo": _sysfs_text(
            "/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "sustained_clock_validation": sustained,
        "git_head": git_head,
        "git_dirty": dirty,
        "thermal_before_millicelsius": _thermal_snapshot(),
    }


class RunnerSession:
    def __init__(self, runner: Path, expected_name: str, flags_sha: str, cpu: int):
        if not runner.is_file():
            raise FileNotFoundError(f"runner is missing: {runner} (build it first)")
        child_env = {
            "BASELINE_BUILD_FLAGS_SHA": flags_sha,
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "OMP_NUM_THREADS": "1",
        }

        def pin_child() -> None:
            os.sched_setaffinity(0, {cpu})

        self.proc = subprocess.Popen(
            [str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            env=child_env, preexec_fn=pin_child)
        observed_affinity = os.sched_getaffinity(self.proc.pid)
        if observed_affinity != {cpu}:
            self.close(force=True)
            raise RuntimeError(
                f"runner affinity is {sorted(observed_affinity)}, expected [{cpu}]")
        hello = self._recv()
        if hello.get("type") != "hello" or hello.get("name") != expected_name:
            self.close(force=True)
            raise RuntimeError(
                f"bad runner handshake for {expected_name}: {hello}")
        self.hello = hello

    def _send(self, envelope: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise EOFError(f"runner closed stdout; stderr={stderr[-2000:]}")
        return json.loads(line)

    def op(self, op: str, cell_id: str, input_path: Path, output_path: Path,
           config: dict, needle: bytes | None = None) -> tuple[dict, dict[str, float]]:
        envelope = {
            "type": "op", "op": op, "cell_id": cell_id,
            "input_path": str(input_path), "output_path": str(output_path),
            "config_json": json.dumps(config, separators=(",", ":")),
        }
        if needle is not None:
            try:
                envelope["needle"] = needle.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"query needle is not UTF-8 and cannot cross the NDJSON protocol: {needle!r}") from exc
        self._send(envelope)
        response = self._recv()
        if response.get("type") != "result" or response.get("status") != "ok":
            raise RuntimeError(f"runner op failed: {response}")
        return response, _metric_map(response)

    def close(self, force: bool = False) -> None:
        if self.proc.poll() is None and not force:
            try:
                self._send({"type": "bye"})
                self.proc.wait(timeout=10)
            except Exception:
                force = True
        if self.proc.poll() is None and force:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream and not stream.closed:
                stream.close()


def _safe_ascii_prefix(row: bytes, length: int) -> bytes | None:
    prefix = row[:length]
    if len(prefix) != length:
        return None
    if not all(32 <= byte <= 126 for byte in prefix):
        return None
    return prefix


def _count_prefixes(column: Column, candidates: Iterable[bytes]) -> dict[bytes, int]:
    unique = tuple(dict.fromkeys(candidates))
    counts = {candidate: 0 for candidate in unique}
    for row_idx in range(column.num_rows):
        row = column.at(row_idx)
        for candidate in unique:
            if row.startswith(candidate):
                counts[candidate] += 1
    return counts


def discover_queries(column_path: Path, lengths: list[int],
                     sample_cap: int = 100_000) -> tuple[list[PrefixQuery], dict]:
    """Choose deterministic common, sample-rare, and absent ASCII prefixes.

    Common/rare candidates are selected from an evenly spaced sample, then all
    candidate selectivities are counted exactly over the full column. This
    makes query discovery inexpensive without misreporting selectivity.
    """
    column = read_column(str(column_path))
    if column.num_rows == 0:
        raise ValueError(f"cannot discover prefix queries on empty column: {column_path}")
    lengths = sorted({length for length in lengths if length > 0})
    if not lengths:
        raise ValueError("at least one positive prefix length is required")

    stride = max(1, column.num_rows // max(1, sample_cap))
    sampled = list(range(0, column.num_rows, stride))[:sample_cap]
    counters = {length: Counter() for length in lengths}
    for row_idx in sampled:
        row = column.at(row_idx)
        for length in lengths:
            prefix = _safe_ascii_prefix(row, length)
            if prefix is not None:
                counters[length][prefix] += 1

    selected: list[tuple[str, str, bytes]] = []
    absent_candidates: dict[int, list[bytes]] = {}
    for length in lengths:
        counter = counters[length]
        if not counter:
            continue
        ordered_common = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        common = ordered_common[0][0]
        selected.append((f"common-L{length}", "common", common))

        ordered_rare = sorted(counter.items(), key=lambda item: (item[1], item[0]))
        rare = next((candidate for candidate, _ in ordered_rare if candidate != common), None)
        if rare is not None:
            selected.append((f"rare-L{length}", "sample-rare", rare))

        seeds = (b"~ICDE2027~", b"^^^^^^^^^^", b"||||||||||", b"}}}}}}}}}}")
        absent_candidates[length] = [
            (seed * (length // len(seed) + 1))[:length] for seed in seeds]

    all_candidates = [needle for _, _, needle in selected]
    for candidates in absent_candidates.values():
        all_candidates.extend(candidates)
    counts = _count_prefixes(column, all_candidates)

    for length, candidates in absent_candidates.items():
        absent = next((candidate for candidate in candidates if counts[candidate] == 0), None)
        if absent is not None:
            selected.append((f"absent-L{length}", "absent", absent))

    deduplicated: list[tuple[str, str, bytes]] = []
    seen: set[bytes] = set()
    for item in selected:
        if item[2] not in seen:
            seen.add(item[2])
            deduplicated.append(item)
    queries = [
        PrefixQuery(query_id, query_class, needle, counts[needle], column.num_rows)
        for query_id, query_class, needle in deduplicated
    ]
    metadata = {
        "query_discovery": "evenly-spaced sample candidates; exact full-column counts",
        "sample_cap": sample_cap,
        "sample_size": len(sampled),
        "sample_stride": stride,
        "lengths": lengths,
        "num_rows": column.num_rows,
        "payload_bytes": len(column.bytes_),
    }
    return queries, metadata


def _observation(dataset: str, query: PrefixQuery, method: str, sample_idx: int,
                 elapsed_ns: float, payload_bytes: int, **components: float) -> dict:
    result = {
        "dataset": dataset,
        "query_id": query.query_id,
        "query_class": query.query_class,
        "needle_utf8": query.needle.decode("utf-8"),
        "needle_b64": base64.b64encode(query.needle).decode("ascii"),
        "needle_bytes": len(query.needle),
        "n_hits": query.n_hits,
        "num_rows": query.n_rows,
        "selectivity": query.selectivity,
        "method": method,
        "sample_idx": sample_idx,
        "t_ns": elapsed_ns,
        "rows_per_second": query.n_rows * 1e9 / elapsed_ns,
        "payload_gib_per_second": payload_bytes * 1e9 / elapsed_ns / (1 << 30),
    }
    result.update(components)
    return result


def _median_mad(values: list[float]) -> tuple[float, float]:
    median = float(statistics.median(values))
    mad = float(statistics.median(abs(value - median) for value in values))
    return median, mad


def summarise(observations: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in observations:
        grouped.setdefault(
            (row["dataset"], row["query_id"], row["method"]), []).append(row)
    raw_medians: dict[tuple[str, str], float] = {}
    summaries: list[dict] = []
    for key, rows in sorted(grouped.items()):
        times = [float(row["t_ns"]) for row in rows]
        median_ns, mad_ns = _median_mad(times)
        first = rows[0]
        summary = {
            "dataset": key[0], "query_id": key[1], "method": key[2],
            "query_class": first["query_class"],
            "needle_utf8": first["needle_utf8"],
            "needle_bytes": first["needle_bytes"],
            "n_hits": first["n_hits"], "num_rows": first["num_rows"],
            "selectivity": first["selectivity"], "n_samples": len(rows),
            "median_ns": median_ns, "mad_ns": mad_ns,
            "median_rows_per_second": first["num_rows"] * 1e9 / median_ns,
        }
        summaries.append(summary)
        if key[2] == "uncompressed_scan":
            raw_medians[(key[0], key[1])] = median_ns
    for summary in summaries:
        raw_ns = raw_medians[(summary["dataset"], summary["query_id"])]
        summary["speedup_vs_uncompressed"] = raw_ns / summary["median_ns"]
    return summaries


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _parse_columns(values: list[str]) -> list[tuple[str, Path]]:
    result = []
    for value in values:
        if "=" in value:
            label, path_text = value.split("=", 1)
        else:
            path_text = value
            label = Path(value).stem
        path = Path(path_text).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"column is missing: {path}")
        result.append((label, path))
    return result


def run_dataset(label: str, column_path: Path, build_root: Path, workdir: Path,
                cpu: int, samples: int, warmups: int, lengths: list[int],
                sample_cap: int, flags_sha: str) -> tuple[list[dict], dict]:
    queries, query_metadata = discover_queries(column_path, lengths, sample_cap)
    if not queries:
        raise RuntimeError(f"no protocol-safe prefix queries found for {label}")
    with column_path.open("rb") as stream:
        n_rows, payload_bytes = struct.unpack("<II", stream.read(8))
    source_sha = _sha256(column_path)
    dataset_dir = workdir / label.replace("/", "__")
    dataset_dir.mkdir(parents=True, exist_ok=True)

    sessions: dict[str, RunnerSession] = {}
    wires: dict[str, Path] = {"uncompressed": column_path}
    compress_metrics: dict[str, dict] = {}
    decode_samples: dict[str, list[float]] = {codec: [] for codec in DECODE_CODECS}
    observations: list[dict] = []
    try:
        for name, spec in RUNNERS.items():
            sessions[name] = RunnerSession(
                build_root / spec.relative_path, spec.name, flags_sha, cpu)

        # Compress once per codec. Compression timing is retained as provenance
        # but is not used in the prefix comparison.
        for name in (*DECODE_CODECS, "onpair"):
            spec = RUNNERS[name]
            wire = dataset_dir / f"{name}.wire"
            _, metrics = sessions[name].op(
                "compress", f"prefix-{label}-{name}", column_path, wire, spec.config)
            wires[name] = wire
            compress_metrics[name] = metrics

        # Warm and measure payload-only decompression. Repetition order rotates
        # so slow thermal drift cannot consistently favour one codec.
        discard = {codec: dataset_dir / f"{codec}.decoded.col" for codec in DECODE_CODECS}
        for _ in range(warmups):
            for codec in DECODE_CODECS:
                sessions[codec].op(
                    "bulk_decode", f"prefix-{label}-{codec}", wires[codec],
                    discard[codec], RUNNERS[codec].config)
        for sample_idx in range(samples):
            order = list(DECODE_CODECS)
            order = order[sample_idx % len(order):] + order[:sample_idx % len(order)]
            for codec in order:
                _, metrics = sessions[codec].op(
                    "bulk_decode", f"prefix-{label}-{codec}", wires[codec],
                    discard[codec], RUNNERS[codec].config)
                decode_samples[codec].append(metrics["t_decode_ns"])
                if sample_idx == 0 and _sha256(discard[codec]) != source_sha:
                    raise RuntimeError(f"{codec} bulk-decode round trip failed for {label}")

        # Compare native and raw prefix scans. Alternate which implementation
        # runs first per repetition, and verify identical materialised hit files.
        raw_times: dict[str, list[float]] = {}
        for query in queries:
            raw_out = dataset_dir / "raw.hits"
            onpair_out = dataset_dir / "onpair.hits"
            for _ in range(warmups):
                sessions["uncompressed"].op(
                    "prefix_search", f"prefix-{label}-raw", column_path,
                    raw_out, {}, query.needle)
                sessions["onpair"].op(
                    "prefix_search", f"prefix-{label}-onpair", wires["onpair"],
                    onpair_out, RUNNERS["onpair"].config, query.needle)
            raw_times[query.query_id] = []
            for sample_idx in range(samples):
                order = ("uncompressed", "onpair") if sample_idx % 2 == 0 else (
                    "onpair", "uncompressed")
                measured: dict[str, tuple[dict[str, float], Path]] = {}
                for method in order:
                    if method == "uncompressed":
                        _, metrics = sessions[method].op(
                            "prefix_search", f"prefix-{label}-raw", column_path,
                            raw_out, {}, query.needle)
                        measured[method] = (metrics, raw_out)
                    else:
                        _, metrics = sessions[method].op(
                            "prefix_search", f"prefix-{label}-onpair", wires["onpair"],
                            onpair_out, RUNNERS["onpair"].config, query.needle)
                        measured[method] = (metrics, onpair_out)
                if measured["uncompressed"][0]["n_hits"] != query.n_hits:
                    raise RuntimeError(f"raw hit count mismatch for {label}/{query.query_id}")
                if measured["onpair"][0]["n_hits"] != query.n_hits:
                    raise RuntimeError(f"OnPair hit count mismatch for {label}/{query.query_id}")
                if raw_out.read_bytes() != onpair_out.read_bytes():
                    raise RuntimeError(f"hit-list mismatch for {label}/{query.query_id}")

                raw_ns = measured["uncompressed"][0]["t_search_ns"]
                onpair_ns = measured["onpair"][0]["t_search_ns"]
                raw_times[query.query_id].append(raw_ns)
                observations.append(_observation(
                    label, query, "uncompressed_scan", sample_idx, raw_ns,
                    payload_bytes, t_scan_ns=raw_ns, t_query_prepare_ns=0.0))
                observations.append(_observation(
                    label, query, "onpair_native", sample_idx, onpair_ns,
                    payload_bytes,
                    t_scan_ns=measured["onpair"][0]["t_scan_ns"],
                    t_query_prepare_ns=measured["onpair"][0]["t_query_prepare_ns"]))

        # Pair each payload-only codec repetition with the corresponding raw
        # scan repetition. This is a lower bound for materialise-then-scan.
        for query in queries:
            for codec in DECODE_CODECS:
                for sample_idx, (decode_ns, scan_ns) in enumerate(zip(
                        decode_samples[codec], raw_times[query.query_id], strict=True)):
                    observations.append(_observation(
                        label, query, f"{codec}_decode_plus_raw_scan_lower_bound",
                        sample_idx, decode_ns + scan_ns, payload_bytes,
                        t_decode_ns=decode_ns, t_scan_ns=scan_ns,
                        t_query_prepare_ns=0.0))
    finally:
        for session in sessions.values():
            session.close()

    metadata = {
        "column_path": str(column_path), "column_sha256": source_sha,
        "num_rows": n_rows, "payload_bytes": payload_bytes,
        "query_metadata": query_metadata,
        "queries": [dataclasses.asdict(query) | {
            "needle_b64": base64.b64encode(query.needle).decode("ascii"),
            "needle_bytes": len(query.needle),
            "selectivity": query.selectivity} for query in queries],
        "compress_metrics": compress_metrics,
        "decode_samples_ns": decode_samples,
        "wire_bytes": {name: path.stat().st_size for name, path in wires.items()},
    }
    # bytes are represented by needle_b64 in serialized metadata.
    for item in metadata["queries"]:
        del item["needle"]
    return observations, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--column", action="append", required=True,
                        help="repeatable LABEL=PATH (or PATH)")
    parser.add_argument("--build-root", type=Path, default=DEFAULT_BUILD)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--out-json", type=Path,
                        default=DEFAULT_WORKDIR / "results.json")
    parser.add_argument("--out-csv", type=Path,
                        default=DEFAULT_WORKDIR / "observations.csv")
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--cpu", type=int, default=1)
    parser.add_argument("--lengths", default="1,4,8,16")
    parser.add_argument("--query-sample-cap", type=int, default=100_000)
    args = parser.parse_args()
    if args.samples < 3 or args.samples % 2 == 0:
        parser.error("--samples must be an odd integer >= 3 for median/MAD")
    if args.warmups < 1:
        parser.error("--warmups must be >= 1")
    lengths = [int(value) for value in args.lengths.split(",") if value]
    columns = _parse_columns(args.column)
    args.workdir.mkdir(parents=True, exist_ok=True)

    machine = _machine_preflight(args.build_root, args.cpu)
    flags_sha = machine["env_probe"]["build_flags_sha"]
    observations: list[dict] = []
    datasets: dict[str, dict] = {}
    started = _now_utc()
    for label, path in columns:
        print(f"prefix: {label} ({path})", flush=True)
        rows, metadata = run_dataset(
            label, path, args.build_root, args.workdir, args.cpu,
            args.samples, args.warmups, lengths, args.query_sample_cap, flags_sha)
        observations.extend(rows)
        datasets[label] = metadata
    machine["thermal_after_millicelsius"] = _thermal_snapshot()
    summaries = summarise(observations)
    result = {
        "experiment": "prefix_filter",
        "started_utc": started, "finished_utc": _now_utc(),
        "methodology": {
            "samples": args.samples, "warmups": args.warmups,
            "primary_timer": "in-memory; excludes file I/O and hit-list serialization",
            "hit_materialization": "uint32 row ids included for raw and OnPair",
            "onpair_time": "query automaton preparation + compressed scan",
            "decode_plus_scan": (
                "optimistic lower bound: preallocated payload-only bulk decode + raw scan; "
                "excludes output allocation, offsets, and intermediate materialization"),
            "cache_state": "warm OS page cache; no cache flush",
            "runner_affinity": [args.cpu],
        },
        "machine": machine, "datasets": datasets,
        "summaries": summaries, "observations": observations,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, indent=2, sort_keys=True))
    _write_csv(args.out_csv, observations)
    print(f"prefix: wrote {len(observations)} observations to {args.out_json}")
    print(f"prefix: summaries={len(summaries)} csv={args.out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
