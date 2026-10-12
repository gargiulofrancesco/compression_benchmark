"""BPE through the flat decoder against SentencePiece's own (stage `bpedecoder`).

Reuses the BPE and OnPair compressed columns written by the `decode` stage and
compares:
  * the optimized flat BPE decoder in SentencePiece's learned ID order;
  * the same BPE pieces/tokens after a semantics-preserving lexicographic remap;
  * SentencePiece's public Decode API per row;
  * an optimistic single Decode call over the whole column; and
  * OnPair's native flat decoder.

Every first output is checked byte-for-byte against the source column.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import struct
from dataclasses import dataclass
from pathlib import Path

from experiments.prefix_driver import (
    RunnerSession,
    _machine_preflight,
    _sha256,
    _thermal_snapshot,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUILD = REPO_ROOT / "build"
DEFAULT_WIRES = DEFAULT_BUILD / "decode"
DEFAULT_WORKDIR = DEFAULT_BUILD / "bpedecoder"
BPE_CONFIG = {
    "vocab_size": 65536,
    "sample_fraction": 0.15,
    "threads": 1,
    "mem_cap_gb": 64,
}
ONPAIR_CONFIG = {
    "bits": 16,
    "sample_fraction": 0.15,
    "seed": 42,
    "threads": 1,
    "mem_cap_gb": 64,
}


@dataclass(frozen=True)
class Variant:
    runner: str
    wire: str
    decode_variant: str | None
    timing_semantics: str


VARIANTS = {
    "bpe_flat": Variant(
        "bpe", "bpe_v65536", "flat",
        "packed-id unpack fused with flat raw-piece expansion"),
    "bpe_flat_lexicographic": Variant(
        "bpe", "bpe_v65536", "flat_lexicographic",
        "same BPE pieces/tokens after lexicographic id remap; identical flat kernel"),
    "sentencepiece_rows": Variant(
        "bpe", "bpe_v65536", "sentencepiece_rows",
        "public Decode(ids,string*) per row; pre-unpacked ids; mapped UTF-8 output"),
    "sentencepiece_column": Variant(
        "bpe", "bpe_v65536", "sentencepiece_column",
        "one public Decode(ids,string*) call; pre-unpacked ids; mapped UTF-8 output"),
    "onpair": Variant(
        "onpair", "onpair", None,
        "native packed-id unpack fused with flat raw-token expansion"),
}


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_columns(values: list[str]) -> list[tuple[str, Path]]:
    result = []
    for value in values:
        label, path_text = value.split("=", 1) if "=" in value else (Path(value).stem, value)
        path = Path(path_text).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"column is missing: {path}")
        result.append((label, path))
    return result


def _median_mad(values: list[float]) -> tuple[float, float]:
    median = float(statistics.median(values))
    mad = float(statistics.median(abs(value - median) for value in values))
    return median, mad


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _measurement_order(index: int) -> list[str]:
    names = list(VARIANTS)
    shift = index % len(names)
    order = names[shift:] + names[:shift]
    if index % 2:
        order.reverse()
    return order


def run_dataset(
    label: str,
    column: Path,
    build: Path,
    wire_root: Path,
    workdir: Path,
    cpu: int,
    flags_sha: str,
    samples: int,
    warmups: int,
) -> tuple[list[dict], dict]:
    with column.open("rb") as stream:
        num_rows, input_bytes = struct.unpack("<II", stream.read(8))
    source_sha = _sha256(column)
    wire_dir = wire_root / label.replace("/", "__")
    wires = {
        "bpe_v65536": wire_dir / "bpe_v65536.wire",
        "onpair": wire_dir / "onpair.wire",
    }
    for wire in wires.values():
        if not wire.is_file():
            raise FileNotFoundError(f"wire is missing: {wire}")

    dataset_dir = workdir / label.replace("/", "__")
    dataset_dir.mkdir(parents=True, exist_ok=True)
    sessions = {
        name: RunnerSession(
            build / ("runners/bpe/bpe_runner" if variant.runner == "bpe"
                     else "runners/onpair/onpair_runner"),
            variant.runner,
            flags_sha,
            cpu,
        )
        for name, variant in VARIANTS.items()
    }
    timings = {name: [] for name in VARIANTS}
    complete_timings = {name: [] for name in VARIANTS}
    observations: list[dict] = []
    first_metrics: dict[str, dict[str, float]] = {}
    try:
        for iteration in range(-warmups, samples):
            order = _measurement_order(iteration + warmups)
            for name in order:
                variant = VARIANTS[name]
                cfg = dict(BPE_CONFIG if variant.runner == "bpe" else ONPAIR_CONFIG)
                if variant.decode_variant is not None:
                    cfg["decode_variant"] = variant.decode_variant
                output = dataset_dir / f"{name}.decoded.col"
                _, metrics = sessions[name].op(
                    "bulk_decode",
                    f"bpe-decode-diagnostic-{label}-{name}-{iteration}",
                    wires[variant.wire],
                    output,
                    cfg,
                )
                if iteration < 0:
                    continue
                if name not in first_metrics:
                    if _sha256(output) != source_sha:
                        raise RuntimeError(f"byte-exact check failed: {label}/{name}")
                    first_metrics[name] = metrics
                decode_ns = metrics["t_decode_ns"]
                complete_ns = (
                    decode_ns
                    + metrics.get("t_unpack_setup_ns", 0.0)
                    + metrics.get("t_bytemap_inverse_ns", 0.0)
                )
                timings[name].append(decode_ns)
                complete_timings[name].append(complete_ns)
                observations.append({
                    "dataset": label,
                    "variant": name,
                    "sample_idx": iteration,
                    "input_bytes": input_bytes,
                    "num_rows": num_rows,
                    "t_decode_ns": decode_ns,
                    "t_complete_adapted_ns": complete_ns,
                    "t_unpack_setup_ns": metrics.get("t_unpack_setup_ns", 0.0),
                    "t_bytemap_inverse_ns": metrics.get("t_bytemap_inverse_ns", 0.0),
                    "gib_per_second": input_bytes * 1e9 / decode_ns / (1 << 30),
                    "complete_adapted_gib_per_second":
                        input_bytes * 1e9 / complete_ns / (1 << 30),
                })
    finally:
        for session in sessions.values():
            session.close()

    summary = {}
    for name in VARIANTS:
        median_ns, mad_ns = _median_mad(timings[name])
        complete_ns, complete_mad_ns = _median_mad(complete_timings[name])
        summary[name] = {
            "n_samples": len(timings[name]),
            "median_decode_ns": median_ns,
            "mad_decode_ns": mad_ns,
            "median_gib_per_second":
                input_bytes * 1e9 / median_ns / (1 << 30),
            "median_complete_adapted_ns": complete_ns,
            "mad_complete_adapted_ns": complete_mad_ns,
            "median_complete_adapted_gib_per_second":
                input_bytes * 1e9 / complete_ns / (1 << 30),
        }
    summary["bpe_flat_lexicographic"]["slowdown_vs_bpe_native_id_order"] = (
        summary["bpe_flat_lexicographic"]["median_decode_ns"]
        / summary["bpe_flat"]["median_decode_ns"]
    )
    summary["bpe_flat"]["speedup_vs_onpair"] = (
        summary["onpair"]["median_decode_ns"]
        / summary["bpe_flat"]["median_decode_ns"]
    )
    return observations, {
        "column_path": str(column),
        "column_sha256": source_sha,
        "num_rows": num_rows,
        "input_bytes": input_bytes,
        "wires": {
            key: {
                "path": str(path),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for key, path in wires.items()
        },
        "first_sample_metrics": first_metrics,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--column", action="append", required=True,
                        help="repeatable LABEL=PATH")
    parser.add_argument("--build-root", type=Path, default=DEFAULT_BUILD)
    parser.add_argument("--wire-root", type=Path, default=DEFAULT_WIRES)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--cpu", type=int, default=1)
    args = parser.parse_args()
    if args.samples < 3 or args.samples % 2 == 0:
        parser.error("--samples must be an odd integer >= 3")
    if args.warmups < 1:
        parser.error("--warmups must be >= 1")

    columns = _parse_columns(args.column)
    build = args.build_root.resolve()
    wire_root = args.wire_root.resolve()
    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    out_json = workdir / "results.json"
    out_csv = workdir / "observations.csv"

    machine = _machine_preflight(build, args.cpu)
    flags_sha = machine["env_probe"]["build_flags_sha"]
    started = _now_utc()
    datasets: dict[str, dict] = {}
    observations: list[dict] = []

    def checkpoint(complete: bool) -> None:
        machine["thermal_after_millicelsius"] = _thermal_snapshot()
        result = {
            "experiment": "bpe_decoder_comparison",
            "status": "complete" if complete else "in_progress",
            "started_utc": started,
            "updated_utc": _now_utc(),
            "methodology": {
                "samples": args.samples,
                "warmups": args.warmups,
                "runner_affinity": [args.cpu],
                "variants": {
                    name: {
                        "decode_variant": variant.decode_variant,
                        "timing_semantics": variant.timing_semantics,
                    }
                    for name, variant in VARIANTS.items()
                },
                "sentencepiece_note": (
                    "native API timers exclude initial packed-id unpack; complete "
                    "adapted timers add unpack and byte-to-Unicode inversion"
                ),
            },
            "machine": machine,
            "datasets": datasets,
            "observations": observations,
        }
        out_json.write_text(json.dumps(result, indent=2, sort_keys=True))
        _write_csv(out_csv, observations)

    for label, column in columns:
        print(f"BPE decode diagnostic: {label}", flush=True)
        rows, metadata = run_dataset(
            label, column, build, wire_root, workdir, args.cpu, flags_sha,
            args.samples, args.warmups)
        observations.extend(rows)
        datasets[label] = metadata
        checkpoint(False)
        for name, values in metadata["summary"].items():
            if isinstance(values, dict) and "median_gib_per_second" in values:
                print(f"  {name}: {values['median_gib_per_second']:.4f} GiB/s",
                      flush=True)
    checkpoint(True)
    print(f"wrote {out_json} and {out_csv}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

