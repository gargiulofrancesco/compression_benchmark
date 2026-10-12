"""Top-level dataset materialisation driver.

Reads experiments/datasets.yaml, dispatches per-entry to its extractor module,
invokes the held-out splitter on each output column. Network IO + duckdb
invocation happen here; smoke does NOT call this script.

By default only the columns the paper reports (`headline: true` in the
manifest) are materialised: five sources, six columns, about 14 GB of
downloads. `--extra-columns` adds the other columns the manifest declares
(TPC-H l_comment, c_address, s_comment and the DBLP dump), which no experiment
of the paper reads.

CLI:
  python -m tools.datasets.prepare --all
  python -m tools.datasets.prepare --all --extra-columns
  python -m tools.datasets.prepare --dataset amazon --dataset tpch
  python -m tools.datasets.prepare --dataset tpch --skip-extract
  python -m tools.datasets.prepare --all --update-checksums
"""
from __future__ import annotations
import argparse
import importlib
from pathlib import Path

import yaml

from orchestrator.heldout import split_heldout
from ._common import cardinality, sha256_of


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_manifest(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def paper_columns(entry: dict) -> dict:
    """The entry restricted to the columns the paper reports (`headline: true`).
    An entry without such a column comes back with an empty column list."""
    kept = [c for c in entry.get("columns") or [] if c.get("headline")]
    return {**entry, "columns": kept}


def _dispatch_entry(entry: dict, raw_dir: Path, columns_dir: Path,
                    skip_extract: bool) -> dict[str, str]:
    """Run one dataset entry: download → SHA-256 verify → extract → heldout-split.
    Returns observed-output-SHA-256 per column for manifest write-back."""
    name = entry["name"]
    print(f"\n=== {name} ===")
    if entry.get("kind") != "real":
        print(f"  skip non-real entry (kind={entry.get('kind')})")
        return {}

    mod_name = entry["extractor"]
    mod = importlib.import_module(mod_name)

    raw_target = raw_dir / name
    col_target = columns_dir / name
    raw_target.mkdir(parents=True, exist_ok=True)
    col_target.mkdir(parents=True, exist_ok=True)

    # Idempotent resume: if every declared column is already materialised
    # (size > 0), skip the (expensive) download + extract entirely and just
    # recompute checksums / ensure heldout splits below. Lets a re-run after a
    # mid-pipeline failure (e.g. a transient dblp 503) avoid re-parsing a
    # multi-GB dump, and still write back ALL checksums on the final pass.
    declared = entry.get("columns") or []
    expected = {c["name"]: col_target / c["output_filename"] for c in declared}
    already = bool(expected) and all(
        p.exists() and p.stat().st_size > 0 for p in expected.values())

    if already:
        print(f"  already materialised ({len(expected)} columns) — skipping "
              f"download + extract")
        column_paths = expected
    else:
        raw_path = mod.download(raw_target, entry)
        if skip_extract:
            print(f"  --skip-extract: stopping after download → {raw_path}")
            return {}
        column_paths = mod.extract_columns(raw_path, col_target, entry, entry.get("subset"))

    observed: dict[str, str] = {}
    for col_name, col_path in column_paths.items():
        sha = sha256_of(col_path)
        observed[col_name] = sha
        print(f"  {col_name}: {col_path}  ({col_path.stat().st_size} bytes; sha256={sha[:16]}...)")

        # Report (do not gate) cardinality — feeds the high-cardinality dataset
        # table and flags any headline column that is unexpectedly low-cardinality.
        col_decl = next((c for c in declared if c["name"] == col_name), {})
        N, U = cardinality(col_path)
        frac = U / N if N else 0.0
        flag = "  [!] fewer than 85% distinct rows in a column of the paper" if (
            col_decl.get("headline") and frac < 0.85) else ""
        print(f"    cardinality: N={N} U={U} U/N={frac:.4f}{flag}")

        # Held-out split: produce <col>.train.col + <col>.heldout.col next to <col>.col.
        held_out = entry.get("held_out") or {}
        frac = held_out.get("fraction")
        if frac:
            train_out   = col_path.with_suffix(".train.col")
            heldout_out = col_path.with_suffix(".heldout.col")
            if (train_out.exists() and train_out.stat().st_size > 0
                    and heldout_out.exists() and heldout_out.stat().st_size > 0):
                print(f"    heldout split already present for {col_name} — skipping")
            else:
                n_train, n_held = split_heldout(col_path, train_out, heldout_out, frac)
                print(f"    heldout split: N_train={n_train}, N_heldout={n_held} "
                      f"(fraction={frac})")

    return observed


def _is_digest(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def check_recorded_checksums(entry: dict, observed: dict[str, str]) -> list[str]:
    """Compare each materialised column with the SHA-256 the manifest records.
    Returns the headline columns that differ: those are the paper's inputs, so
    a difference means the measurements would not be on the published bytes.
    Other columns (e.g. from a rolling dump) only get a warning."""
    recorded = entry.get("output_sha256") or {}
    headline = {c["name"] for c in entry.get("columns") or [] if c.get("headline")}
    failed = []
    for col, sha in observed.items():
        want = recorded.get(col)
        if not _is_digest(want):
            continue
        if sha == want:
            print(f"  {col}: matches the recorded sha256")
        elif col in headline:
            print(f"  {col}: [!] sha256 {sha} differs from the recorded {want}")
            failed.append(f"{entry['name']}/{col}")
        else:
            print(f"  {col}: warning: sha256 differs from the recorded value "
                  f"(not a headline column)")
    return failed


def _writeback_checksums(entry: dict, observed: dict[str, str]) -> None:
    """Patch the manifest entry's output_sha256 in-place with observed values
    when the existing value is missing or the "TBD-AT-INTEGRATION" marker."""
    target = entry.setdefault("output_sha256", {})
    for col, sha in observed.items():
        if target.get(col) in (None, "TBD-AT-INTEGRATION"):
            target[col] = sha


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest",     type=Path, default=REPO_ROOT / "experiments/datasets.yaml")
    p.add_argument("--raw-dir",      type=Path, default=REPO_ROOT / "datasets/raw")
    p.add_argument("--columns-dir",  type=Path, default=REPO_ROOT / "datasets/materialized")
    p.add_argument("--dataset",      action="append", help="repeatable; default = all real entries")
    p.add_argument("--all",          action="store_true")
    p.add_argument("--extra-columns", action="store_true",
                   help="also materialise the columns no experiment of the paper reads")
    p.add_argument("--skip-extract", action="store_true")
    p.add_argument("--update-checksums", action="store_true",
                   help="write observed post-extraction SHA-256 back into the manifest")
    a = p.parse_args()

    manifest = _load_manifest(a.manifest)
    entries = manifest["datasets"]

    if a.dataset:
        wanted = set(a.dataset)
        entries = [e for e in entries if e["name"] in wanted]
    elif not a.all:
        # default: same as --all real entries
        entries = [e for e in entries if e.get("kind") == "real"]

    failed: list[str] = []
    for entry in entries:
        selected = entry if a.extra_columns else paper_columns(entry)
        if entry.get("kind") == "real" and not selected["columns"]:
            print(f"\n=== {entry['name']} ===\n  no column of the paper; "
                  f"skipped (--extra-columns materialises it)")
            continue
        observed = _dispatch_entry(selected, a.raw_dir, a.columns_dir, a.skip_extract)
        if a.update_checksums:
            _writeback_checksums(entry, observed)
        failed += check_recorded_checksums(entry, observed)

    if a.update_checksums:
        with open(a.manifest, "w") as f:
            yaml.safe_dump(manifest, f, sort_keys=False)
        print(f"\nmanifest checksums written back → {a.manifest}")

    if failed:
        print(f"\nprepare: {len(failed)} headline column(s) differ from the recorded "
              f"checksums: {', '.join(failed)}")
        return 1
    print("\nprepare: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
