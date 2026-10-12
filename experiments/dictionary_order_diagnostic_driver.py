"""Decode speed by dictionary id order (stage `dictorder`).

For each column of the paper, compares:
  * BPE in native SentencePiece model-ID order;
  * BPE after lexicographic ID remapping;
  * BPE after descending full-column token-frequency remapping;
  * OnPair in pre-sort construction/insertion order;
  * OnPair in its production lexicographic order; and
  * OnPair after descending full-column token-frequency remapping.

All remaps keep dictionary entries, logical token sequences, bit widths, and
decoded output unchanged. Remapping is setup outside the payload-decode timer.
The construction-order OnPair wire is regenerated deterministically from the
same column/configuration and checked against the production wire's token set.
Every first decoded output is checked byte-for-byte against the source column.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
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
DEFAULT_WORKDIR = DEFAULT_BUILD / "dictorder"
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
    decode_variant: str
    timing_semantics: str


VARIANTS = {
    "bpe_native": Variant(
        "bpe", "bpe_v65536", "flat",
        "native SentencePiece model-ID order; flat decoder"),
    "bpe_lexicographic": Variant(
        "bpe", "bpe_v65536", "flat_lexicographic",
        "lexicographic ID order; flat decoder"),
    "bpe_frequency": Variant(
        "bpe", "bpe_v65536", "flat_frequency",
        "descending full-column token frequency; lexicographic tie break"),
    "onpair_construction": Variant(
        "onpair", "onpair_construction", "native",
        "pre-sort token construction/insertion order; library flat decoder"),
    "onpair_lexicographic": Variant(
        "onpair", "onpair_lexicographic", "native",
        "production lexicographic token order; library flat decoder"),
    "onpair_frequency": Variant(
        "onpair", "onpair_lexicographic", "frequency",
        "descending full-column token frequency; lexicographic tie break"),
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
def _read_u32(stream) -> int:
    raw = stream.read(4)
    if len(raw) != 4:
        raise ValueError("truncated OnPair wire")
    return struct.unpack("<I", raw)[0]


def _onpair_wire_metadata(path: Path) -> dict:
    with path.open("rb") as stream:
        if stream.read(8) != b"ONPAIR01":
            raise ValueError(f"invalid OnPair wire: {path}")
        bits_raw = stream.read(1)
        if len(bits_raw) != 1:
            raise ValueError(f"truncated OnPair wire: {path}")
        bits = bits_raw[0]
        dict_bytes = stream.read(_read_u32(stream))
        num_offsets = _read_u32(stream)
        raw_offsets = stream.read(num_offsets * 4)
        if len(raw_offsets) != num_offsets * 4:
            raise ValueError(f"truncated OnPair dictionary offsets: {path}")
        offsets = struct.unpack(f"<{num_offsets}I", raw_offsets)
        real_words = _read_u32(stream)
        stream.seek(real_words * 8, 1)
        num_boundaries = _read_u32(stream)
        raw_boundaries = stream.read(num_boundaries * 4)
        if len(raw_boundaries) != num_boundaries * 4:
            raise ValueError(f"truncated OnPair boundaries: {path}")
        boundaries = struct.unpack(f"<{num_boundaries}I", raw_boundaries)
        if stream.read(1):
            raise ValueError(f"trailing bytes in OnPair wire: {path}")

    pieces = [
        dict_bytes[offsets[index]:offsets[index + 1]]
        for index in range(num_offsets - 1)
    ]

    def digest(sequence: list[bytes]) -> str:
        hasher = hashlib.sha256()
        for piece in sequence:
            hasher.update(struct.pack("<I", len(piece)))
            hasher.update(piece)
        return hasher.hexdigest()

    return {
        "bits": bits,
        "dictionary_entries": len(pieces),
        "token_occurrences": boundaries[-1],
        "dictionary_sequence_sha256": digest(pieces),
        "dictionary_set_sha256": digest(sorted(pieces)),
        "is_lexicographic": pieces == sorted(pieces),
    }



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
    dataset_dir = workdir / label.replace("/", "__")
    dataset_dir.mkdir(parents=True, exist_ok=True)
    wire_dir = wire_root / label.replace("/", "__")
    wires = {
        "bpe_v65536": wire_dir / "bpe_v65536.wire",
        "onpair_lexicographic": wire_dir / "onpair.wire",
        "onpair_construction": dataset_dir / "onpair_construction.wire",
    }
    for key in ("bpe_v65536", "onpair_lexicographic"):
        if not wires[key].is_file():
            raise FileNotFoundError(f"wire is missing: {wires[key]}")

    prepare = RunnerSession(
        build / "runners/onpair/onpair_runner",
        "onpair",
        flags_sha,
        cpu,
    )
    try:
        construction_cfg = dict(ONPAIR_CONFIG)
        construction_cfg["dictionary_order"] = "construction"
        _, construction_metrics = prepare.op(
            "compress",
            f"dictionary-order-prepare-{label}",
            column,
            wires["onpair_construction"],
            construction_cfg,
        )
    finally:
        prepare.close()

    wire_metadata = {
        order: _onpair_wire_metadata(wires[key])
        for order, key in (
            ("construction", "onpair_construction"),
            ("lexicographic", "onpair_lexicographic"),
        )
    }
    construction_meta = wire_metadata["construction"]
    lexicographic_meta = wire_metadata["lexicographic"]
    if construction_meta["dictionary_set_sha256"] != (
            lexicographic_meta["dictionary_set_sha256"]):
        raise RuntimeError(f"OnPair token-set mismatch: {label}")
    if construction_meta["dictionary_entries"] != (
            lexicographic_meta["dictionary_entries"]):
        raise RuntimeError(f"OnPair dictionary-size mismatch: {label}")
    if construction_meta["token_occurrences"] != (
            lexicographic_meta["token_occurrences"]):
        raise RuntimeError(f"OnPair token-count mismatch: {label}")
    if not lexicographic_meta["is_lexicographic"]:
        raise RuntimeError(f"production OnPair wire is not lexicographic: {label}")

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
                cfg["decode_variant"] = variant.decode_variant
                output = dataset_dir / f"{name}.decoded.col"
                output.unlink(missing_ok=True)
                _, metrics = sessions[name].op(
                    "bulk_decode",
                    f"dictionary-order-{label}-{name}-{iteration}",
                    wires[variant.wire],
                    output,
                    cfg,
                )
                if iteration < 0:
                    continue
                    output.unlink(missing_ok=True)
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
                output.unlink(missing_ok=True)
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
    for codec, names in {
        "bpe": ("bpe_native", "bpe_lexicographic", "bpe_frequency"),
        "onpair": (
            "onpair_construction",
            "onpair_lexicographic",
            "onpair_frequency",
        ),
    }.items():
        lex_ns = summary[f"{codec}_lexicographic"]["median_decode_ns"]
        for name in names:
            summary[name]["throughput_vs_lexicographic"] = (
                lex_ns / summary[name]["median_decode_ns"]
            )

    order_comparisons = {}
    for order, bpe_name, onpair_name in (
        ("original_algorithm_order", "bpe_native", "onpair_construction"),
        ("lexicographic", "bpe_lexicographic", "onpair_lexicographic"),
        ("frequency", "bpe_frequency", "onpair_frequency"),
    ):
        bpe_gib = summary[bpe_name]["median_gib_per_second"]
        onpair_gib = summary[onpair_name]["median_gib_per_second"]
        order_comparisons[order] = {
            "bpe_variant": bpe_name,
            "onpair_variant": onpair_name,
            "bpe_gib_per_second": bpe_gib,
            "onpair_gib_per_second": onpair_gib,
            "bpe_over_onpair": bpe_gib / onpair_gib,
        }
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
        "onpair_wire_order_metadata": wire_metadata,
        "construction_wire_metrics": construction_metrics,
        "order_comparisons": order_comparisons,
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
    parser.add_argument("--restart", action="store_true",
                        help="discard a prior checkpoint instead of resuming")
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
    experiment_name = "icde2027_dictionary_order_diagnostic"
    started = _now_utc()
    datasets: dict[str, dict] = {}
    observations: list[dict] = []
    if out_json.is_file() and not args.restart:
        previous = json.loads(out_json.read_text())
        previous_method = previous.get("methodology", {})
        expected = (args.samples, args.warmups, [args.cpu])
        observed = (
            previous_method.get("samples"),
            previous_method.get("warmups"),
            previous_method.get("runner_affinity"),
        )
        if previous.get("experiment") != experiment_name or observed != expected:
            raise RuntimeError(
                "checkpoint methodology differs; pass --restart to replace it")
        started = previous["started_utc"]
        datasets = previous.get("datasets", {})
        observations = previous.get("observations", [])
        print(f"resuming {len(datasets)} completed datasets from {out_json}",
              flush=True)

    def checkpoint(complete: bool) -> None:
        machine["thermal_after_millicelsius"] = _thermal_snapshot()
        result = {
            "experiment": experiment_name,
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
                "ordering_definition": {
                    "original_algorithm_order": (
                        "BPE native SentencePiece model IDs; OnPair pre-sort "
                        "construction/insertion IDs"),
                    "lexicographic": "ascending raw token bytes",
                    "frequency": (
                        "descending full-column token occurrences; ties by "
                        "ascending raw token bytes, then prior ID"),
                },
                "timer_scope": (
                    "payload decode only; all dictionary/token remapping and "
                    "construction-wire generation are excluded"),
                "runner_sha256": {
                    "bpe": _sha256(build / "runners/bpe/bpe_runner"),
                    "onpair": _sha256(build / "runners/onpair/onpair_runner"),
                },
            },
            "machine": machine,
            "datasets": datasets,
            "observations": observations,
        }
        json_tmp = out_json.with_suffix(".json.tmp")
        csv_tmp = out_csv.with_suffix(".csv.tmp")
        json_tmp.write_text(json.dumps(result, indent=2, sort_keys=True))
        _write_csv(csv_tmp, observations)
        json_tmp.replace(out_json)
        csv_tmp.replace(out_csv)

    checkpoint(False)
    for label, column in columns:
        if label in datasets:
            print(f"dictionary-order diagnostic: {label} already complete",
                  flush=True)
            continue
        print(f"dictionary-order diagnostic: {label}", flush=True)
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

