"""Paired BPE vs OnPair bulk decode and construction phases (stage `decode`).

Compresses each column of the paper once, retains phase-separated encoder
metrics, then measures payload-only bulk decompression for 11 repetitions after
warmup. Both runners are pinned to one fixed-frequency CPU and every first
decode is checked byte-for-byte (SHA-256) against the source column.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import statistics
import struct
from pathlib import Path

from experiments.prefix_driver import (
    RunnerSession,
    _machine_preflight,
    _sha256,
    _thermal_snapshot,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUILD = REPO_ROOT / "build"
DEFAULT_WORKDIR = DEFAULT_BUILD / "decode"


@dataclasses.dataclass(frozen=True)
class DecodeSpec:
    name: str
    hello_name: str
    relative_path: str
    config: dict


SPECS = {
    "bpe_v65536": DecodeSpec(
        name="bpe_v65536", hello_name="bpe",
        relative_path="runners/bpe/bpe_runner",
        config={"vocab_size": 65536, "sample_fraction": 0.15,
                "threads": 1, "mem_cap_gb": 64}),
    "onpair": DecodeSpec(
        name="onpair", hello_name="onpair",
        relative_path="runners/onpair/onpair_runner",
        config={"bits": 16, "sample_fraction": 0.15, "seed": 42,
                "threads": 1, "mem_cap_gb": 64}),
}


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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


def _median_mad(values: list[float]) -> tuple[float, float]:
    median = float(statistics.median(values))
    mad = float(statistics.median(abs(value - median) for value in values))
    return median, mad


def _space_summary(metrics: dict[str, float], wire_path: Path,
                   input_bytes: int) -> dict:
    dictionary = int(metrics["dict_bytes"])
    tokens = int(metrics["tokens_bytes"])
    boundaries = int(metrics["boundaries_bytes"])
    payload_accounted = dictionary + tokens
    full_accounted = payload_accounted + boundaries
    wire_bytes = wire_path.stat().st_size
    return {
        "dict_bytes": dictionary,
        "tokens_bytes": tokens,
        "boundaries_bytes": boundaries,
        "payload_accounted_bytes": payload_accounted,
        "full_accounted_bytes": full_accounted,
        "wire_bytes": wire_bytes,
        "payload_ratio": input_bytes / payload_accounted,
        "full_accounted_ratio": input_bytes / full_accounted,
        "wire_ratio": input_bytes / wire_bytes,
        "model_proto_bytes": int(metrics.get("model_proto_bytes", 0)),
    }


def run_dataset(label: str, column_path: Path, build_root: Path, workdir: Path,
                cpu: int, samples: int, warmups: int,
                flags_sha: str,
                reuse_wire_root: Path | None = None) -> tuple[list[dict], dict]:
    with column_path.open("rb") as stream:
        num_rows, input_bytes = struct.unpack("<II", stream.read(8))
    source_sha = _sha256(column_path)
    dataset_dir = workdir / label.replace("/", "__")
    dataset_dir.mkdir(parents=True, exist_ok=True)
    wire_dir = (reuse_wire_root / label.replace("/", "__")
                if reuse_wire_root is not None else dataset_dir)
    sessions: dict[str, RunnerSession] = {}
    wires: dict[str, Path] = {}
    phase_metrics: dict[str, dict[str, float]] = {}
    observations: list[dict] = []
    decode_samples: dict[str, list[float]] = {name: [] for name in SPECS}
    try:
        for name, spec in SPECS.items():
            sessions[name] = RunnerSession(
                build_root / spec.relative_path, spec.hello_name, flags_sha, cpu)

        for name, spec in SPECS.items():
            wire = wire_dir / f"{name}.wire"
            if reuse_wire_root is not None:
                if not wire.is_file():
                    raise FileNotFoundError(f"reused wire is missing: {wire}")
                print(f"  reuse {label}/{name}: {wire}", flush=True)
            else:
                print(f"  compress {label}/{name}", flush=True)
                _, metrics = sessions[name].op(
                    "compress", f"decode-{label}-{name}", column_path,
                    wire, spec.config)
                phase_metrics[name] = metrics
            wires[name] = wire

        decoded = {name: dataset_dir / f"{name}.decoded.col" for name in SPECS}
        for warmup_idx in range(warmups):
            order = tuple(SPECS) if warmup_idx % 2 == 0 else tuple(reversed(SPECS))
            for name in order:
                sessions[name].op(
                    "bulk_decode", f"decode-{label}-{name}", wires[name],
                    decoded[name], SPECS[name].config)

        for sample_idx in range(samples):
            order = tuple(SPECS) if sample_idx % 2 == 0 else tuple(reversed(SPECS))
            for name in order:
                _, metrics = sessions[name].op(
                    "bulk_decode", f"decode-{label}-{name}", wires[name],
                    decoded[name], SPECS[name].config)
                elapsed_ns = metrics["t_decode_ns"]
                decode_samples[name].append(elapsed_ns)
                if sample_idx == 0 and _sha256(decoded[name]) != source_sha:
                    raise RuntimeError(f"{name} bulk-decode round trip failed for {label}")
                observations.append({
                    "dataset": label, "method": name, "sample_idx": sample_idx,
                    "t_decode_ns": elapsed_ns, "input_bytes": input_bytes,
                    "num_rows": num_rows,
                    "gib_per_second": input_bytes * 1e9 / elapsed_ns / (1 << 30),
                })
    finally:
        for session in sessions.values():
            session.close()

    summary: dict[str, dict] = {}
    for name, values in decode_samples.items():
        median_ns, mad_ns = _median_mad(values)
        summary[name] = {
            "n_samples": len(values), "median_decode_ns": median_ns,
            "mad_decode_ns": mad_ns,
            "median_gib_per_second": input_bytes * 1e9 / median_ns / (1 << 30),
        }
    summary["onpair"]["speedup_vs_bpe"] = (
        summary["bpe_v65536"]["median_decode_ns"]
        / summary["onpair"]["median_decode_ns"])
    metadata = {
        "column_path": str(column_path), "column_sha256": source_sha,
        "num_rows": num_rows, "input_bytes": input_bytes,
        "wire_source": ("reused" if reuse_wire_root is not None else "fresh"),
        "wires": {name: {"path": str(wire), "sha256": _sha256(wire),
                         "bytes": wire.stat().st_size}
                  for name, wire in wires.items()},
        "decode_samples_ns": decode_samples,
        "summary": summary,
    }
    if phase_metrics:
        metadata["phase_metrics"] = phase_metrics
        metadata["space"] = {name: _space_summary(
            phase_metrics[name], wires[name], input_bytes) for name in SPECS}
    return observations, metadata


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


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
    parser.add_argument(
        "--reuse-wire-root", type=Path,
        help=("skip compression and reuse <root>/<LABEL>/<method>.wire; "
              "LABEL replaces '/' with '__'"))
    args = parser.parse_args()
    if args.samples < 3 or args.samples % 2 == 0:
        parser.error("--samples must be an odd integer >= 3")
    if args.warmups < 1:
        parser.error("--warmups must be >= 1")
    columns = _parse_columns(args.column)
    args.workdir.mkdir(parents=True, exist_ok=True)
    reuse_wire_root = (args.reuse_wire_root.resolve()
                       if args.reuse_wire_root is not None else None)

    machine = _machine_preflight(args.build_root, args.cpu)
    flags_sha = machine["env_probe"]["build_flags_sha"]
    started = _now_utc()
    observations: list[dict] = []
    datasets: dict[str, dict] = {}

    def checkpoint(complete: bool) -> None:
        machine["thermal_after_millicelsius"] = _thermal_snapshot()
        result = {
            "experiment": "paired_decode",
            "status": "complete" if complete else "in_progress",
            "started_utc": started, "updated_utc": _now_utc(),
            "methodology": {
                "samples": args.samples, "warmups": args.warmups,
                "runner_affinity": [args.cpu],
                "wire_source": ("reused" if reuse_wire_root is not None
                                else "freshly compressed"),
                "reuse_wire_root": (str(reuse_wire_root)
                                    if reuse_wire_root is not None else None),
                "timer": "payload-only decode into preallocated output; file I/O excluded",
                "bpe_config": SPECS["bpe_v65536"].config,
                "onpair_config": SPECS["onpair"].config,
                "phase_semantics": {
                    "t_codec_first_use_ns": (
                        "codec phases only: training/sample preparation through parse+pack"),
                    "t_first_use_ns": "observed runner first use plus wire serialization",
                    "t_reuse_compress_ns": "BPE model-reuse encode plus bit-pack",
                },
            },
            "machine": machine, "datasets": datasets,
            "observations": observations,
        }
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(result, indent=2, sort_keys=True))
        _write_csv(args.out_csv, observations)

    for label, path in columns:
        print(f"decode: {label} ({path})", flush=True)
        rows, metadata = run_dataset(
            label, path, args.build_root, args.workdir, args.cpu,
            args.samples, args.warmups, flags_sha, reuse_wire_root)
        observations.extend(rows)
        datasets[label] = metadata
        checkpoint(complete=False)
    checkpoint(complete=True)
    print(f"decode: wrote {len(observations)} observations to {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
