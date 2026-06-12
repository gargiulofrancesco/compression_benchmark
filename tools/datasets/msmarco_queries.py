"""MS MARCO queries extractor — high-cardinality short natural-language text.

Upstream: queries.tar.gz (a tarball of qid<tab>query .tsv files: train/dev/eval).
One row per query (field 1) across all .tsv members. ~1.01M rows, ~100% distinct,
L_avg ~36 B — the short-NL honest case. Used at native size (no cap).
"""
from __future__ import annotations
import io
import tarfile
from pathlib import Path

from ._common import download_if_missing, write_rows


def download(target_dir: Path, manifest_entry: dict) -> Path:
    target = target_dir / "queries.tar.gz"
    return download_if_missing(
        manifest_entry["source"]["url"], target,
        expected_sha256=manifest_entry["source"]["sha256"],
    )


def extract_columns(raw_path: Path, output_dir: Path, manifest_entry: dict,
                    subset_params: dict | None = None) -> dict[str, Path]:
    queries: list[bytes] = []
    with tarfile.open(str(raw_path), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith(".tsv"):
                continue
            f_in = tar.extractfile(member)
            if f_in is None:
                continue
            with io.TextIOWrapper(f_in, encoding="utf-8") as text_file:
                for line in text_file:
                    parts = line.split("\t", 1)
                    if len(parts) >= 2:
                        query = parts[1].strip()
                        if query:
                            queries.append(query.encode("utf-8"))
    out = output_dir / "query.col"
    write_rows(out, queries)
    return {"query": out}
