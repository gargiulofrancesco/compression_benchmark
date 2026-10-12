"""The measurements behind the paper, as published in results/.

results/onpair_icde2027_measurements.tar.xz holds the result stores of the
three passes that produced the paper's numbers, with the run logs.
`--unpack` extracts it next to the archive (results/pass1, pass2, pass3,
superseded, logs) and merges the passes into results/combined with
tools/combine_passes.py, the build root the paper's tables, figures and audit
are generated from:

    ./reproduce.sh results

See results/README.md for what each file is and how it was measured.

Usage: PYTHONPATH=. .venv/bin/python -m tools.published_results --unpack
"""
from __future__ import annotations
import argparse
import hashlib
import shutil
import tarfile
from pathlib import Path

from tools import combine_passes

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "results"
ARCHIVE = RESULTS / "onpair_icde2027_measurements.tar.xz"
SHA256 = "7a0c21e9c3f74947ba6029dce240087113f5313c9ce9c5d6a2f94b9156ed4a71"
PASSES = ("pass1", "pass2", "pass3")


def unpack(results: Path = RESULTS, archive: Path = ARCHIVE) -> Path:
    """Extract the archive (unless already extracted) and combine the passes.
    Returns the combined build root."""
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if archive == ARCHIVE and digest != SHA256:
        raise SystemExit(f"published_results: {archive} has sha256 {digest}, "
                         f"expected {SHA256}")
    if not all((results / p / "results.sqlite").exists() for p in PASSES):
        with tarfile.open(archive, "r:xz") as tar:
            tar.extractall(results, filter="data")
        print(f"published_results: extracted {archive.name} into {results}")
    combined = results / "combined"
    if combined.exists():
        shutil.rmtree(combined)
    combine_passes.combine([results / p for p in PASSES], combined)
    return combined


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unpack", action="store_true",
                    help="extract the archive and build results/combined")
    a = ap.parse_args()
    if not a.unpack:
        ap.print_help()
        return 1
    print(f"published_results: combined build root at {unpack()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
